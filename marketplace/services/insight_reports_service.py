"""Marketplace insight reports — the ones that report what is MISSING.

The reports in :mod:`reports_service` all share one shape: a flat row dump filtered
by a date range. That shape answers "give me the list", but it can only report rows
that EXIST — never the delivery note that was never cut, the order that blew its
dispatch-by date, or the sheet that quietly imported and went nowhere. Those are the
failures that stay silent until someone counts by hand.

Each builder here returns ``(header, rows, totals)``:

  * ``header`` — CSV column names
  * ``rows``   — list of lists, already stringified for CSV
  * ``totals`` — a flat ``{name: value}`` dict the UI shows above the table

``reports_service`` wraps them into CSV downloads and ``ReportPreviewView`` serves
the same three pieces to the screen, so what you see is exactly what you download.

Reports:
  * ``sap-posting-gap``  — confirmed dispatches with no SAP delivery note
  * ``ageing``           — open orders against their marketplace dispatch-by date
  * ``sheet-audit``      — per-sheet funnel: file rows → orders → parcels → posted
  * ``sku-coverage``     — marketplace SKUs/FSNs with and without an item mapping
  * ``gst-branch``       — posted DNs by GST place of supply, rule vs what posted
  * ``scan-throughput``  — parcels scanned per operator per day
"""
import datetime
from collections import defaultdict
from decimal import Decimal, InvalidOperation

from django.db.models import Count, Q, Sum
from django.utils import timezone


# ── shared helpers ───────────────────────────────────────────────────────────
def _d(value):
    return Decimal(value or 0)


def _money(value):
    return f"{_d(value):.2f}"


def _dt(value):
    return timezone.localtime(value).strftime("%Y-%m-%d %H:%M") if value else ""


def _day(value):
    """ISO date for a datetime OR a date — ``sap_delivery_note_doc_date`` is a plain
    date, and ``localtime`` rejects one."""
    if not value:
        return ""
    if isinstance(value, datetime.datetime):
        return timezone.localtime(value).date().isoformat()
    return value.isoformat()


def _scanned_by_order(order_ids):
    """``{order_id: {tracking, ...}}`` — the parcels scanned on each order.

    One query for the whole report. Cancelled dispatches are excluded: their scans
    were undone, so counting them would overstate progress.
    """
    from ..models import MarketplaceDispatchStatus, MarketplaceScan

    ids = list(order_ids)
    out = defaultdict(set)
    if not ids:
        return out
    rows = (
        MarketplaceScan.objects.filter(is_active=True, dispatch__order_id__in=ids)
        .exclude(dispatch__status=MarketplaceDispatchStatus.CANCELLED)
        .values_list("dispatch__order_id", "barcode_raw")
    )
    for order_id, barcode in rows.iterator():
        out[order_id].add((barcode or "").split("#", 1)[0])
    return out


def _sheet_label(batch):
    return f"#{batch.id}" if batch else ""


def _scanned_by_dispatch(dispatch_ids):
    """``{dispatch_id: {tracking, ...}}`` — the parcels scanned into each dispatch.

    ``scan_service.scanned_trackings`` reads one dispatch at a time, and calling
    ``.filter()`` on a prefetched relation bypasses the prefetch cache, so a report
    over every delivery note issued one query per dispatch: 1,901 round trips, 69
    seconds, and a worker timeout that reached the browser as a 500. One query here
    instead.
    """
    from ..models import MarketplaceScan

    ids = list(dispatch_ids)
    out = defaultdict(set)
    if not ids:
        return out
    rows = MarketplaceScan.objects.filter(
        is_active=True, dispatch_id__in=ids
    ).values_list("dispatch_id", "barcode_raw")
    for dispatch_id, barcode in rows.iterator():
        out[dispatch_id].add((barcode or "").split("#", 1)[0])
    return out


