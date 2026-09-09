"""
packing_material/services.py

Orchestration and arithmetic for the packing-material board.

The readers return flat rows and join nothing. Everything that turns those
rows into the figures on screen -- the per-warehouse roll-up, the BOM
explosion, the ranking and the coverage -- happens in the module-level
functions below, on plain dicts. They take no connection and no company, so
every number this board shows is unit-testable without SAP.

Three reads, three panels, three endpoints:

    get_stock()       the four cards and what opens behind each one
    get_production()  packing material the line issued over the period
    get_dispatch()    packing material that left inside what was dispatched

They are separate because they answer questions at different times. Stock is
NOW and does not move when the month changes; the two top lists are a period
and reload when it does; and only the dispatch list changes when the SAP/
FactoryFlow toggle is flipped. One endpoint would reload all three every time
any one of them was asked a new question.
"""

import logging
from datetime import date, datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence

from sap_client.context import CompanyContext

from .app_reader import PackingMaterialAppReader
from .constants import (
    DISPATCH_BASIS,
    MAX_LISTED_DRIVERS,
    MAX_LISTED_ITEMS,
    MAX_PLAN_LIST_LIMIT,
    PLAN_LIST_LIMIT,
    FG_ITEM_GROUP,
    PM_ITEM_GROUP,
    PM_ITEM_GROUP_NAME,
    RANKED_BY,
    SOURCE_APP,
    SOURCE_SAP,
    consumption_warehouses,
    intercompany_card_codes,
    stock_warehouses,
    supply_warehouses,
)
from .errors import PlanNotFound
from .hana_reader import PackingMaterialReader

logger = logging.getLogger(__name__)

# How many un-explodable item codes the coverage block names before it stops.
# The count is always exact; the list is there to start an investigation, and
# a hundred codes in a JSON payload starts nothing.
MAX_LISTED_GAPS = 25


# ---------------------------------------------------------------------------
# Arithmetic -- no SAP, no Django, no company
# ---------------------------------------------------------------------------


