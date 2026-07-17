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


def load_mappings(company, channel):
    """Active mappings for a channel, indexed by BOTH FSN and marketplace SKU
    (upper-cased). FSN is the primary key; SKU is the fallback for rows with no FSN.

    Load once and reuse across many orders (e.g. a whole import batch) to avoid a
    per-order query — see ``batch_resolve_service``.
    """
    index = {}
    for m in (
        SkuMapping.objects.filter(company=company, channel=channel, is_active=True)
        .select_related("combo").prefetch_related("combo__components")
    ):
        if m.marketplace_sku:
            index.setdefault(m.marketplace_sku.strip().upper(), m)
        if m.fsn:
            index[m.fsn.strip().upper()] = m  # FSN entry wins its own key
    return index


def resolve_order(order, mappings=None):
    """Return ``{"resolved_lines": [...], "unmapped_skus": [...]}`` for an order.

    Pass a pre-built ``mappings`` dict (from :func:`load_mappings`) to skip the
    lookup query when resolving many orders. Each resolved line: item_code,
    item_name, component_type, required_quantity (Decimal), uom, warehouse_code,
    source_skus (list).
    """
    if mappings is None:
        mappings = load_mappings(order.company, order.channel)
    return resolve_lines(order.lines.all(), order.sap_warehouse_code or "", mappings)


def resolve_lines(lines, warehouse_code, mappings):
    """Resolve an arbitrary set of order lines into aggregated FG/PM component
    lines. Used for a whole order (:func:`resolve_order`) and for the single item
    behind one scanned tracking ID (see ``scan_service``)."""
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

    for line in lines:
        ordered = Decimal(line.ordered_quantity)
        # Match by FSN first (primary key), then fall back to marketplace SKU.
        fsn = (line.fsn or "").strip().upper()
        sku = (line.marketplace_sku or "").strip().upper()
        mapping = (mappings.get(fsn) if fsn else None) or mappings.get(sku)
        if mapping is None:
            key = line.fsn or line.marketplace_sku  # report the FSN it should map on
            if key and key not in unmapped:
                unmapped.append(key)
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
