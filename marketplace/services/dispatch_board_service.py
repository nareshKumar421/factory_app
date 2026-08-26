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
from django.db.models import Prefetch

from ..models import (
    MarketplaceDispatch,
    MarketplaceDispatchStatus,
    MarketplaceImportSkip,
    MarketplaceOrder,
    MarketplacePacking,
    MarketplacePackingStatus,
    MarketplaceScan,
    OrderImportBatch,
)
from .dispatch_gate import order_dispatch_ready
from .scan_service import confirmed_trackings


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
            "order_item_id": l.order_item_id or "",
            "fsn": l.fsn or "",
            "hsn": l.hsn_code or "",
            "unit_price": str(l.unit_price),
            "invoice_amount": str(l.invoice_amount),
            "tax_amount": str(l.tax_amount),
            "order_state": l.order_state or "",
        })
    return items


def _scan_detail(dispatch):
    """``{tracking prefix: {"scanned_at", "scanned_by"}}`` for a dispatch's active
    scans — so each item can show WHEN and by WHOM its tracking ID was scanned."""
    if dispatch is None:
        return {}
    out = {}
    for s in dispatch.scans.all():
        if not s.is_active:
            continue
        prefix = (s.barcode_raw or "").split("#", 1)[0]
        if prefix and prefix not in out:
            out[prefix] = {
                "scanned_at": s.scanned_at.isoformat() if s.scanned_at else None,
                "scanned_by": (s.scanned_by.full_name if s.scanned_by_id else ""),
            }
    return out


def _ready_map(company, channel, orders):
    """``{order_id(pk): dispatch_ready_bool}`` for a batch of orders in 1–2 queries.

    Hoists what ``order_dispatch_ready`` does per order (a skip-packing lookup + a
    per-order PACKED check) into two set-based queries, so the board doesn't fire
    ~2 queries per order (crippling over a remote DB — see list_sheets).
    """
    from .settings_service import is_skip_packing

    if is_skip_packing(company, channel):
        return {o.id: not o.is_cancelled for o in orders}
    # Packing required: an order is ready when PACKED, or when it's not from a sheet.
    sheet_order_ids = [o.id for o in orders if o.import_batch_id]
    packed = set(
        MarketplacePacking.objects.filter(
            order_id__in=sheet_order_ids, status=MarketplacePackingStatus.PACKED
        ).values_list("order_id", flat=True)
    )
    return {o.id: (True if not o.import_batch_id else o.id in packed) for o in orders}


