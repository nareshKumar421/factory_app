"""
pm_demand/services.py

Turns the five SAP reads into the board: which packing material production
consumed, which packing material walked out of the gate inside finished
goods, and the gap between the two.

THE THREE COLUMNS, AND WHY THEY DIFFER
--------------------------------------
    consumed    what the line issued, from OINM -- fact
    bom         what the recipes say that production should have taken --
                standard
    dispatched  what the recipes say went out inside the finished goods
                that were invoiced -- fact times standard

``consumed`` against ``bom`` is a yield question: over-issue is scrap,
theft, a stale recipe or a mis-booked warehouse. On August 2026 Oil, most
items agree to the unit and PM0000085 (CAPS 1 OR 2 LTR WHITE AND YELLOW WITH
LOGO) does not -- 570,331 issued against 516,323 called for, 10.5% over.

``consumed`` against ``dispatched`` is a working-capital question. Packing
material consumed but not yet shipped is sitting in the finished-goods
godown; ship more than was made in the period and the figure goes negative,
which is finished-goods stock being drawn down, not an error. That is exactly
the "made 400, dispatched 200" split, read per packing item.

DAYS OF COVER
-------------
Stock on hand divided by the rate the line burned it. The rate is per WORKING
day, because Sunday consumes nothing -- August 2026 was 26 working days, not
31, and a calendar-day rate would overstate every cover figure by 19%. The
stock is a snapshot of now while the rate comes from the period asked for;
that mixture is the question, not a mistake. `constants` records where the
stock is read from and why that list is not the planning module's.

Every quantity is in the item's own inventory unit, so quantities across
items are NOT comparable -- tape is metres, caps are pieces. That is why the
ranking is by value. Value is quantity times the item's unit price from the
item master. On the Oil company ``OITM.AvgPrice`` is zero for all 201
packaging items consumed in August, so in practice every price used is
``LastPurPrc``, and all 201 have one.
"""

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from sap_client.context import CompanyContext

from . import constants
from .app_reader import PmDemandAppReader
from .hana_reader import PmDemandReader

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pure helpers -- no SAP, no Django, unit-testable
# ---------------------------------------------------------------------------


def index_bom(bom_lines: Iterable[Dict[str, Any]]) -> Dict[str, List[Tuple[str, float]]]:
    """Group BOM lines by parent item: ``{parent: [(pm_code, per_unit), ...]}``."""
    index: Dict[str, List[Tuple[str, float]]] = {}
    for line in bom_lines:
        parent = line.get("parent_code") or ""
        pm_code = line.get("pm_code") or ""
        per_unit = float(line.get("qty_per_unit") or 0)
        if not parent or not pm_code or per_unit <= 0:
            continue
        index.setdefault(parent, []).append((pm_code, per_unit))
    return index


def explode(
    fg_qty_by_item: Dict[str, float],
    bom_index: Dict[str, List[Tuple[str, float]]],
) -> Tuple[Dict[str, float], Dict[str, Any]]:
    """Explode finished-goods quantities into packing material through the BOM.

    Returns the packing material required, keyed by item code, and a coverage
    report. Coverage is not decoration: a finished good with no production BOM
    contributes nothing, so a dashboard that did not state its coverage would
    silently under-report every component of that SKU. On August 2026 Oil the
    coverage is 97 of 97 items and 100% of the quantity, and it is printed so
    that the day it stops being 100% somebody sees it.

    A negative finished-goods quantity -- a month whose returns for one SKU
    outweighed its invoices -- explodes negative too. It is a real reduction
    in the packaging that left the factory, so it is kept rather than clamped.
    """
    required: Dict[str, float] = {}
    covered_items = 0
    covered_qty = 0.0
    missing_items: List[str] = []
    total_qty = 0.0

    for item_code, qty in fg_qty_by_item.items():
        total_qty += qty
        lines = bom_index.get(item_code)
        if not lines:
            if qty:
                missing_items.append(item_code)
            continue
        covered_items += 1
        covered_qty += qty
        for pm_code, per_unit in lines:
            required[pm_code] = required.get(pm_code, 0.0) + qty * per_unit

    coverage = {
        "fg_items": len(fg_qty_by_item),
        "fg_items_with_bom": covered_items,
        "fg_items_without_bom": sorted(missing_items),
        "qty_total": round(total_qty, 3),
        "qty_with_bom": round(covered_qty, 3),
        "qty_covered_pct": round(covered_qty / total_qty * 100, 2) if total_qty else 0.0,
    }
    return required, coverage


