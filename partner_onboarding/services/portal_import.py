"""
Import SAP Portal's registration tables (``ZCUST_PORTAL``, ``ZVENDOR_PORTAL``).

The input is a JSON export of the table — one object per row, keyed exactly by
the portal's column names (``ID``, ``CARD_NAME``, ``ALL_BILL_ADDRS`` …; see
``docs/README.md`` for the SELECT). Nothing here reads HANA.

How rows land (column by column in ``CUSTOMER_COLUMNS`` / ``VENDOR_COLUMNS``):

* **Idempotent** on ``legacy_portal_id`` (the row's ``ID``): a row imported
  before is left alone, or — with ``update`` — refreshed, but only while nobody
  has acted on it in JI (its events are all import events).
* **Company**: the ``COMPANY`` column holds the SAP database name
  (``JIVO_OIL_HANADB``); it is turned back into a company code through
  ``settings.COMPANY_DB``. An unknown or blank one is reported and skipped
  unless a default company is given.
* **Addresses** come from the JSON arrays ``ALL_BILL_ADDRS`` / ``ALL_SHIP_ADDRS``
  (customers) or, when empty, from the single ``BILL_*`` / ``SHIP_*`` columns —
  the same fallbacks the portal's payload builders used. The vendor table never
  stored its addresses beyond ``BILL_*``, so a vendor gets that one address,
  shipped to itself, named ``<NAME[:25]>-<STATE>`` as createVendor named it.
* **Bank accounts** from ``BANK_ACCOUNTS``; **documents** from ``ATTACHMENTS``,
  whose base64 data URLs are decoded into files.
* **Timestamps** were written by the portal as UTC without a zone
  (``toTs(new Date().toISOString())``); they are read as UTC.
* Columns JI has no field for (the portal's zone / RSM / ASM … fields, which no
  screen used and SAP never received) are kept in ``legacy_fields``.

Every judgement call (a clipped value, an unknown state, an undecodable file)
is a note on the row, printed by the command and kept on its import event.
"""

import base64
import binascii
import json
import mimetypes
import os
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, time
from datetime import timezone as dt_timezone
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from company.models import Company

from ..constants import (
    ALLOWED_UPLOADS,
    COUNTRY_BY_NAME,
    CURRENCY_BY_LABEL,
    SAP_ADDRESS_NAME_MAX,
    AddressType,
    AttachmentKind,
    BankAccountType,
    Country,
    Currency,
    CustomerType,
    EventKind,
    RegistrationStatus,
    VendorType,
    portal_state_code,
)
from ..families import CUSTOMER_FAMILY, VENDOR_FAMILY
from ..models import RegistrationEvent
from .records import (
    add_attachment,
    delete_files,
    owner_kwargs,
    replace_addresses,
    replace_bank_accounts,
    unique_names,
)


class ImportFileError(Exception):
    """The file cannot be read as a table export at all."""


@dataclass
class PlannedDocument:
    kind: str
    name: str
    content_type: str
    content: bytes


@dataclass
class PlannedRow:
    legacy_id: int | None
    status: str = ""
    company_code: str = ""
    card_name: str = ""
    columns: dict = field(default_factory=dict)
    addresses: list = field(default_factory=list)
    bank_accounts: list = field(default_factory=list)
    documents: list = field(default_factory=list)
    events: list = field(default_factory=list)
    legacy_fields: dict = field(default_factory=dict)
    notes: list = field(default_factory=list)
    error: str = ""
    skip: str = ""


# ---------------------------------------------------------------------------
# reading values
# ---------------------------------------------------------------------------


def _text(value) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _clip(value, limit: int, label: str, notes: list) -> str:
    text = _text(value)
    if len(text) > limit:
        notes.append(f"{label} was {len(text)} characters; kept the first {limit}.")
        return text[:limit]
    return text


def _int(value, label: str, notes: list):
    text = _text(value)
    if not text:
        return None
    try:
        return int(Decimal(text))
    except (InvalidOperation, ValueError):
        notes.append(f"{label} {text!r} is not a number; left empty.")
        return None


def _positive(value, label: str, notes: list):
    number = _int(value, label, notes)
    return number if number and number > 0 else None


def _decimal(value, label: str, notes: list, default="0") -> Decimal:
    text = _text(value)
    if not text:
        return Decimal(default)
    try:
        return Decimal(text).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        notes.append(f"{label} {text!r} is not a number; used {default}.")
        return Decimal(default)


