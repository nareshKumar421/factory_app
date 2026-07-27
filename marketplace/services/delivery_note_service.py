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
from collections import OrderedDict
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


def _collect(company, channel, dispatch_ids=None, batch_id=None):
    """Split awaiting dispatches into (includable, blocked).

    ``includable`` is a list of dicts ``{dispatch, fg, pm, amount}``; ``blocked``
    is ``{order_id, dispatch_id, reason}`` for dispatches that cannot be posted.
    ``batch_id`` scopes the cut to one sheet (import batch); ``dispatch_ids`` to an
    explicit subset.
    """
    from .dispatch_gate import order_is_packed
    from .resolve_service import load_mappings, resolve_lines
    from .settings_service import is_skip_packing

    # Pull every order's lines (and their chosen variant) up front, and load the
    # SKU mappings + the skip-packing setting ONCE. Doing these per order turned a
    # few-hundred-dispatch cut into thousands of round trips to a remote database.
    qs = awaiting_dispatches(company, channel).prefetch_related(
        "order__lines", "order__lines__chosen_option__combo__components",
    )
    if batch_id:
        qs = qs.filter(order__import_batch_id=batch_id)
    if dispatch_ids:
        qs = qs.filter(id__in=dispatch_ids)

    mappings = load_mappings(company, channel)
    skip_packing = is_skip_packing(company, channel)

    includable, blocked = [], []
    for dispatch in qs:
        order = dispatch.order
        if order.is_cancelled:
            blocked.append({"order_id": order.order_id, "dispatch_id": dispatch.id,
                            "reason": "Order cancelled on the marketplace."})
            continue
        ready = True if skip_packing else order_is_packed(order)
        if not ready:
            blocked.append({"order_id": order.order_id, "dispatch_id": dispatch.id,
                            "reason": "Order is not ready to dispatch (not packed / not issued)."})
            continue
        lines = list(order.lines.all())  # prefetched — no query
        resolved = resolve_lines(lines, order.sap_warehouse_code or "", mappings)
        if resolved["unmapped_skus"]:
            blocked.append({"order_id": order.order_id, "dispatch_id": dispatch.id,
                            "reason": "Order has unmapped SKUs."})
            continue
        amount = sum((Decimal(l.invoice_amount) for l in lines), Decimal("0"))
        includable.append({
            "dispatch": dispatch,
            "fg": fg_lines(resolved["resolved_lines"]),
            "pm": pm_lines(resolved["resolved_lines"]),
            "amount": amount,
        })
    return includable, blocked


def _available_onhand(company_code, item_codes, warehouse_code):
    """``{item_code: Decimal on-hand}`` in a warehouse (SAP OITW). Best-effort;
    single source shared with the immediate-confirm stock check."""
    from .sap_gateway import oitw_onhand

    return oitw_onhand(company_code, item_codes, warehouse_code)