def _billed_lines(dispatch, scanned):
    """The lines a dispatch shipped, falling back to the whole order.

    Same rule as ``scan_service.dispatch_lines`` — an order whose lines carry no
    tracking ID has nothing to scope by, so the dispatch covers all of it — but fed
    from a bulk scan map rather than a query per dispatch.

    The fallback is the difference. When Flipkart re-manifests an order it is pulled
    onto a newer sheet with a NEW tracking ID, so an already-shipped dispatch's scans
    stop matching any line and the match comes back empty; 12 posted delivery notes
    read as zero parcels and zero rupees that way. An empty match never means
    "shipped nothing" (it was confirmed and a note was cut), it means the tracking
    data moved out from under the dispatch — and the whole order is then the best
    available answer, which is what an empty ``shipped_trackings`` has always meant.
    A genuinely partial dispatch matches at least one line and never reaches it.
    """
    lines = list(dispatch.order.lines.all())
    if not any((l.tracking_id or "").strip() for l in lines):
        return lines
    return [l for l in lines if (l.tracking_id or "").strip() in scanned] or lines


# GST totals that can legitimately be a RATE in the Flipkart sheet. The CGST/IGST/
# SGST columns carry a rate on some exports ("NA/5/NA", "2.5/NA/2.5") and a rupee
# amount on others ("0/40.43/0", "61.88/0/61.88"), with nothing but the value to
# tell them apart — and the importer stores whichever it finds as ``tax_amount``.
# Reading a 5% rate as ₹5 of tax understates GST on the great majority of lines.
_GST_RATES = {
    Decimal("0.25"), Decimal("3"), Decimal("5"),
    Decimal("12"), Decimal("18"), Decimal("28"),
}


def _gst_split(line):
    """``(taxable, tax, rate)`` for a line whose invoice amount is GST-inclusive."""
    total = _d(line.invoice_amount)
    raw = line.raw_row or {}
    rate = Decimal("0")
    for key in ("cgst", "sgst", "igst"):
        try:
            rate += Decimal(str(raw.get(key) or "0").strip() or "0")
        except (InvalidOperation, ValueError, ArithmeticError):
            continue
    if rate in _GST_RATES:
        taxable = (total / (1 + rate / 100)).quantize(Decimal("0.01"))
        return taxable, total - taxable, rate
    # Not a recognisable rate → those columns held rupee amounts.
    tax = rate if rate else _d(line.tax_amount)
    return total - tax, tax, None


# ── 1. SAP posting gap ───────────────────────────────────────────────────────
_GAP_HEADER = [
    "Order ID", "Buyer", "City", "State", "Sheet", "Sheet file", "Confirmed at",
    "Days waiting", "Order status", "SAP post status", "SAP error",
    "Parcels shipped", "Value", "Gate status", "Dispatch ID",
]


def sap_posting_gap(company, channel, *, date_from=None, date_to=None,
                    min_age_days=None, **_):
    """Dispatches confirmed as shipped that carry no SAP delivery note.

    The goods left the building; SAP has no document for them. That means stock is
    overstated, the sale is unbooked and there is no GST document — and because the
    existing delivery-notes report lists notes that DID post, nothing else in the
    system can show it.

    Some of these are simply waiting for the bulk cut (``defer_delivery_note``), so
    the age column is what separates "queued" from "stuck". ``min_age_days`` drops
    the fresh ones and leaves only the backlog.
    """
    from ..models import MarketplaceDispatch, MarketplaceDispatchStatus

    qs = (
        MarketplaceDispatch.objects.filter(
            company=company, channel=channel,
            status=MarketplaceDispatchStatus.CONFIRMED,
            sap_delivery_note_doc_entry__isnull=True,
        )
        .select_related("order", "order__import_batch", "import_batch")
        .prefetch_related("order__lines")
        .order_by("confirmed_at", "id")
    )
    if date_from:
        qs = qs.filter(confirmed_at__date__gte=date_from)
    if date_to:
        qs = qs.filter(confirmed_at__date__lte=date_to)

    cutoff = int(min_age_days or 0)
    now = timezone.now()
    dispatches = list(qs)
    scanned_map = _scanned_by_dispatch(d.pk for d in dispatches)
    rows, orders, parcels, value = [], set(), 0, Decimal("0")
    over_7 = over_20 = failed = 0

    for d in dispatches:
        age = (now - d.confirmed_at).days if d.confirmed_at else 0
        if cutoff and age < cutoff:
            continue
        order = d.order
        lines = _billed_lines(d, scanned_map[d.pk])
        amount = sum((_d(l.invoice_amount) for l in lines), Decimal("0"))
        batch = d.import_batch or order.import_batch

        orders.add(order.pk)
        parcels += len(lines)
        value += amount
        if age >= 7:
            over_7 += 1
        if age >= 20:
            over_20 += 1
        if d.sap_post_status == "FAILED":
            failed += 1

        rows.append([
            order.order_id, order.buyer_name, order.city, order.state,
            _sheet_label(batch), batch.filename if batch else "",
            _dt(d.confirmed_at), age, order.status, d.sap_post_status,
            (d.sap_error or "").replace("\n", " ")[:200],
            len(lines), _money(amount), d.gate_status, d.pk,
        ])

    totals = {
        "dispatches": len(rows),
        "orders": len(orders),
        "parcels": parcels,
        "value": _money(value),
        "over_7_days": over_7,
        "over_20_days": over_20,
        "failed": failed,
    }
    return _GAP_HEADER, rows, totals