def _flag(value) -> bool:
    """HAS_MSME is TINYINT 0/1 in the customer table and 'Y'/'N' in the vendor table."""
    return _text(value).upper() in {"1", "Y", "YES", "TRUE", "T"}


def _when(value, label: str, notes: list):
    """A portal timestamp as an aware UTC datetime (the portal wrote UTC, zoneless)."""
    text = _text(value)
    if not text:
        return None
    parsed = parse_datetime(text.replace("Z", "+00:00") if text.endswith("Z") else text)
    if parsed is None:
        day = parse_date(text[:10])
        if day is None:
            notes.append(f"{label} {text!r} is not a date; left empty.")
            return None
        parsed = datetime.combine(day, time.min)
    if timezone.is_naive(parsed):
        parsed = parsed.replace(tzinfo=dt_timezone.utc)
    return parsed


def _json(value, fallback, label: str, notes: list):
    if value in (None, ""):
        return fallback
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        notes.append(f"{label} is not valid JSON; ignored.")
        return fallback


def _currency(value, label: str, notes: list, blank_ok=False) -> str:
    text = _text(value)
    if not text:
        return "" if blank_ok else Currency.INR
    if text.upper() in Currency.values:
        return text.upper()
    if text in CURRENCY_BY_LABEL:
        return CURRENCY_BY_LABEL[text]
    notes.append(f"{label} {text!r} is not a known currency; used INR (the portal's own fallback).")
    return Currency.INR


def _country(value, notes: list) -> str:
    text = _text(value)
    if not text:
        return Country.IN
    if text in COUNTRY_BY_NAME:
        return COUNTRY_BY_NAME[text]
    if text.upper() in Country.values:
        return text.upper()
    notes.append(f"Country {text!r} is not one the forms offered; used India.")
    return Country.IN


def _state(value, notes: list) -> str:
    text = _text(value)
    code = portal_state_code(text)
    if text and not code:
        notes.append(f"State {text!r} has no SAP code in the portal's table; left empty.")
    return code


def _choice(value, choices, default, label: str, notes: list, aliases=None) -> str:
    text = _text(value).upper()
    if not text:
        return default
    text = (aliases or {}).get(text, text)
    if text in choices:
        return text
    notes.append(f"{label} {value!r} is not one JI knows; used {default}.")
    return default


def _company_codes_by_database() -> dict:
    return {str(db).strip().upper(): code for code, db in settings.COMPANY_DB.items() if db}


# ---------------------------------------------------------------------------
# children
# ---------------------------------------------------------------------------


def _address(source: dict, address_type: str, notes: list, label: str) -> dict:
    return {
        "address_type": address_type,
        "address_name": _clip(source.get("addrName") or source.get("addressName"), SAP_ADDRESS_NAME_MAX, f"{label} name", notes).upper(),
        "street": _clip(source.get("street"), 100, f"{label} street", notes).upper(),
        "block": _clip(source.get("block"), 100, f"{label} block", notes).upper(),
        "city": _clip(source.get("city"), 100, f"{label} city", notes).upper(),
        "zip_code": _clip(source.get("zip"), 20, f"{label} PIN", notes).upper(),
        "state": _state(source.get("state"), notes),
        "country": _country(source.get("country"), notes),
        "gstin": _clip(source.get("gstin"), 15, f"{label} GSTIN", notes).upper(),
    }


def _address_list(value, address_type: str, notes: list, label: str) -> list:
    items = _json(value, [], label, notes)
    if not isinstance(items, list):
        notes.append(f"{label} is not a list; ignored.")
        return []
    return [
        _address(item, address_type, notes, label)
        for item in items
        if isinstance(item, dict) and (_text(item.get("street")) or _text(item.get("addrName")))
    ]


def _columns_address(row: dict, prefix: str, name: str, address_type: str, notes: list) -> dict | None:
    if not (_text(row.get(f"{prefix}_STREET")) or _text(row.get(f"{prefix}_CITY"))):
        return None
    return _address(
        {
            "addrName": name,
            "street": row.get(f"{prefix}_STREET"),
            "block": row.get(f"{prefix}_BLOCK"),
            "city": row.get(f"{prefix}_CITY"),
            "zip": row.get(f"{prefix}_ZIP"),
            "state": row.get(f"{prefix}_STATE"),
            "country": row.get(f"{prefix}_COUNTRY"),
        },
        address_type,
        notes,
        f"{prefix.title()} address",
    )


