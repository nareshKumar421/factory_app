"""Taking the SAP copies, and serving one when HANA does not answer.

Two rules keep a copy honest:

* **Live first.** A reader asks SAP, and only an unreachable HANA
  (:func:`hana_unreachable`) sends it to the copy. A refused query -- a bug --
  still fails, so a copy never papers over one.
* **Whole or not at all.** A refresh replaces the list in one transaction, and a
  failed or empty answer leaves the last good copy in place.

The master lists change rarely, so each is taken once a night
(:data:`NIGHT_STARTS`); the dispatch bills at every run (``bills``). A copy
that fails is retried on every run until one succeeds.
"""

import logging
from dataclasses import dataclass
from datetime import time, timedelta
from typing import Callable

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from hdbcli import dbapi

from company.models import Company
from sap_client.exceptions import SAPConnectionError

from .codec import pack, unpack
from .models import MirrorDataset, MirrorRow

logger = logging.getLogger(__name__)

#: A nightly copy is due at the first run after this local time.
NIGHT_STARTS = time(1, 0)


# ---------------------------------------------------------------------------
# what is copied
# ---------------------------------------------------------------------------

class KeepCopy(Exception):
    """SAP answered, but not with something to replace the copy by."""


def _fetch_fg_items(company_code):
    from barcode.services.oitm_item_service import OitmItemService

    return OitmItemService(company_code).list_all_for_copy()


def _fetch_warehouses(company_code):
    from warehouse.services.wms_hana_reader import WMSHanaReader

    return WMSHanaReader(company_code=company_code).get_warehouses()


def _fetch_production_boms(company_code):
    from production_execution.services.sap_reader import ProductionOrderReader

    # use_copy=False: a HANA failure half-way fails this run, not the copy.
    return ProductionOrderReader(company_code, use_copy=False).get_all_boms_for_copy()


def _replace_list(fetch, key, search_text):
    """A refresh that swaps one company's copy of a small list for SAP's answer."""

    def refresh(company, state, now):
        rows = list(fetch(company.code))
        if not rows and state.row_count:
            # An empty master list is far likelier a broken answer than a real one.
            raise KeepCopy("SAP answered with no rows; the previous copy was kept.")
        unique = {}
        for row in rows:
            unique.setdefault(str(key(row))[:100], row)
        with transaction.atomic():
            state.rows.all().delete()
            MirrorRow.objects.bulk_create(
                [
                    MirrorRow(
                        dataset=state, key=code, search_text=search_text(row)[:400], data=pack(row)
                    )
                    for code, row in unique.items()
                ]
            )
        return len(unique)

    return refresh


def _fetch_warehouse_print_info(company_code):
    from sap_client.context import CompanyContext
    from sap_client.hana.warehouse_reader import HanaWarehouseReader

    # use_copy=False: a HANA failure half-way fails this run, not the copy.
    return HanaWarehouseReader(CompanyContext(company_code), use_copy=False).all_print_info_for_copy()


def _fetch_oil_item_mapping(company_code):
    from barcode.services.oitm_item_service import OitmItemService

    return OitmItemService(company_code).list_oil_item_mappings_for_copy()


def _fetch_vendors(company_code):
    from dataclasses import asdict

    from sap_client.context import CompanyContext
    from sap_client.hana.vendor_reader import HanaVendorReader

    # use_copy=False: a HANA failure half-way fails this run, not the copy.
    reader = HanaVendorReader(CompanyContext(company_code), use_copy=False)
    return [asdict(vendor) for vendor in reader.get_active_vendors()]


def _refresh_bills(company, state, now):
    from . import bills

    return bills.refresh(company, state, now)


def _refresh_purchase_orders(company, state, now):
    from . import purchase_orders

    return purchase_orders.refresh(company, state, now)