def index_master(master: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """The packaging item master keyed by item code."""
    return {row["item_code"]: row for row in master if row.get("item_code")}


def _describe(item_code: str, master: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Name, unit, family and unit cost for one code, blank if unknown.

    A code with no master row is still reported rather than dropped: it means
    the item group changed under a movement that has already happened, and a
    silently missing row would understate the month.
    """
    row = master.get(item_code) or {}
    return {
        "item_code": item_code,
        "item_name": row.get("item_name", ""),
        "uom": row.get("uom", ""),
        "sub_group": row.get("sub_group", ""),
        "unit_price": float(row.get("unit_price", 0) or 0),
    }


def build_stock_board(
    warehouse_codes: Sequence[str],
    warehouse_names: Iterable[Dict[str, Any]],
    stock_rows: Iterable[Dict[str, Any]],
    master: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    """The stock cards: one per warehouse asked for, plus the total of them.

    Warehouses come back in the order they were asked for, which is the order
    the cards appear in, so a card can never swap places with another between
    two loads.

    The total's ``item_count`` counts DISTINCT items, not the sum of the three
    card counts: a carton held in two of the stores is one item the factory
    has, and adding the counts would report it as two.
    """
    known = {row["code"]: row for row in warehouse_names if row.get("code")}

    grouped: Dict[str, List[Dict[str, Any]]] = {code: [] for code in warehouse_codes}
    for row in stock_rows:
        code = row.get("warehouse") or ""
        if code not in grouped:
            # A warehouse the reader returned that nobody asked for. Cannot
            # happen through the API, which passes the same list to both, but
            # dropping it is safer than inventing a card for it.
            continue
        qty = float(row.get("stock_qty", 0) or 0)
        value = float(row.get("stock_value", 0) or 0)
        item = _describe(row.get("item_code") or "", master)
        item.update({"stock_qty": qty, "stock_value": value})
        grouped[code].append(item)

    warehouses: List[Dict[str, Any]] = []
    total_qty = 0.0
    total_value = 0.0
    distinct_items = set()

    for code in warehouse_codes:
        items = sorted(grouped[code], key=lambda row: -row["stock_qty"])
        qty = sum(row["stock_qty"] for row in items)
        value = sum(row["stock_value"] for row in items)
        total_qty += qty
        total_value += value
        distinct_items.update(row["item_code"] for row in items)

        meta = known.get(code)
        warehouses.append(
            {
                "code": code,
                # SAP's own name, so the card is labelled the way the store is
                # labelled on the floor. Falls back to the code rather than to
                # a blank, which would give an unnamed card.
                "name": (meta or {}).get("name") or code,
                "inactive": bool((meta or {}).get("inactive", False)),
                # A code SAP does not have at all in this company. Reported so
                # a misconfigured warehouse list reads as a mistake instead of
                # as an empty store.
                "exists": meta is not None,
                "item_count": len(items),
                "total_qty": round(qty, 3),
                "total_value": round(value, 2),
                "items": items,
            }
        )

    for warehouse in warehouses:
        warehouse["share_pct"] = (
            round(warehouse["total_qty"] / total_qty * 100, 2) if total_qty else 0.0
        )

    return {
        "warehouses": warehouses,
        "total": {
            "warehouse_count": len(warehouses),
            "item_count": len(distinct_items),
            "total_qty": round(total_qty, 3),
            "total_value": round(total_value, 2),
        },
    }


def rank_by_qty(
    quantities: Dict[str, float],
    master: Dict[str, Dict[str, Any]],
    top_n: int,
) -> Dict[str, Any]:
    """The top ``top_n`` items by quantity, with the period's totals beside.

    Ranked on quantity because that is how the factory counts packaging, and
    the rupee value rides along on every row: a top ten by pieces is led by
    caps and labels, a top ten by rupees by bottles, and a reader who can see
    both columns can tell which question they are looking at the answer to.

    ``share_pct`` is a share of the WHOLE period, not of the ten rows shown,
    so "the top ten are 78% of the month" is a statement the numbers support.
    """
    rows: List[Dict[str, Any]] = []
    for item_code, qty in quantities.items():
        qty = float(qty or 0)
        if qty <= 0:
            continue
        row = _describe(item_code, master)
        row["qty"] = round(qty, 3)
        row["value"] = round(qty * row["unit_price"], 2)
        rows.append(row)

    rows.sort(key=lambda row: (-row["qty"], row["item_code"]))

    total_qty = sum(row["qty"] for row in rows)
    total_value = sum(row["value"] for row in rows)

    top = rows[: max(top_n, 0)]
    for rank, row in enumerate(top, start=1):
        row["rank"] = rank
        row["share_pct"] = round(row["qty"] / total_qty * 100, 2) if total_qty else 0.0

    return {
        "items": top,
        "totals": {
            "item_count": len(rows),
            "total_qty": round(total_qty, 3),
            "total_value": round(total_value, 2),
            "shown_qty": round(sum(row["qty"] for row in top), 3),
            "shown_value": round(sum(row["value"] for row in top), 2),
            "shown_share_pct": (
                round(sum(row["qty"] for row in top) / total_qty * 100, 2)
                if total_qty
                else 0.0
            ),
        },
    }


def index_bom(bom_lines: Iterable[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """Packing-material BOM components, keyed by the item they belong to.

    A component appearing twice on one recipe is summed rather than kept
    twice, because SAP allows a material on two lines of the same BOM (a
    printed and an unprinted carton on one gift pack) and exploding both
    separately would put the same item on the board twice.
    """
    by_parent: Dict[str, Dict[str, float]] = {}
    for line in bom_lines:
        parent = line.get("parent_code") or ""
        pm_code = line.get("pm_code") or ""
        per_unit = float(line.get("qty_per_unit", 0) or 0)
        if not parent or not pm_code or per_unit <= 0:
            continue
        by_parent.setdefault(parent, {})
        by_parent[parent][pm_code] = by_parent[parent].get(pm_code, 0.0) + per_unit

    return {
        parent: [{"pm_code": code, "qty_per_unit": qty} for code, qty in components.items()]
        for parent, components in by_parent.items()
    }


def explode_dispatch(
    fg_rows: Iterable[Dict[str, Any]],
    bom: Dict[str, List[Dict[str, Any]]],
) -> Dict[str, Any]:
    """Packing material inside the finished goods dispatched.

    One level of explosion: every finished item dispatched, times the packing
    material its production recipe calls for per unit. See ``constants`` for
    why one level is complete here rather than approximate.

    Coverage is returned with it and is not optional. An item with no
    production BOM contributes nothing, and without the coverage figures a
    board that explodes 40% of the month looks exactly like a board that
    explodes all of it.

    Coverage counts ABSOLUTE quantity, and that is not a detail. An item can
    be net negative over a period -- more of it came back than went out -- and
    on the live September data three such items had no production BOM, which
    made the un-explodable volume NEGATIVE and reported coverage as 100.29%.
    Magnitude is the right measure for "how much of the traffic could be
    explained" anyway; the net figure is what ``summary.fg_dispatched_qty`` is
    for.
    """
    quantities: Dict[str, float] = {}
    volume_total = 0.0
    volume_with_bom = 0.0
    items_with_bom: List[str] = []
    items_without_bom: List[str] = []

    for row in fg_rows:
        parent = (row.get("item_code") or "").strip()
        qty = float(row.get("qty", 0) or 0)
        if not parent:
            continue

        volume_total += abs(qty)

        components = bom.get(parent)
        if not components:
            if qty:
                items_without_bom.append(parent)
            continue

        items_with_bom.append(parent)
        volume_with_bom += abs(qty)
        # A net-negative item still explodes, and takes packaging back off the
        # board with it: the month is net and so is this.
        for component in components:
            code = component["pm_code"]
            quantities[code] = quantities.get(code, 0.0) + qty * component["qty_per_unit"]

    return {
        "quantities": quantities,
        "coverage": {
            "fg_items": len(items_with_bom) + len(items_without_bom),
            "fg_items_with_bom": len(items_with_bom),
            "fg_items_without_bom": sorted(items_without_bom)[:MAX_LISTED_GAPS],
            "fg_items_without_bom_count": len(items_without_bom),
            "qty_total": round(volume_total, 3),
            "qty_with_bom": round(volume_with_bom, 3),
            "qty_covered_pct": (
                round(volume_with_bom / volume_total * 100, 2) if volume_total else 0.0
            ),
        },
    }


def split_dispatch_lines(
    rows: Iterable[Dict[str, Any]],
    groups: Optional[Dict[str, int]] = None,
) -> Dict[str, Any]:
    """Dispatched lines split into finished goods and packing material.

    ``groups`` maps item code to item group and is needed only for the
    FactoryFlow source, whose rows carry no group of their own. SAP rows
    arrive already tagged.

    Anything that is neither finished goods nor packaging -- raw material,
    consumables, the odd asset sold off a bill -- is counted and set aside.
    Silently folding it into either bucket would put oil in a packaging
    figure.
    """
    groups = groups or {}
    fg: List[Dict[str, Any]] = []
    direct_pm: List[Dict[str, Any]] = []
    other_count = 0
    other_qty = 0.0

    for row in rows:
        code = (row.get("item_code") or "").strip()
        if not code:
            continue
        group = int(row.get("item_group") or groups.get(code) or 0)
        if group == FG_ITEM_GROUP:
            fg.append(row)
        elif group == PM_ITEM_GROUP:
            direct_pm.append(row)
        else:
            other_count += 1
            other_qty += float(row.get("qty", 0) or 0)

    return {
        "fg": fg,
        "direct_pm": direct_pm,
        "other_item_count": other_count,
        "other_qty": round(other_qty, 3),
    }


def summarise_dispatch(fg_rows: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """What was dispatched, in finished-goods pieces, and to whom."""
    rows = list(fg_rows)
    net = sum(float(row.get("qty", 0) or 0) for row in rows)
    intercompany = sum(float(row.get("intercompany_qty", 0) or 0) for row in rows)
    returns = sum(float(row.get("return_qty", 0) or 0) for row in rows)
    return {
        "fg_dispatched_qty": round(net, 3),
        "fg_intercompany_qty": round(intercompany, 3),
        "fg_third_party_qty": round(net - intercompany, 3),
        "fg_returns_qty": round(returns, 3),
        "fg_item_count": len(rows),
    }


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# The requirement board -- arithmetic, no SAP, no Django, no company
# ---------------------------------------------------------------------------


def _as_date(value) -> Optional[date]:
    """A ``date`` from whatever HANA handed back for a date column."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value)[:10]
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def resolve_plan(plans: Sequence[Dict[str, Any]], as_of: date) -> Optional[Dict[str, Any]]:
    """Which plan the board opens on when nobody has picked one.

    In order: the plan whose period CONTAINS the day (the month being worked,
    which is what somebody opening this board almost always wants), then the
    most recent plan that has already started, then the newest plan there is.

    The last fallback matters on a company where planners work a month ahead:
    with no plan yet started, an empty board would look like a broken one.
    Highest ``abs_id`` breaks a tie, because a planner who has authored the
    same month twice meant the second one.
    """
    if not plans:
        return None

    def started(plan) -> Optional[date]:
        return _as_date(plan.get("start_date"))

    def ends(plan) -> Optional[date]:
        return _as_date(plan.get("end_date"))

    current = [
        plan
        for plan in plans
        if started(plan) and started(plan) <= as_of and (ends(plan) is None or ends(plan) >= as_of)
    ]
    if current:
        return max(current, key=lambda plan: (started(plan), plan.get("abs_id", 0)))

    past = [plan for plan in plans if started(plan) and started(plan) <= as_of]
    if past:
        return max(past, key=lambda plan: (started(plan), plan.get("abs_id", 0)))

    return max(plans, key=lambda plan: plan.get("abs_id", 0))


def issue_window(plan: Dict[str, Any], as_of: date) -> Dict[str, date]:
    """The period `Issue (PC)` counts: the plan's first day to today.

    "1st of the month to today" generalised, so a plan somebody opens after it
    has finished reports the whole of its own month rather than nothing. The
    end is the EARLIER of today and the plan's last day: counting movements
    after the plan closed would charge next month's transfers against this
    month's requirement.
    """
    start = _as_date(plan.get("start_date")) or as_of
    end = _as_date(plan.get("end_date")) or as_of
    return {"date_from": start, "date_to": min(end, as_of)}


def index_drivers(
    drivers: Iterable[Dict[str, Any]], max_per_item: int
) -> Dict[str, List[Dict[str, Any]]]:
    """Driving SKUs per component, biggest contributor first, capped.

    The cap is a payload limit, not a truth limit: the `Planning` figure on
    the row is always the sum of ALL drivers, and the row reports how many
    there are so a truncated list cannot be mistaken for the whole of it.
    """
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for row in drivers:
        code = row.get("item_code")
        if not code:
            continue
        grouped.setdefault(code, []).append(
            {
                "parent_code": row.get("parent_code", ""),
                "parent_name": row.get("parent_name", ""),
                "plan_qty": float(row.get("plan_qty", 0) or 0),
                "qty_per_unit": float(row.get("qty_per_unit", 0) or 0),
                "required_qty": float(row.get("required_qty", 0) or 0),
            }
        )
    return {
        code: sorted(rows, key=lambda row: -row["required_qty"])[:max_per_item]
        for code, rows in grouped.items()
    }


def build_requirement_rows(
    requirement: Iterable[Dict[str, Any]],
    received: Iterable[Dict[str, Any]],
    on_hand: Iterable[Dict[str, Any]],
    open_po: Iterable[Dict[str, Any]],
    master: Dict[str, Dict[str, Any]],
    drivers: Dict[str, List[Dict[str, Any]]],
    driver_counts: Dict[str, int],
    plan_end: Optional[date],
    as_of: date,
) -> List[Dict[str, Any]]:
    """One row per packing-material component the plan needs.

    The seven columns of the buyer's sheet, in the order they are read:

        planning_qty      the BOM requirement for the whole plan
        issued_pc_qty     what has already reached the floor
        rest_planning_qty planning - issued
        on_hand_qty       what the feeding stores still hold
        req_qty           on hand - rest planning      (negative = short)
        open_po_qty       what is already on order
        req_after_po_qty  req + open PO                (negative = still short)

    NOTHING IS FLOORED AT ZERO. `rest_planning_qty` goes negative where the
    floor drew more than the plan called for, and both `req` figures go
    negative for the ordinary case of a shortage -- which is the entire point
    of the board. Flooring the first would hide over-issue, flooring the
    second would hide the shortage. `short_qty` is provided alongside as the
    positive magnitude, because summing and sorting on a shortage is easier
    when it is a positive number, and totals must not let a surplus on one
    row cancel a shortage on another.

    Rows are keyed off the REQUIREMENT and not off the movements: an item
    received onto the floor that the plan does not call for is unplanned
    consumption, a real thing worth knowing, but it is not a row on a list of
    what the plan needs. It is counted in the meta block instead.
    """
    received_by_code = {
        row["item_code"]: row for row in received if row.get("item_code")
    }
    on_hand_by_code = {
        row["item_code"]: float(row.get("on_hand_qty", 0) or 0)
        for row in on_hand
        if row.get("item_code")
    }
    po_by_code = {row["item_code"]: row for row in open_po if row.get("item_code")}

    rows: List[Dict[str, Any]] = []
    for entry in requirement:
        code = entry.get("item_code")
        if not code:
            continue

        planning = float(entry.get("planning_qty", 0) or 0)
        movement = received_by_code.get(code, {})
        issued = float(movement.get("received_qty", 0) or 0)
        rest = planning - issued
        held = on_hand_by_code.get(code, 0.0)
        req = held - rest

        po = po_by_code.get(code, {})
        open_po_qty = float(po.get("open_po_qty", 0) or 0)
        req_after_po = req + open_po_qty

        earliest_due = _as_date(po.get("po_earliest_due"))
        unit_price = float((master.get(code) or {}).get("unit_price", 0) or 0)
        short_qty = max(0.0, -req_after_po)

        rows.append(
            {
                **_describe(code, master),
                "planning_qty": round(planning, 3),
                "sku_count": int(entry.get("sku_count", 0) or 0),
                "issued_pc_qty": round(issued, 3),
                # The split, so a row can be read either way without a second
                # request: transferred up from the stores, or made in-house
                # straight onto the floor. See constants for why both count.
                "issued_transfer_qty": round(float(movement.get("transfer_qty", 0) or 0), 3),
                "issued_produced_qty": round(float(movement.get("produced_qty", 0) or 0), 3),
                "issued_other_qty": round(float(movement.get("other_qty", 0) or 0), 3),
                "rest_planning_qty": round(rest, 3),
                "on_hand_qty": round(held, 3),
                "req_qty": round(req, 3),
                "open_po_qty": round(open_po_qty, 3),
                "po_lines": int(po.get("po_lines", 0) or 0),
                "po_earliest_due": earliest_due.isoformat() if earliest_due else None,
                "po_latest_due": (
                    _as_date(po.get("po_latest_due")).isoformat()
                    if _as_date(po.get("po_latest_due"))
                    else None
                ),
                "req_after_po_qty": round(req_after_po, 3),
                "short_qty": round(short_qty, 3),
                "short_value": round(short_qty * unit_price, 2),
                # The floor drew more of this than the plan asked for. Left in
                # the arithmetic rather than clamped, and flagged so it reads
                # as a question about the plan instead of as spare stock.
                "over_issued": issued > planning,
                # An open order exists and closes the gap -- but the earliest
                # of it is not due until after the plan is over, so it does
                # not close it IN TIME. Both facts, because "covered" and
                # "covered this month" are different answers.
                "po_covers_shortage": req < 0 and req_after_po >= 0,
                "po_due_after_plan": bool(
                    open_po_qty
                    and plan_end
                    and (earliest_due is None or earliest_due > plan_end)
                ),
                # The order is open and its due date has already gone by.
                # On Oil this is not the exception: on 9 September 2026
                # every one of the 172 open packing-material lines was
                # past due, the furthest-out due date on the whole book
                # being 7 September. Without this, `po_covers_shortage`
                # reads as goods arriving when it may mean an order
                # nobody has chased since 2024.
                "po_overdue": bool(
                    open_po_qty and earliest_due and earliest_due < as_of
                ),
                "drivers": drivers.get(code, []),
                "driver_count": int(driver_counts.get(code, 0)),
            }
        )

    # Worst first: the biggest remaining shortfall in rupees at the top, so
    # the row a buyer has to act on today is the row they land on. Rupees and
    # not pieces, because 500,000 caps short and 500 tins short are not the
    # same problem. Item code breaks ties so the order is stable between
    # reads of the same data.
    rows.sort(key=lambda row: (-row["short_value"], -row["short_qty"], row["item_code"]))
    return rows


def requirement_totals(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """The column sums, and the counts that make the board readable at a glance.

    Shortages are summed from ``short_qty`` -- the positive magnitude -- and
    never from ``req_after_po_qty``. Summing the signed figure lets a surplus
    of caps cancel a shortage of cartons and reports a factory that is fine
    when it is not.
    """

    def total(key: str) -> float:
        return round(sum(float(row.get(key, 0) or 0) for row in rows), 3)

    short_before = [row for row in rows if row["req_qty"] < 0]
    short_after = [row for row in rows if row["req_after_po_qty"] < 0]

    return {
        "item_count": len(rows),
        "planning_qty": total("planning_qty"),
        "issued_pc_qty": total("issued_pc_qty"),
        "issued_transfer_qty": total("issued_transfer_qty"),
        "issued_produced_qty": total("issued_produced_qty"),
        "rest_planning_qty": total("rest_planning_qty"),
        "on_hand_qty": total("on_hand_qty"),
        "open_po_qty": total("open_po_qty"),
        # Counts and magnitudes, before and after open orders are netted off.
        # The pair is the headline: how many holes there are, and how many are
        # still holes once what is already bought is taken into account.
        "short_before_po_count": len(short_before),
        "short_before_po_qty": round(sum(-row["req_qty"] for row in short_before), 3),
        "short_after_po_count": len(short_after),
        "short_after_po_qty": total("short_qty"),
        "short_after_po_value": round(sum(float(row["short_value"]) for row in rows), 2),
        "covered_by_po_count": sum(1 for row in rows if row["po_covers_shortage"]),
        # Short, an order exists, and the earliest of it lands after the plan
        # ends. Covered on paper, not covered in time.
        "po_due_after_plan_count": sum(
            1 for row in rows if row["po_due_after_plan"] and row["req_qty"] < 0
        ),
        # Rows leaning on an order that is already late. Counted only
        # where the row is short, for the same reason as above: an
        # over-issued item needs nothing, however overdue its order.
        "po_overdue_count": sum(
            1 for row in rows if row["po_overdue"] and row["req_qty"] < 0
        ),
        "over_issued_count": sum(1 for row in rows if row["over_issued"]),
        "surplus_count": sum(1 for row in rows if row["req_after_po_qty"] > 0),
    }


def unplanned_issue(
    received: Iterable[Dict[str, Any]],
    planned_codes: Sequence[str],
    master: Dict[str, Dict[str, Any]],
    max_listed: int,
) -> Dict[str, Any]:
    """Packing material that reached the floor without being in the plan's BOM.

    Not rows on the table -- the table answers what the plan needs -- but a
    number worth seeing: 26 of the 84 items received into BH-PC in September
    2026 (169,917 units) were not on the plan's bill of materials at all. That
    is either production the plan does not describe, or a recipe that is out
    of date, and both are somebody's to look at.
    """
    known = set(planned_codes)
    extra = [
        row
        for row in received
        if row.get("item_code") and row["item_code"] not in known
    ]
    extra.sort(key=lambda row: -float(row.get("received_qty", 0) or 0))
    return {
        "item_count": len(extra),
        "qty": round(sum(float(row.get("received_qty", 0) or 0) for row in extra), 3),
        "items": [
            {
                **_describe(row["item_code"], master),
                "qty": round(float(row.get("received_qty", 0) or 0), 3),
            }
            for row in extra[:max_listed]
        ],
    }


def plan_coverage_summary(
    coverage: Sequence[Dict[str, Any]], max_listed: int
) -> Dict[str, Any]:
    """How much of the plan the requirement above actually accounts for.

    A planned SKU with no production BOM contributes nothing to `Planning` and
    is indistinguishable, on the table alone, from one that needs no
    packaging. Reported as a share of PLANNED QUANTITY rather than of item
    count: three missing recipes out of 84 sounds negligible, and whether it
    is depends entirely on whether those three are 1.5% of the month or 40% of
    it.
    """
    total_qty = sum(float(row.get("plan_qty", 0) or 0) for row in coverage)
    without_bom = [row for row in coverage if not row.get("has_bom")]
    without_pm = [row for row in coverage if row.get("has_bom") and not row.get("has_pm")]
    missing_qty = sum(float(row.get("plan_qty", 0) or 0) for row in without_bom)

    return {
        "plan_item_count": len(coverage),
        "plan_qty": round(total_qty, 3),
        "items_without_bom": len(without_bom),
        "items_without_bom_qty": round(missing_qty, 3),
        "items_without_bom_list": [
            {
                "item_code": row.get("item_code", ""),
                "item_name": row.get("item_name", ""),
                "plan_qty": round(float(row.get("plan_qty", 0) or 0), 3),
            }
            for row in sorted(
                without_bom, key=lambda row: -float(row.get("plan_qty", 0) or 0)
            )[:max_listed]
        ],
        # A recipe that exists but names no packaging. A different fact from a
        # missing recipe, and not necessarily wrong -- loose oil in a drum may
        # genuinely have none.
        "items_with_bom_without_pm": len(without_pm),
        "qty_covered_pct": (
            round((total_qty - missing_qty) / total_qty * 100, 1) if total_qty else 0.0
        ),
    }


class PackingMaterialService:
    """One company's packing-material board.

    Readers are injectable so the arithmetic above can be exercised end to end
    against fixed rows, with no HANA connection and no live database.
    """

    def __init__(
        self,
        company_code: str,
        reader: Optional[PackingMaterialReader] = None,
        app_reader: Optional[PackingMaterialAppReader] = None,
    ):
        self.company_code = company_code
        self.reader = reader if reader is not None else PackingMaterialReader(
            CompanyContext(company_code)
        )
        self.app_reader = (
            app_reader if app_reader is not None else PackingMaterialAppReader(company_code)
        )

    # ------------------------------------------------------------------
    # Shared meta
    # ------------------------------------------------------------------

    def _group_meta(self) -> Dict[str, Any]:
        """Which item group was counted, and whether SAP still agrees.

        ``pm_item_group_matches`` is false when code 105 no longer carries the
        name it is supposed to. The board keeps working on the code -- that is
        what SAP enforces -- and says on screen that the two have parted, which
        is the only way a renumbered group shows up as a question rather than
        as a month where the factory apparently used no packaging.
        """
        name = self.reader.pm_group_name()
        return {
            "pm_item_group": PM_ITEM_GROUP,
            "pm_item_group_name": name,
            "pm_item_group_matches": name.strip().upper() == PM_ITEM_GROUP_NAME,
        }

    # ------------------------------------------------------------------
    # The four cards
    # ------------------------------------------------------------------

    def get_stock(self) -> Dict[str, Any]:
        """Packing-material stock in each store, and the total of them."""
        warehouses = stock_warehouses(self.company_code)
        master = index_master(self.reader.pm_master())
        board = build_stock_board(
            warehouses,
            self.reader.warehouse_names(warehouses),
            self.reader.pm_stock_by_warehouse(warehouses),
            master,
        )
        board["meta"] = {
            "company_code": self.company_code,
            "stock_warehouses": warehouses,
            "ranked_by": RANKED_BY,
            "fetched_at": _now_iso(),
            **self._group_meta(),
        }
        return board

    # ------------------------------------------------------------------
    # Section one: into production
    # ------------------------------------------------------------------

    def get_production(self, date_from, date_to, top_n: int) -> Dict[str, Any]:
        """Packing material the line issued over the period, top first."""
        warehouses = consumption_warehouses(self.company_code)
        master = index_master(self.reader.pm_master())
        issued = self.reader.pm_issued(warehouses, date_from, date_to)

        ranked = rank_by_qty(
            {row["item_code"]: row["issued_qty"] for row in issued if row.get("item_code")},
            master,
            top_n,
        )
        ranked["meta"] = {
            "company_code": self.company_code,
            "date_from": str(date_from),
            "date_to": str(date_to),
            "top_n": top_n,
            "ranked_by": RANKED_BY,
            # The goods issue, not the transfer to the line and not the
            # approved BOM. Named on every response so no screen can relabel
            # it -- see constants for what reading the transfer instead cost.
            "basis": "issued",
            "consumption_warehouses": warehouses,
            "fetched_at": _now_iso(),
            **self._group_meta(),
        }
        return ranked

    # ------------------------------------------------------------------
    # Section two: out through the gate
    # ------------------------------------------------------------------

    def get_dispatch(self, date_from, date_to, top_n: int, source: str) -> Dict[str, Any]:
        """Packing material that left inside the finished goods dispatched.

        Bills over the period, the SKUs on them, each SKU's production recipe,
        and the packing material that recipe calls for -- summed per packing
        item and ranked.
        """
        master = index_master(self.reader.pm_master())
        bom = index_bom(self.reader.pm_bom_lines())

        if source == SOURCE_APP:
            rows = self.app_reader.dispatched_lines(date_from, date_to)
            counts = self.app_reader.dispatch_counts(date_from, date_to)
            groups = self.reader.item_group_map([row["item_code"] for row in rows])
            split = split_dispatch_lines(rows, groups)
            documents = counts["document_count"]
            gate_outs = counts["gate_out_count"]
        else:
            rows = self.reader.dispatched_lines(
                date_from, date_to, intercompany_card_codes(self.company_code)
            )
            split = split_dispatch_lines(rows)
            documents = self.reader.dispatch_document_count(date_from, date_to)
            gate_outs = None

        exploded = explode_dispatch(split["fg"], bom)
        ranked = rank_by_qty(exploded["quantities"], master, top_n)

        coverage = exploded["coverage"]
        # Packaging invoiced as itself rather than inside a finished good. It
        # left the factory, but not through a recipe, so it is reported here
        # and never added to the ranked list -- see constants.
        coverage["direct_pm_items"] = len(split["direct_pm"])
        coverage["direct_pm_qty"] = round(
            sum(float(row.get("qty", 0) or 0) for row in split["direct_pm"]), 3
        )
        coverage["other_item_count"] = split["other_item_count"]
        coverage["other_qty"] = split["other_qty"]

        ranked["coverage"] = coverage
        ranked["summary"] = summarise_dispatch(split["fg"])
        ranked["summary"]["document_count"] = documents
        ranked["summary"]["gate_out_count"] = gate_outs
        ranked["meta"] = {
            "company_code": self.company_code,
            "date_from": str(date_from),
            "date_to": str(date_to),
            "top_n": top_n,
            "ranked_by": RANKED_BY,
            "source": source,
            # 'invoiced' on SAP, 'gated-out' on FactoryFlow. A different fact,
            # named, because the two do not cover the same set of bills.
            "basis": DISPATCH_BASIS.get(source, ""),
            # Group-company bills are counted: the packaging physically left.
            # The summary carries the split so neither reading can be quoted
            # as the other. The FactoryFlow register cannot tell a group truck
            # from any other, so the split is SAP-only.
            "include_intercompany": True,
            "intercompany_known": source == SOURCE_SAP,
            "fetched_at": _now_iso(),
            **self._group_meta(),
        }
        return ranked

    # ------------------------------------------------------------------
    # Section three: the plan against what is left to buy
    # ------------------------------------------------------------------

    def get_plans(self, limit: int = PLAN_LIST_LIMIT) -> Dict[str, Any]:
        """The plan headers the board can be pointed at.

        Its own endpoint because the picker is filled once and the table
        reloads every time somebody changes plan; folding the list into the
        requirement response would re-read every OFCT header on each change.
        """
        plans = self.reader.plan_list(limit)
        today = date.today()
        default = resolve_plan(plans, today)
        return {
            "plans": [
                {
                    "abs_id": plan["abs_id"],
                    "code": plan["code"],
                    "name": plan["name"],
                    "start_date": (
                        _as_date(plan["start_date"]).isoformat()
                        if _as_date(plan["start_date"])
                        else None
                    ),
                    "end_date": (
                        _as_date(plan["end_date"]).isoformat()
                        if _as_date(plan["end_date"])
                        else None
                    ),
                    "form_view": plan["form_view"],
                    "item_count": plan["item_count"],
                    "planned_qty": plan["planned_qty"],
                }
                for plan in plans
            ],
            "meta": {
                "company_code": self.company_code,
                # Which one the board opens on, so the front end does not have
                # to re-implement the choice and reach a different answer.
                "default_abs_id": default["abs_id"] if default else None,
                "as_of": today.isoformat(),
                "fetched_at": _now_iso(),
            },
        }

    def get_requirement(self, abs_id: Optional[int] = None) -> Dict[str, Any]:
        """The plan, exploded through its BOMs, against stock and open orders.

        Six reads, one response, deliberately -- unlike the three panels above.
        Every column here is part of one row of arithmetic: `Req` cannot be
        computed without the plan AND the movements AND the stock, so there is
        no useful partial answer to stream, and splitting them would make the
        front end join what SQL already joined.

        Raises ``PlanNotFound`` when there is no plan to read, rather than
        returning an empty table that reads as "the plan needs no packaging".
        """
        today = date.today()
        plans = self.reader.plan_list(MAX_PLAN_LIST_LIMIT)
        if not plans:
            raise PlanNotFound("SAP holds no production plan for this company.")

        if abs_id is None:
            plan = resolve_plan(plans, today)
        else:
            plan = next((row for row in plans if row["abs_id"] == int(abs_id)), None)
            if plan is None:
                raise PlanNotFound(f"Production plan {abs_id} was not found in SAP.")
        if plan is None:
            raise PlanNotFound("SAP holds no production plan for this company.")

        window = issue_window(plan, today)
        issue_stores = consumption_warehouses(self.company_code)
        supply_stores = supply_warehouses(self.company_code)

        master = index_master(self.reader.pm_master())
        requirement = self.reader.plan_pm_requirement(plan["abs_id"])
        received = self.reader.pm_received(
            issue_stores, window["date_from"], window["date_to"]
        )
        on_hand = self.reader.pm_on_hand(supply_stores)
        open_po = self.reader.pm_open_po()
        driver_rows = self.reader.plan_pm_drivers(plan["abs_id"])
        coverage = self.reader.plan_coverage(plan["abs_id"])

        driver_counts: Dict[str, int] = {}
        for row in driver_rows:
            code = row.get("item_code")
            if code:
                driver_counts[code] = driver_counts.get(code, 0) + 1

        plan_end = _as_date(plan.get("end_date"))
        rows = build_requirement_rows(
            requirement,
            received,
            on_hand,
            open_po,
            master,
            index_drivers(driver_rows, MAX_LISTED_DRIVERS),
            driver_counts,
            plan_end,
            today,
        )

        return {
            "data": rows,
            "totals": requirement_totals(rows),
            "coverage": plan_coverage_summary(coverage, MAX_LISTED_ITEMS),
            "unplanned": unplanned_issue(
                received, [row["item_code"] for row in requirement], master, MAX_LISTED_ITEMS
            ),
            "plan": {
                "abs_id": plan["abs_id"],
                "code": plan["code"],
                "name": plan["name"],
                "start_date": (
                    _as_date(plan["start_date"]).isoformat()
                    if _as_date(plan["start_date"])
                    else None
                ),
                "end_date": plan_end.isoformat() if plan_end else None,
                "form_view": plan["form_view"],
                "item_count": plan["item_count"],
                "planned_qty": plan["planned_qty"],
            },
            "meta": {
                "company_code": self.company_code,
                # The window `Issue (PC)` counted, spelled out. "1st of the
                # month to today" is only true while the plan is the current
                # month, and a board read in October against September must
                # say which nine or thirty days it added up.
                "date_from": window["date_from"].isoformat(),
                "date_to": window["date_to"].isoformat(),
                "as_of": today.isoformat(),
                "issue_warehouses": issue_stores,
                "supply_warehouses": supply_stores,
                # What each figure IS, named on the response so no screen can
                # relabel it. `Issue (PC)` is RECEIPTS onto the floor, which is
                # not the same as the goods issue the production panel counts,
                # and the two will not agree.
                "basis": "plan-vs-receipts",
                "issue_basis": "received-into-consumption-store",
                # `Req` does not net off OITW.IsCommited -- the commitment on a
                # packing material is mostly this plan's own production orders,
                # so subtracting it would count the same demand twice. The
                # Planning & Purchase module nets it and starts from a
                # different figure; the two disagree by design.
                "nets_committed": False,
                "fetched_at": _now_iso(),
                **self._group_meta(),
            },
        }