#: The portal's ``mimeMap`` (uploadAttachmentsToSAP), for files with no extension.
_EXTENSION_BY_MIME = {
    "application/pdf": "pdf",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/tiff": "tif",
    "application/msword": "doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
}
_KIND_BY_KEY = {kind.value.lower(): kind for kind in AttachmentKind}


def _documents(value, notes: list) -> list:
    data = _json(value, {}, "ATTACHMENTS", notes)
    if not isinstance(data, dict):
        notes.append("ATTACHMENTS is not an object; ignored.")
        return []
    documents = []
    for key, entry in data.items():
        kind = _KIND_BY_KEY.get(str(key).lower())
        entries = entry if isinstance(entry, list) else [entry]
        for item in entries:
            if not isinstance(item, dict) or not item.get("data"):
                continue
            if kind is None:
                notes.append(f"Document slot {key!r} is not one JI knows; filed as Other.")
            raw = str(item["data"])
            header, _, encoded = raw.partition(",") if raw.startswith("data:") else ("", "", raw)
            mime = header[5:].split(";")[0].strip().lower() if header else _text(item.get("type")).lower()
            try:
                content = base64.b64decode(encoded, validate=False)
            except (binascii.Error, ValueError):
                notes.append(f"Document {item.get('name') or key!r} could not be decoded; skipped.")
                continue
            if not content:
                notes.append(f"Document {item.get('name') or key!r} is empty; skipped.")
                continue
            name = os.path.basename(_text(item.get("name"))) or f"{str(key).lower()}"
            extension = os.path.splitext(name)[1].lower().lstrip(".")
            if not extension:
                extension = _EXTENSION_BY_MIME.get(mime, "pdf")
                name = f"{name}.{extension}"
            content_type = (
                ALLOWED_UPLOADS.get(extension, (None,))[0]
                or mime
                or mimetypes.guess_type(name)[0]
                or "application/octet-stream"
            )
            documents.append(PlannedDocument(kind or AttachmentKind.OTHER, name[:255], content_type, content))
    return documents


def _bank_accounts(value, notes: list) -> list:
    items = _json(value, [], "BANK_ACCOUNTS", notes)
    if not isinstance(items, list):
        notes.append("BANK_ACCOUNTS is not a list; ignored.")
        return []
    accounts = []
    for item in items:
        if not isinstance(item, dict):
            continue
        number = _text(item.get("accNo")).replace(" ", "").upper()
        if not number:
            continue
        account_type = _text(item.get("accountType")) or BankAccountType.CURRENT
        if account_type not in BankAccountType.values:
            notes.append(f"Account type {account_type!r} is not one the form offered; used Current.")
            account_type = BankAccountType.CURRENT
        accounts.append(
            {
                "bank_name": _clip(item.get("bankName"), 100, "Bank name", notes).upper(),
                "branch": _clip(item.get("branch"), 50, "Bank branch", notes).upper(),
                "account_number": _clip(number, 50, "Account number", notes),
                "ifsc": _clip(item.get("ifsc"), 11, "IFSC", notes).upper(),
                "account_type": account_type,
                "swift_code": _clip(item.get("swiftCode"), 11, "SWIFT", notes).upper(),
                "sap_bank_code": _clip(item.get("bankCode") or item.get("mgrBankCode"), 30, "Bank code", notes).upper(),
            }
        )
    return accounts


# ---------------------------------------------------------------------------
# column maps
# ---------------------------------------------------------------------------

