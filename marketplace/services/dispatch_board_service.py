"""Sheet-wise Outward dispatch board.

The operator picks a *sheet* (an :class:`OrderImportBatch` — one uploaded Flipkart
order CSV) and scans its shipments. This service powers that view:

  * :func:`list_sheets` — every sheet with dispatchable orders + a live scan summary
    (orders total / completed / pending, tracking IDs scanned / remaining, progress).
  * :func:`sheet_board` — for one sheet: the same insights plus every order with its
    per-item tracking IDs and each item's scanned state, so the Order Card can show
    which orders (and which tracking IDs) are done vs pending.

A Tracking ID identifies one order ITEM (a multi-item order carries several). An
item is "scanned" when its tracking ID appears in its dispatch's active scans
(``barcode_raw`` is ``"{tracking}#{item_code}"`` — the prefix is the tracking ID).
"""
from ..models import (
    MarketplaceDispatch,
    MarketplaceDispatchStatus,
    MarketplaceOrder,
    OrderImportBatch,
)
from .dispatch_gate import order_dispatch_ready


def _scanned_prefixes(dispatch):
    """Tracking IDs already scanned on a dispatch (the barcode prefix before '#')."""
    if dispatch is None:
        return set()
    return {
        (s.barcode_raw or "").split("#", 1)[0]
        for s in dispatch.scans.all()
        if s.is_active
    }


def _order_items(order):
    """One entry per order line: ``{sku_name, quantity, tracking_id}``.

    Falls back to the order-level tracking ID for lines with none (legacy/single
    item data)."""
    items = []
    for l in order.lines.all():
        tid = (l.tracking_id or "").strip() or (order.tracking_id or "").strip()
        items.append({
            "sku_name": l.sku_name or l.marketplace_sku,
            "marketplace_sku": l.marketplace_sku,
            "quantity": str(l.ordered_quantity),
            "tracking_id": tid,
        })
    return items


def _order_view(order, dispatch, mappings=None):
    """Serialize one order for the board with per-item scan state + status.

    When ``mappings`` is given, each order also carries the SAP-item variant choices
    for any line whose FSN maps to more than one item (``variants``)."""
    scanned = _scanned_prefixes(dispatch)
    items = _order_items(order)
    trackings = [t for t in {i["tracking_id"] for i in items if i["tracking_id"]}]
    tracking_total = len(trackings)
    tracking_scanned = sum(1 for t in trackings if t in scanned)

    confirmed = dispatch is not None and dispatch.status == MarketplaceDispatchStatus.CONFIRMED
    fully = tracking_total > 0 and tracking_scanned == tracking_total
    ready = order_dispatch_ready(order)
    if confirmed:
        status = "CONFIRMED"
    elif fully or (dispatch is not None and dispatch.status == MarketplaceDispatchStatus.READY):
        status = "SCANNED"
    elif tracking_scanned > 0:
        status = "PARTIAL"
    else:
        status = "PENDING"

    variants = []
    if mappings is not None:
        from .variant_service import order_variants
        variants = order_variants(order, mappings, choosable_only=True)

    return {
        "order_id": order.order_id,
        "buyer_name": order.buyer_name,
        "order_date": order.order_date.isoformat() if order.order_date else None,
        "dispatch_id": dispatch.id if dispatch else None,
        "dispatch_status": dispatch.status if dispatch else None,
        "sap_post_status": dispatch.sap_post_status if dispatch else None,
        "ready": ready,
        "status": status,
        "tracking_total": tracking_total,
        "tracking_scanned": tracking_scanned,
        "items": [
            {**i, "scanned": bool(i["tracking_id"]) and i["tracking_id"] in scanned}
            for i in items
        ],
        "variants": variants,
    }


def _dispatch_map(company, orders):
    """``{order_id(pk): latest non-cancelled dispatch}`` for the given orders,
    with scans prefetched."""
    dispatches = (
        MarketplaceDispatch.objects.filter(company=company, order__in=orders)
        .exclude(status=MarketplaceDispatchStatus.CANCELLED)
        .prefetch_related("scans")
        .order_by("order_id", "-created_at")
    )
    out = {}
    for d in dispatches:
        out.setdefault(d.order_id, d)  # first per order = latest (created desc)
    return out


def _insights(order_views):
    """Aggregate a list of order views into the sheet-level summary."""
    total = len(order_views)
    completed = sum(1 for o in order_views if o["status"] in ("SCANNED", "CONFIRMED"))
    confirmed = sum(1 for o in order_views if o["status"] == "CONFIRMED")
    tracking_total = sum(o["tracking_total"] for o in order_views)
    tracking_scanned = sum(o["tracking_scanned"] for o in order_views)
    return {
        "total_orders": total,
        "completed_orders": completed,
        "pending_orders": total - completed,
        "confirmed_orders": confirmed,
        "tracking_total": tracking_total,
        "tracking_scanned": tracking_scanned,
        "tracking_remaining": tracking_total - tracking_scanned,
        "progress_pct": round(tracking_scanned * 100 / tracking_total) if tracking_total else 0,
    }


def _sheet_orders(company, channel, batch):
    return list(
        MarketplaceOrder.objects.filter(
            company=company, channel=channel, import_batch=batch, is_cancelled=False
        )
        .prefetch_related("lines", "lines__chosen_option")
        .order_by("order_id")
    )


def sheet_board(company, channel, batch_id):
    """Full board for one sheet: insights + every order with per-item tracking state."""
    from .errors import MarketplaceError

    batch = (
        OrderImportBatch.objects.filter(company=company, channel=channel, id=batch_id).first()
    )
    if batch is None:
        raise MarketplaceError("Sheet not found.", code="NOT_FOUND", status_code=404)
    from .resolve_service import load_mappings
    orders = _sheet_orders(company, channel, batch)
    dmap = _dispatch_map(company, orders)
    mappings = load_mappings(company, channel)
    order_views = [_order_view(o, dmap.get(o.id), mappings) for o in orders]
    return {
        "sheet": {
            "id": batch.id,
            "filename": batch.filename,
            "status": batch.status,
            "created_at": batch.created_at.isoformat(),
        },
        "insights": _insights(order_views),
        "orders": order_views,
    }


def list_sheets(company, channel):
    """Every sheet that has (non-cancelled) orders, newest first, each with its
    live dispatch insights so the operator can pick one and see progress at a glance."""
    batches = list(
        OrderImportBatch.objects.filter(company=company, channel=channel)
        .order_by("-created_at")
    )
    if not batches:
        return {"sheets": []}

    # One pass over all orders in these sheets, grouped by batch.
    orders = list(
        MarketplaceOrder.objects.filter(
            company=company, channel=channel, import_batch__in=batches, is_cancelled=False
        )
        .prefetch_related("lines")
        .order_by("order_id")
    )
    dmap = _dispatch_map(company, orders)
    by_batch = {}
    for o in orders:
        by_batch.setdefault(o.import_batch_id, []).append(_order_view(o, dmap.get(o.id)))

    sheets = []
    for b in batches:
        views = by_batch.get(b.id, [])
        if not views:
            continue  # skip empty sheets (nothing to dispatch)
        sheets.append({
            "id": b.id,
            "filename": b.filename,
            "status": b.status,
            "created_at": b.created_at.isoformat(),
            "insights": _insights(views),
        })
    return {"sheets": sheets}
