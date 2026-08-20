"""Fold a whole import batch into ONE consolidated stock list.

Runs the existing per-order combo explosion (:func:`resolve_service.resolve_order`)
across every order in a batch and aggregates the resolved component lines by
``(item_code, component_type)``. This is the single FG/PM list the warehouse is
asked to issue, and what dispatch later scans against. See
``MARKETPLACE_FLIPKART_SHEET_FLOW.md`` §5.2.
"""
from collections import OrderedDict
from decimal import Decimal

from django.db import transaction

from ..models import ComboComponentType, MarketplaceOrderStatus
from .item_names import fill_missing_names
from .resolve_service import load_mappings, resolve_order


def build_stock_list(batch, *, include_cancelled=False):
    """Return ``{"lines": [...], "unmapped_skus": [...], "orders": N}``.

    Each line: item_code, item_name, component_type, uom, required_quantity (Decimal),
    source_skus (list). Cancelled orders are excluded by default.

    Mappings are loaded once and reused across every order (a couple of queries for
    the whole batch), so this stays fast on a large sheet + remote database.
    """
    agg = OrderedDict()
    unmapped = []
    orders = batch.orders.all().prefetch_related("lines")
    mappings = load_mappings(batch.company, batch.channel)
    counted = 0

    for order in orders:
        if not include_cancelled and order.is_cancelled:
            continue
        counted += 1
        resolved = resolve_order(order, mappings)
        for sku in resolved["unmapped_skus"]:
            if sku not in unmapped:
                unmapped.append(sku)
        for line in resolved["resolved_lines"]:
            key = (line["item_code"].strip().upper(), line["component_type"])
            if key not in agg:
                agg[key] = {
                    "item_code": line["item_code"],
                    "item_name": line["item_name"],
                    "component_type": line["component_type"],
                    "uom": line["uom"],
                    "required_quantity": Decimal("0"),
                    "source_skus": [],
                }
            row = agg[key]
            row["required_quantity"] += line["required_quantity"]
            for s in line["source_skus"]:
                if s not in row["source_skus"]:
                    row["source_skus"].append(s)
            if not row["item_name"] and line["item_name"]:
                row["item_name"] = line["item_name"]
            if not row["uom"] and line["uom"]:
                row["uom"] = line["uom"]

    lines = list(agg.values())
    lines.sort(key=lambda r: (r["component_type"], r["item_code"]))
    # A master saved while the SAP item master was unreachable carries no name, and
    # the warehouse is then asked to pick against a bare item code. Fall back to a
    # name the database has already recorded for that code before giving up on it.
    fill_missing_names(lines)
    return {"lines": lines, "unmapped_skus": unmapped, "orders": counted}


def orders_with_unmapped_skus(batch):
    """List the batch's orders that still have at least one unmapped SKU/FSN.

    Returns ``[{order_id, buyer_name, unmapped_skus, has_dispatch}]``. ``has_dispatch``
    flags orders that are already in the dispatch flow — those cannot be removed.
    """
    mappings = load_mappings(batch.company, batch.channel)
    out = []
    for order in batch.orders.all().prefetch_related("lines", "dispatches"):
        if order.is_cancelled:
            continue
        resolved = resolve_order(order, mappings)
        if not resolved["unmapped_skus"]:
            continue
        out.append({
            "order_id": order.order_id,
            "buyer_name": order.buyer_name,
            "unmapped_skus": resolved["unmapped_skus"],
            "has_dispatch": order.dispatches.exists(),
        })
    return out


def skip_unmapped_orders(batch, *, user=None):
    """Remove the batch's orders that still have an unmapped SKU/FSN.

    Lets the rest of the batch proceed when some SKUs can't (or shouldn't) be
    mapped. Orders already in the dispatch flow are kept and reported as
    ``blocked`` (their ``order`` FK is PROTECTed, so they can't be deleted).
    Deleting an order cascades to its lines. Returns a summary dict.
    """
    candidates = orders_with_unmapped_skus(batch)
    removable = [c for c in candidates if not c["has_dispatch"]]
    blocked = [c for c in candidates if c["has_dispatch"]]

    removed_ids = [c["order_id"] for c in removable]
    with transaction.atomic():
        batch.orders.filter(order_id__in=removed_ids).delete()

    stock = build_stock_list(batch)
    return {
        "removed_count": len(removed_ids),
        "removed_order_ids": removed_ids,
        "blocked_order_ids": [c["order_id"] for c in blocked],
        "remaining_unmapped_skus": stock["unmapped_skus"],
    }


def fg_count(stock_list):
    return sum(1 for l in stock_list["lines"] if l["component_type"] == ComboComponentType.FG)