def share_pct(value: float, total: float) -> float:
    return round(value / total * 100, 2) if total else 0.0


# How much of an item's consumption must be met by the factory's own output
# before it is called in-house.
#
# A bare "any receipt at all" test is useless here: August 2026 Oil booked a
# TransType 59 receipt into BH-PC for most packaging items -- 2,578 caps and
# 1,283 labels against consumption of 570,331 and 390,503 -- which are rework
# and line-return bookings, not manufacture. That test flagged bought-in
# cartons, caps and labels as made in-house, which is every row and therefore
# no information. At half of consumption the badge picks out the three PET
# bottles and the HDPE bottle the plant genuinely blows, which is the fact
# worth showing: their demand is really preform demand.
IN_HOUSE_SHARE_THRESHOLD = 0.5


def is_in_house(in_house_qty: float, consumed_qty: float) -> bool:
    """Whether the factory's own output covers this item's consumption.

    An item received from production but never issued counts as in-house on
    any positive receipt -- there is no consumption to take a share of, and
    something was plainly made.
    """
    if in_house_qty <= 0:
        return False
    if consumed_qty <= 0:
        return True
    return in_house_qty / consumed_qty >= IN_HOUSE_SHARE_THRESHOLD


def working_days(date_from: date, date_to: date, non_working: Sequence[int]) -> int:
    """Days the factory actually ran in an inclusive date range.

    Counted rather than approximated, because the answer drives every cover
    figure: August 2026 has 31 calendar days and 26 working ones, and using
    the wrong one moves the burn rate by 19%.

    Never returns 0. A range consisting only of Sundays would otherwise make
    the rate infinite and the cover zero, reporting a shortage that does not
    exist; a single working day is the safe floor.
    """
    closed = set(non_working)
    if date_to < date_from:
        date_from, date_to = date_to, date_from
    days = sum(
        1
        for offset in range((date_to - date_from).days + 1)
        if (date_from + timedelta(days=offset)).weekday() not in closed
    )
    return max(days, 1)


def cover_days(stock_qty: float, consumed_qty: float, period_working_days: int) -> Optional[float]:
    """Working days the stock on hand will last at the period's burn rate.

    ``None`` when nothing was consumed: an item with stock and no consumption
    has unbounded cover, and printing a very large number would rank it as the
    safest thing in the store when really it is not moving at all -- which is
    the non-moving dashboard's question, not this one.

    Negative stock -- SAP does allow it -- reads as zero cover rather than
    negative days, because there is nothing on the shelf either way.
    """
    if consumed_qty <= 0 or period_working_days <= 0:
        return None
    per_day = consumed_qty / period_working_days
    if per_day <= 0:
        return None
    return round(max(stock_qty, 0) / per_day, 1)


def cover_status(days: Optional[float]) -> str:
    """Which band a cover figure falls in: the thing a buyer acts on.

    Fed the incoming-inclusive cover, never the on-hand figure. An item
    replenished in monthly bulk lots is at 0.3 days on hand for a day before
    every delivery, and a board that shouted at the buyer each time would be
    ignored within a week.
    """
    if days is None:
        return "unknown"
    if days < constants.COVER_CRITICAL_DAYS:
        return "critical"
    if days < constants.COVER_LOW_DAYS:
        return "low"
    return "ok"


def rank(rows: List[Dict[str, Any]], value_key: str, qty_key: str) -> Tuple[List[Dict[str, Any]], str]:
    """Sort rows most-important first, by value where value exists.

    Falls back to quantity when nothing carries a price at all -- a company
    whose packaging item master has no prices should still get a readable
    board rather than an arbitrary order -- and reports which axis was used
    so the screen can say so.
    """
    if any((row.get(value_key) or 0) for row in rows):
        return sorted(rows, key=lambda r: r.get(value_key) or 0, reverse=True), "value"
    return sorted(rows, key=lambda r: r.get(qty_key) or 0, reverse=True), "quantity"


