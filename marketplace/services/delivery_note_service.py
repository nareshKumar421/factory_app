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
from datetime import date
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from ..models import (
    ComboComponentType,
    MarketplaceBillingStatus,
    MarketplaceDispatch,
    MarketplaceDispatchStatus,
    MarketplaceOrderBilling,
    MarketplaceSapPostStatus,
)
from .confirm_service import (
    _next_invoice_number, _shipto_for_state, _warehouse_for,
)
from .dispatch_gate import order_dispatch_ready
from .errors import MarketplaceError
from .resolve_service import fg_lines, pm_lines, resolve_order
from .sap_gateway import MarketplaceSapGateway

logger = logging.getLogger(__name__)


BACKDATE_PERM = "marketplace.backdate_delivery_note"


def _month(value):
    return (value.year, value.month)


def _previous_month_start(today):
    return date(today.year - 1, 12, 1) if today.month == 1 else date(today.year, today.month - 1, 1)


def _confirmed_on(dispatch):
    """The date a dispatch was confirmed — the earliest its delivery note can bear."""
    confirmed = dispatch.confirmed_at
    return timezone.localtime(confirmed).date() if confirmed else None


def resolve_doc_date(includable, doc_date=None, user=None, today=None):
    """The posting date for this cut, validated.

    ``None`` means today — the behaviour before back-dating existed, and still the
    default. A month can close with orders confirmed but not yet cut, and those
    delivery notes belong in the month the goods actually left, so an explicit
    ``doc_date`` may be passed to post into a previous period. That is a financial
    act, hence the guard rails:

      * never the future, and never before the goods were confirmed out;
      * only the current or immediately previous month — older periods are closed
        work, not a slip someone is catching up on;
      * a back-dated cut may not mix months, or one month's orders would be
        dragged into another month's books;
      * back-dating needs ``marketplace.backdate_delivery_note``.

    SAP picks its monthly numbering series from this date (the warehouse master
    pins no series), so a July date lands on the July series by itself.
    """
    today = today or timezone.localdate()
    if doc_date is None:
        return today, False

    if doc_date > today:
        raise MarketplaceError(
            f"A delivery note cannot be dated in the future ({doc_date:%d %b %Y}).",
            code="DOC_DATE_FUTURE", status_code=400,
        )

    backdated = _month(doc_date) != _month(today)
    if not backdated:
        return doc_date, False

    if user is not None and not user.has_perm(BACKDATE_PERM):
        raise MarketplaceError(
            "You do not have permission to cut a delivery note into a previous month.",
            code="BACKDATE_FORBIDDEN", status_code=403,
        )

    if doc_date < _previous_month_start(today):
        raise MarketplaceError(
            f"{doc_date:%B %Y} is closed. A delivery note can only be back-dated into "
            f"{_previous_month_start(today):%B %Y}.",
            code="DOC_DATE_TOO_OLD", status_code=400,
        )

    # Every dispatch must already have been confirmed out on that date, and all of
    # them must belong to the month being posted into.
    confirmed = [d for d in (_confirmed_on(item["dispatch"]) for item in includable) if d]
    if confirmed:
        # Mixed months first: it is the more specific diagnosis, and it is also what
        # a spread looks like from the inside — the newest dispatch is bound to sit
        # after the back-dated day, so the pre-date check below would otherwise fire
        # and blame the date rather than the selection.
        months = {_month(d) for d in confirmed}
        if len(months) > 1:
            spread = ", ".join(f"{date(y, m, 1):%b %Y}" for y, m in sorted(months))
            raise MarketplaceError(
                f"These dispatches span {spread}. Filter to one month before back-dating, "
                "otherwise one month's orders post into another month's books.",
                code="DOC_DATE_MIXED_MONTHS", status_code=400,
            )
        latest = max(confirmed)
        if doc_date < latest:
            raise MarketplaceError(
                f"{doc_date:%d %b %Y} is before the last dispatch was confirmed "
                f"({latest:%d %b %Y}). The delivery note cannot pre-date the goods leaving.",
                code="DOC_DATE_BEFORE_CONFIRM", status_code=400,
            )
    return doc_date, True


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
            # Frozen at post time so a later master edit cannot rewrite history.
            "posted_lines": _posted_lines_snapshot(
                order, order.sap_warehouse_code or "", mappings),
        })
    return includable, blocked