#: Columns read into fields (or used to build children); everything else that
#: is not empty goes to ``legacy_fields``.
_BLOBS = {"ATTACHMENTS", "ALL_BILL_ADDRS", "ALL_SHIP_ADDRS", "BANK_ACCOUNTS"}
CUSTOMER_COLUMNS = _BLOBS | {
    "ID", "COMPANY", "STATUS", "CARD_NAME", "FOREIGN_NAME", "CUSTOMER_TYPE", "TYPE_OF_BUSINESS",
    "INDUSTRY", "MOBILE", "EMAIL", "WEBSITE", "CONTACT_FIRST", "CONTACT_LAST", "CONTACT_TITLE",
    "CONTACT_MOBILE", "CONTACT_EMAIL", "GSTIN", "PAN", "CURRENCY", "REMARKS", "HAS_MSME", "MSME_NO",
    "MSME_TYPE", "MSME_BTYPE", "SUBMITTED_AT", "VERIFIED_AT", "APPROVED_AT", "SAP_CARD_CODE",
    "SAP_ATT_ENTRY", "SAME_AS_BILL", "BILL_ADDR_NAME", "BILL_STREET", "BILL_BLOCK", "BILL_CITY",
    "BILL_ZIP", "BILL_STATE", "BILL_COUNTRY", "SHIP_ADDR_NAME", "SHIP_STREET", "SHIP_BLOCK",
    "SHIP_CITY", "SHIP_ZIP", "SHIP_STATE", "SHIP_COUNTRY", "MGR_PREFIX", "MGR_GROUP_CODE",
    "MGR_GROUP", "MGR_CURRENCY", "MGR_CHAIN", "MGR_MAIN_GROUP", "MGR_SALES_EMP", "MGR_SLP_CODE",
    "MGR_TERRITORY", "MGR_NOTES", "MGR_CREDIT_LMT", "MGR_PAY_TERMS", "MGR_PAY_CODE",
    "MGR_AR_ACCOUNT", "MGR_AR_ACC_NAME",
}  # fmt: skip
VENDOR_COLUMNS = _BLOBS | {
    "ID", "COMPANY", "STATUS", "VENDOR_TYPE", "CARD_NAME", "FOREIGN_NAME", "TYPE_OF_BUSINESS",
    "INDUSTRY", "PRODUCTS", "PAYMENT_TERMS", "CONTACT_FIRST", "CONTACT_LAST", "CONTACT_TITLE",
    "MOBILE", "ALT_CONTACT", "EMAIL", "BILL_STREET", "BILL_BLOCK", "BILL_CITY", "BILL_ZIP",
    "BILL_STATE", "BILL_COUNTRY", "GSTIN", "PAN", "TAN", "CURRENCY", "HAS_TDS", "TDS_CATEGORY",
    "TDS_RATE", "TDS_LDC_NO", "HAS_MSME", "MSME_NO", "MSME_TYPE", "MSME_BTYPE", "FSSAI_NO",
    "REMARKS", "SUBMITTED_AT", "MGR_CARD_CODE_PREFIX", "MGR_GROUP_CODE", "MGR_GROUP",
    "MGR_PAY_TERMS_CODE", "MGR_PAY_TERMS", "MGR_PURCHASE_ACCOUNT", "MGR_PURCHASE_ACCT_NAME",
    "MGR_CURRENCY", "MGR_CREDIT_LIMIT", "MGR_NOTES", "MGR_TERRITORY", "MGR_SALES_PERSON_CODE",
    "MGR_SALES_EMPLOYEE", "VERIFIED_AT", "APPROVED_AT", "REJECTED_AT", "SAP_CARD_CODE",
    "SAP_ATTACHMENT_ENTRY",
}  # fmt: skip

#: CUSTOMER_TYPE is NVARCHAR(10): the portal cut "Distributor" to "DISTRIBUTO".
_CUSTOMER_TYPE_ALIASES = {"DISTRIBUTO": CustomerType.DISTRIBUTOR}


def _common(row: dict, planned: PlannedRow, notes: list) -> dict:
    return {
        "card_name": planned.card_name,
        "foreign_name": _clip(row.get("FOREIGN_NAME"), 100, "Foreign name", notes),
        "type_of_business": _clip(row.get("TYPE_OF_BUSINESS"), 30, "Type of business", notes),
        "industry": _clip(row.get("INDUSTRY"), 100, "Industry", notes),
        "contact_first_name": _clip(row.get("CONTACT_FIRST"), 50, "Contact first name", notes),
        "contact_last_name": _clip(row.get("CONTACT_LAST"), 50, "Contact last name", notes),
        "contact_title": _clip(row.get("CONTACT_TITLE"), 90, "Contact title", notes),
        "mobile": _clip(row.get("MOBILE"), 20, "Mobile", notes),
        "email": _clip(row.get("EMAIL"), 100, "Email", notes).lower(),
        "currency": _currency(row.get("CURRENCY"), "Currency", notes),
        "gstin": _clip(row.get("GSTIN"), 15, "GSTIN", notes).upper(),
        "pan": _clip(row.get("PAN"), 10, "PAN", notes).upper(),
        "has_msme": _flag(row.get("HAS_MSME")),
        "msme_number": _clip(row.get("MSME_NO"), 30, "MSME number", notes).upper(),
        "msme_type": _clip(row.get("MSME_TYPE"), 20, "MSME type", notes),
        "msme_business_type": _clip(row.get("MSME_BTYPE"), 30, "MSME business type", notes),
        "remarks": _text(row.get("REMARKS")),
        "submitted_at": _when(row.get("SUBMITTED_AT"), "SUBMITTED_AT", notes) or timezone.now(),
        "sap_card_code": _clip(row.get("SAP_CARD_CODE"), 15, "SAP card code", notes),
        "bp_group_name": _clip(row.get("MGR_GROUP"), 100, "BP group", notes),
        "sap_currency": _currency(row.get("MGR_CURRENCY"), "Manager currency", notes, blank_ok=True),
        "credit_limit": Decimal("0"),
        "territory": _clip(row.get("MGR_TERRITORY"), 100, "Territory", notes),
        "manager_notes": _text(row.get("MGR_NOTES")),
        "payment_terms_name": _clip(row.get("MGR_PAY_TERMS"), 100, "Payment terms", notes),
    }


