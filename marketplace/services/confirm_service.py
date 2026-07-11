"""Atomic confirm for an outward marketplace dispatch.

One transaction: validate scan completeness → re-expand combos from masters →
verify stock → SAP Delivery Note (FG, decrements stock) → SAP Goods Issue (PM) →
internal billing document → stamp the dispatch/order. Any SAP failure rolls back.
"""
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from ..models import (
    MarketplaceBillingStatus,
    MarketplaceDispatchStatus,
    MarketplaceOrderBilling,
    MarketplaceOrderStatus,
    MarketplaceWarehouse,
)
from .dispatch_gate import order_is_issued
from .errors import MarketplaceError
from .resolve_service import fg_lines, pm_lines, resolve_order
from .sap_gateway import MarketplaceSapGateway
from .scan_service import build_progress, is_fully_scanned


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


@transaction.atomic
def confirm_dispatch(dispatch, *, user, override_deviation=False, remarks=""):
    if dispatch.status == MarketplaceDispatchStatus.CONFIRMED:
        return dispatch  # idempotent
    if dispatch.status == MarketplaceDispatchStatus.CANCELLED:
        raise MarketplaceError("Dispatch is cancelled.", code="INVALID_STATE", status_code=409)

    order = dispatch.order
    if order.is_cancelled:
        raise MarketplaceError(
            "Order is cancelled on the marketplace; cannot dispatch.",
            code="ORDER_CANCELLED", status_code=409,
        )
    if not order_is_issued(order):
        raise MarketplaceError(
            "This order's materials have not been issued from the warehouse yet.",
            code="NOT_ISSUED", status_code=409,
        )
    resolved = resolve_order(order)
    if resolved["unmapped_skus"]:
        raise MarketplaceError(
            "Order has unmapped SKUs; add mappings in Masters before confirming.",
            code="UNMAPPED_SKUS", status_code=409, detail=resolved["unmapped_skus"],
        )

    all_lines = resolved["resolved_lines"]
    flines = fg_lines(all_lines)
    scanned_map = {}
    for ic, q in dispatch.scans.filter(is_active=True).values_list("item_code", "quantity"):
        k = _u(ic)
        scanned_map[k] = scanned_map.get(k, Decimal("0")) + _d(q)
    progress = build_progress(flines, scanned_map)
    deviating = [r for r in progress if r["status"] in ("UNDER", "OVER")]
    if deviating and not override_deviation:
        raise MarketplaceError(
            "Scan counts deviate from the order.",
            code="SCAN_DEVIATION", status_code=409, detail=deviating,
        )

    warehouse = _warehouse_for(dispatch)
    gateway = MarketplaceSapGateway(dispatch.company.code)
    gateway.verify_stock(all_lines, warehouse.sap_warehouse_code)

    doc_date = timezone.localdate()
    dn = gateway.create_delivery_note(
        ref=dispatch.pk,
        card_code=warehouse.sap_customer_card_code,
        warehouse_code=warehouse.sap_warehouse_code,
        fg_lines=flines,
        doc_date=doc_date,
    )
    gateway.create_goods_issue(
        ref=dispatch.pk,
        warehouse_code=warehouse.sap_warehouse_code,
        pm_lines=pm_lines(all_lines),
        doc_date=doc_date,
    )

    total_amount = sum(
        (Decimal(a) for a in order.lines.values_list("invoice_amount", flat=True)),
        Decimal("0"),
    )
    billing = MarketplaceOrderBilling.objects.create(
        company=dispatch.company,
        channel=dispatch.channel,
        order_id=order.order_id,
        invoice_number=_next_invoice_number(dispatch.company),
        buyer_name=order.buyer_name,
        sap_delivery_note_doc_entry=dn["DocEntry"],
        sap_delivery_note_num=dn["DocNum"],
        total_amount=total_amount,
        status=MarketplaceBillingStatus.CONFIRMED,
        created_by=user,
    )

    dispatch.status = MarketplaceDispatchStatus.CONFIRMED
    dispatch.sap_delivery_note_doc_entry = dn["DocEntry"]
    dispatch.sap_delivery_note_num = dn["DocNum"]
    dispatch.internal_billing = billing
    dispatch.confirmed_by = user
    dispatch.confirmed_at = timezone.now()
    dispatch.updated_by = user
    dispatch.save()

    order.status = MarketplaceOrderStatus.DISPATCHED
    order.save(update_fields=["status", "updated_at"])
    return dispatch


def _u(code):
    return (code or "").strip().upper()


def _d(value):
    return Decimal(value)