def _partition_by_stock(company_code, includable, warehouse_code, onhand=None):
    """Split ``includable`` into (fulfillable, held) by real SAP on-hand.

    Because the bulk delivery note is ONE all-or-nothing document, a single
    stock-short line would fail the whole post. This greedily includes each
    dispatch only while its finished-goods demand still fits the warehouse's
    remaining on-hand (dispatches share stock), and holds the rest with a reason.
    When on-hand can't be read (HANA down) nothing is held — the post proceeds and
    SAP remains the final arbiter.

    ``onhand`` may be passed in when the caller has already fetched it (the summary
    reuses one lookup for both the partition and the shortfall breakdown); when
    omitted it is fetched here so standalone callers keep working.
    """
    items = {l["item_code"] for it in includable for l in it["fg"]}
    if onhand is None:
        onhand = _available_onhand(company_code, items, warehouse_code)
    if not onhand:
        return includable, []
    remaining = dict(onhand)
    fulfillable, held = [], []
    for it in includable:
        # code -> {item_name, uom, qty} — carry the name/uom so the held card can
        # show a proper FG code + name (not just a bare code in a reason string).
        need = {}
        for l in it["fg"]:
            code = l["item_code"]
            row = need.get(code)
            if row is None:
                row = need[code] = {
                    "item_name": l.get("item_name", ""), "uom": l.get("uom", ""),
                    "qty": Decimal("0"),
                }
            row["qty"] += Decimal(l["required_quantity"])
            if not row["item_name"] and l.get("item_name"):
                row["item_name"] = l["item_name"]
        short_items = []
        for code in sorted(need):
            row = need[code]
            available = remaining.get(code, Decimal("0"))
            if available < row["qty"]:
                short_items.append({
                    "item_code": code,
                    "item_name": row["item_name"],
                    "uom": row["uom"],
                    "required_quantity": str(row["qty"]),
                    "available_quantity": str(max(available, Decimal("0"))),
                    "shortfall_quantity": str(row["qty"] - available),
                })
        if short_items:
            d = it["dispatch"]
            held.append({
                "order_id": d.order.order_id, "dispatch_id": d.id,
                "reason": "Insufficient stock: " + ", ".join(s["item_code"] for s in short_items),
                # Structured short lines (with FG names + how much is short) so the
                # held section can render a table instead of a code-only reason.
                "short_items": short_items,
            })
        else:
            for code, row in need.items():
                remaining[code] -= row["qty"]
            fulfillable.append(it)
    return fulfillable, held


def _stock_shortfall(includable, warehouse_code, onhand):
    """Per-FG-item top-up the warehouse must supply to ship every awaiting order.

    ``required`` sums the finished-goods demand of ALL includable dispatches;
    ``available`` is SAP on-hand; ``shortfall = required − available``. Only items
    that are actually short are returned (empty when on-hand is unknown), so the
    operator can request exactly the missing quantity from the warehouse.
    """
    if not warehouse_code or not onhand:
        return []
    demand = {}  # item_code -> {item_name, uom, required}
    for it in includable:
        for l in it["fg"]:
            code = l["item_code"]
            row = demand.get(code)
            if row is None:
                row = demand[code] = {
                    "item_name": l["item_name"], "uom": l["uom"], "required": Decimal("0"),
                }
            row["required"] += Decimal(l["required_quantity"])
            if not row["item_name"] and l["item_name"]:
                row["item_name"] = l["item_name"]
    out = []
    for code, row in demand.items():
        available = onhand.get(code, Decimal("0"))
        short = row["required"] - available
        if short > 0:
            out.append({
                "item_code": code,
                "item_name": row["item_name"],
                "uom": row["uom"],
                "warehouse_code": warehouse_code,
                "required_quantity": str(row["required"]),
                "available_quantity": str(available),
                "shortfall_quantity": str(short),
            })
    out.sort(key=lambda r: r["item_code"])
    return out


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


def list_dn_sheets(company, channel):
    """Sheets (import batches) that have dispatches awaiting a delivery note, each
    with its awaiting + already-posted counts, so the operator can post a delivery
    note per sheet and see progress. Newest sheet first."""
    from django.db.models import Count
    from ..models import OrderImportBatch

    awaiting = (
        awaiting_dispatches(company, channel)
        .order_by()  # clear the queryset's ordering so it doesn't leak into GROUP BY
        .values("order__import_batch_id")
        .annotate(n=Count("id"))
    )
    awaiting_by_batch = {
        r["order__import_batch_id"]: r["n"] for r in awaiting if r["order__import_batch_id"]
    }
    # Dispatches in these sheets that already carry a delivery note (posted).
    batch_ids = list(awaiting_by_batch)
    posted_by_batch = {}
    if batch_ids:
        posted = (
            MarketplaceDispatch.objects.filter(
                company=company, order__import_batch_id__in=batch_ids,
            )
            .exclude(sap_delivery_note_num="")
            .values("order__import_batch_id")
            .annotate(n=Count("id"))
        )
        if channel:
            # channel filter kept consistent with awaiting_dispatches
            posted = posted.filter(channel=channel)
        posted_by_batch = {r["order__import_batch_id"]: r["n"] for r in posted}

    if not batch_ids:
        return {"sheets": []}
    batches = OrderImportBatch.objects.filter(
        company=company, channel=channel, id__in=batch_ids
    ).order_by("-created_at")
    sheets = [{
        "id": b.id,
        "filename": b.filename,
        "status": b.status,
        "created_at": b.created_at.isoformat(),
        "awaiting_count": awaiting_by_batch.get(b.id, 0),
        "posted_count": posted_by_batch.get(b.id, 0),
    } for b in batches]
    return {"sheets": sheets}


