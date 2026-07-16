"""Scan capture + progress computation for outward dispatch and inward returns.

Operators scan **finished goods (FG)** only; packing materials are consumed
automatically at confirm. Duplicate scans are detected via the
``(dispatch, barcode_raw)`` unique constraint (returns HTTP 200 ``duplicate: true``).
"""
from decimal import Decimal

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


def _order_by_tracking(company, channel, barcode):
    """Resolve a marketplace order by its Flipkart Tracking ID (same lookup the
    Packing scan uses). Raises NOT_FOUND if no order carries this tracking ID."""
    from ..models import MarketplaceOrder

    code = (barcode or "").strip()
    if not code:
        raise MarketplaceError("Scan a Tracking ID.", code="EMPTY", status_code=400)
    order = (
        MarketplaceOrder.objects.filter(
            company=company, channel=channel, tracking_id=code
        )
        .order_by("-created_at")
        .first()
    )
    if order is None:
        raise MarketplaceError(
            f"No order found for Tracking ID {code}.", code="NOT_FOUND", status_code=404,
        )
    return order, code


def scan_dispatch_by_tracking(company, channel, *, barcode, user=None):
    """Scan a whole order into Outward by its Tracking ID.

    One Flipkart Tracking ID identifies the entire shipment, so a single scan
    completes **every** FG line of the order. Resolves the order, enforces the
    dispatch gate (order must be PACKED), gets/creates its dispatch, records a
    completing scan per FG line, and sets the dispatch READY. Returns
    ``(dispatch, created, duplicate)`` — ``duplicate`` when the order was already
    fully scanned (READY) or CONFIRMED.
    """
    from ..models import MarketplaceDispatch
    from .dispatch_gate import order_dispatch_ready
    from .settings_service import is_skip_packing

    order, code = _order_by_tracking(company, channel, barcode)
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

    if dispatch.status in (MarketplaceDispatchStatus.READY, MarketplaceDispatchStatus.CONFIRMED):
        return dispatch, created, True  # already scanned/dispatched

    resolved = resolve_order(order)
    flines = fg_lines(resolved["resolved_lines"])
    for line in flines:
        bc = f"{code}#{line['item_code']}"
        if dispatch.scans.filter(barcode_raw=bc).exists():
            continue
        MarketplaceScan.objects.create(
            company=company, dispatch=dispatch, barcode_raw=bc,
            item_code=line["item_code"], item_name=line["item_name"],
            component_type=ComboComponentType.FG,
            source_sku=(line["source_skus"][0] if line["source_skus"] else ""),
            quantity=Decimal(line["required_quantity"]), uom=line["uom"],
            warehouse_code=line["warehouse_code"], scanned_by=user,
        )
    dispatch.status = MarketplaceDispatchStatus.READY
    dispatch.updated_by = user
    dispatch.save(update_fields=["status", "updated_by", "updated_at"])
    return dispatch, created, False


# ── Returns ──────────────────────────────────────────────────────────────────
def return_progress(mp_return):
    resolved = resolve_order(mp_return.order)
    flines = fg_lines(resolved["resolved_lines"])
    scanned_map = _scanned_by_item(mp_return.scans.filter(is_active=True))
    return build_progress(flines, scanned_map)


def record_return_scan(mp_return, *, barcode_raw, item_code=None, quantity=None, user=None):
    """Record a returned-item scan. Returns ``(scan, created, duplicate)``."""
    quantity = Decimal(quantity) if quantity is not None else ONE
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


def scan_return_by_tracking(company, channel, *, barcode, user=None):
    """Scan a whole order into Inward by its Tracking ID — the returns mirror of
    :func:`scan_dispatch_by_tracking`.

    Resolves the order, gets/creates its (non-cancelled) return, and records a
    returned-item scan for **every** FG line of the order. Returns
    ``(mp_return, created, duplicate)`` — ``duplicate`` when the order's return was
    already SUBMITTED or every FG line was already scanned.
    """
    from ..models import MarketplaceReturn

    order, code = _order_by_tracking(company, channel, barcode)
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

    resolved = resolve_order(order)
    flines = fg_lines(resolved["resolved_lines"])
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
