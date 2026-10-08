"""
vehicle_management/sap_transporters.py

A vehicle's transporter, picked from SAP.

The picker offers every active vendor of the company the user is working in,
the TRANSPORTER group first. Real transporters are not all in that group:
Shambhu Roadways sits in PURCHASE, Bhargave Road Carrier in Beverages' SERVICE,
Chetna Roadlines in IMPORT & EXPORT, and a supplier delivering in its own truck
is that truck's transporter. Anyone SAP does not have is typed by hand and stays
an unlinked Transporter, exactly as before.

Picking a vendor never makes a second Transporter for someone the app already
knows. It is matched, in order, by an existing link to the same code, by a link
in another company carrying the same GSTIN (one transporter is a different code
in each company), by name, and by a GSTIN typed on exactly one transporter.
"""

import logging
import re

from django.db import IntegrityError, transaction
from django.db.models import Count
from hdbcli import dbapi

from sap_client.context import CompanyContext
from sap_client.exceptions import SAPConnectionError, SAPDataError
from sap_client.hana.connection import HanaConnection

from .models import Transporter, TransporterSAPLink

logger = logging.getLogger(__name__)

TRANSPORTER_GROUP = "TRANSPORTER"
_GSTIN = re.compile(r"^[0-9]{2}[A-Z0-9]{13}$")

# GSTIN from the vendor's addresses (CRD1), else the header's tax number, which
# some vendors carry a PAN in instead -- `clean_gstin` drops anything that is not
# shaped like a GSTIN.
_VENDOR_SQL = """
    SELECT c."CardCode", c."CardName", IFNULL(g."GroupName", ''),
           IFNULL(a."GSTIN", c."LicTradNum"), c."frozenFor"
    FROM "{schema}"."OCRD" c
    LEFT JOIN "{schema}"."OCRG" g ON g."GroupCode" = c."GroupCode"
    LEFT JOIN (
        SELECT "CardCode", MAX("GSTRegnNo") AS "GSTIN"
        FROM "{schema}"."CRD1"
        WHERE IFNULL("GSTRegnNo", '') <> ''
        GROUP BY "CardCode"
    ) a ON a."CardCode" = c."CardCode"
    WHERE c."CardType" = 'S' {where}
"""


class VendorNotFound(Exception):
    """The code is not an active vendor in this company's SAP."""


def clean_gstin(value) -> str:
    value = (value or "").strip().upper()
    return value if _GSTIN.match(value) else ""


def clean_name(value) -> str:
    return " ".join((value or "").split())


def _vendor(row) -> dict:
    code, name, group, gstin, frozen = row
    return {
        "card_code": code,
        "card_name": clean_name(name),
        "group": group or "",
        "is_transporter": (group or "").upper() == TRANSPORTER_GROUP,
        "gstin": clean_gstin(gstin),
        "frozen": frozen == "Y",
    }


def _read(company_code, where="", params=()):
    connection = HanaConnection(CompanyContext(company_code).hana)
    try:
        conn = connection.connect()
    except dbapi.Error as exc:
        raise SAPConnectionError("Unable to connect to SAP HANA. Please try again later.") from exc
    try:
        cursor = conn.cursor()
        cursor.execute(_VENDOR_SQL.format(schema=connection.schema, where=where), list(params))
        return [_vendor(row) for row in cursor.fetchall()]
    except dbapi.Error as exc:
        logger.error("SAP vendor read failed for %s: %s", company_code, exc)
        raise SAPDataError("Failed to read vendors from SAP.") from exc
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def _copy(company_code, exc, *, search=""):
    """The nightly vendor copy, when HANA could not be asked; else re-raise.

    The copy holds codes and names only, so a vendor already linked to a
    transporter stands in for the TRANSPORTER group.
    """
    from sap_mirror import services as sap_mirror

    if not sap_mirror.hana_unreachable(exc):
        raise exc
    copy = sap_mirror.copied_rows(company_code, sap_mirror.VENDORS, search=search)
    if copy is None:
        raise exc
    rows, as_of = copy
    linked = set(
        TransporterSAPLink.objects.filter(company__code=company_code).values_list(
            "card_code", flat=True
        )
    )
    logger.warning("HANA unreachable; transporter vendors for %s answered from the copy", company_code)
    vendors = [
        {
            "card_code": row["vendor_code"],
            "card_name": clean_name(row["vendor_name"]),
            "group": "",
            "is_transporter": row["vendor_code"] in linked,
            "gstin": "",
            "frozen": False,
        }
        for row in rows
    ]
    return vendors, as_of