def _posted_lines_snapshot(order, warehouse_code, mappings):
    """What each of an order's lines resolves to RIGHT NOW, ready to freeze.

    Resolved per order line, not per order: ``resolve_lines`` aggregates by item
    code, so an order carrying the same SKU on two lines would otherwise report
    the order's total against both. The export renders one row per line, so the
    snapshot has to be per line for it to replace that resolve.

    ``order_line_id`` is what lets the export match a stored entry back to the row
    it is printing, and survives a line being edited afterwards.
    """
    from .resolve_service import resolve_lines

    snapshot = []
    for line in order.lines.all():
        resolved = resolve_lines([line], warehouse_code, mappings)["resolved_lines"]
        for r in resolved:
            snapshot.append({
                "order_line_id": line.id,
                "item_code": r["item_code"],
                "item_name": r["item_name"],
                "component_type": r["component_type"],
                "quantity": str(r["required_quantity"]),
                "uom": r["uom"],
                "warehouse_code": r["warehouse_code"],
                "source_skus": list(r["source_skus"]),
            })
    return snapshot


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


def build_bulk_summary(company, channel, dispatch_ids=None, warehouse_id=None, batch_id=None,
                       user=None):
    """Preview the combined delivery note without posting anything.

    ``warehouse_id`` selects which warehouse master to post against; when omitted
    the channel default is used. The full list of warehouse options is returned so
    the operator can switch at cut time.

    Also reports what posting dates this cut would accept, so the screen can offer
    back-dating into the previous month (and refuse it) without a round trip.
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
        # Every line, not just the ones with a choice: the cut screen shows what
        # each order ships as, and reserves the picker for the few that can vary.
        "variants": order_variants(item["dispatch"].order, mappings),
    } for item in includable]

    today = timezone.localdate()
    confirmed_dates = [d for d in (_confirmed_on(it["dispatch"]) for it in includable) if d]
    min_doc_date = max(confirmed_dates) if confirmed_dates else None
    confirmed_months = sorted({f"{d:%Y-%m}" for d in confirmed_dates})

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
        "doc_date": today.isoformat(),
        # Back-dating envelope for the cut screen: the earliest date the note may
        # bear (the goods must already have been confirmed out), the oldest month
        # still open to it, whether this user may at all, and the months these
        # dispatches span — more than one and a back-dated cut is refused.
        "doc_date_min": min_doc_date.isoformat() if min_doc_date else None,
        "doc_date_floor": _previous_month_start(today).isoformat(),
        "can_backdate": bool(user and user.has_perm(BACKDATE_PERM)),
        "confirmed_months": confirmed_months,
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

    # A post that dies AFTER SAP commits the note but before the DocEntry comes back
    # used to leave nothing behind: the ref was only ever stored on the approval
    # branch, so JI had no record it had even tried. Re-cutting then created a second
    # note for the same goods and stock left twice. DN 1507264771 (MKT-20260731-970)
    # is exactly that: posted 4 Aug 2026, invisible to JI, duplicated by 1508264503.
    #
    # So: stamp the ref BEFORE posting, and adopt any note a previous attempt already
    # created — under this attempt's ref or under one a previous attempt recorded,
    # since re-cutting with a different document date changes the ref.
    # Only a dispatch carrying a ref from an EARLIER attempt is worth asking about:
    # on a first cut there is nothing in SAP yet, so the happy path costs no extra
    # round-trip. If that lookup fails we stop rather than risk a second note.
    prior_refs = [r for r in dict.fromkeys(d.sap_dn_ref for d in dispatches) if r]
    dn = None
    for candidate in prior_refs:
        existing = gateway.find_delivery_note_by_ref(candidate)
        if existing and existing.get("DocEntry"):
            logger.warning(
                "Delivery note %s (ref %s) already exists in SAP for dispatches %s — "
                "adopting it instead of cutting a second note.",
                existing.get("DocNum"), candidate, [d.pk for d in dispatches])
            dn = existing
            break

    if dn is None:
        with transaction.atomic():
            for d in dispatches:
                d.sap_dn_ref = num_at_card
                d.sap_delivery_note_doc_date = doc_date
                d.save(update_fields=[
                    "sap_dn_ref", "sap_delivery_note_doc_date", "updated_at"])
        # ONE Delivery Note for this group's finished goods, with its ship-to
        # (place of supply).
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
                d.sap_delivery_note_doc_date = doc_date
                d.sap_ship_to_code = ship_to_code
                d.sap_post_status = MarketplaceSapPostStatus.AWAITING_APPROVAL
                d.sap_error = ""
                d.updated_by = user
                d.save(update_fields=[
                    "sap_delivery_note_draft_entry", "sap_dn_ref",
                    "sap_delivery_note_doc_date", "sap_ship_to_code", "sap_post_status",
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
        d.sap_delivery_note_doc_date = doc_date
        d.sap_ship_to_code = ship_to_code
        d.save(update_fields=[
            "sap_delivery_note_doc_entry", "sap_delivery_note_num",
            "sap_delivery_note_doc_date", "sap_ship_to_code", "updated_at",
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


def cut_bulk_delivery_note(company, channel, *, dispatch_ids=None, warehouse_id=None, user=None,
                           batch_id=None, doc_date=None):
    """Post the awaiting dispatches as SAP Delivery Note(s).

    Dispatches are grouped by ship-to address (``warehouse.shipto_by_state`` → the
    GST place of supply) and ONE delivery note is posted per group — so a ship-to
    state split (e.g. Delhi vs the rest) yields one note per place of supply, all
    under the same branch + warehouse. With no map configured this is a single note,
    exactly as before. SAP writes run outside any DB transaction.

    ``doc_date`` posts into a past period (see :func:`resolve_doc_date`); omitted,
    it is today, exactly as before.
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
    doc_date, backdated = resolve_doc_date(includable, doc_date, user=user)
    if backdated:
        logger.warning(
            "Marketplace %s: back-dated delivery note cut to %s by %s (%d dispatch(es))",
            channel, doc_date, getattr(user, "email", user), len(includable),
        )
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
        # The period the documents actually landed in — the UI confirms this back to
        # the operator, loudly when it is not the current month.
        "doc_date": doc_date.isoformat(),
        "doc_month": f"{doc_date:%B %Y}",
        "backdated": backdated,
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


