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


def _scan_target_by_tracking(company, channel, barcode):
    """Resolve what a scanned Tracking ID refers to.

    A Tracking ID belongs to a specific order ITEM (line) — a multi-item order has
    several. Returns ``(order, matched_lines)`` where ``matched_lines`` are the
    order's lines carrying this tracking ID. Falls back to the order-level tracking
    ID (whole order) for single-item / legacy data. NOT_FOUND if nothing matches.
    """
    from ..models import MarketplaceOrder, MarketplaceOrderLine

    code = (barcode or "").strip()
    if not code:
        raise MarketplaceError("Scan a Tracking ID.", code="EMPTY", status_code=400)
    lines = list(
        MarketplaceOrderLine.objects.filter(
            order__company=company, order__channel=channel, tracking_id=code
        ).select_related("order").order_by("-order__created_at")
    )
    if lines:
        order = lines[0].order
        return order, [l for l in lines if l.order_id == order.id]
    order = (
        MarketplaceOrder.objects.filter(company=company, channel=channel, tracking_id=code)
        .order_by("-created_at").first()
    )
    if order is None:
        raise MarketplaceError(
            f"No order found for Tracking ID {code}.", code="NOT_FOUND", status_code=404,
        )
    return order, list(order.lines.all())


@transaction.atomic
def scan_dispatch_by_tracking(company, channel, *, barcode, user=None):
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

    order, matched_lines = _scan_target_by_tracking(company, channel, barcode)
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
        .order_by("-created_at")
        .first()
    )
    created = False
    if dispatch is None:
        dispatch = MarketplaceDispatch.objects.create(
            company=company, channel=channel, order=order,
            sap_warehouse_code=order.sap_warehouse_code or "",
            status=MarketplaceDispatchStatus.DRAFT, created_by=user, updated_by=user,
        )
        created = True

    if dispatch.status == MarketplaceDispatchStatus.CONFIRMED:
        return dispatch, created, True

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
    """The set of tracking IDs already scanned on a dispatch (barcode prefix)."""
    return {
        (bc or "").split("#", 1)[0]
        for bc in dispatch.scans.filter(is_active=True).values_list("barcode_raw", flat=True)
    }


def dispatch_is_fully_scanned(dispatch, mappings=None):
    """Whether a dispatch's order is completely scanned.

    Primary rule: every order-line tracking ID has been scanned — robust even if
    scan quantities are inconsistent (e.g. mixed old/new scan formats). Falls back
    to the finished-goods quantity check for legacy orders with no per-line
    tracking IDs.
    """
    wanted = {
        (t or "").strip()
        for t in dispatch.order.lines.values_list("tracking_id", flat=True)
        if (t or "").strip()
    }
    if wanted:
        return wanted.issubset(scanned_trackings(dispatch))
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
def scan_return_by_tracking(company, channel, *, barcode, user=None):
    """Scan one returned shipment (Tracking ID) into Inward — the returns mirror of
    :func:`scan_dispatch_by_tracking`.

    A Tracking ID identifies an order ITEM, so it records only that item's FG
    line(s). Returns ``(mp_return, created, duplicate)`` — ``duplicate`` when the
    return is SUBMITTED or this tracking ID adds nothing new.
    """
    from ..models import MarketplaceReturn
    from .resolve_service import load_mappings, resolve_lines

    order, matched_lines = _scan_target_by_tracking(company, channel, barcode)
    _require_dispatched(order)
    code = (barcode or "").strip()
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