def build_bulk_summary(company, channel, dispatch_ids=None, warehouse_id=None, batch_id=None):
    """Preview the combined delivery note without posting anything.

    ``warehouse_id`` selects which warehouse master to post against; when omitted
    the channel default is used. The full list of warehouse options is returned so
    the operator can switch at cut time.
    """
    includable, blocked = _collect(company, channel, dispatch_ids, batch_id=batch_id)

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

    # Keep a handle on every dispatch BEFORE the stock filter, so a held order can
    # still be enriched with its variant choices (that's the only way the operator
    # can switch it to an item that IS in stock).
    _pre_partition = {it["dispatch"].id: it for it in includable}

    # Preview only the dispatches the warehouse can actually fulfil; hold the rest
    # (matches what cut_bulk_delivery_note posts). Best-effort — all included when
    # on-hand can't be read.
    held_for_stock = []
    stock_shortfall = []
    if warehouse_code:
        # One on-hand lookup feeds both the include/hold partition and the
        # shortfall breakdown, so the summary hits HANA once, not twice.
        all_fg_items = {l["item_code"] for it in _pre_partition.values() for l in it["fg"]}
        onhand = _available_onhand(company.code, all_fg_items, warehouse_code)
        includable, held_for_stock = _partition_by_stock(
            company.code, includable, warehouse_code, onhand)
        stock_shortfall = _stock_shortfall(
            list(_pre_partition.values()), warehouse_code, onhand)

    fg = _merge_lines([l for item in includable for l in item["fg"]])
    pm = _merge_lines([l for item in includable for l in item["pm"]]) if post_goods_issue else []

    # Per-order SAP-item variant choices (only orders whose FSN maps to >1 item),
    # so the cut screen can let the operator switch the item before posting.
    from .resolve_service import load_mappings
    from .variant_service import order_variants
    mappings = load_mappings(company, channel)

    # A held order is excluded from the table above, so attach its variants here —
    # otherwise there is no way to switch it to an in-stock item.
    for h in held_for_stock:
        item = _pre_partition.get(h["dispatch_id"])
        if item is None:
            continue
        order = item["dispatch"].order
        h["buyer_name"] = order.buyer_name
        h["order_date"] = order.order_date.isoformat() if order.order_date else None
        h["variants"] = order_variants(order, mappings, choosable_only=True)
    dispatches = [{
        "dispatch_id": item["dispatch"].id,
        "order_id": item["dispatch"].order.order_id,
        "buyer_name": item["dispatch"].order.buyer_name,
        # Lets the cut screen narrow by order date — e.g. exclude a period already
        # delivered on a manually-created note.
        "order_date": (item["dispatch"].order.order_date.isoformat()
                       if item["dispatch"].order.order_date else None),
        "fg_line_count": len(item["fg"]),
        "amount": str(item["amount"]),
        "variants": order_variants(item["dispatch"].order, mappings, choosable_only=True),
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
        # Per-item top-up the warehouse must supply so every held order can ship.
        "stock_shortfall": stock_shortfall,
        "totals": {
            "dispatch_count": len(dispatches),
            "fg_item_count": len(fg),
            "fg_total_quantity": str(sum((l["required_quantity"] for l in fg), Decimal("0"))),
            "total_amount": str(sum((item["amount"] for item in includable), Decimal("0"))),
        },
    }


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


def _group_by_shipto(includable, warehouse):
    """Group includable items by resolved ship-to address code, first-seen order.

    Returns ``[(ship_to_code, [items]), ...]``. A single group ("") when no
    shipto_by_state map is configured — i.e. the pre-split single delivery note.
    """
    groups, order = {}, []
    for item in includable:
        code = _shipto_for_state(item["dispatch"].order.state, warehouse)
        if code not in groups:
            groups[code] = []
            order.append(code)
        groups[code].append(item)
    return [(code, groups[code]) for code in order]


def _post_group(company, channel, gateway, warehouse, items, ship_to_code, doc_date, user):
    """Post ONE delivery note (+ optional Goods Issue + billing) for one ship-to group.

    All groups share the warehouse's branch (sap_branch_id) and warehouse; only the
    ``ship_to_code`` (GST place of supply) differs, so a state split produces one
    valid delivery note per place of supply.
    """
    dispatches = [item["dispatch"] for item in items]
    order_ids = [d.order.order_id for d in dispatches]
    ref = dispatches[0].pk  # a stable numeric ref for the (simulated) document

    all_fg = [l for item in items for l in item["fg"]]
    all_pm = [l for item in items for l in item["pm"]]
    gateway.verify_stock(all_fg + all_pm, warehouse.sap_warehouse_code)

    num_at_card = f"MKT-{doc_date:%Y%m%d}-{ref}"  # unique ref to reconcile the approved DN
    label = ship_to_code or "default"
    comments = (f"Marketplace {channel} bulk delivery note · {len(dispatches)} orders "
                f"· {label} · {doc_date:%Y-%m-%d}")

    # ONE Delivery Note for this group's finished goods, with its ship-to (place of supply).
    dn = gateway.create_delivery_note(
        ref=ref, card_code=warehouse.sap_customer_card_code,
        warehouse_code=warehouse.sap_warehouse_code, fg_lines=_merge_lines(all_fg),
        doc_date=doc_date, num_at_card=num_at_card,
        comments=comments, series=warehouse.sap_series, tax_code=warehouse.sap_tax_code,
        branch_id=warehouse.sap_branch_id, ship_to_code=ship_to_code,
    )

    # SAP routed it into an approval process → saved as a DRAFT, not posted. Record
    # the draft on every dispatch and stop for this group (no GI, no billing).
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
            "ship_to_code": ship_to_code, "pending_approval": True,
            "draft_entry": dn.get("draft_entry"),
            "delivery_note_num": "", "delivery_note_doc_entry": None,
            "dispatch_count": len(dispatches), "order_ids": order_ids,
        }

    dn_doc_entry, dn_num = dn["DocEntry"], dn["DocNum"]

    # Persist the DN on every dispatch immediately so a later failure never re-cuts it.
    for d in dispatches:
        d.sap_delivery_note_doc_entry = dn_doc_entry
        d.sap_delivery_note_num = dn_num
        d.save(update_fields=[
            "sap_delivery_note_doc_entry", "sap_delivery_note_num", "updated_at",
        ])

    # ONE Goods Issue for this group's packing material (if the master enables it).
    if warehouse.post_goods_issue and _merge_lines(all_pm):
        gi = gateway.create_goods_issue(
            ref=ref, warehouse_code=warehouse.sap_warehouse_code,
            pm_lines=_merge_lines(all_pm), doc_date=doc_date, num_at_card=num_at_card,
            comments=f"{comments} · packing material", series=warehouse.sap_series,
            branch_id=warehouse.sap_branch_id,
        )
        for d in dispatches:
            d.sap_goods_issue_doc_entry = gi["DocEntry"]
            d.sap_goods_issue_num = gi["DocNum"]
            d.save(update_fields=[
                "sap_goods_issue_doc_entry", "sap_goods_issue_num", "updated_at",
            ])

    # Per-order internal billing + mark each dispatch POSTED (local only).
    _finalize_posted(items, company, dn_doc_entry, dn_num, user)

    return {
        "ship_to_code": ship_to_code, "pending_approval": False,
        "delivery_note_num": dn_num, "delivery_note_doc_entry": dn_doc_entry,
        "dispatch_count": len(dispatches), "order_ids": order_ids,
    }


