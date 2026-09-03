"""Scan capture + progress computation for outward dispatch and inward returns.

Operators scan **finished goods (FG)** only; packing materials are consumed
automatically at confirm. Duplicate scans are detected via the
``(dispatch, barcode_raw)`` unique constraint (returns HTTP 200 ``duplicate: true``).
"""
from decimal import Decimal

from django.db import transaction

from ..models import (
    ComboComponentType,
    MarketplaceDispatchStatus,
    MarketplaceReturnStatus,
    MarketplaceScan,
    MarketplaceReturnScan,
)
from .errors import MarketplaceError
from .resolve_service import fg_lines, resolve_order

ONE = Decimal("1")


def _norm(code):
    return (code or "").strip().upper()


def _scanned_by_item(scans_qs):
    totals = {}
    for item_code, qty in scans_qs.values_list("item_code", "quantity"):
        totals[_norm(item_code)] = totals.get(_norm(item_code), Decimal("0")) + Decimal(qty)
    return totals


def _match_line(flines, code):
    n = _norm(code)
    for line in flines:
        if _norm(line["item_code"]) == n:
            return line
    return None


def build_progress(flines, scanned_map):
    """Per FG-line required-vs-scanned summary."""
    rows = []
    for line in flines:
        required = Decimal(line["required_quantity"])
        scanned = scanned_map.get(_norm(line["item_code"]), Decimal("0"))
        if scanned == 0:
            status = "PENDING"
        elif scanned < required:
            status = "UNDER"
        elif scanned == required:
            status = "COMPLETE"
        else:
            status = "OVER"
        rows.append({
            "item_code": line["item_code"],
            "item_name": line["item_name"],
            "component_type": ComboComponentType.FG,
            "required_quantity": str(required),
            "scanned_quantity": str(scanned),
            "status": status,
        })
    return rows


def dispatch_progress(dispatch):
    resolved = resolve_order(dispatch.order)
    flines = fg_lines(resolved["resolved_lines"])
    scanned_map = _scanned_by_item(dispatch.scans.filter(is_active=True))
    return build_progress(flines, scanned_map)


def is_fully_scanned(progress_rows):
    return bool(progress_rows) and all(r["status"] == "COMPLETE" for r in progress_rows)


@transaction.atomic
def record_dispatch_scan(dispatch, *, barcode_raw, item_code=None, quantity=None, user=None):
    """Record a scan against an outward dispatch.

    Returns ``(scan, created, duplicate)``. Raises :class:`MarketplaceError` for
    items not on the order or over-scans.
    """
    quantity = Decimal(quantity) if quantity is not None else ONE
    resolved = resolve_order(dispatch.order)
    flines = fg_lines(resolved["resolved_lines"])

    # A packing barcode resolves to its item + quantity (so one scan completes a line).
    from ..models import MarketplacePackBarcode

    pack_bc = MarketplacePackBarcode.objects.filter(
        order=dispatch.order, barcode=barcode_raw
    ).first()
    if pack_bc is not None:
        item_code = pack_bc.item_code
        if quantity == ONE:
            quantity = Decimal(pack_bc.quantity)

    code = (item_code or barcode_raw)
    line = _match_line(flines, code)
    if line is None:
        raise MarketplaceError(
            f"Scanned item '{code}' is not on order {dispatch.order.order_id}.",
            code="ITEM_NOT_ON_ORDER", status_code=400,
        )

    existing = dispatch.scans.filter(barcode_raw=barcode_raw).first()
    if existing is not None:
        return existing, False, True  # duplicate

    scanned_map = _scanned_by_item(dispatch.scans.filter(is_active=True))
    already = scanned_map.get(_norm(line["item_code"]), Decimal("0"))
    if already + quantity > Decimal(line["required_quantity"]):
        raise MarketplaceError(
            f"Over-scan: {line['item_code']} already at "
            f"{already}/{line['required_quantity']}.",
            code="OVER_SCAN", status_code=400,
        )

    scan = MarketplaceScan.objects.create(
        company=dispatch.company,
        dispatch=dispatch,
        barcode_raw=barcode_raw,
        item_code=line["item_code"],
        item_name=line["item_name"],
        component_type=ComboComponentType.FG,
        source_sku=(line["source_skus"][0] if line["source_skus"] else ""),
        quantity=quantity,
        uom=line["uom"],
        warehouse_code=line["warehouse_code"],
        scanned_by=user,
    )

    # Recompute readiness and advance status.
    progress = build_progress(flines, _scanned_by_item(dispatch.scans.filter(is_active=True)))
    dispatch.status = (
        MarketplaceDispatchStatus.READY if is_fully_scanned(progress)
        else MarketplaceDispatchStatus.SCANNING
    )
    dispatch.updated_by = user
    dispatch.save(update_fields=["status", "updated_by", "updated_at"])
    return scan, True, False