def _order_view(order, dispatch, mappings=None, ready=None, cancelled_dispatch=None):
    """Serialize one order for the board with per-item scan state + status.

    When ``mappings`` is given, each order also carries the SAP-item variant choices
    for any line whose FSN maps to more than one item (``variants``). ``ready`` may be
    passed pre-computed (see :func:`_ready_map`) to avoid a per-order query.

    ``cancelled_dispatch`` is the order's latest CANCELLED dispatch (see
    :func:`_cancelled_map`). An order that was scanned and then cancelled at pickup
    has no active dispatch but a cancelled one — it shows as its own ``CANCELLED``
    status (the "cancel after scan" section) with its scan data preserved, and is
    excluded from the delivery-note flow."""
    # A cancelled dispatch keeps the order's scan data but takes it out of the DN
    # flow. Show CANCELLED only when there is no ACTIVE dispatch (a re-scan makes a
    # fresh dispatch, which wins).
    dispatches = list(dispatch or []) if isinstance(dispatch, (list, tuple)) else (
        [dispatch] if dispatch is not None else []
    )
    dispatch = dispatches[0] if dispatches else None
    cancelled_after_scan = dispatch is None and cancelled_dispatch is not None
    effective = dispatch if dispatch is not None else cancelled_dispatch
    # Scan state spans every live dispatch of THIS SHEET, not just the newest: a
    # parcel that already shipped was scanned into the dispatch that carried it, and
    # must not read as unscanned just because a later box opened a new one.
    #
    # Dispatches from an EARLIER sheet are history, not progress here. A carried-over
    # order is re-listed precisely so its box is scanned again on this sheet, so a
    # scan made on the sheet it came from must not tick it off on this one — that
    # inflated the sheet's scanned count by every parcel it had brought with it.
    sources = [d for d in dispatches
               if d.import_batch_id in (None, order.import_batch_id)]
    if not sources and cancelled_dispatch is not None:
        sources = [cancelled_dispatch]
    scanned, scan_detail = set(), {}
    for d in sources:
        scanned |= _scanned_prefixes(d)
        for k, v in _scan_detail(d).items():
            scan_detail.setdefault(k, v)
    # What has actually GONE. Deliberately not folded into ``scanned``: the board must
    # keep reporting the real number of Tracking IDs scanned, so an order confirmed
    # without a full scan still reads honestly instead of being back-filled to "done".
    shipped = confirmed_trackings(order, sources)
    items = _order_items(order)
    trackings = [t for t in {i["tracking_id"] for i in items if i["tracking_id"]}]
    tracking_total = len(trackings)
    tracking_scanned = sum(1 for t in trackings if t in scanned)
    tracking_confirmed = sum(1 for t in trackings if t in shipped)

    confirmed = dispatch is not None and dispatch.status == MarketplaceDispatchStatus.CONFIRMED
    has_trackings = tracking_total > 0
    fully = has_trackings and tracking_scanned == tracking_total
    if ready is None:
        ready = order_dispatch_ready(order)

    # Give priority to Tracking IDs while an order is still being scanned. An order
    # that carries per-item tracking IDs is complete only when EVERY tracking ID has
    # been scanned — a quantity-only completion (e.g. one packing-barcode scan that
    # fills an item's whole quantity, or two same-item lines with different tracking
    # IDs) can mark the dispatch READY while leaving a tracking ID unscanned. Judging
    # by dispatch status alone then hides real work ("nothing to scan" while the sheet
    # still owes tracking IDs), so such an order stays PARTIAL until each tracking ID
    # is scanned. Orders with no per-line tracking IDs fall back to dispatch status.
    #
    # A CONFIRMED order can no longer be scanned, so it drops out of the "To scan"
    # work-list on status alone. We DON'T fake its tracking_scanned to the total:
    # the board and CSV must show the REAL number of Tracking IDs that were scanned,
    # so an order confirmed without a full scan (e.g. a supervisor override) is
    # visible as such instead of silently reading "done".
    if cancelled_after_scan:
        status = "CANCELLED"
    elif has_trackings:
        # CONFIRMED means every parcel has gone. While some boxes are still owed the
        # order stays in the work-list, however many of its siblings already shipped —
        # ``tracking_confirmed`` and each item's ``confirmed`` flag say what is done,
        # so the same order can be shown under Confirmed AND under To scan.
        if tracking_confirmed == tracking_total:
            status = "CONFIRMED"
        else:
            status = "SCANNED" if fully else ("PARTIAL" if tracking_scanned > 0 else "PENDING")
    elif confirmed:
        status = "CONFIRMED"
    elif dispatch is not None and dispatch.status == MarketplaceDispatchStatus.READY:
        status = "SCANNED"
    else:
        status = "PENDING"

    variants = []
    if mappings is not None:
        from .variant_service import order_variants
        variants = order_variants(order, mappings, choosable_only=True)

    bill = getattr(effective, "internal_billing", None) if effective else None
    return {
        "order_id": order.order_id,
        "buyer_name": order.buyer_name,
        "order_date": order.order_date.isoformat() if order.order_date else None,
        "order_type": order.order_type or "",
        "shipment_id": order.flipkart_shipment_id or "",
        "dispatch_by": order.dispatch_by.isoformat() if order.dispatch_by else None,
        "ship_to_name": order.ship_to_name or "",
        "address_line1": order.address_line1 or "",
        "address_line2": order.address_line2 or "",
        "city": order.city or "",
        "state": order.state or "",
        "pin_code": order.pin_code or "",
        "dispatch_id": effective.id if effective else None,
        "dispatch_status": effective.status if effective else None,
        "sap_post_status": effective.sap_post_status if effective else None,
        "cancel_reason": (cancelled_dispatch.cancel_reason if cancelled_after_scan else ""),
        "invoice_number": (bill.invoice_number if bill else ""),
        "invoice_date": (bill.created_at.isoformat() if bill else None),
        "dn_number": (effective.sap_delivery_note_num if effective else ""),
        "gi_number": (effective.sap_goods_issue_num if effective else ""),
        "confirmed_at": (
            effective.confirmed_at.isoformat() if effective and effective.confirmed_at else None
        ),
        "confirmed_by": (
            effective.confirmed_by.full_name if effective and effective.confirmed_by_id else ""
        ),
        "ready": ready,
        "status": status,
        "tracking_total": tracking_total,
        "tracking_scanned": tracking_scanned,
        "tracking_confirmed": tracking_confirmed,
        "items": [
            {
                **i,
                "scanned": bool(i["tracking_id"]) and i["tracking_id"] in scanned,
                "confirmed": bool(i["tracking_id"]) and i["tracking_id"] in shipped,
                "scanned_at": scan_detail.get(i["tracking_id"], {}).get("scanned_at"),
                "scanned_by": scan_detail.get(i["tracking_id"], {}).get("scanned_by", ""),
            }
            for i in items
        ],
        "variants": variants,
    }