def roll_up_families(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Roll the item rows up by packaging family (``OITM.U_Sub_Group``).

    Item codes answer "which label is bleeding"; families answer "where does
    the packaging spend go". An item SAP has left un-grouped rolls up under
    'UNGROUPED' rather than being dropped, so the family totals always add up
    to the item totals.
    """
    buckets: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        family = row.get("sub_group") or "UNGROUPED"
        bucket = buckets.setdefault(
            family,
            {
                "sub_group": family,
                "item_count": 0,
                "consumed_value": 0.0,
                "bom_value": 0.0,
                "dispatched_value": 0.0,
                "wastage_value": 0.0,
            },
        )
        bucket["item_count"] += 1
        bucket["consumed_value"] += row.get("consumed_value") or 0
        bucket["bom_value"] += row.get("bom_value") or 0
        bucket["dispatched_value"] += row.get("dispatched_value") or 0
        bucket["wastage_value"] += row.get("wastage_value") or 0

    total = sum(b["consumed_value"] for b in buckets.values())
    families = []
    for bucket in buckets.values():
        variance = bucket["consumed_value"] - bucket["bom_value"]
        families.append(
            {
                "sub_group": bucket["sub_group"],
                "item_count": bucket["item_count"],
                "consumed_value": round(bucket["consumed_value"], 2),
                "bom_value": round(bucket["bom_value"], 2),
                "variance_value": round(variance, 2),
                "dispatched_value": round(bucket["dispatched_value"], 2),
                "wastage_value": round(bucket["wastage_value"], 2),
                "consumed_share_pct": share_pct(bucket["consumed_value"], total),
            }
        )
    return sorted(families, key=lambda f: f["consumed_value"], reverse=True)


def build_item_rows(
    *,
    consumed: Dict[str, Dict[str, float]],
    bom_from_production: Dict[str, float],
    bom_from_dispatch: Dict[str, float],
    master: Dict[str, Dict[str, Any]],
    fg_produced_qty: float,
    stock: Optional[Dict[str, Dict[str, float]]] = None,
    open_po: Optional[Dict[str, Dict[str, Any]]] = None,
    period_working_days: int = 1,
    today: Optional[date] = None,
) -> List[Dict[str, Any]]:
    """One row per packing item that appears in any of the three columns.

    The union is deliberate. An item the recipes call for but the line never
    issued is a booking gap; an item issued that no recipe calls for is an
    unrecorded change or a mis-posted warehouse. Both are worth seeing, and
    intersecting the sets would hide exactly those two cases.
    """
    stock = stock or {}
    open_po = open_po or {}
    # Only items in one of the three columns get a row, so stock is looked up
    # rather than unioned in: a pallet of packaging that nothing consumed and
    # no recipe calls for belongs to the non-moving dashboard, not this one.
    codes = set(consumed) | set(bom_from_production) | set(bom_from_dispatch)
    rows: List[Dict[str, Any]] = []

    for code in codes:
        movements = consumed.get(code, {})
        item = master.get(code, {})
        price = float(item.get("unit_price") or 0)

        consumed_qty = float(movements.get("issued_qty") or 0)
        in_house_qty = float(movements.get("in_house_qty") or 0)
        on_hand = stock.get(code, {})
        stock_qty = float(on_hand.get("stock_qty") or 0)
        ordered = open_po.get(code, {})
        open_po_qty = float(ordered.get("open_po_qty") or 0)
        earliest_due = ordered.get("earliest_due")

        # Cover on the shelf, and cover once what is already bought arrives.
        # The second drives the status; the first is still shown, because a
        # buyer whose incoming is all overdue needs to see the real shelf.
        days_cover = cover_days(stock_qty, consumed_qty, period_working_days)
        days_cover_incl_po = cover_days(
            stock_qty + open_po_qty, consumed_qty, period_working_days
        )
        bom_qty = float(bom_from_production.get(code) or 0)
        dispatched_qty = float(bom_from_dispatch.get(code) or 0)
        wastage_qty = float(movements.get("wastage_qty") or 0)
        variance_qty = consumed_qty - bom_qty

        rows.append(
            {
                "item_code": code,
                "item_name": item.get("item_name") or "",
                "sub_group": item.get("sub_group") or "",
                "uom": item.get("uom") or "",
                "unit_price": round(price, 4),
                # made in-house rather than bought: explains a consumed item
                # with no purchase transfer behind it
                "in_house": is_in_house(in_house_qty, consumed_qty),
                "in_house_qty": round(in_house_qty, 3),
                "consumed_qty": round(consumed_qty, 3),
                "consumed_value": round(consumed_qty * price, 2),
                "bom_qty": round(bom_qty, 3),
                "bom_value": round(bom_qty * price, 2),
                "variance_qty": round(variance_qty, 3),
                "variance_value": round(variance_qty * price, 2),
                "variance_pct": round(variance_qty / bom_qty * 100, 2) if bom_qty else None,
                "wastage_qty": round(wastage_qty, 3),
                "wastage_value": round(wastage_qty * price, 2),
                "dispatched_qty": round(dispatched_qty, 3),
                "dispatched_value": round(dispatched_qty * price, 2),
                # consumed but not yet shipped -- packaging sitting in the
                # finished-goods godown. Negative means the period shipped
                # more than it made, drawing finished-goods stock down.
                "retained_qty": round(consumed_qty - dispatched_qty, 3),
                "retained_value": round((consumed_qty - dispatched_qty) * price, 2),
                "per_1000_fg": (
                    round(consumed_qty / fg_produced_qty * 1000, 3)
                    if fg_produced_qty
                    else None
                ),
                # Cover: what is on the shelf now, against the rate the line
                # burned it over the period. Null cover means nothing was
                # consumed, not that cover is infinite.
                "stock_qty": round(stock_qty, 3),
                "stock_value": round(float(on_hand.get("stock_value") or 0), 2),
                "avg_daily_qty": (
                    round(consumed_qty / period_working_days, 3)
                    if period_working_days > 0
                    else None
                ),
                "days_cover": days_cover,
                "days_cover_incl_po": days_cover_incl_po,
                "open_po_qty": round(open_po_qty, 3),
                "open_po_lines": int(ordered.get("po_lines") or 0),
                "open_po_earliest_due": (
                    str(earliest_due) if earliest_due else None
                ),
                # Flagged, not netted off: this module cannot tell a late
                # supplier from a purchase order nobody ever closed.
                "open_po_overdue": bool(
                    earliest_due and today and earliest_due < today
                ),
                "cover_status": cover_status(days_cover_incl_po),
            }
        )
    return rows


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class PmDemandService:
    """Orchestrates the PM Demand board for one company.

    Usage:
        service = PmDemandService(company_code="JIVO_OIL")
        board = service.get_report(
            date_from="2026-08-01", date_to="2026-08-31",
            top_n=10, include_intercompany=True,
        )
    """

    def __init__(
        self,
        company_code: str,
        reader: Optional[PmDemandReader] = None,
        app_reader: Optional[PmDemandAppReader] = None,
    ):
        self.company_code = company_code
        self.context = CompanyContext(company_code)
        self.reader = reader or PmDemandReader(self.context)
        self.app_reader = app_reader or PmDemandAppReader(company_code)

    def get_report(
        self,
        *,
        date_from,
        date_to,
        top_n: int = constants.DEFAULT_TOP_N,
        include_intercompany: bool = True,
        source: str = constants.SOURCE_SAP,
    ) -> Dict[str, Any]:
        """The board, from SAP or from FactoryFlow's own records.

        Only the QUANTITIES follow ``source``. The recipe, the stock, the open
        purchase orders and the packing-material item list always come from
        SAP, because the app either has no copy or has only a per-run copy
        that would disagree with itself between periods. ``app_reader``
        documents each case; ``meta.source_notes`` states them on screen.
        """
        if source not in constants.SOURCES:
            source = constants.SOURCE_SAP
        use_app = source == constants.SOURCE_APP
        fg_warehouses = constants.fg_warehouses(self.company_code)
        consumption_warehouses = constants.consumption_warehouses(self.company_code)
        wastage_warehouses = constants.wastage_warehouses(self.company_code)
        upstream_warehouses = constants.upstream_warehouses(self.company_code)
        stock_warehouses = constants.stock_warehouses(self.company_code)
        intercompany = constants.intercompany_card_codes(self.company_code)
        non_working = constants.non_working_weekdays()
        period_working_days = working_days(date_from, date_to, non_working)

        # The recipe and the packing-material item list are SAP's either way:
        # the app has only a per-run BOM copy and carries no item group at all.
        bom_index = index_bom(self.reader.pm_bom_lines())
        master = {row["item_code"]: row for row in self.reader.pm_master()}

        if use_app:
            produced_rows = self.app_reader.fg_produced(date_from, date_to)
            dispatched_rows = self.app_reader.fg_dispatched(date_from, date_to)
            movement_rows = self.app_reader.pm_movements(
                date_from, date_to, master.keys()
            )
        else:
            produced_rows = self.reader.fg_produced(fg_warehouses, date_from, date_to)
            dispatched_rows = self.reader.fg_dispatched(
                date_from, date_to, intercompany
            )
            movement_rows = self.reader.pm_movements(
                date_from,
                date_to,
                consumption_warehouses,
                wastage_warehouses,
                upstream_warehouses,
            )
        stock = {row["item_code"]: row for row in self.reader.pm_stock(stock_warehouses)}
        today = datetime.now(timezone.utc).date()
        open_po = {row["item_code"]: row for row in self.reader.pm_open_po(today)}

        produced_by_item = {r["item_code"]: r["qty"] for r in produced_rows}
        fg_produced_qty = sum(produced_by_item.values())

        # Both dispatch views, always. The one the caller asked for drives the
        # board; the other is still reported, because a 66%-intercompany month
        # quoted as though it were third-party sale is the single easiest way
        # to misread this screen.
        dispatch_total = sum(r["qty"] for r in dispatched_rows)
        dispatch_intercompany = sum(r["intercompany_qty"] for r in dispatched_rows)
        dispatch_returns = sum(r["return_qty"] for r in dispatched_rows)

        dispatched_by_item = {
            r["item_code"]: (
                r["qty"] if include_intercompany else r["qty"] - r["intercompany_qty"]
            )
            for r in dispatched_rows
        }
        dispatch_scoped_qty = sum(dispatched_by_item.values())

        bom_from_production, production_coverage = explode(produced_by_item, bom_index)
        bom_from_dispatch, dispatch_coverage = explode(dispatched_by_item, bom_index)

        consumed = {r["item_code"]: r for r in movement_rows}

        rows = build_item_rows(
            consumed=consumed,
            bom_from_production=bom_from_production,
            bom_from_dispatch=bom_from_dispatch,
            master=master,
            fg_produced_qty=fg_produced_qty,
            stock=stock,
            open_po=open_po,
            period_working_days=period_working_days,
            today=today,
        )

        consumed_total_value = sum(r["consumed_value"] for r in rows)
        dispatched_total_value = sum(r["dispatched_value"] for r in rows)

        production_ranked, ranked_by = rank(
            [r for r in rows if r["consumed_qty"] or r["bom_qty"]],
            "consumed_value",
            "consumed_qty",
        )
        dispatch_ranked, _ = rank(
            [r for r in rows if r["dispatched_qty"]],
            "dispatched_value",
            "dispatched_qty",
        )

        production_top = [
            {**row, "share_pct": share_pct(row["consumed_value"], consumed_total_value)}
            for row in production_ranked[:top_n]
        ]
        dispatch_top = [
            {
                **row,
                "share_pct": share_pct(row["dispatched_value"], dispatched_total_value),
            }
            for row in dispatch_ranked[:top_n]
        ]

        return {
            "summary": self._summary(
                rows=rows,
                fg_produced_qty=fg_produced_qty,
                dispatch_total=dispatch_total,
                dispatch_scoped_qty=dispatch_scoped_qty,
                dispatch_intercompany=dispatch_intercompany,
                dispatch_returns=dispatch_returns,
                consumed_total_value=consumed_total_value,
                dispatched_total_value=dispatched_total_value,
            ),
            "production_top": production_top,
            "dispatch_top": dispatch_top,
            "families": roll_up_families(rows),
            "cover_watch": self._cover_watch(rows, top_n),
            "upstream": self._upstream(movement_rows, master, top_n),
            "meta": {
                "company_code": self.company_code,
                "date_from": str(date_from),
                "date_to": str(date_to),
                "top_n": top_n,
                "include_intercompany": include_intercompany,
                "source": source,
                "consumption_basis": constants.CONSUMPTION_BASIS[source],
                "source_notes": self._source_notes(source),
                "ranked_by": ranked_by,
                "pm_item_group": constants.PM_ITEM_GROUP,
                "pm_item_group_name": self._pm_group_name(),
                "fg_warehouses": fg_warehouses,
                "consumption_warehouses": consumption_warehouses,
                "wastage_warehouses": wastage_warehouses,
                "upstream_warehouses": upstream_warehouses,
                "stock_warehouses": stock_warehouses,
                "period_working_days": period_working_days,
                "cover_critical_days": constants.COVER_CRITICAL_DAYS,
                "cover_low_days": constants.COVER_LOW_DAYS,
                "intercompany_card_codes": intercompany,
                "production_bom_coverage": production_coverage,
                "dispatch_bom_coverage": dispatch_coverage,
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            },
        }

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _source_notes(source: str) -> List[str]:
        """Plain sentences about what this reading can and cannot tell you.

        Rendered on screen. Every one of these is a place a reader could
        otherwise draw a wrong conclusion from a number that looks complete.
        """
        if source == constants.SOURCE_APP:
            return [
                "Quantities are FactoryFlow's own records, not SAP's.",
                "The consumption column is the BOM the warehouse APPROVED, "
                "not what the line issued - the app's issued_qty is never "
                "written, so it holds no record of actual issue.",
                "Production is counted from run cases x pieces per case; "
                "dispatch from gate-out billed quantity.",
                "Group-company and sales-return splits are unavailable: the "
                "app records a truck leaving, not who was invoiced.",
                "The recipe, stock, open purchase orders and the "
                "in-house-manufacture flag still come from SAP.",
            ]
        return [
            "Quantities are SAP's: goods issues out of the production "
            "warehouse, and A/R invoices net of credit notes.",
        ]

    def _pm_group_name(self) -> str:
        """The packaging group's name as SAP has it now.

        A read that fails must not take the whole board down with it -- the
        name is context, not a figure -- so it degrades to blank and the
        screen simply shows the group code it counted.
        """
        try:
            return self.reader.pm_group_name()
        except Exception:  # noqa: BLE001 - context only, never worth a 502
            logger.warning(
                "Could not read the packaging item group name for %s",
                self.company_code,
                exc_info=True,
            )
            return ""

    @staticmethod
    def _summary(
        *,
        rows: List[Dict[str, Any]],
        fg_produced_qty: float,
        dispatch_total: float,
        dispatch_scoped_qty: float,
        dispatch_intercompany: float,
        dispatch_returns: float,
        consumed_total_value: float,
        dispatched_total_value: float,
    ) -> Dict[str, Any]:
        bom_total_value = sum(r["bom_value"] for r in rows)
        wastage_total_value = sum(r["wastage_value"] for r in rows)
        stock_total_value = sum(r.get("stock_value") or 0 for r in rows)
        overdue = [r for r in rows if r.get("open_po_overdue")]
        critical = [r for r in rows if r.get("cover_status") == "critical"]
        low = [r for r in rows if r.get("cover_status") == "low"]
        return {
            "fg_produced_qty": round(fg_produced_qty, 3),
            "fg_dispatched_qty": round(dispatch_scoped_qty, 3),
            "fg_dispatched_all_qty": round(dispatch_total, 3),
            "fg_dispatched_intercompany_qty": round(dispatch_intercompany, 3),
            "fg_dispatched_third_party_qty": round(
                dispatch_total - dispatch_intercompany, 3
            ),
            "fg_returns_qty": round(dispatch_returns, 3),
            # How much of what was made walked out again in the same period.
            # Over 100% means the period shipped out of stock it did not make.
            "dispatch_ratio_pct": (
                round(dispatch_scoped_qty / fg_produced_qty * 100, 2)
                if fg_produced_qty
                else None
            ),
            "pm_items": len(rows),
            "pm_consumed_value": round(consumed_total_value, 2),
            "pm_bom_value": round(bom_total_value, 2),
            "pm_variance_value": round(consumed_total_value - bom_total_value, 2),
            "pm_variance_pct": (
                round((consumed_total_value - bom_total_value) / bom_total_value * 100, 2)
                if bom_total_value
                else None
            ),
            "pm_dispatched_value": round(dispatched_total_value, 2),
            "pm_retained_value": round(consumed_total_value - dispatched_total_value, 2),
            "pm_wastage_value": round(wastage_total_value, 2),
            # Stock only for the items this board reports -- packaging nothing
            # consumed is not counted, so this is not the whole store's value.
            "pm_stock_value": round(stock_total_value, 2),
            "pm_items_critical_cover": len(critical),
            "pm_items_low_cover": len(low),
            "pm_items_overdue_po": len(overdue),
        }

    @staticmethod
    def _cover_watch(rows: List[Dict[str, Any]], top_n: int) -> List[Dict[str, Any]]:
        """The items closest to running out, soonest first.

        This is the one list on the board that is NOT ranked by value, and
        deliberately so: a 22-paise label that stops the line tomorrow halts
        production exactly as hard as a rupee-nine cap would, and ranking the
        watch list by spend would bury it under the bottles. Cheapness is not
        safety.

        Items with no consumption in the period are left out -- they have no
        burn rate, so no date they run out on.

        Ranked on cover INCLUDING open purchase orders, so what surfaces is
        genuinely unbought rather than merely mid-delivery-cycle.
        """
        at_risk = [
            row
            for row in rows
            if row.get("days_cover_incl_po") is not None and row.get("consumed_qty")
        ]
        # Sorted on cover once the open orders land, for the same reason the
        # status is: on-hand alone puts every bulk-bought item at the top of
        # the list on the day before its delivery.
        at_risk.sort(
            key=lambda row: (
                row["days_cover_incl_po"],
                -(row.get("consumed_value") or 0),
            )
        )
        return at_risk[:top_n]

    @staticmethod
    def _upstream(
        movement_rows: Sequence[Dict[str, Any]],
        master: Dict[str, Dict[str, Any]],
        top_n: int,
    ) -> List[Dict[str, Any]]:
        """The blowing line's own consumption, reported apart from every total.

        On Oil this is preforms issued at BH-SDL to be blown into the PET
        bottles that the filling line then consumes. Adding it to the main
        figures would count the same packaging twice, at two stages; leaving
        it out entirely would hide the plant's single largest packaging
        movement by piece count. So: its own list, its own heading.
        """
        rows = []
        for movement in movement_rows:
            qty = float(movement.get("upstream_qty") or 0)
            if qty <= 0:
                continue
            item = master.get(movement["item_code"], {})
            price = float(item.get("unit_price") or 0)
            rows.append(
                {
                    "item_code": movement["item_code"],
                    "item_name": item.get("item_name") or "",
                    "sub_group": item.get("sub_group") or "",
                    "uom": item.get("uom") or "",
                    "unit_price": round(price, 4),
                    "consumed_qty": round(qty, 3),
                    "consumed_value": round(qty * price, 2),
                }
            )
        ranked, _ = rank(rows, "consumed_value", "consumed_qty")
        total = sum(r["consumed_value"] for r in rows)
        return [
            {**row, "share_pct": share_pct(row["consumed_value"], total)}
            for row in ranked[:top_n]
        ]
