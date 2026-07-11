"""Fold a whole import batch into ONE consolidated stock list.

Runs the existing per-order combo explosion (:func:`resolve_service.resolve_order`)
across every order in a batch and aggregates the resolved component lines by
``(item_code, component_type)``. This is the single FG/PM list the warehouse is
asked to issue, and what dispatch later scans against. See
``MARKETPLACE_FLIPKART_SHEET_FLOW.md`` §5.2.
"""
from collections import OrderedDict
from decimal import Decimal

from ..models import ComboComponentType, MarketplaceOrderStatus
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
        if not include_cancelled and (
            order.is_cancelled or order.status == MarketplaceOrderStatus.RETURNED
        ):
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
    return {"lines": lines, "unmapped_skus": unmapped, "orders": counted}


def fg_count(stock_list):
    return sum(1 for l in stock_list["lines"] if l["component_type"] == ComboComponentType.FG)