def _scan_target_by_tracking(company, channel, barcode, batch_id=None,
                             prefer_dispatched=False):
    """Resolve what a scanned Tracking ID refers to.

    A Tracking ID belongs to a specific order ITEM (line) — a multi-item order has
    several. Returns ``(order, matched_lines)`` where ``matched_lines`` are the
    order's lines carrying this tracking ID. Falls back to the order-level tracking
    ID (whole order) for single-item / legacy data. NOT_FOUND if nothing matches.

    ``batch_id`` is the sheet the operator is scanning on, and it is STRICT: the
    scan either lands on that sheet or is refused with ``NOT_ON_SHEET`` naming the
    sheet the tracking actually lives on. There used to be a fallback to the newest
    row anywhere, but it silently put parcels on a sheet nobody was looking at —
    an operator working the 01-09 sheet had 41 scans land on the 31-08 one and the
    counter read 402/443 with nothing visibly wrong. Only a scan with NO sheet
    context (bare gun scan, returns) still resolves across sheets, and even that
    never resolves into a deleted (``is_active=False``) sheet.

    ``prefer_dispatched`` picks the newest row that actually SHIPPED — one carrying a
    CONFIRMED dispatch, which is exactly what :func:`_require_dispatched` demands. A
    return is for goods that went out, and the newest row for a re-listed parcel is
    often an open one on a later sheet that has shipped nothing; resolving to it would
    refuse a legitimate return as NOT_DISPATCHED. Note a part-shipped order stays OPEN
    while holding a CONFIRMED dispatch, so the dispatch is the truth here, not status.
    """
    from ..models import (
        MarketplaceDispatch, MarketplaceDispatchStatus, MarketplaceOrder,
        MarketplaceOrderLine, OrderImportBatch,
    )

    def _shipped_ids(order_ids):
        """Which of ``order_ids`` have a CONFIRMED dispatch (one query)."""
        return set(
            MarketplaceDispatch.objects.filter(
                order_id__in=order_ids, status=MarketplaceDispatchStatus.CONFIRMED
            ).values_list("order_id", flat=True)
        )

    def _not_on_sheet(others):
        """Refuse a scan whose tracking lives on other sheet(s), naming them."""
        names = sorted({
            b.filename or f"sheet #{b.id}"
            for b in others if b is not None and b.is_active
        })
        where = f" It belongs to {', '.join(names)} — select that sheet to scan it." \
            if names else " It only exists on a deleted sheet."
        raise MarketplaceError(
            f"Tracking ID {code} is not on this sheet.{where}",
            code="NOT_ON_SHEET", status_code=409,
        )

    def _live(order):
        """An order counts unless it sits on a deleted sheet."""
        return order.import_batch_id is None or (
            order.import_batch is not None and order.import_batch.is_active
        )

    code = (barcode or "").strip()
    if not code:
        raise MarketplaceError("Scan a Tracking ID.", code="EMPTY", status_code=400)
    if batch_id and not OrderImportBatch.objects.filter(
            id=batch_id, company=company, is_active=True).exists():
        raise MarketplaceError(
            "This sheet has been deleted; nothing can be scanned into it.",
            code="SHEET_DELETED", status_code=409,
        )
    def _deleted():
        """Refuse a scan whose tracking was 'delete remaining'-ed off its sheet."""
        raise MarketplaceError(
            f"Tracking ID {code} was deleted from its sheet. Re-upload the order on "
            "a new sheet and scan it there.",
            code="TRACKING_DELETED", status_code=409,
        )

    lines = list(
        MarketplaceOrderLine.objects.filter(
            order__company=company, order__channel=channel, tracking_id=code
        ).select_related("order", "order__import_batch").order_by("-order__created_at")
    )
    if lines:
        # A soft-deleted line/order no longer exists for scanning — but is kept in
        # the match list so a scan of it can say "deleted" instead of "not found".
        alive = [l for l in lines if l.is_active and l.order.is_active]
        if batch_id:
            pool = [l for l in alive if l.order.import_batch_id == batch_id]
            if not pool:
                if any(l.order.import_batch_id == batch_id for l in lines):
                    _deleted()  # it WAS on this sheet; its remainder was deleted
                if not alive:
                    _deleted()
                _not_on_sheet({l.order.import_batch for l in alive})
        else:
            if not alive:
                _deleted()
            pool = [l for l in alive if _live(l.order)] or alive
        if prefer_dispatched:
            shipped = _shipped_ids({l.order_id for l in pool})
            pool = [l for l in pool if l.order_id in shipped] or pool
        order = pool[0].order
        return order, [l for l in alive if l.order_id == order.id]
    qs = MarketplaceOrder.objects.filter(
        company=company, channel=channel, tracking_id=code
    ).select_related("import_batch")
    alive_qs = qs.filter(is_active=True)
    if batch_id:
        order = alive_qs.filter(import_batch_id=batch_id).order_by("-created_at").first()
        if order is None:
            if qs.filter(import_batch_id=batch_id).exists():
                _deleted()
            others = list(alive_qs)
            if others:
                _not_on_sheet({o.import_batch for o in others})
            if qs.exists():
                _deleted()
    else:
        live = [o for o in alive_qs.order_by("-created_at") if _live(o)] \
            or list(alive_qs.order_by("-created_at"))
        if prefer_dispatched and live:
            shipped = _shipped_ids([o.id for o in live])
            live = [o for o in live if o.id in shipped] or live
        order = live[0] if live else None
        if order is None and qs.exists():
            _deleted()
    if order is None:
        raise MarketplaceError(
            f"No order found for Tracking ID {code}.", code="NOT_FOUND", status_code=404,
        )
    return order, order.live_lines()