def _posted_lines_for(item, company):
    """The snapshot to freeze on one dispatch.

    ``_collect`` builds it while the mappings are already in memory. The approval
    reconciler (:func:`reconcile_approved_delivery_notes`) finalizes dispatches it
    did not collect, so its items carry only ``dispatch``/``amount`` — resolve
    those here rather than storing nothing. Resolving at reconcile time is later
    than at cut time, but it is still the moment of posting rather than the moment
    of export, which is the drift this exists to stop.
    """
    existing = item.get("posted_lines")
    if existing is not None:
        return existing
    from .resolve_service import load_mappings

    d = item["dispatch"]
    order = d.order
    try:
        mappings = load_mappings(company, d.channel)
        return _posted_lines_snapshot(order, order.sap_warehouse_code or "", mappings)
    except Exception as e:  # pragma: no cover - defensive
        # A snapshot is an improvement, never a reason to fail a post that SAP has
        # already accepted. The export falls back to SAP for anything missing.
        logger.warning("Could not snapshot posted lines for order %s (%s)",
                       order.order_id, e)
        return []


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
            d.sap_posted_lines = _posted_lines_for(item, company)
            d.updated_by = user
            d.save(update_fields=[
                "internal_billing", "sap_delivery_note_doc_entry", "sap_delivery_note_num",
                "sap_post_status", "sap_error", "sap_posted_lines", "updated_by", "updated_at",
            ])


def _order_amount(order):
    """Total invoice amount for an order (sum of its line invoice_amounts)."""
    return sum(
        (Decimal(a) for a in order.lines.values_list("invoice_amount", flat=True)),
        Decimal("0"),
    )


# Header for the posted-delivery-note item export.
DN_CSV_HEADER = [
    # Order-item detail (Flipkart order-sheet layout, one row per shipment item)
    "Ordered On", "Shipment ID", "ORDER ITEM ID", "Order Id", "HSN CODE", "FSN",
    "SKU", "Product", "Invoice No.", "Invoice Date (mm/dd/yy)", "Invoice Amount",
    "Quantity", "State", "Dispatch After date", "Dispatch by date", "Tracking ID",
    # Extra context (buyer / tax / resolved SAP item / delivery-note header)
    "Buyer", "City", "PIN", "Unit Price", "CGST", "IGST", "SGST",
    "Order State", "Order Type", "SAP Item Code", "SAP Item Name", "SAP Qty", "UOM",
    "Internal Invoice No", "DN Number", "DN Date", "Channel", "SAP CardCode",
    "Branch", "Warehouse",
    # Where the SAP Item columns came from: posted (frozen at post time), sap
    # (read back from the delivery note), or resolved (re-derived from today's
    # masters, and therefore only as true as they are now).
    "Source",
]