def _decisions(row: dict, family, status: str, columns: dict, planned: PlannedRow, notes: list):
    """Decision timestamps and the historical events. Who decided was a portal
    username, never a JI user, so it goes into the event's ``actor_name``."""
    events = [
        {
            "kind": EventKind.SUBMITTED,
            "at": columns["submitted_at"],
            "actor_name": f"{(columns['contact_first_name'] + ' ' + columns['contact_last_name']).strip()} (public form)",
            "note": "",
        }
    ]
    verified_at = _when(row.get("VERIFIED_AT"), "VERIFIED_AT", notes)
    approved_at = _when(row.get("APPROVED_AT"), "APPROVED_AT", notes)
    rejected_at = _when(row.get("REJECTED_AT"), "REJECTED_AT", notes) if family is VENDOR_FAMILY else None
    if status in (RegistrationStatus.VERIFIED, RegistrationStatus.APPROVED) and verified_at:
        columns["verified_at"] = verified_at
        events.append(
            {"kind": EventKind.VERIFIED, "at": verified_at, "actor_name": _text(row.get("VERIFIED_BY")) or "SAP Portal", "note": ""}
        )
    elif verified_at:
        # The portal's verify route stamped VERIFIED_AT on a rejection too.
        planned.legacy_fields["VERIFIED_AT"] = verified_at.isoformat()
    if status == RegistrationStatus.APPROVED:
        columns["approved_at"] = approved_at
        code = columns["sap_card_code"]
        if not code:
            notes.append("Approved in the portal but no SAP card code was recorded.")
        columns["card_code"] = code
        events.append(
            {
                "kind": EventKind.SAP_CREATED,
                "at": approved_at or columns["submitted_at"],
                "actor_name": _text(row.get("APPROVED_BY")) or "SAP Portal",
                "note": f"Created in SAP as {code} (through SAP Portal)." if code else "Created in SAP through SAP Portal.",
            }
        )
    if status == RegistrationStatus.REJECTED:
        if rejected_at:
            columns["rejected_at"] = rejected_at
            events.append(
                {"kind": EventKind.REJECTED, "at": rejected_at, "actor_name": _text(row.get("REJECTED_BY")) or "SAP Portal", "note": ""}
            )
        else:
            notes.append("Rejected in the portal, which recorded neither who nor when.")
    planned.events = events