def _dispatch_map(company, orders):
    """``{order_id(pk): [non-cancelled dispatches, newest first]}``, scans prefetched.

    A multi-parcel order ships a box at a time, so it can have SEVERAL live
    dispatches at once: the ones that already went out (CONFIRMED) plus the one
    collecting the parcels still on the floor. The board needs them all — the newest
    to scan into, the confirmed ones to know which parcels have already shipped.
    Judging an order by its latest dispatch alone would call it "Confirmed" while a
    box of it is still sitting in the warehouse.
    """
    dispatches = (
        MarketplaceDispatch.objects.filter(company=company, order__in=orders)
        .exclude(status=MarketplaceDispatchStatus.CANCELLED)
        .select_related("internal_billing", "confirmed_by")
        .prefetch_related(Prefetch("scans", queryset=MarketplaceScan.objects.select_related("scanned_by")))
        .order_by("order_id", "-created_at", "-id")
    )
    out = {}
    for d in dispatches:  # ordered newest first (created desc, id desc tiebreak)
        out.setdefault(d.order_id, []).append(d)
    return out


def _cancelled_map(company, orders):
    """``{order_id(pk): latest CANCELLED dispatch}`` for the given orders, with scans
    prefetched — powers the "cancel after scan" status. Set-based (one query)."""
    dispatches = (
        MarketplaceDispatch.objects.filter(
            company=company, order__in=orders, status=MarketplaceDispatchStatus.CANCELLED,
        )
        .select_related("internal_billing", "confirmed_by")
        .prefetch_related(Prefetch("scans", queryset=MarketplaceScan.objects.select_related("scanned_by")))
        .order_by("order_id", "-created_at")
    )
    out = {}
    for d in dispatches:
        out.setdefault(d.order_id, d)
    return out