def _fmt_ordered_on(dt):
    """Order date as ``28-Jul-26`` (matches the Flipkart sheet display)."""
    return dt.strftime("%d-%b-%y") if dt else ""


def _fmt_dispatch_by(dt):
    """Dispatch-by as ``7/29/26 15:00`` (matches the requested layout)."""
    return f"{dt.month}/{dt.day}/{dt:%y} {dt:%H:%M}" if dt else ""


def _item_code(entry):
    return entry.get("item_code") or ""


def _item_name(entry):
    return entry.get("item_name") or ""


def _item_qty(entry):
    """Quantity from any of the three sources.

    A live-resolved line calls it ``required_quantity`` and holds a Decimal; a
    snapshot or SAP row calls it ``quantity`` and holds a string.
    """
    q = entry.get("required_quantity", entry.get("quantity", 0))
    return Decimal(str(q or 0))


def _fmt_qty(q):
    """``Decimal('2.000')`` → ``'2'`` — piece counts read better without the scale."""
    d = Decimal(q).normalize()
    return f"{d:f}"


def export_posted_delivery_note_csv(company, doc_entry, channel=None):
    """Build a CSV of a posted delivery note's items — one row per SAP item with the
    quantity plus DN, warehouse, order and tax context.

    Assembled entirely from marketplace data (resolve → items/qty/uom/warehouse,
    order lines → HSN + amount, warehouse master → CardCode/branch), so it needs no
    live SAP/HANA. Returns ``(filename, csv_text)``.
    """
    import csv
    import io

    from ..models import MarketplaceWarehouse
    from .resolve_service import fg_lines, load_mappings, resolve_lines

    disp_qs = MarketplaceDispatch.objects.filter(
        company=company, sap_delivery_note_doc_entry=doc_entry,
    ).select_related("order", "internal_billing").prefetch_related(
        "order__lines", "order__lines__chosen_option__combo__components")
    if channel:
        disp_qs = disp_qs.filter(channel=channel)
    dispatches = list(disp_qs)
    if not dispatches:
        raise MarketplaceError("Delivery note not found.", code="NOT_FOUND", status_code=404)

    ch = dispatches[0].channel
    doc_num = dispatches[0].sap_delivery_note_num or str(doc_entry)
    posted = max((d.confirmed_at or d.updated_at for d in dispatches if (d.confirmed_at or d.updated_at)), default=None)
    mappings = load_mappings(company, ch)
    wh = (
        MarketplaceWarehouse.objects.filter(company=company, channel=ch, is_active=True)
        .order_by("-is_default", "id").first()
    )
    card_code = wh.sap_customer_card_code if wh else ""
    branch = wh.sap_branch_id if wh else ""
    # The DN is posted against the warehouse master's godown; the order's own
    # sap_warehouse_code is usually blank, so fall back to the master's code.
    wh_code = wh.sap_warehouse_code if wh else ""
    dn_date = posted.date().isoformat() if posted else ""

    # SAP's own record of the note, fetched once and shared by every dispatch on
    # it. Only consulted for dispatches with no snapshot, and only if HANA answers.
    _sap_lines_cache = {}

    def _sap_lines(dispatch):
        entry = dispatch.sap_delivery_note_doc_entry
        if entry not in _sap_lines_cache:
            _sap_lines_cache[entry] = _sap_delivery_note_lines(company, entry)
        return _sap_lines_cache[entry]

    def _resolved_fg_for(line, warehouse_code):
        """Resolved FG lines for ONE order line, so the item codes AND their piece
        counts belong to that row alone.

        Resolve per line, not per order: ``resolve_lines`` aggregates by item code,
        so an order carrying the same SKU (or two SKUs sharing an item) on several
        lines would otherwise report the order's total against every one of them.
        Mappings are fully prefetched by ``load_mappings``, so this stays in memory.
        """
        return fg_lines(resolve_lines([line], warehouse_code, mappings)["resolved_lines"])

    # The note's own SAP lines, for notes with no snapshot. Collected while the
    # order rows are written and printed afterwards, one item per row.
    sap_note_lines = OrderedDict()
    # What the re-derived order rows add up to, per item, so the two can be
    # compared at the end instead of leaving a reader to spot the difference.
    est_totals, est_names = {}, {}

    def _items_for(dispatch, line, warehouse_code):
        """(items, source) for one printed row, newest-truth first.

        1. ``posted``   — frozen on the dispatch when the note was posted. The only
           source that cannot drift, so it always wins.
        2. ``sap``      — read back from the delivery note itself, for notes posted
           before snapshots existed. SAP has no order-line attribution, so no order
           row can claim its items; they are written as their own rows below.
        3. ``resolved`` — re-derived from today's masters. Correct only if nothing
           has been edited since, which is exactly the bug this chain exists for,
           so it is last and is labelled as such.
        """
        entry = dispatch.sap_delivery_note_doc_entry
        snapshot = dispatch.sap_posted_lines or []
        if snapshot:
            mine = [
                s for s in snapshot
                if s.get("order_line_id") == line.id
                and s.get("component_type", ComboComponentType.FG) == ComboComponentType.FG
            ]
            # A snapshot that holds nothing for this line still counts as posted —
            # the line genuinely shipped no finished goods.
            return mine, "posted"

        # SAP records the note per ITEM, with no attribution to an order or a line —
        # a note covering 1265 orders is simply their goods pooled into one item
        # list. So it can say what the NOTE carried, never what an order carried.
        #
        # It used to be attached to the first order row that asked, which put every
        # code of a 29-item note into one cell of row 1 and left every other row
        # blank. The note's list is now written as the note's own rows at the end.
        sap = _sap_lines(dispatch)
        if sap:
            sap_note_lines.setdefault(entry, sap)

        # The order rows still have to say what each order shipped, so they are
        # re-derived per line from the masters — the only per-order answer that
        # exists for a note with no snapshot. Labelled `resolved`, because it is
        # only as true as the mappings are today.
        return _resolved_fg_for(line, warehouse_code), "resolved"

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(DN_CSV_HEADER)
    # One row per order line (shipment item), in the Flipkart order-sheet layout.
    for d in sorted(dispatches, key=lambda x: x.order.order_id):
        order = d.order
        inv_no = d.internal_billing.invoice_number if d.internal_billing_id else ""
        lines = list(order.lines.all())
        for l in lines:
            raw = l.raw_row or {}
            fgs, source = _items_for(d, l, order.sap_warehouse_code or wh_code)
            sap_code = "; ".join(_item_code(f) for f in fgs)
            # Names are filtered, codes and quantities are not — so a nameless item
            # would shift every later name one column left against its code. Emit a
            # placeholder instead, keeping the three lists positionally aligned.
            sap_name = "; ".join(_item_name(f) or "-" for f in fgs)
            # Positionally aligned with SAP Item Code. A combo ships several items and
            # a component can ship more than one piece (``1+1L`` → 2), so the count has
            # to travel with the code or the export cannot be reconciled against SAP.
            sap_qty = "; ".join(_fmt_qty(_item_qty(f)) for f in fgs)
            if source == "resolved":
                # Totalled so the file can say, at the end, exactly where the
                # re-derived rows disagree with the note SAP actually holds.
                for f in fgs:
                    code_ = _item_code(f)
                    est_totals[code_] = est_totals.get(code_, Decimal("0")) + _item_qty(f)
                    est_names.setdefault(code_, _item_name(f))
            # A per-item list like the three columns above, so a multi-item row can
            # be reconciled unit by unit instead of losing its UoMs -- only a
            # single-item row used to report one, and a combo reported none.
            # Blank when NO item carries a UoM (SAP's own read-back has none), so
            # those rows stay empty rather than becoming a row of placeholders.
            uoms = [f.get("uom") or "" for f in fgs]
            uom = "; ".join(u or "-" for u in uoms) if any(uoms) else ""
            writer.writerow([
                # Order-item detail
                _fmt_ordered_on(order.order_date),
                order.flipkart_shipment_id,
                l.order_item_id,
                order.order_id,
                l.hsn_code,
                l.fsn,
                l.marketplace_sku,
                l.sku_name,
                raw.get("invoice_no", ""),
                raw.get("invoice_date", ""),
                str(l.invoice_amount),
                str(l.ordered_quantity),
                order.state,
                raw.get("dispatch_after", ""),
                _fmt_dispatch_by(order.dispatch_by),
                l.tracking_id or order.tracking_id,
                # Extra context
                order.buyer_name,
                order.city,
                order.pin_code,
                str(l.unit_price),
                raw.get("cgst", ""),
                raw.get("igst", ""),
                raw.get("sgst", ""),
                l.order_state,
                order.order_type,
                sap_code,
                sap_name,
                sap_qty,
                uom,
                inv_no,
                doc_num,
                dn_date,
                ch,
                card_code,
                branch,
                wh_code,
                source,
            ])

    # The note's own items, one per row, after the orders they belong to. A note
    # read back from SAP has no order-line attribution, so these carry the delivery
    # note's columns and leave every order column blank — which is what says they
    # describe the note as a whole rather than any single order.
    col = {name: i for i, name in enumerate(DN_CSV_HEADER)}
    for lines in sap_note_lines.values():
        for l in lines:
            row = [""] * len(DN_CSV_HEADER)
            row[col["SAP Item Code"]] = _item_code(l)
            row[col["SAP Item Name"]] = _item_name(l) or "-"
            row[col["SAP Qty"]] = _fmt_qty(_item_qty(l))
            row[col["UOM"]] = l.get("uom") or ""
            row[col["DN Number"]] = doc_num
            row[col["DN Date"]] = dn_date
            row[col["Channel"]] = ch
            row[col["SAP CardCode"]] = card_code
            row[col["Branch"]] = branch
            row[col["Warehouse"]] = l.get("warehouse_code") or wh_code
            # Distinct from the order rows' "sap": these ARE the note's lines,
            # not an order's share of them.
            row[col["Source"]] = "sap (note line)"
            writer.writerow(row)

    # Where the two disagree, say so and by how much. The order rows on a note with
    # no snapshot are re-derived from today's masters, so an item remapped since the
    # note was cut reports against its NEW code — DN 1507264761 shows 132 pieces of
    # FG0000032, an item the note never carried, because a combo was repointed on
    # 11 Aug. Nothing can recover the per-order truth; leaving the reader to notice
    # the difference by hand is what made this look like a quantity bug.
    note_totals, note_names = {}, {}
    for lines in sap_note_lines.values():
        for l in lines:
            code_ = _item_code(l)
            note_totals[code_] = note_totals.get(code_, Decimal("0")) + _item_qty(l)
            note_names.setdefault(code_, _item_name(l))
    if note_totals:
        for code_ in sorted(set(note_totals) | set(est_totals)):
            got, want = est_totals.get(code_, Decimal("0")), note_totals.get(code_, Decimal("0"))
            if got == want:
                continue
            row = [""] * len(DN_CSV_HEADER)
            row[col["SAP Item Code"]] = code_
            # An item only the note carries has no re-derived name to borrow, so
            # fall back to SAP's — a difference nobody can name is not much of a
            # report.
            row[col["SAP Item Name"]] = (
                f"{est_names.get(code_) or note_names.get(code_) or '-'} — "
                f"order rows {_fmt_qty(got)}, delivery note {_fmt_qty(want)}")
            diff = got - want
            # Sign the magnitude rather than trimming a formatted decimal: stripping
            # trailing zeros off "-120.000000" leaves "-12".
            row[col["SAP Qty"]] = ("+" if diff > 0 else "-") + _fmt_qty(abs(diff))
            row[col["DN Number"]] = doc_num
            row[col["DN Date"]] = dn_date
            row[col["Channel"]] = ch
            row[col["Source"]] = "check"
            writer.writerow(row)

    return f"delivery-note-{doc_num or doc_entry}.csv", buf.getvalue()


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


