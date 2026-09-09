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
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence

from sap_client.context import CompanyContext

from .app_reader import PackingMaterialAppReader
from .constants import (
    DISPATCH_BASIS,
    FG_ITEM_GROUP,
    PM_ITEM_GROUP,
    PM_ITEM_GROUP_NAME,
    RANKED_BY,
    SOURCE_APP,
    SOURCE_SAP,
    consumption_warehouses,
    intercompany_card_codes,
    stock_warehouses,
)
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