@dataclass(frozen=True)
class Dataset:
    label: str
    #: ``(company, state, now) -> rows held``; raises to keep the copy as it is.
    refresh: Callable
    #: How often it is due; ``None`` is once a night (:data:`NIGHT_STARTS`).
    every: timedelta | None = None
    #: Only these companies have it; empty is every company.
    companies: tuple = ()


FG_ITEMS = "fg_items"
WAREHOUSES = "warehouses"
PRODUCTION_BOMS = "boms"
VENDORS = "vendors"
WAREHOUSE_PRINT_INFO = "warehouse_print_info"
OIL_ITEM_MAPPING = "oil_item_mapping"
PURCHASE_ORDERS = "purchase_orders"
BILLS = "bills"

DATASETS = {
    # The label page's item picker: without it no production label can be made.
    FG_ITEMS: Dataset(
        label="Finished-goods items",
        refresh=_replace_list(
            fetch=lambda code: _fetch_fg_items(code),
            key=lambda row: row["item_code"],
            search_text=lambda row: f"{row['item_code']} {row['item_name']}".lower(),
        ),
    ),
    # The pallet pages' warehouse dropdown: a new pallet has to say where it is.
    WAREHOUSES: Dataset(
        label="Warehouses",
        refresh=_replace_list(
            fetch=lambda code: _fetch_warehouses(code),
            key=lambda row: row["code"],
            search_text=lambda row: f"{row['code']} {row['name']}".lower(),
        ),
    ),
    # Starting a production run: the run-startable items (finished goods with a
    # BOM), each with its BOM lines, pieces per case and litres per piece. The
    # stock check is not copied -- it already steps aside when SAP is down.
    PRODUCTION_BOMS: Dataset(
        label="Production BOMs",
        refresh=_replace_list(
            fetch=lambda code: _fetch_production_boms(code),
            key=lambda row: row["item"]["ItemCode"],
            search_text=lambda row: (
                f"{row['item']['ItemCode']} {row['item'].get('ItemName') or ''}".lower()
            ),
        ),
    ),
    # A transfer's printed letterhead: each warehouse's address, branch and GSTIN,
    # and the company's name. Fetched on every transfer page, not only to print.
    WAREHOUSE_PRINT_INFO: Dataset(
        label="Warehouse letterheads",
        refresh=_replace_list(
            fetch=lambda code: _fetch_warehouse_print_info(code),
            key=lambda row: row["code"],
            search_text=lambda row: f"{row['code']} {row.get('name', '')}".lower(),
        ),
    ),
    # An Oil <-> Mart transfer (an invoice BST) maps every Oil item to its Mart
    # item. The mapping is a column on Mart's own items (U_Oil_ItemCode), so it
    # is copied for Mart only.
    OIL_ITEM_MAPPING: Dataset(
        label="Oil to Mart item mapping",
        refresh=_replace_list(
            fetch=lambda code: _fetch_oil_item_mapping(code),
            key=lambda row: row["item_code"],
            search_text=lambda row: f"{row['item_code']} {row['oil_item_code']}".lower(),
        ),
        companies=("JIVO_MART",),
    ),
    # The gate: the supplier a truck comes from...
    VENDORS: Dataset(
        label="Active vendors",
        refresh=_replace_list(
            fetch=lambda code: _fetch_vendors(code),
            key=lambda row: row["vendor_code"],
            search_text=lambda row: f"{row['vendor_code']} {row['vendor_name'] or ''}".lower(),
        ),
    ),
    # ...and its open POs, which change through the day as they are raised and
    # received against: due at every run, like the bills. See ``purchase_orders``.
    PURCHASE_ORDERS: Dataset(
        label="Open purchase orders",
        refresh=lambda company, state, now: _refresh_purchase_orders(company, state, now),
        every=timedelta(minutes=10),
    ),
    # Dispatch: the last 30 days of A/R bills, and every older bill still in
    # dispatch planning. Booked the same day they load, so due at every 15-minute
    # run -- a little under 15, so timer jitter never skips one. See ``bills``.
    BILLS: Dataset(
        label="A/R bills (last 30 days and in planning)",
        refresh=lambda company, state, now: _refresh_bills(company, state, now),
        every=timedelta(minutes=10),
    ),
}


