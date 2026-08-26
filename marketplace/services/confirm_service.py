"""Confirm an outward marketplace dispatch.

Validate scan completeness → re-expand combos → mark the order DISPATCHED, then
post the SAP Delivery Note (FG) + Goods Issue (PM) + internal billing.

The SAP posting is **best-effort**: if it fails (e.g. SAP is unreachable) the
order is still dispatched and the dispatch is flagged ``sap_post_status=FAILED``
with the error, so work isn't blocked. The failed post can be retried later via
:func:`retry_delivery_note`.
"""
import logging
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from ..models import (
    MarketplaceBillingStatus,
    MarketplaceDispatchStatus,
    MarketplaceOrderBilling,
    MarketplaceOrderStatus,
    MarketplaceSapPostStatus,
    MarketplaceWarehouse,
)
from .dispatch_gate import order_dispatch_ready
from .errors import MarketplaceError
from .resolve_service import fg_lines, load_mappings, pm_lines, resolve_lines
from .sap_gateway import MarketplaceSapGateway
from .scan_service import (
    build_progress, confirmed_trackings, dispatch_is_fully_scanned, dispatch_lines,
)
from . import settings_service

logger = logging.getLogger(__name__)


def _next_invoice_number(company):
    today = timezone.localdate()
    prefix = f"MKT-{today:%Y%m%d}-"
    count = MarketplaceOrderBilling.objects.filter(
        company=company, invoice_number__startswith=prefix
    ).count()
    return f"{prefix}{count + 1:05d}"


def _warehouse_for(dispatch):
    # Route to the warehouse master matching the dispatch's SAP warehouse code when
    # one is configured (enables multi-branch / per-state posting); otherwise fall
    # back to the channel default (default-first), matching the bulk cut.
    active = MarketplaceWarehouse.objects.filter(
        company=dispatch.company, channel=dispatch.channel, is_active=True
    )
    code = (dispatch.sap_warehouse_code or "").strip()
    wh = None
    if code:
        wh = active.filter(sap_warehouse_code=code).order_by("-is_default", "id").first()
    if wh is None:
        wh = active.order_by("-is_default", "id").first()
    if wh is None:
        raise MarketplaceError(
            f"No active marketplace warehouse configured for {dispatch.channel}.",
            code="NO_WAREHOUSE", status_code=409,
        )
    return wh


def _resolve_dispatch(dispatch, mappings=None):
    """Resolve the parcels THIS dispatch covers, not the whole order.

    A multi-parcel order ships a box at a time: each dispatch owns the parcels
    scanned into it, and its delivery note, goods issue and billing must cover
    exactly those — otherwise the first partial confirm ships and bills the whole
    order and the second one does it again.
    """
    lines = dispatch_lines(dispatch)
    if mappings is None:
        mappings = load_mappings(dispatch.company, dispatch.channel)
    return resolve_lines(lines, dispatch.order.sap_warehouse_code or "", mappings)


def _shipto_for_state(order_state, warehouse):
    """SAP ship-to/bill-to address code for a buyer state, per warehouse.shipto_by_state.

    Returns "" when no map is configured, so the delivery note omits ShipToCode and
    SAP uses the customer's default address (the pre-split behaviour).
    """
    m = warehouse.shipto_by_state or {}
    if not m:
        return ""
    default = m.get("*", "")
    return m.get((order_state or "").strip(), default)


