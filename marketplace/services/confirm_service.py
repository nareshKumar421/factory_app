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
from .resolve_service import fg_lines, pm_lines, resolve_order
from .sap_gateway import MarketplaceSapGateway
from .scan_service import build_progress, is_fully_scanned
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
    wh = (
        MarketplaceWarehouse.objects.filter(
            company=dispatch.company, channel=dispatch.channel, is_active=True
        )
        .order_by("id")
        .first()
    )
    if wh is None:
        raise MarketplaceError(
            f"No active marketplace warehouse configured for {dispatch.channel}.",
            code="NO_WAREHOUSE", status_code=409,
        )
    return wh


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
    resolved = resolve_order(order)
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
    # The order is verified at Packing (single Tracking-ID scan), so item scans at
    # Outward are optional. Only enforce the scan/order deviation check when the
    # operator actually scanned items here; otherwise trust the packed order.
    deviating = [r for r in build_progress(flines, scanned_map) if r["status"] in ("UNDER", "OVER")]
    if deviating and scanned_map and not override_deviation:
        raise MarketplaceError(
            "Scan counts deviate from the order.",
            code="SCAN_DEVIATION", status_code=409, detail=deviating,
        )

    # Mark the order dispatched — this always persists, even if SAP is down.
    with transaction.atomic():
        dispatch.status = MarketplaceDispatchStatus.CONFIRMED
        dispatch.sap_post_status = MarketplaceSapPostStatus.PENDING
        dispatch.confirmed_by = user
        dispatch.confirmed_at = timezone.now()
        dispatch.updated_by = user
        dispatch.save()
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
        resolved = resolve_order(order)
    all_lines = resolved["resolved_lines"]
    warehouse = _warehouse_for(dispatch)  # raises NO_WAREHOUSE → caught as a failed post
    gateway = MarketplaceSapGateway(dispatch.company.code)
    gateway.verify_stock(all_lines, warehouse.sap_warehouse_code)
    doc_date = timezone.localdate()

    # Delivery-note posting config comes from the warehouse master (ops-editable).
    series = warehouse.sap_series
    tax_code = warehouse.sap_tax_code

    # 1) Delivery Note (FG). Skip if a prior attempt already created it.
    fg = fg_lines(all_lines)
    if fg and not dispatch.sap_delivery_note_doc_entry:
        dn = gateway.create_delivery_note(
            ref=dispatch.pk, card_code=warehouse.sap_customer_card_code,
            warehouse_code=warehouse.sap_warehouse_code, fg_lines=fg, doc_date=doc_date,
            num_at_card=order.order_id,
            comments=f"Marketplace {dispatch.channel} dispatch {dispatch.pk} · order {order.order_id}",
            series=series, tax_code=tax_code, branch_id=warehouse.sap_branch_id,
        )
        # Persist immediately — before the Goods Issue — so a GI failure can never
        # re-create this Delivery Note on retry.
        dispatch.sap_delivery_note_doc_entry = dn["DocEntry"]
        dispatch.sap_delivery_note_num = dn["DocNum"]
        dispatch.save(update_fields=[
            "sap_delivery_note_doc_entry", "sap_delivery_note_num", "updated_at",
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
            total_amount = sum(
                (Decimal(a) for a in order.lines.values_list("invoice_amount", flat=True)),
                Decimal("0"),
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
