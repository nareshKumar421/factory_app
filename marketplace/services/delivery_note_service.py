"""Bulk SAP Delivery Note for marketplace dispatches.

Normally each confirmed dispatch posts its own SAP Delivery Note. When a channel
turns on ``defer_delivery_note`` (see settings_service), confirmed dispatches wait
with ``sap_post_status=PENDING`` and their delivery notes are cut together here:

  * :func:`build_bulk_summary` previews exactly what will be sent — the combined
    line items, the SAP customer + warehouse, totals, and any blocked dispatches.
  * :func:`cut_bulk_delivery_note` posts ONE SAP Delivery Note covering every
    included dispatch's finished goods in a single request, then records that
    document on each dispatch and writes their internal billing.

All marketplace dispatches on a channel share the warehouse master's SAP customer
(CardCode) and godown, so one document validly covers them all.
"""
import logging
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from ..models import (
    MarketplaceBillingStatus,
    MarketplaceDispatch,
    MarketplaceDispatchStatus,
    MarketplaceOrderBilling,
    MarketplaceSapPostStatus,
)
from .confirm_service import _next_invoice_number, _warehouse_for
from .dispatch_gate import order_dispatch_ready
from .errors import MarketplaceError
from .resolve_service import fg_lines, pm_lines, resolve_order
from .sap_gateway import MarketplaceSapGateway

logger = logging.getLogger(__name__)


def awaiting_dispatches(company, channel):
    """Confirmed dispatches that still need a SAP Delivery Note, newest first."""
    qs = (
        MarketplaceDispatch.objects.filter(
            company=company,
            status=MarketplaceDispatchStatus.CONFIRMED,
            sap_delivery_note_doc_entry__isnull=True,
        )
        .exclude(sap_post_status=MarketplaceSapPostStatus.POSTED)
        .select_related("order")
        .order_by("-confirmed_at", "-id")
    )
    if channel:
        qs = qs.filter(channel=channel)
    return qs


def _merge_lines(rows):
    """Merge resolved lines by (item_code, warehouse_code), summing quantity."""
    merged = {}
    for line in rows:
        key = (line["item_code"], line.get("warehouse_code") or "")
        cur = merged.get(key)
        if cur is None:
            merged[key] = {
                "item_code": line["item_code"],
                "item_name": line["item_name"],
                "uom": line["uom"],
                "warehouse_code": line.get("warehouse_code") or "",
                "quantity": Decimal(line["required_quantity"]),
            }
        else:
            cur["quantity"] += Decimal(line["required_quantity"])
            if not cur["item_name"] and line["item_name"]:
                cur["item_name"] = line["item_name"]
    return list(merged.values())


def _collect(company, channel, dispatch_ids=None):
    """Split awaiting dispatches into (includable, blocked).

    ``includable`` is a list of dicts ``{dispatch, fg, pm, amount}``; ``blocked``
    is ``{order_id, dispatch_id, reason}`` for dispatches that cannot be posted.
    """
    qs = awaiting_dispatches(company, channel)
    if dispatch_ids:
        qs = qs.filter(id__in=dispatch_ids)

    includable, blocked = [], []
    for dispatch in qs:
        order = dispatch.order
        if order.is_cancelled:
            blocked.append({"order_id": order.order_id, "dispatch_id": dispatch.id,
                            "reason": "Order cancelled on the marketplace."})
            continue
        if not order_dispatch_ready(order):
            blocked.append({"order_id": order.order_id, "dispatch_id": dispatch.id,
                            "reason": "Order is not ready to dispatch (not packed / not issued)."})
            continue
        resolved = resolve_order(order)
        if resolved["unmapped_skus"]:
            blocked.append({"order_id": order.order_id, "dispatch_id": dispatch.id,
                            "reason": "Order has unmapped SKUs."})
            continue
        amount = sum(
            (Decimal(a) for a in order.lines.values_list("invoice_amount", flat=True)),
            Decimal("0"),
        )
        includable.append({
            "dispatch": dispatch,
            "fg": fg_lines(resolved["resolved_lines"]),
            "pm": pm_lines(resolved["resolved_lines"]),
            "amount": amount,
        })
    return includable, blocked


def build_bulk_summary(company, channel, dispatch_ids=None):
    """Preview the combined delivery note without posting anything."""
    includable, blocked = _collect(company, channel, dispatch_ids)

    card_code = warehouse_code = ""
    post_goods_issue = False
    if includable:
        # Every dispatch on a channel resolves to the same warehouse master.
        warehouse = _warehouse_for(includable[0]["dispatch"])
        card_code = warehouse.sap_customer_card_code
        warehouse_code = warehouse.sap_warehouse_code
        post_goods_issue = warehouse.post_goods_issue

    fg = _merge_lines([l for item in includable for l in item["fg"]])
    pm = _merge_lines([l for item in includable for l in item["pm"]]) if post_goods_issue else []

    dispatches = [{
        "dispatch_id": item["dispatch"].id,
        "order_id": item["dispatch"].order.order_id,
        "buyer_name": item["dispatch"].order.buyer_name,
        "fg_line_count": len(item["fg"]),
        "amount": str(item["amount"]),
    } for item in includable]

    return {
        "channel": channel,
        "card_code": card_code,
        "warehouse_code": warehouse_code,
        "doc_date": timezone.localdate().isoformat(),
        "post_goods_issue": post_goods_issue,
        "dispatches": dispatches,
        "fg_lines": [{**l, "quantity": str(l["quantity"])} for l in fg],
        "pm_lines": [{**l, "quantity": str(l["quantity"])} for l in pm],
        "blocked": blocked,
        "totals": {
            "dispatch_count": len(dispatches),
            "fg_item_count": len(fg),
            "fg_total_quantity": str(sum((l["quantity"] for l in fg), Decimal("0"))),
            "total_amount": str(sum((item["amount"] for item in includable), Decimal("0"))),
        },
    }