def cut_bulk_delivery_note(company, channel, *, dispatch_ids=None, warehouse_id=None, user=None, batch_id=None):
    """Post the awaiting dispatches as SAP Delivery Note(s).

    Dispatches are grouped by ship-to address (``warehouse.shipto_by_state`` → the
    GST place of supply) and ONE delivery note is posted per group — so a ship-to
    state split (e.g. Delhi vs the rest) yields one note per place of supply, all
    under the same branch + warehouse. With no map configured this is a single note,
    exactly as before. SAP writes run outside any DB transaction.
    """
    includable, _blocked = _collect(company, channel, dispatch_ids, batch_id=batch_id)
    if not includable:
        raise MarketplaceError(
            "No confirmed dispatches are awaiting a delivery note.",
            code="EMPTY", status_code=400,
        )

    warehouse = resolve_cut_warehouse(company, channel, warehouse_id)
    gateway = MarketplaceSapGateway(company.code)

    # Hold dispatches the warehouse can't physically fulfil FIRST, across every
    # ship-to group (they share the one warehouse's stock), so a stock-short order
    # can't fail a bulk document (SAP -10 negative inventory).
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

    # One delivery note per ship-to (GST place of supply). A single group when no
    # shipto_by_state map is configured — identical to the pre-split behaviour.
    doc_date = timezone.localdate()
    groups, errors, first_exc = [], [], None
    for ship_to_code, items in _group_by_shipto(includable, warehouse):
        try:
            groups.append(_post_group(company, channel, gateway, warehouse, items,
                                      ship_to_code, doc_date, user))
        except Exception as exc:  # noqa: BLE001 — one group's failure must not
            # discard another group's already-posted (and committed) delivery note.
            if first_exc is None:
                first_exc = exc
            logger.warning("Bulk DN group %s failed: %s", ship_to_code or "default", exc)
            errors.append({
                "ship_to_code": ship_to_code,
                "order_ids": [it["dispatch"].order.order_id for it in items],
                "error": str(exc),
            })
    if not groups:
        # Nothing was posted — surface the error exactly as before (a SAP rejection
        # stays a MarketplaceError, not a silently swallowed partial success).
        raise first_exc

    out = {
        "groups": groups,
        "errors": errors,
        "dispatch_count": sum(g["dispatch_count"] for g in groups),
        "order_ids": [oid for g in groups for oid in g["order_ids"]],
        "held_for_stock": held_for_stock,
    }
    # Flat fields kept for backward compatibility when a single note was cut.
    if len(groups) == 1:
        g = groups[0]
        out["delivery_note_num"] = g["delivery_note_num"]
        out["delivery_note_doc_entry"] = g["delivery_note_doc_entry"]
        out["pending_approval"] = g["pending_approval"]
        if "draft_entry" in g:
            out["draft_entry"] = g["draft_entry"]
    return out


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