@transaction.atomic
def scan_dispatch_by_tracking(company, channel, *, barcode, user=None, batch_id=None):
    """Scan one shipment (Tracking ID) into Outward.

    A Tracking ID identifies an order ITEM, so it completes only that item's FG
    quantity. A multi-item order (whose items carry different tracking IDs) becomes
    READY only once every item's tracking ID has been scanned. Returns
    ``(dispatch, created, duplicate)`` — ``duplicate`` when this scan adds nothing
    new (already scanned) or the order is CONFIRMED.
    """
    from ..models import MarketplaceDispatch
    from .dispatch_gate import order_dispatch_ready
    from .resolve_service import load_mappings, resolve_lines
    from .settings_service import is_skip_packing

    order, matched_lines = _scan_target_by_tracking(company, channel, barcode, batch_id)
    code = (barcode or "").strip()
    if order.is_cancelled:
        raise MarketplaceError(
            f"Order {order.order_id} is cancelled.", code="ORDER_CANCELLED", status_code=409,
        )
    if not order_dispatch_ready(order):
        skip = is_skip_packing(company, channel)
        raise MarketplaceError(
            "This order's materials have not been issued yet." if skip
            else "This order has not been packed yet.",
            code="NOT_ISSUED" if skip else "NOT_PACKED", status_code=409,
        )

    dispatch = (
        MarketplaceDispatch.objects.filter(company=company, order=order)
        .exclude(status=MarketplaceDispatchStatus.CANCELLED)
        .order_by("-created_at", "-id")
        .first()
    )
    created = False
    if dispatch is None:
        dispatch = MarketplaceDispatch.objects.create(
            company=company, channel=channel, order=order,
            import_batch_id=order.import_batch_id,
            sap_warehouse_code=order.sap_warehouse_code or "",
            status=MarketplaceDispatchStatus.DRAFT, created_by=user, updated_by=user,
        )
        created = True

    if dispatch.status == MarketplaceDispatchStatus.CONFIRMED:
        # The last dispatch already shipped. If THIS parcel went out on it (or on any
        # earlier one) the scan adds nothing. Otherwise a box of this order is still
        # on the floor — a partial confirm left it behind, or Flipkart re-manifested
        # it — so open a fresh dispatch and scan it there rather than refusing.
        if code in confirmed_trackings(order):
            return dispatch, created, True
        dispatch = MarketplaceDispatch.objects.create(
            company=company, channel=channel, order=order,
            import_batch_id=order.import_batch_id,
            sap_warehouse_code=order.sap_warehouse_code or "",
            status=MarketplaceDispatchStatus.DRAFT, created_by=user, updated_by=user,
        )
        created = True
    elif dispatch.import_batch_id != order.import_batch_id:
        # The live dispatch was opened while this order sat on an EARLIER sheet, and
        # the order has since been re-listed on a newer one. The board only counts a
        # scan against the sheet its dispatch is stamped with (see
        # dispatch_board_service._order_view), so reusing that dispatch records the
        # parcel where nobody is looking — and, because the barcode is already on it,
        # every later scan comes back "already scanned". The parcel then can NEVER be
        # ticked off on the sheet the packer is actually working.
        #
        # Open a fresh dispatch stamped to the order's current sheet instead, exactly
        # as the CONFIRMED case above does. The scan lands on the sheet in front of
        # the operator; the older dispatch keeps its own history.
        dispatch = MarketplaceDispatch.objects.create(
            company=company, channel=channel, order=order,
            import_batch_id=order.import_batch_id,
            sap_warehouse_code=order.sap_warehouse_code or "",
            status=MarketplaceDispatchStatus.DRAFT, created_by=user, updated_by=user,
        )
        created = True

    mappings = load_mappings(company, channel)
    # Record scans for ONLY the item(s) behind the scanned tracking ID.
    resolved = resolve_lines(matched_lines, order.sap_warehouse_code or "", mappings)
    item_flines = fg_lines(resolved["resolved_lines"])
    if not item_flines:
        # Nothing to scan means the SKU behind this tracking doesn't resolve to a
        # finished good — almost always because it isn't mapped yet. Say so clearly
        # instead of the misleading "already scanned" (which is what an empty scan
        # loop would otherwise report).
        if resolved["unmapped_skus"]:
            sku = (matched_lines[0].marketplace_sku or matched_lines[0].fsn or "").strip()
            raise MarketplaceError(
                f"SKU '{sku}' (FSN {resolved['unmapped_skus'][0]}) is not mapped to a SAP "
                "item — add a mapping in Masters, then scan.",
                code="UNMAPPED", status_code=409,
            )
        raise MarketplaceError(
            "This shipment has no finished-good item to scan.",
            code="NO_FG", status_code=409,
        )
    any_new = False
    for line in item_flines:
        bc = f"{code}#{line['item_code']}"
        if dispatch.scans.filter(barcode_raw=bc).exists():
            continue
        any_new = True
        MarketplaceScan.objects.create(
            company=company, dispatch=dispatch, barcode_raw=bc,
            item_code=line["item_code"], item_name=line["item_name"],
            component_type=ComboComponentType.FG,
            source_sku=(line["source_skus"][0] if line["source_skus"] else ""),
            quantity=Decimal(line["required_quantity"]), uom=line["uom"],
            warehouse_code=line["warehouse_code"], scanned_by=user,
        )

    # READY once every one of the order's item tracking IDs has been scanned.
    dispatch.status = (
        MarketplaceDispatchStatus.READY if dispatch_is_fully_scanned(dispatch, mappings)
        else MarketplaceDispatchStatus.SCANNING
    )
    dispatch.updated_by = user
    dispatch.save(update_fields=["status", "updated_by", "updated_at"])
    return dispatch, created, (not any_new)