def confirm_dispatch(dispatch, *, user, override_deviation=False, remarks=""):
    """Dispatch the order, then post the SAP delivery note (best-effort).

    Pre-conditions (packed, mapped, scans matched) still block. Once past them the
    order is DISPATCHED regardless of whether SAP accepts the delivery note; a
    failed post is flagged FAILED and can be retried.
    """
    if dispatch.status == MarketplaceDispatchStatus.CONFIRMED:
        return dispatch  # idempotent (use retry_delivery_note for a failed post)
    if dispatch.status == MarketplaceDispatchStatus.CANCELLED:
        raise MarketplaceError("Dispatch is cancelled.", code="INVALID_STATE", status_code=409)

    order = dispatch.order
    if order.is_cancelled:
        raise MarketplaceError(
            "Order is cancelled on the marketplace; cannot dispatch.",
            code="ORDER_CANCELLED", status_code=409,
        )
    if not order_dispatch_ready(order):
        raise MarketplaceError(
            "This order is not ready to dispatch yet (not packed / not issued).",
            code="NOT_READY", status_code=409,
        )
    resolved = _resolve_dispatch(dispatch)
    if resolved["unmapped_skus"]:
        raise MarketplaceError(
            "Order has unmapped SKUs; add mappings in Masters before confirming.",
            code="UNMAPPED_SKUS", status_code=409, detail=resolved["unmapped_skus"],
        )

    flines = fg_lines(resolved["resolved_lines"])
    scanned_map = {}
    for ic, q in dispatch.scans.filter(is_active=True).values_list("item_code", "quantity"):
        k = _u(ic)
        scanned_map[k] = scanned_map.get(k, Decimal("0")) + _d(q)
    # Every shipment must be scanned in Outward before it can be dispatched: an
    # order is confirmable only once every one of its Tracking IDs has been scanned
    # here (finished-goods quantity check for legacy orders with no per-line
    # tracking IDs). A supervisor can still force a confirm with override_deviation
    # (e.g. a damaged/unscannable label) — that path is audited via the remark.
    # Confirm ships EXACTLY the parcels scanned into this dispatch, so a part-scanned
    # order can go out a box at a time. What was not scanned is simply not shipped:
    # it stays in "To scan" and confirms later on its own dispatch. Nothing unscanned
    # ever leaves — which is what the old all-or-nothing gate was really protecting,
    # and it is now enforced by what the note contains rather than by refusing.
    if not override_deviation and not dispatch.scans.filter(is_active=True).exists():
        raise MarketplaceError(
            "Nothing has been scanned for this order — scan a Tracking ID in Outward "
            "before confirming.",
            code="NOT_SCANNED", status_code=409,
        )
    # Guard against a mis-scan (wrong item / wrong count) when items were scanned
    # by item code rather than Tracking ID.
    deviating = [r for r in build_progress(flines, scanned_map) if r["status"] in ("UNDER", "OVER")]
    if deviating and scanned_map and not override_deviation:
        raise MarketplaceError(
            "Scan counts deviate from the order.",
            code="SCAN_DEVIATION", status_code=409, detail=deviating,
        )

    # Mark the order dispatched — this always persists, even if SAP is down.
    with transaction.atomic():
        dispatch.status = MarketplaceDispatchStatus.CONFIRMED
        # Record exactly which parcels went out on this note, so the board can tell a
        # part-shipped order from a finished one without guessing.
        dispatch.shipped_trackings = sorted(
            {(l.tracking_id or "").strip() for l in dispatch_lines(dispatch)
             if (l.tracking_id or "").strip()}
        )
        dispatch.sap_post_status = MarketplaceSapPostStatus.PENDING
        dispatch.confirmed_by = user
        dispatch.confirmed_at = timezone.now()
        dispatch.updated_by = user
        dispatch.save()
        # The ORDER is dispatched only once every one of its parcels has shipped.
        # Until then it stays open so its remaining boxes keep showing in "To scan".
        wanted = {(l.tracking_id or "").strip() for l in order.lines.all()
                  if (l.tracking_id or "").strip()}
        if not wanted or not (wanted - confirmed_trackings(order)):
            order.status = MarketplaceOrderStatus.DISPATCHED
            order.save(update_fields=["status", "updated_at"])

    # Best-effort SAP delivery note — never rolls back the dispatch. When the
    # channel defers delivery notes, the dispatch stays PENDING and its DN is cut
    # later in bulk from the SAP Delivery Notes page.
    if not settings_service.is_defer_delivery_note(order.company, dispatch.channel):
        _try_post_delivery_note(dispatch, user, resolved=resolved)
    return dispatch


def retry_delivery_note(dispatch, *, user):
    """Re-attempt posting the SAP delivery note for a confirmed dispatch."""
    if dispatch.status != MarketplaceDispatchStatus.CONFIRMED:
        raise MarketplaceError(
            "Only a confirmed dispatch can post a delivery note.",
            code="INVALID_STATE", status_code=409,
        )
    if dispatch.sap_post_status == MarketplaceSapPostStatus.POSTED:
        return dispatch  # already posted
    _try_post_delivery_note(dispatch, user)
    dispatch.refresh_from_db()
    return dispatch


def _try_post_delivery_note(dispatch, user, resolved=None):
    """Post DN+GI+billing; on any failure flag FAILED (so work isn't blocked)."""
    try:
        _post_delivery_note(dispatch, user, resolved=resolved)
    except Exception as exc:  # noqa: BLE001 — deliberate: dispatch must not be blocked
        logger.warning("Marketplace DN post failed for dispatch %s: %s", dispatch.pk, exc)
        dispatch.sap_post_status = MarketplaceSapPostStatus.FAILED
        dispatch.sap_error = str(exc)[:2000]
        dispatch.save(update_fields=["sap_post_status", "sap_error", "updated_at"])