# ── 2. Order ageing (dispatch-by breach) ─────────────────────────────────────
_AGEING_HEADER = [
    "Order ID", "Buyer", "City", "State", "Sheet", "Sheet file", "Order date",
    "Dispatch by", "Days overdue", "Bucket", "Order status", "Parcels",
    "Scanned", "Not scanned", "Value",
]

# Ordered worst-last so a sort by bucket reads as an escalation.
AGEING_BUCKETS = ["No due date", "Not due", "Under 2 days", "2-7 days", "7-30 days", "Over 30 days"]


def _ageing_bucket(dispatch_by, now):
    if not dispatch_by:
        return "No due date", ""
    days = (now - dispatch_by).days
    if days < 0:
        return "Not due", days
    if days < 2:
        return "Under 2 days", days
    if days < 7:
        return "2-7 days", days
    if days < 30:
        return "7-30 days", days
    return "Over 30 days", days


def ageing(company, channel, *, bucket=None, **_):
    """Open orders measured against the dispatch-by date the marketplace set.

    ``dispatch_by`` is captured on every imported order and, until this report, was
    read by nothing. Flipkart penalises a breach and can suppress the listing, so
    the question "what must go out today, and what have we already missed" needs an
    answer that does not depend on someone eyeballing a sheet.

    Cancelled orders are out; everything not yet DISPATCHED is in, including orders
    that are part-scanned.
    """
    from ..models import MarketplaceOrder, MarketplaceOrderStatus

    qs = (
        MarketplaceOrder.objects.filter(company=company, channel=channel, is_cancelled=False)
        .exclude(status=MarketplaceOrderStatus.DISPATCHED)
        .select_related("import_batch")
        .prefetch_related("lines")
        .order_by("dispatch_by", "id")
    )
    orders = list(qs)
    scanned_map = _scanned_by_order(o.pk for o in orders)

    now = timezone.now()
    rows, value = [], Decimal("0")
    counts = {b: 0 for b in AGEING_BUCKETS}
    overdue = parcels_total = scanned_total = 0

    for o in orders:
        label, days = _ageing_bucket(o.dispatch_by, now)
        counts[label] += 1
        lines = list(o.lines.all())
        trackings = {(l.tracking_id or "").strip() for l in lines if (l.tracking_id or "").strip()}
        hit = trackings & scanned_map.get(o.pk, set())
        amount = sum((_d(l.invoice_amount) for l in lines), Decimal("0"))
        n_parcels = len(trackings) or len(lines)

        if label not in ("Not due", "No due date"):
            overdue += 1
        parcels_total += n_parcels
        scanned_total += len(hit)
        if bucket and label != bucket:
            continue
        value += amount
        batch = o.import_batch
        rows.append([
            o.order_id, o.buyer_name, o.city, o.state,
            _sheet_label(batch), batch.filename if batch else "",
            o.order_date.isoformat() if o.order_date else "",
            _dt(o.dispatch_by), days, label, o.status,
            n_parcels, len(hit), max(n_parcels - len(hit), 0), _money(amount),
        ])

    totals = {
        "orders": len(rows),
        "overdue": overdue,
        "parcels": parcels_total,
        "scanned": scanned_total,
        "value": _money(value),
    }
    totals.update({b.lower().replace(" ", "_").replace("-", "_"): counts[b] for b in AGEING_BUCKETS})
    return _AGEING_HEADER, rows, totals