# ---------------------------------------------------------------------------
# taking the copies
# ---------------------------------------------------------------------------

def last_night_start(now):
    local = timezone.localtime(now)
    start = local.replace(
        hour=NIGHT_STARTS.hour, minute=NIGHT_STARTS.minute, second=0, microsecond=0
    )
    if local < start:
        start -= timedelta(days=1)
    return start


def is_due(state, now, every=None) -> bool:
    if state is None or state.synced_at is None:
        return True
    if every is not None:
        return state.synced_at <= now - every
    return state.synced_at < last_night_start(now)


def refresh(company, name, now=None) -> MirrorDataset:
    """Replace one company's copy of one list with what SAP answers now.

    A failed or unusable answer keeps the copy as it was and says why in
    ``last_error``; it stays due, so the next run tries again.
    """
    now = now or timezone.now()
    spec = DATASETS[name]
    state, _ = MirrorDataset.objects.get_or_create(company=company, name=name)
    state.last_attempt_at = now
    try:
        count = spec.refresh(company, state, now)
    except KeepCopy as keep:
        state.last_error = str(keep)
        state.save(update_fields=["last_attempt_at", "last_error"])
        logger.warning("SAP copy of %s for %s kept: %s", name, company.code, keep)
        return state
    except Exception as exc:  # noqa: BLE001 -- any failure keeps the copy; it is logged
        state.last_error = f"SAP did not answer ({exc}); the copy was kept."[:5000]
        state.save(update_fields=["last_attempt_at", "last_error"])
        logger.warning("SAP copy of %s for %s not refreshed: %s", name, company.code, exc)
        return state
    state.synced_at = now
    state.row_count = count
    state.last_error = ""
    state.save(update_fields=["last_attempt_at", "synced_at", "row_count", "last_error"])
    logger.info("SAP copy of %s for %s: %s rows", name, company.code, count)
    return state


def sync_due(now=None, *, force=False):
    """Refresh every copy that is due, for every company the app has SAP for."""
    now = now or timezone.now()
    states = {
        (state.company_id, state.name): state
        for state in MirrorDataset.objects.all()
    }
    refreshed = []
    for company in Company.objects.filter(code__in=list(settings.COMPANY_DB)).order_by("code"):
        for name, spec in DATASETS.items():
            if spec.companies and company.code not in spec.companies:
                continue
            if force or is_due(states.get((company.id, name)), now, spec.every):
                refreshed.append(refresh(company, name, now))
    return refreshed


# ---------------------------------------------------------------------------
# serving a copy
# ---------------------------------------------------------------------------

def hana_unreachable(exc) -> bool:
    """Whether a failed read means HANA could not be asked, not that it refused.

    Follows the chain of causes, because every reader wraps the driver's error
    in its own. A refused statement (``ProgrammingError`` and friends) is a bug
    or a data problem, and a copy would only hide it.
    """
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, SAPConnectionError):
            return True
        if isinstance(exc, dbapi.Error):
            return not isinstance(
                exc, (dbapi.ProgrammingError, dbapi.IntegrityError, dbapi.DataError)
            )
        exc = exc.__cause__ or exc.__context__
    return False


def copied_rows(company_code, name, *, search="", limit=None):
    """``(rows, as_of)`` from a company's copy of a list, or ``None`` if it has none."""
    state = (
        MirrorDataset.objects.filter(
            company__code=company_code, name=name, synced_at__isnull=False
        )
        .only("id", "synced_at")
        .first()
    )
    if state is None:
        return None
    rows = MirrorRow.objects.filter(dataset=state).order_by("key")
    search = (search or "").strip().lower()
    if search:
        rows = rows.filter(search_text__contains=search)
    if limit:
        rows = rows[:limit]
    return [unpack(row.data) for row in rows], timezone.localtime(state.synced_at)