def cut_bulk_delivery_note(company, channel, *, dispatch_ids=None, user=None):
    """Post ONE SAP Delivery Note for all included dispatches (single request).

    Records the document on every dispatch, posts a single bulk Goods Issue for
    packing material when enabled, writes per-order internal billing, and marks
    each dispatch POSTED. SAP writes run outside any DB transaction.
    """
    includable, _blocked = _collect(company, channel, dispatch_ids)
    if not includable:
        raise MarketplaceError(
            "No confirmed dispatches are awaiting a delivery note.",
            code="EMPTY", status_code=400,
        )

    dispatches = [item["dispatch"] for item in includable]
    warehouse = _warehouse_for(dispatches[0])
    gateway = MarketplaceSapGateway(company.code)
    doc_date = timezone.localdate()
    order_ids = [d.order.order_id for d in dispatches]
    ref = dispatches[0].pk  # a stable numeric ref for the (simulated) document

    all_fg = [l for item in includable for l in item["fg"]]
    all_pm = [l for item in includable for l in item["pm"]]
    gateway.verify_stock(all_fg + all_pm, warehouse.sap_warehouse_code)

    comments = (
        f"Marketplace {channel} bulk delivery note · {len(dispatches)} orders: "
        f"{', '.join(order_ids[:20])}{' …' if len(order_ids) > 20 else ''}"
    )

    # 1) ONE Delivery Note for all finished goods across every dispatch.
    dn = gateway.create_delivery_note(
        ref=ref, card_code=warehouse.sap_customer_card_code,
        warehouse_code=warehouse.sap_warehouse_code, fg_lines=_merge_lines(all_fg),
        doc_date=doc_date, num_at_card=f"MKT-BULK-{doc_date:%Y%m%d}",
        comments=comments, series=warehouse.sap_series, tax_code=warehouse.sap_tax_code,
    )
    dn_doc_entry, dn_num = dn["DocEntry"], dn["DocNum"]

    # Persist the DN on every dispatch immediately so a later failure never
    # re-cuts it for the same dispatches on a retry.
    for d in dispatches:
        d.sap_delivery_note_doc_entry = dn_doc_entry
        d.sap_delivery_note_num = dn_num
        d.save(update_fields=[
            "sap_delivery_note_doc_entry", "sap_delivery_note_num", "updated_at",
        ])

    # 2) ONE bulk Goods Issue for packing material (if the master enables it).
    if warehouse.post_goods_issue and _merge_lines(all_pm):
        gi = gateway.create_goods_issue(
            ref=ref, warehouse_code=warehouse.sap_warehouse_code,
            pm_lines=_merge_lines(all_pm), doc_date=doc_date,
            num_at_card=f"MKT-BULK-{doc_date:%Y%m%d}",
            comments=f"{comments} · packing material", series=warehouse.sap_series,
        )
        for d in dispatches:
            d.sap_goods_issue_doc_entry = gi["DocEntry"]
            d.sap_goods_issue_num = gi["DocNum"]
            d.save(update_fields=[
                "sap_goods_issue_doc_entry", "sap_goods_issue_num", "updated_at",
            ])

    # 3) Per-order internal billing + mark each dispatch POSTED (local only).
    with transaction.atomic():
        for item in includable:
            d = item["dispatch"]
            order = d.order
            if d.internal_billing is None:
                d.internal_billing = MarketplaceOrderBilling.objects.create(
                    company=company, channel=d.channel, order_id=order.order_id,
                    invoice_number=_next_invoice_number(company), buyer_name=order.buyer_name,
                    sap_delivery_note_doc_entry=dn_doc_entry, sap_delivery_note_num=dn_num,
                    total_amount=item["amount"], status=MarketplaceBillingStatus.CONFIRMED,
                    created_by=user,
                )
            d.sap_post_status = MarketplaceSapPostStatus.POSTED
            d.sap_error = ""
            d.updated_by = user
            d.save(update_fields=[
                "internal_billing", "sap_post_status", "sap_error", "updated_by", "updated_at",
            ])

    return {
        "delivery_note_num": dn_num,
        "delivery_note_doc_entry": dn_doc_entry,
        "dispatch_count": len(dispatches),
        "order_ids": order_ids,
    }