def _insights(order_views):
    """Aggregate a list of order views into the sheet-level summary."""
    total = len(order_views)
    completed = sum(1 for o in order_views if o["status"] in ("SCANNED", "CONFIRMED"))
    confirmed = sum(1 for o in order_views if o["status"] == "CONFIRMED")
    cancelled = sum(1 for o in order_views if o["status"] == "CANCELLED")
    # Cancelled-after-scan orders are out of the flow — don't count their tracking
    # toward the sheet's scan progress.
    active = [o for o in order_views if o["status"] != "CANCELLED"]
    tracking_total = sum(o["tracking_total"] for o in active)
    tracking_scanned = sum(o["tracking_scanned"] for o in active)
    return {
        "total_orders": total,
        "completed_orders": completed,
        "pending_orders": total - completed - cancelled,
        "confirmed_orders": confirmed,
        "cancelled_orders": cancelled,
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


def _carried_over(company, batch):
    """Orders present in this sheet's uploaded CSV but kept on an earlier sheet.

    Purely informational — these belong to the OTHER sheet, so they never touch this
    sheet's insight counts. Set-based: one query for the skips, one for their live
    dispatches (see :func:`_dispatch_map`)."""
    skips = list(
        batch.skips.select_related("kept_order", "kept_order__import_batch").order_by("order_id")
    )
    if not skips:
        return []
    kept = [s.kept_order for s in skips if s.kept_order_id]
    dmap = _dispatch_map(company, kept) if kept else {}
    rows = []
    for s in skips:
        o = s.kept_order
        ds = dmap.get(o.id) or [] if o is not None else []
        d = ds[0] if ds else None
        rows.append({
            "order_id": s.order_id,
            "reason": s.reason,
            "buyer_name": o.buyer_name if o is not None else "",
            "tracking_ids": s.tracking_ids or [],
            "kept_on_batch_id": o.import_batch_id if o is not None else None,
            "kept_on_filename": (o.import_batch.filename if o is not None and o.import_batch_id else ""),
            "dispatch_id": d.id if d is not None else None,
            "dispatch_status": d.status if d is not None else None,
        })
    return rows


def sheet_board(company, channel, batch_id):
    """Full board for one sheet: insights + every order with per-item tracking state,
    plus the ``carried_over`` orders kept on an earlier sheet (informational)."""
    from .errors import MarketplaceError

    batch = (
        OrderImportBatch.objects.filter(company=company, channel=channel, id=batch_id).first()
    )
    if batch is None:
        raise MarketplaceError("Sheet not found.", code="NOT_FOUND", status_code=404)
    from .resolve_service import load_mappings
    orders = _sheet_orders(company, channel, batch)
    dmap = _dispatch_map(company, orders)
    cmap = _cancelled_map(company, orders)
    mappings = load_mappings(company, channel)
    ready_map = _ready_map(company, channel, orders)
    order_views = [
        _order_view(o, dmap.get(o.id), mappings, ready=ready_map.get(o.id),
                    cancelled_dispatch=cmap.get(o.id))
        for o in orders
    ]
    return {
        "sheet": {
            "id": batch.id,
            "filename": batch.filename,
            "status": batch.status,
            "created_at": batch.created_at.isoformat(),
            "row_count": batch.row_count,
            "summary": batch.summary or {},
        },
        "insights": _insights(order_views),
        "orders": order_views,
        "carried_over": _carried_over(company, batch),
    }


def orders_in_range(company, channel, date_from=None, date_to=None, date_field="upload"):
    """Every non-cancelled order in [date_from, date_to], across ALL sheets, serialized
    exactly like the per-sheet board. ``date_field`` picks which date the range applies
    to: ``"upload"`` (the sheet's upload date — used by the Outward all-sheets export)
    or ``"order"`` (the marketplace order date — used by the Orders report). Dates are
    ISO strings / date objects, or None (open-ended)."""
    from .resolve_service import load_mappings

    qs = MarketplaceOrder.objects.filter(company=company, channel=channel, is_cancelled=False)
    lo, hi = (
        ("order_date__gte", "order_date__lte") if date_field == "order"
        else ("import_batch__created_at__date__gte", "import_batch__created_at__date__lte")
    )
    if date_from:
        qs = qs.filter(**{lo: date_from})
    if date_to:
        qs = qs.filter(**{hi: date_to})
    orders = list(
        qs.prefetch_related("lines", "lines__chosen_option").order_by("order_date", "order_id")
    )
    dmap = _dispatch_map(company, orders)
    cmap = _cancelled_map(company, orders)
    ready_map = _ready_map(company, channel, orders)
    mappings = load_mappings(company, channel)
    return {
        "orders": [
            _order_view(o, dmap.get(o.id), mappings, ready=ready_map.get(o.id),
                        cancelled_dispatch=cmap.get(o.id))
            for o in orders
        ],
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
    cmap = _cancelled_map(company, orders)
    ready_map = _ready_map(company, channel, orders)
    by_batch = {}
    for o in orders:
        by_batch.setdefault(o.import_batch_id, []).append(
            _order_view(o, dmap.get(o.id), ready=ready_map.get(o.id),
                        cancelled_dispatch=cmap.get(o.id)))

    # Carried-over count per sheet in one grouped query (for the card badge).
    from django.db.models import Count
    carried_counts = dict(
        MarketplaceImportSkip.objects.filter(company=company, import_batch__in=batches)
        .values_list("import_batch")
        .annotate(n=Count("id"))
        .values_list("import_batch", "n")
    )

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
            "carried_over_count": carried_counts.get(b.id, 0),
        })
    return {"sheets": sheets}
