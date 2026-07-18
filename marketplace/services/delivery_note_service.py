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
    """Confirmed dispatches that still need a SAP Delivery Note, newest first.

    Excludes POSTED and AWAITING_APPROVAL — a dispatch whose delivery note is
    already sitting in SAP's approval queue must not be cut again (that is what
    piled up duplicate drafts).
    """
    qs = (
        MarketplaceDispatch.objects.filter(
            company=company,
            status=MarketplaceDispatchStatus.CONFIRMED,
            sap_delivery_note_doc_entry__isnull=True,
        )
        .exclude(sap_post_status__in=[
            MarketplaceSapPostStatus.POSTED,
            MarketplaceSapPostStatus.AWAITING_APPROVAL,
        ])
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
                # Keep the same key the raw resolved lines use so merged lines are
                # drop-in compatible with the SAP gateway (which reads
                # ``required_quantity``); the summary serializes this as ``quantity``.
                "required_quantity": Decimal(line["required_quantity"]),
            }
        else:
            cur["required_quantity"] += Decimal(line["required_quantity"])
            if not cur["item_name"] and line["item_name"]:
                cur["item_name"] = line["item_name"]
    return list(merged.values())


def _summary_line(line):
    """Serialize a merged line for the summary API (quantity as a string)."""
    return {
        "item_code": line["item_code"],
        "item_name": line["item_name"],
        "uom": line["uom"],
        "warehouse_code": line["warehouse_code"],
        "quantity": str(line["required_quantity"]),
    }


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


def _available_onhand(company_code, item_codes, warehouse_code):
    """``{item_code: Decimal on-hand}`` in a warehouse (SAP OITW). Best-effort:
    returns ``{}`` when HANA is unavailable, so callers treat stock as unknown and
    do not hold anything."""
    codes = [c for c in {(c or "").strip() for c in item_codes} if c]
    if not codes or not warehouse_code:
        return {}
    try:
        from hdbcli import dbapi
        from sap_client.context import CompanyContext
        h = CompanyContext(company_code).hana
    except Exception as e:  # pragma: no cover - env specific
        logger.warning("On-hand lookup unavailable (%s)", e)
        return {}
    ph = ",".join(["?"] * len(codes))
    sql = (
        f'SELECT "ItemCode","OnHand" FROM "{h["schema"]}"."OITW" '
        f'WHERE "WhsCode"=? AND "ItemCode" IN ({ph})'
    )
    out, conn = {}, None
    try:
        conn = dbapi.connect(address=h["host"], port=int(h["port"]), user=h["user"],
                             password=h["password"], encrypt=True, sslValidateCertificate=False)
        cur = conn.cursor()
        cur.execute(sql, [warehouse_code, *codes])
        for item, onhand in cur.fetchall():
            out[item] = Decimal(str(onhand))
        cur.close()
    except Exception as e:  # pragma: no cover - env specific
        logger.warning("On-hand query failed (%s)", e)
        return {}
    finally:
        if conn is not None:
            conn.close()
    return out


def _partition_by_stock(company_code, includable, warehouse_code):
    """Split ``includable`` into (fulfillable, held) by real SAP on-hand.

    Because the bulk delivery note is ONE all-or-nothing document, a single
    stock-short line would fail the whole post. This greedily includes each
    dispatch only while its finished-goods demand still fits the warehouse's
    remaining on-hand (dispatches share stock), and holds the rest with a reason.
    When on-hand can't be read (HANA down) nothing is held — the post proceeds and
    SAP remains the final arbiter.
    """
    items = {l["item_code"] for it in includable for l in it["fg"]}
    onhand = _available_onhand(company_code, items, warehouse_code)
    if not onhand:
        return includable, []
    remaining = dict(onhand)
    fulfillable, held = [], []
    for it in includable:
        need = {}
        for l in it["fg"]:
            need[l["item_code"]] = need.get(l["item_code"], Decimal("0")) + Decimal(l["required_quantity"])
        short = [i for i, q in need.items() if remaining.get(i, Decimal("0")) < q]
        if short:
            d = it["dispatch"]
            held.append({
                "order_id": d.order.order_id, "dispatch_id": d.id,
                "reason": "Insufficient stock: " + ", ".join(sorted(short)),
            })
        else:
            for i, q in need.items():
                remaining[i] -= q
            fulfillable.append(it)
    return fulfillable, held


def _channel_warehouses(company, channel):
    """Active warehouse masters for a channel, default-first then by id."""
    from ..models import MarketplaceWarehouse

    return list(
        MarketplaceWarehouse.objects.filter(
            company=company, channel=channel, is_active=True
        ).order_by("-is_default", "id")
    )


def resolve_cut_warehouse(company, channel, warehouse_id=None):
    """The warehouse master to post the delivery note against.

    Honours an explicit ``warehouse_id`` (the operator's choice at cut time),
    otherwise the channel's default (``is_default``), otherwise the first active
    one. Raises NO_WAREHOUSE if none are configured.
    """
    warehouses = _channel_warehouses(company, channel)
    if not warehouses:
        raise MarketplaceError(
            f"No active marketplace warehouse configured for {channel}.",
            code="NO_WAREHOUSE", status_code=409,
        )
    if warehouse_id:
        wh = next((w for w in warehouses if w.id == int(warehouse_id)), None)
        if wh is None:
            raise MarketplaceError(
                "Selected warehouse is not available for this channel.",
                code="BAD_WAREHOUSE", status_code=400,
            )
        return wh
    return warehouses[0]  # default-first ordering