def posted_delivery_notes(company, channel=None, limit=50):
    """Delivery notes this module has posted, newest first, with their metadata.

    Combines what we recorded locally (which orders went on the note, our internal
    bill, when) with the live SAP header/lines (doc date, customer, branch, cost
    centre, quantities, cancelled flag) so the operator can see exactly what was
    sent without opening SAP. The SAP half is best-effort — the local half still
    renders if HANA is unavailable.
    """
    qs = (
        MarketplaceDispatch.objects.filter(
            company=company, sap_delivery_note_doc_entry__isnull=False,
        )
        .select_related("order", "internal_billing")
        .order_by("-sap_delivery_note_doc_entry", "order__order_id")
    )
    if channel:
        qs = qs.filter(channel=channel)

    grouped = OrderedDict()
    for d in qs:
        key = d.sap_delivery_note_doc_entry
        g = grouped.setdefault(key, {
            "doc_entry": key,
            "doc_num": d.sap_delivery_note_num or "",
            "channel": d.channel,
            "posted_at": None,
            "orders": [],
            "dispatch_count": 0,
            "sap_post_status": d.sap_post_status,
        })
        g["dispatch_count"] += 1
        g["orders"].append({
            "order_id": d.order.order_id,
            "buyer_name": d.order.buyer_name,
            "order_date": d.order.order_date.isoformat() if d.order.order_date else None,
            "invoice_number": getattr(d.internal_billing, "invoice_number", "") or "",
        })
        stamp = d.updated_at or d.confirmed_at
        if stamp and (g["posted_at"] is None or stamp.isoformat() > g["posted_at"]):
            g["posted_at"] = stamp.isoformat()
        if len(grouped) >= limit and key not in grouped:
            break

    notes = list(grouped.values())[:limit]
    _attach_sap_metadata(company, notes)
    return {"notes": notes}


