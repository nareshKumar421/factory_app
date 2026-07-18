"""Per-order SAP-item variant choice.

One Flipkart product (FSN) can ship as several SAP items — see
:class:`SkuMappingOption`. This service lists the choices for an order's lines and
records the operator's pick on each line's ``chosen_option``. The pick is made at
sheet processing and/or delivery-note cut time; resolution (``resolve_service``)
then ships the chosen item.
"""
from ..models import MarketplaceOrderLine, SkuMappingOption
from .errors import MarketplaceError
from .resolve_service import effective_option, load_mappings


def mapping_for_line(line, mappings):
    """The active mapping for an order line — matched by FSN first, then SKU."""
    fsn = (line.fsn or "").strip().upper()
    sku = (line.marketplace_sku or "").strip().upper()
    return (mappings.get(fsn) if fsn else None) or mappings.get(sku)


def _option_view(o):
    return {
        "id": o.id,
        "label": o.label or o.fg_item_code or (o.combo.code if o.combo_id else ""),
        "sku_type": o.sku_type,
        "fg_item_code": o.fg_item_code,
        "combo_code": o.combo.code if o.combo_id else "",
        "is_default": o.is_default,
    }


def line_variants(line, mapping):
    """``{line_id, sku_name, fsn, options[], chosen_option_id, has_choice}`` for a
    line. ``has_choice`` is True only when its FSN maps to more than one SAP item."""
    opts = list(mapping.options.all()) if mapping else []  # default-first ordering
    chosen = effective_option(line, mapping) if mapping else None
    return {
        "line_id": line.id,
        "sku_name": line.sku_name or line.marketplace_sku,
        "fsn": line.fsn,
        "marketplace_sku": line.marketplace_sku,
        "has_choice": len(opts) > 1,
        "options": [_option_view(o) for o in opts],
        "chosen_option_id": chosen.id if chosen is not None else None,
    }


def order_variants(order, mappings=None, choosable_only=False):
    """Variant choices for every line of an order (or only lines with a real
    choice when ``choosable_only``)."""
    if mappings is None:
        mappings = load_mappings(order.company, order.channel)
    lines = order.lines.select_related("chosen_option").all()
    out = []
    for l in lines:
        v = line_variants(l, mapping_for_line(l, mappings))
        if choosable_only and not v["has_choice"]:
            continue
        out.append(v)
    return out


def set_line_option(company, *, line_id, option_id, user=None):
    """Record (or clear) the operator's SAP-item pick for one order line.

    ``option_id`` None/0 clears the pick (reverts to the mapping default). Rejects
    an option that doesn't belong to this line's FSN mapping.
    """
    line = (
        MarketplaceOrderLine.objects.select_related("order")
        .filter(id=line_id, order__company=company).first()
    )
    if line is None:
        raise MarketplaceError("Order line not found.", code="NOT_FOUND", status_code=404)

    if not option_id:
        line.chosen_option = None
        line.save(update_fields=["chosen_option"])
        return line

    opt = SkuMappingOption.objects.select_related("mapping").filter(id=option_id).first()
    if opt is None:
        raise MarketplaceError("Variant not found.", code="NOT_FOUND", status_code=404)

    mappings = load_mappings(company, line.order.channel)
    mapping = mapping_for_line(line, mappings)
    if mapping is None or opt.mapping_id != mapping.id:
        raise MarketplaceError(
            "That variant does not belong to this product.",
            code="BAD_OPTION", status_code=400,
        )
    line.chosen_option = opt
    line.save(update_fields=["chosen_option"])
    return line