def list_vendors(company_code):
    """``(vendors, copy_as_of)``: the active vendors, transporters first.

    ``copy_as_of`` is None when SAP answered, else when the copy was taken.
    """
    try:
        vendors, as_of = _read(company_code, """AND c."frozenFor" = 'N'"""), None
    except (SAPConnectionError, SAPDataError) as exc:
        vendors, as_of = _copy(company_code, exc)
    vendors.sort(key=lambda v: (not v["is_transporter"], v["card_name"], v["card_code"]))
    return vendors, as_of


def get_vendor(company_code, card_code):
    """One vendor, frozen or not, or None if the company's SAP has no such code."""
    card_code = (card_code or "").strip()
    if not card_code:
        return None
    try:
        rows = _read(company_code, """AND c."CardCode" = ?""", [card_code])
    except (SAPConnectionError, SAPDataError) as exc:
        rows, _ = _copy(company_code, exc, search=card_code)
        rows = [row for row in rows if row["card_code"] == card_code]
    return rows[0] if rows else None


def vendors_by_code(company_code):
    """Every vendor of the company, frozen ones included, by code.

    For linking in bulk: one read instead of one per code. No copy fallback --
    a bulk link waits for SAP rather than trust a list without groups or GSTINs.
    """
    return {vendor["card_code"]: vendor for vendor in _read(company_code)}


def _most_used(queryset):
    return (
        queryset.annotate(vehicle_count=Count("vehicle", distinct=True))
        .order_by("-vehicle_count", "id")
        .first()
    )


def _known_transporter(company, vendor):
    """The Transporter this vendor already is, or None."""
    linked = _most_used(
        Transporter.objects.filter(
            sap_links__company=company, sap_links__card_code=vendor["card_code"]
        ).distinct()
    )
    if linked:
        return linked
    gstin = vendor["gstin"]
    if gstin:
        elsewhere = _most_used(Transporter.objects.filter(sap_links__gstin=gstin).distinct())
        if elsewhere:
            return elsewhere
    by_name = Transporter.objects.filter(name__iexact=vendor["card_name"]).order_by("id").first()
    if by_name:
        return by_name
    if gstin:
        # A GSTIN typed in the app is only trusted when it is on one transporter:
        # some were typed onto the wrong one (J M S Tempo Service carries Punjab
        # Himachal's).
        typed = list(Transporter.objects.filter(gstin__iexact=gstin)[:2])
        if len(typed) == 1:
            return typed[0]
    return None


def link(transporter, company, vendor, user=None):
    """Tie ``transporter`` to ``vendor`` in ``company``; fill a blank GSTIN."""
    TransporterSAPLink.objects.get_or_create(
        transporter=transporter,
        company=company,
        card_code=vendor["card_code"],
        defaults={
            "card_name": vendor["card_name"][:150],
            "gstin": vendor["gstin"],
            "created_by": user,
            "updated_by": user,
        },
    )
    if vendor["gstin"] and not transporter.gstin:
        transporter.gstin = vendor["gstin"]
        transporter.updated_by = user
        transporter.save(update_fields=["gstin", "updated_by", "updated_at"])


def transporter_from_sap(company, card_code, user=None):
    """The Transporter for a vendor picked in ``company``'s SAP, made if new.

    Raises ``VendorNotFound`` for a code that is not an active vendor there.
    """
    vendor = get_vendor(company.code, card_code)
    if vendor is None or vendor["frozen"]:
        raise VendorNotFound(f"{card_code} is not an active vendor in {company.name}'s SAP.")
    with transaction.atomic():
        transporter = _known_transporter(company, vendor)
        if transporter is None:
            transporter = Transporter.objects.create(
                name=vendor["card_name"][:150],
                gstin=vendor["gstin"],
                created_by=user,
                updated_by=user,
            )
        link(transporter, company, vendor, user)
    return transporter


def transporter_by_name(name, user=None):
    """The Transporter typed by hand: an existing one by name, else a new one."""
    name = clean_name(name)[:150]
    if not name:
        raise ValueError("A transporter name is required.")
    existing = Transporter.objects.filter(name__iexact=name).order_by("id").first()
    if existing:
        return existing
    try:
        with transaction.atomic():
            return Transporter.objects.create(name=name, created_by=user, updated_by=user)
    except IntegrityError:
        return Transporter.objects.get(name=name)