def scanned_trackings(dispatch):
    """The set of tracking IDs already scanned on a dispatch (barcode prefix).

    Iterates the (possibly prefetched) scans and filters ``is_active`` in Python:
    ``.filter(is_active=True)`` builds a new queryset off the related manager, which
    walks past ``prefetch_related("scans")`` and costs a query per dispatch.
    """
    return {
        (s.barcode_raw or "").split("#", 1)[0]
        for s in dispatch.scans.all()
        if s.is_active
    }


def dispatch_lines(dispatch):
    """The order lines (parcels) THIS dispatch covers — the ones scanned into it.

    A multi-parcel order can now be dispatched a box at a time: each dispatch owns
    only the parcels whose Tracking ID was scanned into it, and its delivery note
    covers exactly those. Legacy orders carrying no per-line tracking ID have
    nothing to scope by, so the dispatch covers the whole order as before.
    """
    lines = dispatch.order.live_lines()
    if not any((l.tracking_id or "").strip() for l in lines):
        return lines
    scanned = scanned_trackings(dispatch)
    return [l for l in lines if (l.tracking_id or "").strip() in scanned]


def shipped_lines(dispatch):
    """The order lines this dispatch actually SHIPS — what its delivery note, its
    print and its CSV export must cover, and nothing else.

    A part-confirmed order leaves a box at a time, so its note may cover one parcel
    of a two-parcel order. Building that note from ``order.lines`` would put the
    unscanned parcel on it too — issuing stock that is still on the floor, and then
    issuing it AGAIN when its own dispatch confirms later.

    ``shipped_trackings`` is stamped at confirm and cannot drift, so it wins once the
    dispatch is confirmed; before that (the cut preview) the scans are the only truth.
    Two fall-backs keep whole-order dispatches whole: an order with no per-line
    tracking IDs has nothing to scope by, and a dispatch a supervisor forced through
    with no scan at all (``override_deviation``) means "ship the lot".
    """
    lines = dispatch.order.live_lines()
    # ``lines and`` matters: with every line retired, ``any()`` over nothing is False
    # and this read as "an order with no tracking IDs", returning the empty list as
    # though that were the answer. It is the case the fall-back below exists for.
    if lines and not any((l.tracking_id or "").strip() for l in lines):
        return lines
    keys = {t for t in (dispatch.shipped_trackings or []) if t} or scanned_trackings(dispatch)
    if not keys:
        return lines
    matched = [l for l in lines if (l.tracking_id or "").strip() in keys]
    if matched:
        return matched
    # The stamp names parcels no LIVE line carries any more — the line was retired
    # by a re-manifest or a remediation after this dispatch shipped. An empty answer
    # here is the wrong one: it silently drops every row from the note's export and
    # blocks its cut. Fall back to all of the order's lines, live or not, and only
    # then to the whole order — the same "an empty match never means shipped
    # nothing" rule insight_reports already applies.
    every = list(dispatch.order.lines.all())
    return [l for l in every if (l.tracking_id or "").strip() in keys] or lines