# ── 3. Sheet audit (import funnel) ───────────────────────────────────────────
_SHEET_HEADER = [
    "Sheet", "Filename", "Uploaded", "Uploaded by", "Status", "File rows",
    "Orders imported", "Lines imported", "Orders now", "Parcels", "Scanned",
    "Not scanned", "Orders dispatched", "DNs posted", "Skipped rows",
    "Unaccounted rows", "Scanned %",
]


def sheet_audit(company, channel, *, date_from=None, date_to=None, **_):
    """Per-sheet funnel: file rows → orders → parcels → scanned → dispatched → posted.

    Two things hide without it. A sheet can import and then go nowhere — nobody
    notices, because the board only ever shows the sheet you opened. And rows can
    go missing between the file and the parcels: ``Unaccounted rows`` is
    ``file rows − lines imported − skipped rows``, which is the exact discrepancy
    that took a hand-diff of the CSV to find last time.
    """
    from ..models import (
        MarketplaceDispatch, MarketplaceImportSkip, MarketplaceOrder,
        MarketplaceOrderLine, MarketplaceOrderStatus, OrderImportBatch,
    )

    batches = OrderImportBatch.objects.filter(
        company=company, channel=channel, is_active=True
    )
    if date_from:
        batches = batches.filter(created_at__date__gte=date_from)
    if date_to:
        batches = batches.filter(created_at__date__lte=date_to)
    batches = list(batches.select_related("created_by").order_by("-id"))
    ids = [b.id for b in batches]

    order_stats = {
        r["import_batch"]: r
        for r in MarketplaceOrder.objects.filter(import_batch_id__in=ids)
        .values("import_batch")
        .annotate(
            n=Count("id"),
            dispatched=Count("id", filter=Q(status=MarketplaceOrderStatus.DISPATCHED)),
        )
    }
    line_rows = list(
        MarketplaceOrderLine.objects.filter(order__import_batch_id__in=ids)
        .values_list("order__import_batch_id", "order_id", "tracking_id")
    )
    scanned_map = _scanned_by_order({r[1] for r in line_rows})
    parcels, scanned = defaultdict(int), defaultdict(int)
    for batch_id, order_id, tracking in line_rows:
        tracking = (tracking or "").strip()
        if not tracking:
            continue
        parcels[batch_id] += 1
        if tracking in scanned_map.get(order_id, set()):
            scanned[batch_id] += 1

    dns = {
        r["order__import_batch"]: r["n"]
        for r in MarketplaceDispatch.objects.filter(
            order__import_batch_id__in=ids, sap_delivery_note_doc_entry__isnull=False
        )
        .values("order__import_batch")
        .annotate(n=Count("sap_delivery_note_doc_entry", distinct=True))
    }
    skips = {
        r["import_batch"]: r
        for r in MarketplaceImportSkip.objects.filter(import_batch_id__in=ids)
        .values("import_batch")
        .annotate(rows=Sum("row_count"), n=Count("id"))
    }

    rows, unaccounted_total, stalled = [], 0, 0
    for b in batches:
        stat = order_stats.get(b.id) or {}
        skip = skips.get(b.id) or {}
        skipped_rows = int(skip.get("rows") or 0)
        n_parcels, n_scanned = parcels[b.id], scanned[b.id]
        dispatched = int(stat.get("dispatched") or 0)
        gap = int(b.row_count or 0) - int(b.line_count or 0) - skipped_rows
        unaccounted_total += max(gap, 0)
        if (stat.get("n") or 0) and not dispatched:
            stalled += 1
        rows.append([
            b.id, b.filename, _day(b.created_at),
            b.created_by.full_name if b.created_by_id else "",
            b.status, b.row_count, b.order_count, b.line_count,
            int(stat.get("n") or 0), n_parcels, n_scanned,
            max(n_parcels - n_scanned, 0), dispatched, int(dns.get(b.id) or 0),
            skipped_rows, gap,
            f"{(n_scanned * 100.0 / n_parcels):.0f}%" if n_parcels else "",
        ])

    totals = {
        "sheets": len(rows),
        "file_rows": sum(int(b.row_count or 0) for b in batches),
        "parcels": sum(parcels[b.id] for b in batches),
        "scanned": sum(scanned[b.id] for b in batches),
        "unaccounted_rows": unaccounted_total,
        "sheets_with_no_dispatch": stalled,
    }
    return _SHEET_HEADER, rows, totals


