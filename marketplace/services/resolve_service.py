"""Resolve a marketplace order into scan-ready component lines.

Expands each order line's SKU via the JI masters (SkuMapping / ComboDefinition):
  * RAW SKU   → one FG line (qty = ordered_quantity)
  * COMBO SKU → one line per component (FG or PM), qty = ordered_quantity × component.quantity

Lines are aggregated by (item_code, component_type). SKUs with no active mapping are
returned in ``unmapped_skus`` and must block dispatch confirm.
"""
from collections import OrderedDict
from decimal import Decimal

from ..models import ComboComponentType, SkuMapping, SkuType


def _key(item_code, component_type):
    return (item_code.strip().upper(), component_type)


def resolve_order(order):
    """Return ``{"resolved_lines": [...], "unmapped_skus": [...]}`` for an order.

    Each resolved line: item_code, item_name, component_type, required_quantity (Decimal),
    uom, warehouse_code, source_skus (list).
    """
    company = order.company
    channel = order.channel
    warehouse_code = order.sap_warehouse_code or ""

    mappings = {
        m.marketplace_sku.strip().upper(): m
        for m in SkuMapping.objects.filter(
            company=company, channel=channel, is_active=True
        ).select_related("combo").prefetch_related("combo__components")
    }

    agg = OrderedDict()  # key -> resolved line dict
    unmapped = []

    def add(item_code, item_name, component_type, qty, uom, source_sku):
        if not item_code:
            return
        k = _key(item_code, component_type)
        if k not in agg:
            agg[k] = {
                "item_code": item_code,
                "item_name": item_name or "",
                "component_type": component_type,
                "required_quantity": Decimal("0"),
                "uom": uom or "",
                "warehouse_code": warehouse_code,
                "source_skus": [],
            }
        line = agg[k]
        line["required_quantity"] += Decimal(qty)
        if source_sku and source_sku not in line["source_skus"]:
            line["source_skus"].append(source_sku)
        if not line["item_name"] and item_name:
            line["item_name"] = item_name
        if not line["uom"] and uom:
            line["uom"] = uom

    for line in order.lines.all():
        sku = line.marketplace_sku.strip().upper()
        ordered = Decimal(line.ordered_quantity)
        mapping = mappings.get(sku)
        if mapping is None:
            if line.marketplace_sku not in unmapped:
                unmapped.append(line.marketplace_sku)
            continue

        if mapping.sku_type == SkuType.COMBO and mapping.combo_id:
            for comp in mapping.combo.components.all():
                add(
                    comp.item_code, comp.item_name, comp.component_type,
                    ordered * Decimal(comp.quantity), comp.uom, line.marketplace_sku,
                )
        else:  # RAW
            add(
                mapping.fg_item_code, mapping.fg_item_name or line.sku_name,
                ComboComponentType.FG, ordered,
                mapping.default_uom, line.marketplace_sku,
            )

    return {"resolved_lines": list(agg.values()), "unmapped_skus": unmapped}


def fg_lines(resolved_lines):
    return [l for l in resolved_lines if l["component_type"] == ComboComponentType.FG]


def pm_lines(resolved_lines):
    return [l for l in resolved_lines if l["component_type"] == ComboComponentType.PM]