def confirmed_trackings(order, dispatches=None):
    """Tracking IDs of ``order`` that have already shipped.

    Reads each CONFIRMED dispatch's ``shipped_trackings`` stamp. A dispatch confirmed
    before per-parcel shipping existed has none — it shipped the WHOLE order, so it
    claims every tracking ID. Without that fallback ~78 orders already delivered under
    the old all-or-nothing rule would reappear as unfinished work and could be shipped
    a second time.
    """
    if dispatches is None:
        dispatches = (order.dispatches.exclude(status=MarketplaceDispatchStatus.CANCELLED)
                      .order_by("-created_at", "-id"))
    dispatches = list(dispatches)
    newest = dispatches[0] if dispatches else None
    all_trackings = None
    out = set()
    for d in dispatches:
        if d.status != MarketplaceDispatchStatus.CONFIRMED:
            continue
        stamped = [t for t in (d.shipped_trackings or []) if t]
        if stamped:
            out |= set(stamped)
            continue
        # No stamp: confirmed before per-parcel shipping existed, so it took the WHOLE
        # order — claim every parcel, or the ~78 orders already delivered under the old
        # all-or-nothing rule would reappear as unfinished work and could ship twice.
        # Unless a NEWER dispatch exists: that one is the live work (a re-manifested
        # parcel, or the boxes this one left behind), so the old note owns only what it
        # actually scanned.
        if d is not newest:
            out |= scanned_trackings(d)
            continue
        if all_trackings is None:
            all_trackings = {(l.tracking_id or "").strip()
                             for l in order.live_lines()
                             if (l.tracking_id or "").strip()}
        out |= all_trackings
    return out