# ── 4. SKU / FSN mapping coverage ────────────────────────────────────────────
_SKU_HEADER = [
    "FSN/ASIN", "Marketplace SKU", "SKU name", "Mapped", "Mapping type",
    "SAP item / Combo", "Order lines", "Orders", "Open lines", "Quantity",
    "Value", "First seen", "Last seen",
]


def sku_coverage(company, channel, *, mapped=None, **_):
    """Every marketplace SKU seen in an order, and whether it resolves to an item.

    An unmapped line cannot be issued and cannot go on a delivery note, so today it
    is discovered when an order fails at the counter. Listed the other way round —
    SKUs first, with the order volume behind each — the failures are visible before
    they happen, worst first.

    ``mapped`` filters to ``"yes"`` or ``"no"``.
    """
    from ..models import MarketplaceOrderLine, MarketplaceOrderStatus, SkuMapping

    by_fsn, by_sku = {}, {}
    for m in SkuMapping.objects.filter(
        company=company, channel=channel, is_active=True
    ).select_related("combo"):
        if (m.fsn or "").strip():
            by_fsn.setdefault(m.fsn.strip(), m)
        if (m.marketplace_sku or "").strip():
            by_sku.setdefault(m.marketplace_sku.strip(), m)

    groups = {}
    rows_qs = MarketplaceOrderLine.objects.filter(
        order__company=company, order__channel=channel
    ).values_list(
        "fsn", "marketplace_sku", "sku_name", "order_id", "order__status",
        "ordered_quantity", "invoice_amount", "order__created_at",
    )
    for fsn, sku, name, order_id, status, qty, amount, created in rows_qs.iterator():
        key = ((fsn or "").strip(), (sku or "").strip())
        g = groups.setdefault(key, {
            "name": name or "", "lines": 0, "orders": set(), "open": 0,
            "qty": Decimal("0"), "value": Decimal("0"), "first": created, "last": created,
        })
        g["lines"] += 1
        g["orders"].add(order_id)
        if status != MarketplaceOrderStatus.DISPATCHED:
            g["open"] += 1
        g["qty"] += _d(qty)
        g["value"] += _d(amount)
        if created and (not g["first"] or created < g["first"]):
            g["first"] = created
        if created and (not g["last"] or created > g["last"]):
            g["last"] = created
        if not g["name"] and name:
            g["name"] = name

    rows = []
    n_mapped = n_unmapped = unmapped_lines = unmapped_open = 0
    unmapped_value = Decimal("0")
    for (fsn, sku), g in sorted(groups.items(), key=lambda kv: -kv[1]["lines"]):
        m = by_fsn.get(fsn) if fsn else None
        if m is None:
            m = by_sku.get(sku)
        is_mapped = m is not None
        if is_mapped:
            n_mapped += 1
        else:
            n_unmapped += 1
            unmapped_lines += g["lines"]
            unmapped_open += g["open"]
            unmapped_value += g["value"]
        if mapped == "yes" and not is_mapped:
            continue
        if mapped == "no" and is_mapped:
            continue
        target = ""
        if m is not None:
            target = m.combo.code if m.combo_id else (m.fg_item_code or "")
        rows.append([
            fsn, sku, g["name"], "yes" if is_mapped else "no",
            m.sku_type if m is not None else "", target,
            g["lines"], len(g["orders"]), g["open"], f"{g['qty']:.3f}",
            _money(g["value"]), _day(g["first"]), _day(g["last"]),
        ])

    totals = {
        "skus": n_mapped + n_unmapped,
        "mapped": n_mapped,
        "unmapped": n_unmapped,
        "unmapped_lines": unmapped_lines,
        "unmapped_open_lines": unmapped_open,
        "unmapped_value": _money(unmapped_value),
    }
    return _SKU_HEADER, rows, totals