def _plan_customer(row: dict, planned: PlannedRow, notes: list):
    columns = _common(row, planned, notes)
    columns.update(
        customer_type=_choice(
            row.get("CUSTOMER_TYPE"), CustomerType.values, CustomerType.B2B, "Customer type", notes, _CUSTOMER_TYPE_ALIASES
        ),
        website=_clip(row.get("WEBSITE"), 100, "Website", notes),
        contact_mobile=_clip(row.get("CONTACT_MOBILE"), 20, "Contact mobile", notes),
        contact_email=_clip(row.get("CONTACT_EMAIL"), 100, "Contact email", notes).lower(),
        ship_same_as_bill=_flag(row.get("SAME_AS_BILL")),
        sap_attachment_entry=_positive(row.get("SAP_ATT_ENTRY"), "SAP_ATT_ENTRY", notes),
        card_code_prefix=_clip(row.get("MGR_PREFIX"), 10, "Card-code prefix", notes).upper() or CUSTOMER_FAMILY.default_prefix,
        bp_group_code=_int(row.get("MGR_GROUP_CODE"), "MGR_GROUP_CODE", notes),
        chain=_clip(row.get("MGR_CHAIN"), 50, "Chain", notes),
        main_group=_clip(row.get("MGR_MAIN_GROUP"), 50, "Main group", notes),
        sales_employee_name=_clip(row.get("MGR_SALES_EMP"), 155, "Sales employee", notes),
        sales_employee_code=_int(row.get("MGR_SLP_CODE"), "MGR_SLP_CODE", notes),
        credit_limit=_decimal(row.get("MGR_CREDIT_LMT"), "MGR_CREDIT_LMT", notes),
        payment_terms_code=_int(row.get("MGR_PAY_CODE"), "MGR_PAY_CODE", notes),
        control_account=_clip(row.get("MGR_AR_ACCOUNT"), 15, "AR account", notes),
        control_account_name=_clip(row.get("MGR_AR_ACC_NAME"), 100, "AR account name", notes),
    )
    bill = _address_list(row.get("ALL_BILL_ADDRS"), AddressType.BILL_TO, notes, "ALL_BILL_ADDRS")
    if not bill:
        single = _columns_address(
            row, "BILL", _text(row.get("BILL_ADDR_NAME")) or planned.card_name, AddressType.BILL_TO, notes
        )
        bill = [single] if single else []
    ship = _address_list(row.get("ALL_SHIP_ADDRS"), AddressType.SHIP_TO, notes, "ALL_SHIP_ADDRS")
    if not ship:
        # createCustomer: the SHIP_* columns, each falling back to its BILL_* twin.
        merged = {
            f"SHIP_{part}": row.get(f"SHIP_{part}") or row.get(f"BILL_{part}")
            for part in ("STREET", "BLOCK", "CITY", "ZIP", "STATE", "COUNTRY")
        }
        name = _text(row.get("SHIP_ADDR_NAME")) or _text(row.get("BILL_ADDR_NAME")) or planned.card_name
        single = _columns_address(merged, "SHIP", name, AddressType.SHIP_TO, notes)
        ship = [single] if single else []
    return columns, bill, ship


def _plan_vendor(row: dict, planned: PlannedRow, notes: list):
    columns = _common(row, planned, notes)
    columns.update(
        vendor_type=_choice(row.get("VENDOR_TYPE"), VendorType.values, VendorType.SUPPLIER, "Vendor type", notes),
        products=_clip(row.get("PRODUCTS"), 500, "Products", notes),
        payment_terms_requested=_clip(row.get("PAYMENT_TERMS"), 50, "Requested payment terms", notes),
        alt_contact=_clip(row.get("ALT_CONTACT"), 20, "Alternate contact", notes),
        tan=_clip(row.get("TAN"), 10, "TAN", notes).upper(),
        has_tds=_flag(row.get("HAS_TDS")),
        tds_category=_clip(row.get("TDS_CATEGORY"), 100, "TDS category", notes),
        tds_rate=_decimal(row.get("TDS_RATE"), "TDS_RATE", notes),
        tds_ldc_number=_clip(row.get("TDS_LDC_NO"), 50, "TDS LDC number", notes),
        fssai_number=_clip(row.get("FSSAI_NO"), 14, "FSSAI number", notes).upper(),
        ship_same_as_bill=True,
        sap_attachment_entry=_positive(row.get("SAP_ATTACHMENT_ENTRY"), "SAP_ATTACHMENT_ENTRY", notes),
        card_code_prefix=_clip(row.get("MGR_CARD_CODE_PREFIX"), 10, "Card-code prefix", notes).upper()
        or VENDOR_FAMILY.default_prefix,
        bp_group_code=_int(row.get("MGR_GROUP_CODE"), "MGR_GROUP_CODE", notes),
        payment_terms_code=_int(row.get("MGR_PAY_TERMS_CODE"), "MGR_PAY_TERMS_CODE", notes),
        control_account=_clip(row.get("MGR_PURCHASE_ACCOUNT"), 15, "AP account", notes),
        control_account_name=_clip(row.get("MGR_PURCHASE_ACCT_NAME"), 100, "AP account name", notes),
        credit_limit=_decimal(row.get("MGR_CREDIT_LIMIT"), "MGR_CREDIT_LIMIT", notes),
        sales_employee_code=_int(row.get("MGR_SALES_PERSON_CODE"), "MGR_SALES_PERSON_CODE", notes),
        sales_employee_name=_clip(row.get("MGR_SALES_EMPLOYEE"), 155, "Sales employee", notes),
    )
    state = portal_state_code(_text(row.get("BILL_STATE")))
    # createVendor's name for the one address it had: "<NAME[:25]>-<STATE>".
    name = f"{planned.card_name[:25]}-{state}"
    bill_address = _columns_address(row, "BILL", name, AddressType.BILL_TO, notes)
    bill = [bill_address] if bill_address else []
    if bill_address and columns["gstin"]:
        bill_address["gstin"] = columns["gstin"]
    # createVendor shipped to the billing address, GSTIN and all.
    ship = [dict(address, address_type=AddressType.SHIP_TO) for address in bill]
    return columns, bill, ship