def dispatch_is_fully_scanned(dispatch, mappings=None):
    """Whether a dispatch's order is completely scanned.

    Primary rule: every order-line tracking ID has been scanned — robust even if
    scan quantities are inconsistent (e.g. mixed old/new scan formats). Falls back
    to the finished-goods quantity check for legacy orders with no per-line
    tracking IDs.
    """
    # live_lines(): a 'delete remaining' line must not hold a partial order back
    # from READY — its parcel is gone from this sheet by decision, not owed.
    wanted = {
        (l.tracking_id or "").strip()
        for l in dispatch.order.live_lines()
        if (l.tracking_id or "").strip()
    }
    # Parcels that already shipped on an earlier CONFIRMED dispatch are done; they
    # are not scanned into THIS one and must not hold it back from READY, or an
    # order confirmed a box at a time could never finish.
    wanted -= confirmed_trackings(dispatch.order)
    # Tracking IDs alone only settle it when EVERY live parcel carries one. A sheet
    # that leaves one line's Tracking ID blank used to slip through: the blank line
    # was absent from ``wanted``, so scanning its siblings read as fully scanned, the
    # order confirmed, closed, and that parcel shipped on no note at all — invisibly.
    # With a mix, fall through to the quantity check, which counts every line.
    untracked = any(not (l.tracking_id or "").strip()
                    for l in dispatch.order.live_lines())
    if wanted and not untracked:
        return wanted.issubset(scanned_trackings(dispatch))
    if wanted and not wanted.issubset(scanned_trackings(dispatch)):
        return False
    whole = fg_lines(resolve_order(dispatch.order, mappings)["resolved_lines"])
    return is_fully_scanned(build_progress(whole, _scanned_by_item(dispatch.scans.filter(is_active=True))))


# ── Returns ──────────────────────────────────────────────────────────────────
def return_progress(mp_return):
    resolved = resolve_order(mp_return.order)
    flines = fg_lines(resolved["resolved_lines"])
    scanned_map = _scanned_by_item(mp_return.scans.filter(is_active=True))
    return build_progress(flines, scanned_map)


def _require_dispatched(order):
    """A return may only be recorded against an order that was actually shipped."""
    from ..models import MarketplaceDispatch, MarketplaceDispatchStatus

    if not MarketplaceDispatch.objects.filter(
        order=order, status=MarketplaceDispatchStatus.CONFIRMED
    ).exists():
        raise MarketplaceError(
            f"Order {order.order_id} has not been dispatched — nothing to return.",
            code="NOT_DISPATCHED", status_code=409,
        )


def _returned_by_item(order):
    """Cumulative returned quantity per item across the order's (non-cancelled)
    returns — so a return can't exceed what was shipped."""
    totals = {}
    for r in order.returns.exclude(status=MarketplaceReturnStatus.CANCELLED):
        for item_code, qty in r.scans.filter(is_active=True).values_list("item_code", "quantity"):
            k = _norm(item_code)
            totals[k] = totals.get(k, Decimal("0")) + Decimal(qty)
    return totals


@transaction.atomic
def record_return_scan(mp_return, *, barcode_raw, item_code=None, quantity=None, user=None):
    """Record a returned-item scan. Returns ``(scan, created, duplicate)``."""
    quantity = Decimal(quantity) if quantity is not None else ONE
    _require_dispatched(mp_return.order)
    resolved = resolve_order(mp_return.order)
    flines = fg_lines(resolved["resolved_lines"])

    # A packing barcode resolves to its item + quantity, exactly as at Outward —
    # returned goods carry the same Flipkart Tracking ID labels used at packing.
    from ..models import MarketplacePackBarcode

    pack_bc = MarketplacePackBarcode.objects.filter(
        order=mp_return.order, barcode=barcode_raw
    ).first()
    if pack_bc is not None:
        item_code = pack_bc.item_code
        if quantity == ONE:
            quantity = Decimal(pack_bc.quantity)

    code = (item_code or barcode_raw)
    line = _match_line(flines, code)
    if line is None:
        raise MarketplaceError(
            f"Returned item '{code}' was not on order {mp_return.order.order_id}.",
            code="ITEM_NOT_ON_ORDER", status_code=400,
        )

    existing = mp_return.scans.filter(barcode_raw=barcode_raw).first()
    if existing is not None:
        return existing, False, True

    # Can't return more of an item than was ordered/shipped (cumulative across the
    # order's returns) — the inward mirror of the Outward OVER_SCAN guard.
    already = _returned_by_item(mp_return.order).get(_norm(line["item_code"]), Decimal("0"))
    if already + quantity > Decimal(line["required_quantity"]):
        raise MarketplaceError(
            f"Over-return: {line['item_code']} already at "
            f"{already}/{line['required_quantity']}.",
            code="OVER_RETURN", status_code=400,
        )

    scan = MarketplaceReturnScan.objects.create(
        company=mp_return.company,
        mp_return=mp_return,
        barcode_raw=barcode_raw,
        item_code=line["item_code"],
        item_name=line["item_name"],
        component_type=ComboComponentType.FG,
        source_sku=(line["source_skus"][0] if line["source_skus"] else ""),
        quantity=quantity,
        uom=line["uom"],
        scanned_by=user,
    )
    if mp_return.status == MarketplaceReturnStatus.DRAFT:
        mp_return.status = MarketplaceReturnStatus.SCANNING
        mp_return.updated_by = user
        mp_return.save(update_fields=["status", "updated_by", "updated_at"])
    return scan, True, False