# ── 5. GST place of supply / branch ──────────────────────────────────────────
_GST_HEADER = [
    "DN Number", "DN date", "Order ID", "Buyer", "State", "Place of supply (rule)",
    "Ship-to posted", "Match", "Branch (BPLId)", "Parcels", "GST rate",
    "Taxable", "Tax", "Total",
]


def _warehouse_index(company, channel):
    """``(by_code, default)`` for the channel's active warehouses.

    Mirrors ``confirm_service._warehouse_for`` without a query per row.
    """
    from ..models import MarketplaceWarehouse

    active = list(
        MarketplaceWarehouse.objects.filter(
            company=company, channel=channel, is_active=True
        ).order_by("-is_default", "id")
    )
    by_code = {}
    for w in active:
        by_code.setdefault((w.sap_warehouse_code or "").strip(), w)
    return by_code, (active[0] if active else None)


def gst_branch(company, channel, *, date_from=None, date_to=None, mismatch_only=False, **_):
    """Posted delivery notes by GST place of supply — the rule vs what actually posted.

    The routing rule changed recently (Haryana ships from Haryana, every other state
    from Andhra Pradesh), and it decides the place of supply on nearly every note.
    ``Place of supply (rule)`` is what ``shipto_by_state`` resolves for the buyer's
    state today; ``Ship-to posted`` is what was stamped on the note when it was cut.

    Notes cut before that stamp existed show ``Ship-to posted`` blank and match "—":
    unknowable, not wrong. ``mismatch_only`` keeps just the notes that disagree.
    """
    from ..models import MarketplaceDispatch
    from .confirm_service import _shipto_for_state

    qs = (
        MarketplaceDispatch.objects.filter(
            company=company, channel=channel, sap_delivery_note_doc_entry__isnull=False
        )
        .select_related("order")
        .prefetch_related("order__lines")
        .order_by("-sap_delivery_note_doc_entry", "id")
    )
    if date_from:
        qs = qs.filter(confirmed_at__date__gte=date_from)
    if date_to:
        qs = qs.filter(confirmed_at__date__lte=date_to)

    by_code, default_wh = _warehouse_index(company, channel)
    dispatches = list(qs)
    scanned_map = _scanned_by_dispatch(d.pk for d in dispatches)
    rows, states = [], set()
    taxable_t = tax_t = total_t = Decimal("0")
    mismatched = unknown = no_rule = 0

    for d in dispatches:
        order = d.order
        wh = by_code.get((d.sap_warehouse_code or "").strip()) or default_wh
        rule = _shipto_for_state(order.state, wh) if wh else ""
        posted = (d.sap_ship_to_code or "").strip()
        if not posted:
            match, unknown = "—", unknown + 1
        elif posted == rule:
            match = "yes"
        else:
            match, mismatched = "no", mismatched + 1
        if not rule:
            no_rule += 1

        lines = _billed_lines(d, scanned_map[d.pk])
        total = taxable = tax = Decimal("0")
        rates = set()
        for l in lines:
            l_taxable, l_tax, l_rate = _gst_split(l)
            total += _d(l.invoice_amount)
            taxable += l_taxable
            tax += l_tax
            rates.add(l_rate)
        rates.discard(None)
        states.add((order.state or "").strip())
        taxable_t += taxable
        tax_t += tax
        total_t += total

        if mismatch_only and match != "no":
            continue
        rows.append([
            d.sap_delivery_note_num, _day(d.sap_delivery_note_doc_date or d.confirmed_at),
            order.order_id, order.buyer_name, order.state, rule, posted, match,
            wh.sap_branch_id if wh else "", len(lines),
            "/".join(f"{r:.2f}%".rstrip("0").rstrip(".").replace("%", "") + "%"
                     for r in sorted(rates)) if rates else "",
            _money(taxable), _money(tax), _money(total),
        ])

    totals = {
        "delivery_notes": len(rows),
        "states": len([s for s in states if s]),
        "taxable": _money(taxable_t),
        "tax": _money(tax_t),
        "total": _money(total_t),
        "mismatched": mismatched,
        "not_stamped": unknown,
        "state_has_no_rule": no_rule,
    }
    return _GST_HEADER, rows, totals