def _sap_delivery_note_lines(company, doc_entry):
    """The delivery note's own lines, straight from SAP (DLN1). Best-effort.

    Used for notes posted before snapshots existed: SAP is the only remaining
    record of what actually went out. Returns [] when HANA is unavailable, and
    the caller falls back to a live resolve.
    """
    if doc_entry is None:
        return []
    try:
        from hdbcli import dbapi
        from sap_client.context import CompanyContext
        h = CompanyContext(company.code).hana
    except Exception as e:  # pragma: no cover - env specific
        logger.warning("Delivery-note line lookup unavailable (%s)", e)
        return []
    conn = None
    try:
        conn = dbapi.connect(address=h["host"], port=int(h["port"]), user=h["user"],
                             password=h["password"], encrypt=True, sslValidateCertificate=False)
        cur = conn.cursor()
        cur.execute(
            f'SELECT "ItemCode","Dscription","Quantity","WhsCode" '
            f'FROM "{h["schema"]}"."DLN1" WHERE "DocEntry" = ? ORDER BY "LineNum"',
            [doc_entry])
        rows = [
            {"item_code": c, "item_name": d or "", "quantity": str(q), "warehouse_code": w or ""}
            for c, d, q, w in cur.fetchall()
        ]
        cur.close()
        return rows
    except Exception as e:  # pragma: no cover - env specific
        logger.warning("Delivery-note line query failed (%s)", e)
        return []
    finally:
        if conn is not None:
            conn.close()


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