def build_bulk_summary(company, channel, dispatch_ids=None, warehouse_id=None):
    """Preview the combined delivery note without posting anything.

    ``warehouse_id`` selects which warehouse master to post against; when omitted
    the channel default is used. The full list of warehouse options is returned so
    the operator can switch at cut time.
    """
    includable, blocked = _collect(company, channel, dispatch_ids)

    warehouse_options = _channel_warehouses(company, channel)
    card_code = warehouse_code = ""
    post_goods_issue = False
    selected_id = None
    if warehouse_options:
        warehouse = resolve_cut_warehouse(company, channel, warehouse_id)
        selected_id = warehouse.id
        card_code = warehouse.sap_customer_card_code
        warehouse_code = warehouse.sap_warehouse_code
        post_goods_issue = warehouse.post_goods_issue

    # Preview only the dispatches the warehouse can actually fulfil; hold the rest
    # (matches what cut_bulk_delivery_note posts). Best-effort — all included when
    # on-hand can't be read.
    held_for_stock = []
    if warehouse_code:
        includable, held_for_stock = _partition_by_stock(
            company.code, includable, warehouse_code)

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
        "warehouse_id": selected_id,
        "warehouses": [
            {"id": w.id, "name": w.name, "sap_warehouse_code": w.sap_warehouse_code,
             "sap_customer_card_code": w.sap_customer_card_code, "is_default": w.is_default}
            for w in warehouse_options
        ],
        "doc_date": timezone.localdate().isoformat(),
        "post_goods_issue": post_goods_issue,
        "dispatches": dispatches,
        "fg_lines": [_summary_line(l) for l in fg],
        "pm_lines": [_summary_line(l) for l in pm],
        "blocked": blocked,
        "held_for_stock": held_for_stock,
        "totals": {
            "dispatch_count": len(dispatches),
            "fg_item_count": len(fg),
            "fg_total_quantity": str(sum((l["required_quantity"] for l in fg), Decimal("0"))),
            "total_amount": str(sum((item["amount"] for item in includable), Decimal("0"))),
        },
    }


def cut_bulk_delivery_note(company, channel, *, dispatch_ids=None, warehouse_id=None, user=None):
    """Post ONE SAP Delivery Note for all included dispatches (single request).

    ``warehouse_id`` selects the warehouse master to post against (the operator's
    choice at cut time); the channel default is used when omitted. Records the
    document on every dispatch, posts a single bulk Goods Issue for packing
    material when enabled, writes per-order internal billing, and marks each
    dispatch POSTED. SAP writes run outside any DB transaction.
    """
    includable, _blocked = _collect(company, channel, dispatch_ids)
    if not includable:
        raise MarketplaceError(
            "No confirmed dispatches are awaiting a delivery note.",
            code="EMPTY", status_code=400,
        )

    warehouse = resolve_cut_warehouse(company, channel, warehouse_id)
    gateway = MarketplaceSapGateway(company.code)

    # Hold dispatches the warehouse can't physically fulfil so one stock-short
    # order can't fail the whole bulk document (SAP -10 negative inventory).
    held_for_stock = []
    if not gateway.simulate:
        includable, held_for_stock = _partition_by_stock(
            company.code, includable, warehouse.sap_warehouse_code)
        if not includable:
            raise MarketplaceError(
                "Every awaiting dispatch is blocked by insufficient stock — "
                "nothing to post yet.",
                code="NO_STOCK", status_code=409,
            )

    dispatches = [item["dispatch"] for item in includable]
    doc_date = timezone.localdate()
    order_ids = [d.order.order_id for d in dispatches]
    ref = dispatches[0].pk  # a stable numeric ref for the (simulated) document

    all_fg = [l for item in includable for l in item["fg"]]
    all_pm = [l for item in includable for l in item["pm"]]
    gateway.verify_stock(all_fg + all_pm, warehouse.sap_warehouse_code)

    num_at_card = f"MKT-{doc_date:%Y%m%d}-{ref}"  # unique ref to reconcile the approved DN
    # SAP's Document.Comments is capped at 254 chars — keep it a short summary, not
    # a list of every order id (that overflowed with hundreds of orders).
    comments = f"Marketplace {channel} bulk delivery note · {len(dispatches)} orders · {doc_date:%Y-%m-%d}"

    # 1) ONE Delivery Note for all finished goods across every dispatch.
    dn = gateway.create_delivery_note(
        ref=ref, card_code=warehouse.sap_customer_card_code,
        warehouse_code=warehouse.sap_warehouse_code, fg_lines=_merge_lines(all_fg),
        doc_date=doc_date, num_at_card=num_at_card,
        comments=comments, series=warehouse.sap_series, tax_code=warehouse.sap_tax_code,
        branch_id=warehouse.sap_branch_id,
    )

    # SAP routed the delivery note into an approval process → it is saved as a
    # DRAFT awaiting approval, not posted. Record the draft on every dispatch and
    # stop: no Goods Issue, no billing, not POSTED. AWAITING_APPROVAL keeps these
    # dispatches out of the awaiting list so a re-cut can't create duplicate drafts.
    # They are finalized by reconcile_approved_delivery_notes() once approved.
    if dn.get("pending_approval"):
        with transaction.atomic():
            for d in dispatches:
                d.sap_delivery_note_draft_entry = dn.get("draft_entry")
                d.sap_dn_ref = num_at_card
                d.sap_post_status = MarketplaceSapPostStatus.AWAITING_APPROVAL
                d.sap_error = ""
                d.updated_by = user
                d.save(update_fields=[
                    "sap_delivery_note_draft_entry", "sap_dn_ref", "sap_post_status",
                    "sap_error", "updated_by", "updated_at",
                ])
        return {
            "pending_approval": True,
            "draft_entry": dn.get("draft_entry"),
            "delivery_note_num": "",
            "delivery_note_doc_entry": None,
            "dispatch_count": len(dispatches),
            "order_ids": order_ids,
            "held_for_stock": held_for_stock,
        }

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
            num_at_card=num_at_card,
            comments=f"{comments} · packing material", series=warehouse.sap_series,
            branch_id=warehouse.sap_branch_id,
        )
        for d in dispatches:
            d.sap_goods_issue_doc_entry = gi["DocEntry"]
            d.sap_goods_issue_num = gi["DocNum"]
            d.save(update_fields=[
                "sap_goods_issue_doc_entry", "sap_goods_issue_num", "updated_at",
            ])

    # 3) Per-order internal billing + mark each dispatch POSTED (local only).
    _finalize_posted(includable, company, dn_doc_entry, dn_num, user)

    return {
        "delivery_note_num": dn_num,
        "delivery_note_doc_entry": dn_doc_entry,
        "dispatch_count": len(dispatches),
        "order_ids": order_ids,
        "held_for_stock": held_for_stock,
    }