@transaction.atomic
def scan_return_by_tracking(company, channel, *, barcode, user=None, batch_id=None):
    """Scan one returned shipment (Tracking ID) into Inward — the returns mirror of
    :func:`scan_dispatch_by_tracking`.

    A Tracking ID identifies an order ITEM, so it records only that item's FG
    line(s). Returns ``(mp_return, created, duplicate)`` — ``duplicate`` when the
    return is SUBMITTED or this tracking ID adds nothing new.
    """
    from ..models import MarketplaceReturn
    from .resolve_service import load_mappings, resolve_lines

    order, matched_lines = _scan_target_by_tracking(
        company, channel, barcode, batch_id, prefer_dispatched=True)
    _require_dispatched(order)
    code = (barcode or "").strip()
    # THIS parcel must have shipped, not merely some parcel of the order. An order
    # now goes out a box at a time, so "the order was dispatched" no longer means
    # every box left: without this, a return could be booked against a parcel still
    # sitting on the warehouse floor, crediting goods that never went to the buyer.
    # Orders with no per-parcel tracking have nothing to check and keep the old rule.
    shipped = confirmed_trackings(order)
    if shipped and code not in shipped:
        raise MarketplaceError(
            f"Tracking ID {code} has not shipped on order {order.order_id} — "
            "only a parcel that went out can be returned.",
            code="PARCEL_NOT_DISPATCHED", status_code=409,
        )
    mp_return = (
        MarketplaceReturn.objects.filter(company=company, order=order)
        .exclude(status=MarketplaceReturnStatus.CANCELLED)
        .order_by("-created_at")
        .first()
    )
    created = False
    if mp_return is None:
        mp_return = MarketplaceReturn.objects.create(
            company=company, channel=channel, order=order,
            status=MarketplaceReturnStatus.DRAFT, created_by=user, updated_by=user,
        )
        created = True
    if mp_return.status == MarketplaceReturnStatus.SUBMITTED:
        return mp_return, created, True

    flines = fg_lines(
        resolve_lines(matched_lines, order.sap_warehouse_code or "",
                      load_mappings(company, channel))["resolved_lines"]
    )
    any_new = False
    for line in flines:
        bc = f"{code}#{line['item_code']}"
        if mp_return.scans.filter(barcode_raw=bc).exists():
            continue
        any_new = True
        MarketplaceReturnScan.objects.create(
            company=company, mp_return=mp_return, barcode_raw=bc,
            item_code=line["item_code"], item_name=line["item_name"],
            component_type=ComboComponentType.FG,
            source_sku=(line["source_skus"][0] if line["source_skus"] else ""),
            quantity=Decimal(line["required_quantity"]), uom=line["uom"], scanned_by=user,
        )
    if mp_return.status == MarketplaceReturnStatus.DRAFT and any_new:
        mp_return.status = MarketplaceReturnStatus.SCANNING
        mp_return.updated_by = user
        mp_return.save(update_fields=["status", "updated_by", "updated_at"])
    return mp_return, created, (not any_new)
