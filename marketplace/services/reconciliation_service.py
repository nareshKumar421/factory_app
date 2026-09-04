"""Per-order deviation reconciliation.

For each order (with a confirmed dispatch and/or a return) in the window, compares:
  * outward (scanned out) vs inward (scanned back)  → outward_vs_inward_deviation
  * shipped (ordered quantity of the parcels that actually went out) vs physical
    (net shipped = outward − inward)                → portal_vs_physical_deviation

The comparison is against SHIPPED, not against the whole order. An order ships a
box at a time, so a parcel still on the floor is not a discrepancy — it has simply
not gone yet. Measuring the whole order against what moved reported a deviation
for every in-flight partial shipment, which is noise that trains people to stop
reading the report. ``portal_quantity`` still carries the full ordered figure and
``pending_quantity`` names the difference, so nothing is hidden — it is just no
longer counted as a deviation.
"""
from decimal import Decimal

from ..models import (
    MarketplaceDispatch,
    MarketplaceDispatchStatus,
    MarketplaceOrder,
    MarketplaceReturn,
)
from .resolve_service import (
    RESOLVE_PREFETCH,
    fg_lines,
    load_mappings,
    resolve_lines,
    resolve_order,
)
from .scan_service import shipped_lines


def _u(code):
    return (code or "").strip().upper()


def _accumulate(scans, bucket):
    # Iterate the PREFETCHED scans and filter is_active in Python — calling
    # ``.filter(is_active=True)`` here would issue a fresh query per order and
    # defeat the ``dispatches__scans`` / ``returns__scans`` prefetch.
    for s in scans:
        if not getattr(s, "is_active", True):
            continue
        k = _u(s.item_code)
        bucket[k] = bucket.get(k, Decimal("0")) + Decimal(s.quantity)


def build_report(company, *, channel=None, from_date=None, to_date=None, order_id=None):
    orders = MarketplaceOrder.objects.filter(company=company)
    if channel:
        orders = orders.filter(channel=channel)
    if order_id:
        orders = orders.filter(order_id=order_id)
    if from_date:
        orders = orders.filter(created_at__date__gte=from_date)
    if to_date:
        orders = orders.filter(created_at__date__lte=to_date)
    orders = orders.prefetch_related(
        *RESOLVE_PREFETCH, "dispatches__scans", "returns__scans")

    # Load SKU mappings once per channel (not once per order) — reused across the
    # whole report.
    mappings_cache = {}

    rows = []
    orders_with_deviation = 0
    for order in orders:
        confirmed = [
            d for d in order.dispatches.all()
            if d.status == MarketplaceDispatchStatus.CONFIRMED
        ]
        if not confirmed and not order.returns.all():
            continue

        mappings = mappings_cache.get(order.channel)
        if mappings is None:
            mappings = mappings_cache[order.channel] = load_mappings(company, order.channel)
        resolved = resolve_order(order, mappings)
        portal = {_u(l["item_code"]): Decimal(l["required_quantity"]) for l in fg_lines(resolved["resolved_lines"])}
        names = {_u(l["item_code"]): l["item_name"] for l in fg_lines(resolved["resolved_lines"])}

        # The ordered quantity of the parcels that actually LEFT. Deduped by line:
        # a line re-scanned onto a second dispatch is one parcel, not two, and
        # double-counting it would invent the opposite deviation.
        seen, went_out = set(), []
        for d in confirmed:
            for l in shipped_lines(d):
                if l.pk not in seen:
                    seen.add(l.pk)
                    went_out.append(l)
        shipped_fg = fg_lines(
            resolve_lines(went_out, order.sap_warehouse_code or "", mappings)["resolved_lines"]
        )
        shipped = {_u(l["item_code"]): Decimal(l["required_quantity"]) for l in shipped_fg}
        for l in shipped_fg:
            names.setdefault(_u(l["item_code"]), l["item_name"])

        outward, inward = {}, {}
        for d in confirmed:
            _accumulate(d.scans.all(), outward)
        for r in order.returns.all():
            _accumulate(r.scans.all(), inward)

        order_has_dev = False
        for item in sorted(set(portal) | set(shipped) | set(outward) | set(inward)):
            p = portal.get(item, Decimal("0"))
            sh = shipped.get(item, Decimal("0"))
            out = outward.get(item, Decimal("0"))
            inw = inward.get(item, Decimal("0"))
            physical = out - inw
            # Keeps its published meaning: net unreturned goods (out − in), which for
            # an ordinary shipment equals what went out.
            ovi = out - inw
            # Against what went out, not against the whole order.
            pvp = sh - physical
            # ``ovi`` is NOT a deviation by itself. It is the same expression as
            # ``physical``, so testing ``ovi != 0`` marked every shipped-and-not-
            # returned order as deviating — which is every normal order, and is why
            # orders_with_deviation counted essentially the whole book. What is
            # genuinely wrong is MORE coming back than ever went out.
            has_dev = inw > out or pvp != 0
            order_has_dev = order_has_dev or has_dev
            rows.append({
                "order_id": order.order_id,
                "channel": order.channel,
                "item_code": item,
                "item_name": names.get(item, ""),
                "portal_quantity": str(p),
                "shipped_quantity": str(sh),
                # What the order still owes: ordered but not yet on any dispatch.
                # Informational — deliberately NOT a deviation.
                "pending_quantity": str(p - sh),
                "outward_quantity": str(out),
                "inward_quantity": str(inw),
                "physical_quantity": str(physical),
                "outward_vs_inward_deviation": str(ovi),
                "portal_vs_physical_deviation": str(pvp),
                "has_deviation": has_dev,
            })
        if order_has_dev:
            orders_with_deviation += 1

    return {
        "channel": channel or "",
        "from_date": from_date.isoformat() if from_date else None,
        "to_date": to_date.isoformat() if to_date else None,
        "rows": rows,
        "total_orders": len({r["order_id"] for r in rows}),
        "orders_with_deviation": orders_with_deviation,
    }