def _finalize_posted(includable, company, dn_doc_entry, dn_num, user):
    """Write per-order internal billing and mark each dispatch POSTED (local only)."""
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
            d.sap_delivery_note_doc_entry = dn_doc_entry
            d.sap_delivery_note_num = dn_num
            d.sap_post_status = MarketplaceSapPostStatus.POSTED
            d.sap_error = ""
            d.updated_by = user
            d.save(update_fields=[
                "internal_billing", "sap_delivery_note_doc_entry", "sap_delivery_note_num",
                "sap_post_status", "sap_error", "updated_by", "updated_at",
            ])


def _order_amount(order):
    """Total invoice amount for an order (sum of its line invoice_amounts)."""
    return sum(
        (Decimal(a) for a in order.lines.values_list("invoice_amount", flat=True)),
        Decimal("0"),
    )


def reconcile_approved_delivery_notes(company, channel=None, user=None):
    """Finalize dispatches whose delivery note was AWAITING_APPROVAL.

    For each approval batch (grouped by the NumAtCard ref), ask SAP whether the
    approved delivery note now exists (approved → posted). If so, record the real
    document, write per-order billing, and mark POSTED. If the approval was
    rejected, mark FAILED so the operator can cut it again. Otherwise leave it
    pending. Returns ``{finalized, rejected, still_pending}``.
    """
    qs = MarketplaceDispatch.objects.filter(
        company=company, sap_post_status=MarketplaceSapPostStatus.AWAITING_APPROVAL,
    ).select_related("order")
    if channel:
        qs = qs.filter(channel=channel)

    groups = {}
    for d in qs:
        groups.setdefault(d.sap_dn_ref or "", []).append(d)

    gateway = MarketplaceSapGateway(company.code)
    finalized, rejected, still_pending = [], [], 0
    for ref, ds in groups.items():
        if not ref:
            still_pending += len(ds)
            continue
        dn = gateway.find_delivery_note_by_ref(ref)
        if dn and dn.get("DocEntry"):
            includable = [{"dispatch": d, "amount": _order_amount(d.order)} for d in ds]
            _finalize_posted(includable, company, dn["DocEntry"], dn["DocNum"], user)
            finalized.extend(d.order.order_id for d in ds)
        elif gateway.draft_rejected(ds[0].sap_delivery_note_draft_entry):
            for d in ds:
                d.sap_post_status = MarketplaceSapPostStatus.FAILED
                d.sap_delivery_note_draft_entry = None
                d.sap_dn_ref = ""
                d.sap_error = "SAP approval was rejected — cut the delivery note again."
                d.updated_by = user
                d.save(update_fields=[
                    "sap_post_status", "sap_delivery_note_draft_entry", "sap_dn_ref",
                    "sap_error", "updated_by", "updated_at",
                ])
            rejected.extend(d.order.order_id for d in ds)
        else:
            still_pending += len(ds)
    return {"finalized": finalized, "rejected": rejected, "still_pending": still_pending}