# ── 6. Scan throughput ───────────────────────────────────────────────────────
_THROUGHPUT_HEADER = [
    "Date", "Operator", "Parcels scanned", "Item scans", "Orders", "Sheets",
    "First scan", "Last scan", "Active hours", "Parcels / hour",
]


def scan_throughput(company, channel, *, date_from=None, date_to=None, **_):
    """Parcels scanned per operator per day, with the working span behind it.

    Outward volume is bursty and, right now, concentrated on very few people — which
    is invisible while the only view of scanning is one sheet's board. A day-by-day,
    operator-by-operator count is what staffing and shift decisions need.

    ``Item scans`` counts barcodes; ``Parcels scanned`` counts distinct Tracking IDs,
    since a multi-item parcel is scanned more than once but ships once.
    """
    from ..models import MarketplaceScan

    qs = MarketplaceScan.objects.filter(
        company=company, dispatch__channel=channel, is_active=True
    )
    if date_from:
        qs = qs.filter(scanned_at__date__gte=date_from)
    if date_to:
        qs = qs.filter(scanned_at__date__lte=date_to)

    buckets = {}
    for at, name, barcode, order_id, batch_id in qs.values_list(
        "scanned_at", "scanned_by__full_name", "barcode_raw",
        "dispatch__order_id", "dispatch__order__import_batch_id",
    ).iterator():
        local = timezone.localtime(at)
        key = (local.date(), name or "—")
        b = buckets.setdefault(key, {
            "scans": 0, "parcels": set(), "orders": set(), "sheets": set(),
            "first": local, "last": local,
        })
        b["scans"] += 1
        b["parcels"].add((barcode or "").split("#", 1)[0])
        b["orders"].add(order_id)
        if batch_id:
            b["sheets"].add(batch_id)
        b["first"] = min(b["first"], local)
        b["last"] = max(b["last"], local)

    rows, parcels_t, scans_t, best = [], 0, 0, ("", 0)
    for (day, name), b in sorted(buckets.items(), key=lambda kv: (kv[0][0], kv[0][1]), reverse=True):
        n_parcels = len(b["parcels"])
        hours = (b["last"] - b["first"]).total_seconds() / 3600.0
        parcels_t += n_parcels
        scans_t += b["scans"]
        if n_parcels > best[1]:
            best = (f"{day.isoformat()} · {name}", n_parcels)
        rows.append([
            day.isoformat(), name, n_parcels, b["scans"], len(b["orders"]), len(b["sheets"]),
            b["first"].strftime("%H:%M"), b["last"].strftime("%H:%M"),
            f"{hours:.1f}" if hours else "",
            f"{(n_parcels / hours):.0f}" if hours >= 0.25 else "",
        ])

    days = {day for day, _n in buckets}
    operators = {name for _day, name in buckets}
    totals = {
        "rows": len(rows),
        "days_worked": len(days),
        "operators": len(operators),
        "parcels": parcels_t,
        "item_scans": scans_t,
        "parcels_per_day": f"{(parcels_t / len(days)):.0f}" if days else "0",
        "best_day": best[0],
    }
    return _THROUGHPUT_HEADER, rows, totals


INSIGHTS = {
    "sap-posting-gap": sap_posting_gap,
    "ageing": ageing,
    "sheet-audit": sheet_audit,
    "sku-coverage": sku_coverage,
    "gst-branch": gst_branch,
    "scan-throughput": scan_throughput,
}