def plan_rows(rows: list, family, default_company: str = "") -> list[PlannedRow]:
    """Read and check every row; nothing is written."""
    by_database = _company_codes_by_database()
    mapped = CUSTOMER_COLUMNS if family is CUSTOMER_FAMILY else VENDOR_COLUMNS
    seen_ids, seen_codes, planned_rows = set(), {}, []
    for raw in rows:
        row = {str(key).upper(): value for key, value in raw.items()}
        notes: list = []
        legacy_id = _int(row.get("ID"), "ID", notes)
        planned = PlannedRow(legacy_id=legacy_id, notes=notes)
        planned_rows.append(planned)
        if legacy_id is None or legacy_id <= 0:
            planned.error = "no ID"
            continue
        if legacy_id in seen_ids:
            planned.error = f"ID {legacy_id} appears more than once in the file"
            continue
        seen_ids.add(legacy_id)

        planned.status = _text(row.get("STATUS")).upper() or RegistrationStatus.PENDING
        if planned.status not in RegistrationStatus.values:
            planned.error = f"unknown status {planned.status!r}"
            continue
        planned.card_name = _clip(row.get("CARD_NAME"), 100, "Name", notes).upper()
        if not planned.card_name:
            planned.error = "no CARD_NAME"
            continue

        database = _text(row.get("COMPANY")).upper()
        planned.company_code = by_database.get(database, "")
        if not planned.company_code:
            if default_company:
                planned.company_code = default_company
                notes.append(f"COMPANY {database or '(blank)'} is not a known SAP database; used {default_company}.")
            else:
                planned.skip = f"unknown company {database or '(blank)'}"
                continue

        builder = _plan_customer if family is CUSTOMER_FAMILY else _plan_vendor
        columns, bill, ship = builder(row, planned, notes)
        if not bill:
            notes.append("No billing address in the portal row.")
        elif columns["gstin"] and not bill[0]["gstin"]:
            bill[0]["gstin"] = columns["gstin"]
        _decisions(row, family, planned.status, columns, planned, notes)
        columns["status"] = planned.status
        code = columns.get("card_code")
        if code:
            key = (planned.company_code, code)
            if key in seen_codes:
                notes.append(f"Card code {code} is also on portal row {seen_codes[key]}; kept only as the SAP code.")
                columns["card_code"] = ""
            else:
                seen_codes[key] = legacy_id
        planned.columns = columns
        planned.addresses = unique_names(bill + ship, planned.card_name) if bill or ship else []
        if family is VENDOR_FAMILY:
            planned.bank_accounts = _bank_accounts(row.get("BANK_ACCOUNTS"), notes)
        planned.documents = _documents(row.get("ATTACHMENTS"), notes)
        for column, value in row.items():
            if column not in mapped and _text(value) and column not in planned.legacy_fields:
                planned.legacy_fields[column] = value if isinstance(value, (int, float, bool)) else _text(value)
    return planned_rows


def load_rows(path: str) -> list:
    """The export as a list of row objects: a JSON array, or ``{"rows": […]}``."""
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except OSError as exc:
        raise ImportFileError(f"Cannot read {path}: {exc}") from exc
    except ValueError as exc:
        raise ImportFileError(f"{path} is not JSON: {exc}") from exc
    if isinstance(data, dict):
        data = data.get("rows") or data.get("value") or data.get("data")
    if not isinstance(data, list) or not all(isinstance(row, dict) for row in data):
        raise ImportFileError(f"{path} must hold a JSON array of row objects (column name → value).")
    return data


# ---------------------------------------------------------------------------
# writing
# ---------------------------------------------------------------------------


def _touched_in_ji(registration) -> bool:
    """Anybody did anything to it here: an event without the ``imported`` key.

    ``has_key`` rather than ``data__imported=True``: excluding a JSON value
    compares NULL for the events that lack the key, and SQL drops those rows —
    so every hand-made event would have looked like an import.
    """
    return registration.events.exclude(data__has_key="imported").exists()


