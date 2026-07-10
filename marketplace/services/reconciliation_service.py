"""Per-order deviation reconciliation.

For each order (with a confirmed dispatch and/or a return) in the window, compares:
  * outward (scanned out) vs inward (scanned back)  → outward_vs_inward_deviation
  * portal (ordered per marketplace) vs physical (net shipped = outward − inward)
    → portal_vs_physical_deviation
"""
from decimal import Decimal

from ..models import (
    MarketplaceDispatch,
    MarketplaceDispatchStatus,
    MarketplaceOrder,
    MarketplaceReturn,
)
from .resolve_service import fg_lines, resolve_order


def _u(code):
    return (code or "").strip().upper()


def _accumulate(scan_qs, bucket):
    for item_code, qty in scan_qs.values_list("item_code", "quantity"):
        k = _u(item_code)
        bucket[k] = bucket.get(k, Decimal("0")) + Decimal(qty)


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
    orders = orders.prefetch_related("lines", "dispatches__scans", "returns__scans")

    rows = []
    orders_with_deviation = 0
    for order in orders:
        confirmed = [
            d for d in order.dispatches.all()
            if d.status == MarketplaceDispatchStatus.CONFIRMED
        ]
        if not confirmed and not order.returns.all():
            continue

        resolved = resolve_order(order)
        portal = {_u(l["item_code"]): Decimal(l["required_quantity"]) for l in fg_lines(resolved["resolved_lines"])}
        names = {_u(l["item_code"]): l["item_name"] for l in fg_lines(resolved["resolved_lines"])}

        outward, inward = {}, {}
        for d in confirmed:
            _accumulate(d.scans.filter(is_active=True), outward)
        for r in order.returns.all():
            _accumulate(r.scans.filter(is_active=True), inward)

        order_has_dev = False
        for item in sorted(set(portal) | set(outward) | set(inward)):
            p = portal.get(item, Decimal("0"))
            out = outward.get(item, Decimal("0"))
            inw = inward.get(item, Decimal("0"))
            physical = out - inw
            ovi = out - inw
            pvp = p - physical
            has_dev = ovi != 0 or pvp != 0
            order_has_dev = order_has_dev or has_dev
            rows.append({
                "order_id": order.order_id,
                "channel": order.channel,
                "item_code": item,
                "item_name": names.get(item, ""),
                "portal_quantity": str(p),
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