def _attach_sap_metadata(company, notes):
    """Enrich posted notes with their SAP header + lines. Best-effort."""
    entries = [n["doc_entry"] for n in notes if n["doc_entry"] is not None]
    if not entries:
        return
    try:
        from hdbcli import dbapi
        from sap_client.context import CompanyContext
        h = CompanyContext(company.code).hana
    except Exception as e:  # pragma: no cover - env specific
        logger.warning("Delivery-note metadata unavailable (%s)", e)
        return
    ph = ",".join(["?"] * len(entries))
    conn = None
    try:
        conn = dbapi.connect(address=h["host"], port=int(h["port"]), user=h["user"],
                             password=h["password"], encrypt=True, sslValidateCertificate=False)
        cur = conn.cursor()
        cur.execute(
            f'SELECT "DocEntry","DocNum","DocDate","CardCode","CardName","NumAtCard",'
            f'"Comments","BPLId","CANCELED","DocTotal" FROM "{h["schema"]}"."ODLN" '
            f'WHERE "DocEntry" IN ({ph})', entries)
        head = {r[0]: r for r in cur.fetchall()}
        cur.execute(
            f'SELECT "DocEntry","ItemCode","Dscription","Quantity","WhsCode","OcrCode" '
            f'FROM "{h["schema"]}"."DLN1" WHERE "DocEntry" IN ({ph}) ORDER BY "DocEntry","LineNum"',
            entries)
        lines = {}
        for e, code, desc, qty, whs, ocr in cur.fetchall():
            lines.setdefault(e, []).append({
                "item_code": code, "item_name": desc or "",
                "quantity": str(qty), "warehouse_code": whs or "", "cost_center": ocr or "",
            })
        cur.close()
    except Exception as e:  # pragma: no cover - env specific
        logger.warning("Delivery-note metadata query failed (%s)", e)
        return
    finally:
        if conn is not None:
            conn.close()

    for n in notes:
        h_row = head.get(n["doc_entry"])
        if h_row:
            n["sap"] = {
                "doc_num": str(h_row[1] or ""),
                "doc_date": h_row[2].isoformat() if h_row[2] else None,
                "card_code": h_row[3] or "",
                "card_name": h_row[4] or "",
                "num_at_card": h_row[5] or "",
                "comments": h_row[6] or "",
                "branch_id": h_row[7],
                "cancelled": (h_row[8] or "N") == "Y",
                "doc_total": str(h_row[9] or 0),
            }
        n["lines"] = lines.get(n["doc_entry"], [])
        n["total_quantity"] = str(sum(Decimal(l["quantity"]) for l in n["lines"]))
