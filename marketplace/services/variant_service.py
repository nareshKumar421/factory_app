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
    return mappings.lookup(line.fsn, line.marketplace_sku)


def _option_view(o):
    return {
        "id": o.id,
        "label": o.label or o.fg_item_code or (o.combo.code if o.combo_id else ""),
        "sku_type": o.sku_type,
        "fg_item_code": o.fg_item_code,
        "combo_code": o.combo.code if o.combo_id else "",
        "is_default": o.is_default,
    }


def _component_views(line, mapping):
    """Per-combo-component alternatives for a line: ``[{component_id, label,
    options[], chosen_option_id, has_choice}]``. Empty unless the line resolves to
    a combo whose components carry alternatives."""
    if mapping is None:
        return []
    opt = effective_option(line, mapping)
    combo = opt.combo if opt is not None else mapping.combo
    is_combo = (opt.sku_type if opt is not None else mapping.sku_type) == "COMBO"
    if not (is_combo and combo is not None):
        return []
    picks = getattr(line, "component_choices", None) or {}
    out = []
    for comp in combo.components.all():
        opts = list(comp.options.all())  # default-first
        if not opts:
            continue
        picked = picks.get(str(comp.id))
        chosen = next((o for o in opts if o.id == picked), opts[0])
        out.append({
            "component_id": comp.id,
            "label": comp.item_name or comp.item_code,
            "quantity": str(comp.quantity),
            "has_choice": len(opts) > 1,
            "options": [
                {"id": o.id, "item_code": o.item_code, "item_name": o.item_name,
                 "quantity": str(o.quantity if o.quantity is not None else comp.quantity),
                 "is_default": o.is_default}
                for o in opts
            ],
            "chosen_option_id": chosen.id,
        })
    return out


def ships_as(line, mapping):
    """The SAP item(s) this line actually ships as, resolved right now.

    ``options`` only says what the operator may CHOOSE between, and most SKUs map
    to a single item and so carry none. The delivery-note screen still has to show
    what every order will ship, not just the ones with a choice, so resolve the
    line the same way the delivery note will: one entry per finished good, with
    the piece count.
    """
    from .resolve_service import MappingIndex, fg_lines, resolve_lines

    if mapping is None:
        return []
    # A one-entry index keyed on this line, so resolve_lines finds THIS mapping
    # without going back to the database.
    index = MappingIndex.for_line(line, mapping)
    resolved = resolve_lines([line], "", index)["resolved_lines"]
    return [
        {
            "item_code": r["item_code"],
            "item_name": r["item_name"],
            "quantity": str(r["required_quantity"]),
        }
        for r in fg_lines(resolved)
    ]


def line_variants(line, mapping):
    """``{line_id, sku_name, fsn, options[], chosen_option_id, has_choice}`` for a
    line, plus ``components`` for combo lines whose slots have alternatives.
    ``has_choice`` is True when the SKU itself, or any of its combo components,
    maps to more than one SAP item."""
    opts = list(mapping.options.all()) if mapping else []  # default-first ordering
    chosen = effective_option(line, mapping) if mapping else None
    components = _component_views(line, mapping)
    return {
        "line_id": line.id,
        "sku_name": line.sku_name or line.marketplace_sku,
        "fsn": line.fsn,
        "marketplace_sku": line.marketplace_sku,
        "has_choice": len(opts) > 1 or any(c["has_choice"] for c in components),
        "options": [_option_view(o) for o in opts],
        "chosen_option_id": chosen.id if chosen is not None else None,
        "components": components,
        "ships_as": ships_as(line, mapping),
    }


def order_variants(order, mappings=None, choosable_only=False):
    """Variant choices for every line of an order (or only lines with a real
    choice when ``choosable_only``)."""
    if mappings is None:
        mappings = load_mappings(order.company, order.channel)
    # ``order.lines.all()`` so a caller that prefetched lines (e.g. the delivery-note
    # summary over hundreds of orders) doesn't pay a query per order.
    lines = order.lines.all()
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

    opt = (
        SkuMappingOption.objects.select_related("mapping")
        .filter(id=option_id, mapping__company=company).first()
    )
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


def set_component_option(company, *, line_id, component_id, option_id, user=None):
    """Record (or clear) which SAP item fills one combo component for this order line.

    ``option_id`` None clears the pick (reverts to the component's default).
    Rejects an option that doesn't belong to the given component.
    """
    from ..models import ComboComponentOption

    line = (
        MarketplaceOrderLine.objects.select_related("order")
        .filter(id=line_id, order__company=company).first()
    )
    if line is None:
        raise MarketplaceError("Order line not found.", code="NOT_FOUND", status_code=404)

    choices = dict(line.component_choices or {})
    key = str(int(component_id))
    if not option_id:
        choices.pop(key, None)
    else:
        opt = ComboComponentOption.objects.filter(
            id=option_id, component__combo__company=company
        ).first()
        if opt is None:
            raise MarketplaceError("Variant not found.", code="NOT_FOUND", status_code=404)
        if str(opt.component_id) != key:
            raise MarketplaceError(
                "That variant does not belong to this combo component.",
                code="BAD_OPTION", status_code=400,
            )
        choices[key] = opt.id
    line.component_choices = choices
    line.save(update_fields=["component_choices"])
    return line