def _post_delivery_note(dispatch, user, resolved=None):
    """Post the SAP Delivery Note (FG) + Goods Issue (PM), then internal billing.

    Idempotent per SAP document: each document's identifiers are persisted the
    instant it is created, so a failure part-way through never re-creates a
    document that already exists on a retry (which would double-decrement stock).

    SAP writes run OUTSIDE any DB transaction — external HTTP calls are not
    rollback-able, so we never hold a DB transaction open across them.
    """
    order = dispatch.order
    if resolved is None:
        resolved = _resolve_dispatch(dispatch)
    all_lines = resolved["resolved_lines"]
    warehouse = _warehouse_for(dispatch)  # raises NO_WAREHOUSE → caught as a failed post
    gateway = MarketplaceSapGateway(dispatch.company.code)
    gateway.verify_stock(all_lines, warehouse.sap_warehouse_code)
    doc_date = timezone.localdate()

    # Delivery-note posting config comes from the warehouse master (ops-editable).
    series = warehouse.sap_series
    tax_code = warehouse.sap_tax_code
    # GST place of supply for THIS order's destination state. The bulk cut groups by
    # this; a single-order confirm has one order, so it just resolves its own. Without
    # it a one-at-a-time confirm silently posted to the customer's default address
    # while the same order cut in bulk went to the state's address.
    ship_to_code = _shipto_for_state(order.state, warehouse)

    # 1) Delivery Note (FG). Skip if a prior attempt already created it.
    fg = fg_lines(all_lines)
    if fg and not dispatch.sap_delivery_note_doc_entry:
        dn = gateway.create_delivery_note(
            ref=dispatch.pk, card_code=warehouse.sap_customer_card_code,
            warehouse_code=warehouse.sap_warehouse_code, fg_lines=fg, doc_date=doc_date,
            num_at_card=order.order_id,
            comments=f"Marketplace {dispatch.channel} dispatch {dispatch.pk} · order {order.order_id}",
            series=series, tax_code=tax_code, branch_id=warehouse.sap_branch_id,
            ship_to_code=ship_to_code,
        )
        # Persist immediately — before the Goods Issue — so a GI failure can never
        # re-create this Delivery Note on retry.
        dispatch.sap_delivery_note_doc_entry = dn["DocEntry"]
        dispatch.sap_delivery_note_num = dn["DocNum"]
        dispatch.sap_ship_to_code = ship_to_code
        dispatch.save(update_fields=[
            "sap_delivery_note_doc_entry", "sap_delivery_note_num",
            "sap_ship_to_code", "updated_at",
        ])

    # 2) Goods Issue (PM consumption) — only if the master enables it. Skip if a
    #    prior attempt already created it.
    pm = pm_lines(all_lines)
    if warehouse.post_goods_issue and pm and not dispatch.sap_goods_issue_doc_entry:
        gi = gateway.create_goods_issue(
            ref=dispatch.pk, warehouse_code=warehouse.sap_warehouse_code,
            pm_lines=pm, doc_date=doc_date, num_at_card=order.order_id,
            comments=f"Marketplace {dispatch.channel} dispatch {dispatch.pk} PM · order {order.order_id}",
            series=series, branch_id=warehouse.sap_branch_id,
        )
        dispatch.sap_goods_issue_doc_entry = gi["DocEntry"]
        dispatch.sap_goods_issue_num = gi["DocNum"]
        dispatch.save(update_fields=[
            "sap_goods_issue_doc_entry", "sap_goods_issue_num", "updated_at",
        ])

    # 3) Internal billing + mark POSTED — local only, so a short transaction.
    with transaction.atomic():
        billing = dispatch.internal_billing
        if billing is None:
            # Bill only the parcels this dispatch shipped. On a split shipment the
            # rest are billed by their own confirm, so the order is never billed twice
            # for the same box.
            total_amount = sum(
                (Decimal(l.invoice_amount) for l in dispatch_lines(dispatch)), Decimal("0"),
            )
            billing = MarketplaceOrderBilling.objects.create(
                company=dispatch.company, channel=dispatch.channel, order_id=order.order_id,
                invoice_number=_next_invoice_number(dispatch.company), buyer_name=order.buyer_name,
                sap_delivery_note_doc_entry=dispatch.sap_delivery_note_doc_entry,
                sap_delivery_note_num=dispatch.sap_delivery_note_num,
                total_amount=total_amount, status=MarketplaceBillingStatus.CONFIRMED, created_by=user,
            )
        dispatch.internal_billing = billing
        dispatch.sap_post_status = MarketplaceSapPostStatus.POSTED
        dispatch.sap_error = ""
        dispatch.updated_by = user
        dispatch.save(update_fields=[
            "internal_billing", "sap_post_status", "sap_error", "updated_by", "updated_at",
        ])


def _u(code):
    return (code or "").strip().upper()


def _d(value):
    return Decimal(value)