def _write(registration, planned: PlannedRow, family, actor, stored: list, *, replacing: bool):
    if replacing:
        old = list(registration.attachments.all())
        registration.attachments.all().delete()
        registration.events.all().delete()
        transaction.on_commit(lambda: delete_files(old))
    replace_addresses(registration, planned.addresses)
    if family is VENDOR_FAMILY:
        replace_bank_accounts(registration, planned.bank_accounts)
    for document in planned.documents:
        stored.append(
            add_attachment(
                registration,
                document.kind,
                name=document.name,
                content_type=document.content_type,
                content=document.content,
            )
        )
    events = [
        RegistrationEvent(
            **owner_kwargs(registration),
            kind=event["kind"],
            actor_name=event["actor_name"][:150],
            at=event["at"],
            note=event["note"],
            data={"imported": True},
        )
        for event in planned.events
    ]
    events.append(
        RegistrationEvent(
            **owner_kwargs(registration),
            kind=EventKind.IMPORTED,
            actor=actor,
            actor_name=(actor.full_name or actor.email) if actor else "SAP Portal import",
            note=f"Imported from SAP Portal {family.portal_table} ID {planned.legacy_id} (status {planned.status}).",
            data={"imported": True, "notes": planned.notes, "documents": len(planned.documents)},
        )
    )
    RegistrationEvent.objects.bulk_create(events)


def existing_ids(family, planned_rows: list) -> dict:
    ids = [row.legacy_id for row in planned_rows if row.legacy_id]
    return {
        registration.legacy_portal_id: registration
        for registration in family.model.objects.filter(legacy_portal_id__in=ids)
    }


def apply_plan(planned_rows: list, family, *, actor=None, update: bool = False) -> Counter:
    """Write the planned rows in one transaction. Returns counts keyed
    ``(outcome, status)``; outcome is created / updated / already imported /
    changed in JI / skipped / invalid."""
    counts: Counter = Counter()
    stored: list = []
    companies = {company.code: company for company in Company.objects.all()}
    try:
        with transaction.atomic():
            existing = existing_ids(family, planned_rows)
            for planned in planned_rows:
                if planned.error:
                    counts[("invalid", planned.status or "?")] += 1
                    continue
                if planned.skip:
                    counts[("skipped", planned.status)] += 1
                    continue
                company = companies.get(planned.company_code)
                if company is None:
                    planned.notes.append(f"No company {planned.company_code} in this database.")
                    counts[("skipped", planned.status)] += 1
                    continue
                code = planned.columns.get("card_code")
                if code and family.model.objects.filter(company=company, card_code=code).exclude(
                    legacy_portal_id=planned.legacy_id
                ).exists():
                    planned.notes.append(f"Card code {code} is already on another registration; kept only as the SAP code.")
                    planned.columns["card_code"] = ""
                current = existing.get(planned.legacy_id)
                if current is not None:
                    if not update:
                        counts[("already imported", planned.status)] += 1
                        continue
                    if _touched_in_ji(current):
                        counts[("changed in JI", current.status)] += 1
                        continue
                    for key, value in planned.columns.items():
                        setattr(current, key, value)
                    current.company = company
                    current.legacy_fields = planned.legacy_fields
                    current.save()
                    _write(current, planned, family, actor, stored, replacing=True)
                    counts[("updated", planned.status)] += 1
                    continue
                registration = family.model.objects.create(
                    company=company,
                    legacy_portal_id=planned.legacy_id,
                    legacy_fields=planned.legacy_fields,
                    created_by=actor,
                    **planned.columns,
                )
                _write(registration, planned, family, actor, stored, replacing=False)
                counts[("created", planned.status)] += 1
    except Exception:
        delete_files(stored)
        raise
    return counts


def preview(planned_rows: list, family, *, update: bool = False) -> Counter:
    """What ``apply_plan`` would do, reading the database but writing nothing."""
    counts: Counter = Counter()
    existing = existing_ids(family, planned_rows)
    known = set(Company.objects.values_list("code", flat=True))
    for planned in planned_rows:
        if planned.error:
            counts[("invalid", planned.status or "?")] += 1
        elif planned.skip or planned.company_code not in known:
            counts[("skipped", planned.status)] += 1
        elif planned.legacy_id in existing:
            current = existing[planned.legacy_id]
            if not update:
                counts[("already imported", planned.status)] += 1
            elif _touched_in_ji(current):
                counts[("changed in JI", current.status)] += 1
            else:
                counts[("updated", planned.status)] += 1
        else:
            counts[("created", planned.status)] += 1
    return counts
