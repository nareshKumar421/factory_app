"""Ingest a Flipkart order sheet (CSV) into marketplace orders + lines.

Replaces the (never-built) live-API intake: an operator uploads the Flipkart
"Order" CSV export and this service parses it, groups rows by ``Order Id`` and
creates/refreshes :class:`MarketplaceOrder` + :class:`MarketplaceOrderLine`,
recording an :class:`OrderImportBatch`.

Idempotent on ``(company, channel, order_id)`` — re-uploading a sheet (or an
overlapping window) refreshes orders in place, never duplicating. See
``MARKETPLACE_FLIPKART_SHEET_FLOW.md`` §3 / §5.1.
"""
import csv
import io
import logging
from datetime import datetime
from decimal import Decimal, InvalidOperation

from django.db import transaction

logger = logging.getLogger(__name__)

from ..models import (
    MarketplaceChannel,
    MarketplaceOrder,
    MarketplaceOrderLine,
    MarketplaceOrderStatus,
    MarketplaceScan,
    OrderImportBatch,
)
from .errors import MarketplaceError

# Canonical header → the sheet's column label. Matching is case/space-insensitive.
COLUMNS = {
    "order_id": "Order Id",
    "order_item_id": "ORDER ITEM ID",
    "shipment_id": "Shipment ID",
    "ordered_on": "Ordered On",
    "order_state": "Order State",
    "order_type": "Order Type",
    "fsn": "FSN",
    "sku": "SKU",
    "product": "Product",
    "quantity": "Quantity",
    "hsn": "HSN CODE",
    "unit_price": "Selling Price Per Item",
    "invoice_amount": "Invoice Amount",
    # Optional — present on Flipkart sheets that carry invoice details. Captured into
    # raw_row so the posted-DN export can reproduce the seller's invoice columns.
    "invoice_no": "Invoice No.",
    "invoice_date": "Invoice Date (mm/dd/yy)",
    "dispatch_after": "Dispatch After date",
    "cgst": "CGST",
    "igst": "IGST",
    "sgst": "SGST",
    "buyer": "Buyer name",
    "ship_to": "Ship to name",
    "addr1": "Address Line 1",
    "addr2": "Address Line 2",
    "city": "City",
    "state": "State",
    "pin": "PIN Code",
    "dispatch_by": "Dispatch by date",
    "tracking": "Tracking ID",
}
_DATE_FORMATS = (
    "%b %d, %Y %H:%M:%S", "%b %d, %Y", "%m/%d/%y", "%m/%d/%Y",
    # ISO (Amazon CSV exports) — additive; a Flipkart date never matches these.
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d",
)


def _norm(s):
    return (s or "").strip().lower().replace("_", " ").replace("  ", " ")


def _dec(value):
    v = (value or "").strip()
    if not v or v.upper() == "NA":
        return Decimal("0")
    try:
        return Decimal(v.replace(",", ""))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def _parse_dt(value):
    v = (value or "").strip()
    if not v:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(v, fmt)
        except ValueError:
            continue
    return None


def _clean_id(value):
    # Flipkart prefixes ORDER ITEM ID with a leading apostrophe to keep Excel from
    # turning it into a number.
    return (value or "").strip().lstrip("'").strip()


def _is_cancelled(order_state):
    return "cancel" in (order_state or "").strip().lower()


def _line_from_row(order, row):
    """Build a :class:`MarketplaceOrderLine` from one parsed CSV row.

    One row is one PARCEL: it carries its own ORDER ITEM ID and its own Tracking ID.
    Callers skip rows whose SKU is blank. Shared by the first import and by the
    re-track reconcile so a line means the same thing however it came to exist.
    """
    return MarketplaceOrderLine(
        order=order,
        marketplace_sku=row["sku"].strip()[:120],
        sku_name=row["product"][:200],
        ordered_quantity=_dec(row["quantity"]) or Decimal("1"),
        order_item_id=_clean_id(row["order_item_id"])[:120],
        fsn=row["fsn"][:60],
        tracking_id=row["tracking"][:120],
        order_state=row["order_state"][:40],
        hsn_code=row["hsn"][:20],
        unit_price=_dec(row["unit_price"]),
        invoice_amount=_dec(row["invoice_amount"]),
        tax_amount=_dec(row["cgst"]) + _dec(row["igst"]) + _dec(row["sgst"]),
        raw_row=row,
    )


def _retrack_carried_over(order, order_rows, dispatch=None):
    """Re-sync a re-listed order's PARCELS from a newer sheet.

    NOT part of the import path any more. Sheets are independent scanning sessions,
    so a re-listed order is imported as its own row on the new sheet instead of being
    dragged across from the old one — nothing is re-tracked in place. Kept because it
    is still the correct way to re-point an EXISTING order's lines at a newer
    manifest, which the remediation commands need.

    Flipkart re-manifests an order and re-lists it in a later CSV — often with
    different Tracking IDs, different ORDER ITEM IDs, and sometimes a different
    number of parcels. One CSV row is one parcel, so the sheet's rows are the truth:
    every row must end up as exactly one line carrying its tracking, or the operator
    is never asked to scan that box and it ships nothing while the order reads done.

    Rows bind to existing lines ONE-TO-ONE — by ORDER ITEM ID first, then by SKU — so
    an order shipping two boxes of the SAME SKU keeps both (matching by SKU alone
    collapses them onto one line and loses a parcel). Matched lines are updated in
    place, which preserves the operator's ``chosen_option`` / ``component_choices``
    picks; rows with no line are added; lines whose parcel has vanished from the
    manifest are removed.

    Returns ``True`` if anything changed. Because scan-completeness is recomputed from
    the line tracking IDs (see ``dispatch_is_fully_scanned`` / the Outward board), a
    change drops the order back into "to scan" and re-blocks confirm until the current
    parcels are scanned — exactly the desired re-open behaviour. Scans made against a
    tracking the order no longer carries are deactivated so they don't double-count at
    confirm ("Scan counts deviate from the order").
    """
    rows = [r for r in order_rows if (r.get("sku") or "").strip()]
    taken = set()  # indexes of rows already bound to a line

    def _claim(match):
        for i, r in enumerate(rows):
            if i not in taken and match(r):
                taken.add(i)
                return r
        return None

    pairs, orphans = [], []
    for line in order.lines.all():
        iid = _clean_id(line.order_item_id)
        row = _claim(lambda r: bool(iid) and _clean_id(r["order_item_id"]) == iid)
        (pairs.append((line, row)) if row is not None else orphans.append(line))
    for line in list(orphans):
        sku = (line.marketplace_sku or "").upper()
        row = _claim(lambda r: r["sku"].strip().upper() == sku)
        if row is not None:
            orphans.remove(line)
            pairs.append((line, row))

    updated = []
    for line, row in pairs:
        tid = row["tracking"].strip()[:120]
        iid = _clean_id(row["order_item_id"])[:120]
        if tid != (line.tracking_id or "").strip() or iid != (line.order_item_id or ""):
            line.tracking_id, line.order_item_id = tid, iid
            updated.append(line)
    added = [_line_from_row(order, r) for i, r in enumerate(rows) if i not in taken]

    if not (updated or added or orphans):
        return False
    if updated:
        MarketplaceOrderLine.objects.bulk_update(updated, ["tracking_id", "order_item_id"])
    if added:
        MarketplaceOrderLine.objects.bulk_create(added, batch_size=500)
    if orphans:
        # The parcel is gone from the manifest. Keeping the line would block confirm
        # forever on a box that is never coming.
        MarketplaceOrderLine.objects.filter(id__in=[l.id for l in orphans]).delete()

    # Keep the order-level tracking (legacy/return-scan fallback) in sync.
    current = {(l.tracking_id or "").strip() for l in order.lines.all()
               if (l.tracking_id or "").strip()}
    first = (MarketplaceOrderLine.objects.filter(order=order).order_by("id")
             .values_list("tracking_id", flat=True).first()) or ""
    if (order.tracking_id or "") != first:
        order.tracking_id = first
        order.save(update_fields=["tracking_id", "updated_at"])

    # Retire scans made against a now-removed tracking so they don't inflate the
    # scanned quantity at confirm.
    if dispatch is not None:
        stale = [
            s for s in dispatch.scans.all()
            if s.is_active and (s.barcode_raw or "").split("#", 1)[0] not in current
        ]
        if stale:
            for s in stale:
                s.is_active = False
            MarketplaceScan.objects.bulk_update(stale, ["is_active"])
    return True


def parse_rows(text):
    """Parse CSV text into a list of dict rows keyed by canonical header.

    Raises ``MarketplaceError('BAD_SHEET')`` if the required columns are absent.
    """
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        raise MarketplaceError("The sheet is empty.", code="BAD_SHEET", status_code=400)
    index = {_norm(h): i for i, h in enumerate(header)}
    col = {}
    for key, label in COLUMNS.items():
        col[key] = index.get(_norm(label))
    if col["order_id"] is None or col["sku"] is None or col["quantity"] is None:
        raise MarketplaceError(
            "Sheet is missing required columns (Order Id, SKU, Quantity).",
            code="BAD_SHEET", status_code=400,
        )

    rows = []
    for raw in reader:
        if not any((c or "").strip() for c in raw):
            continue

        def get(key):
            i = col[key]
            return raw[i].strip() if i is not None and i < len(raw) else ""

        rows.append({key: get(key) for key in COLUMNS})
    return rows


def parse_rows_for(channel, *, text=None, content=None, filename="", content_type=""):
    """Channel-dispatched sheet parsing → canonical row dicts.

    Flipkart uses the CSV parser in this module; Amazon uses its own
    (:mod:`amazon_sheet`). The two formats are kept isolated so a change to one
    channel's columns never affects the other. Both emit the same canonical shape."""
    if channel == MarketplaceChannel.AMAZON:
        from .amazon_sheet import parse_amazon_rows
        return parse_amazon_rows(
            text=text, content=content, filename=filename, content_type=content_type
        )
    # Flipkart (unchanged) — CSV text.
    if text is None and content is not None:
        text = content.decode("utf-8-sig", errors="replace")
    return parse_rows(text)


def _order_fields(head, cancelled):
    """The order-level column values from a row (the first row of an order)."""
    ordered_on = _parse_dt(head["ordered_on"])
    return {
        "order_date": ordered_on.date() if ordered_on else None,
        "buyer_name": head["buyer"][:200],
        "ship_to_name": head["ship_to"][:200],
        "flipkart_shipment_id": head["shipment_id"][:120],
        "order_type": head["order_type"][:40],
        "address_line1": head["addr1"][:255],
        "address_line2": head["addr2"][:255],
        "city": head["city"][:120],
        "state": head["state"][:120],
        "pin_code": head["pin"][:20],
        "dispatch_by": _parse_dt(head["dispatch_by"]),
        "tracking_id": head["tracking"][:120],
        "is_cancelled": cancelled,
    }



def _group_by_order(rows):
    """Group parsed rows by order id; returns (by_order, skipped_no_id)."""
    by_order = {}
    skipped = 0
    for row in rows:
        oid = row["order_id"].strip()
        if not oid:
            skipped += 1
            continue
        by_order.setdefault(oid, []).append(row)
    return by_order, skipped


def analyze(company, *, text=None, content=None, filename="", content_type="",
            channel=MarketplaceChannel.FLIPKART):
    """Dry-run: report which orders are new vs already present on an EARLIER sheet,
    plus any unmapped SKUs — WITHOUT writing anything.

    Informational only. Every order in the sheet is imported either way, because each
    sheet is its own scanning session; the counts just tell the operator which orders
    they have seen before. ``duplicate_*`` keeps its name for API compatibility and
    now means "also on an earlier sheet", not "will be skipped".
    """
    from .resolve_service import load_mappings

    rows = parse_rows_for(channel, text=text, content=content,
                          filename=filename, content_type=content_type)
    by_order, skipped = _group_by_order(rows)
    order_ids = list(by_order)

    existing = set(
        MarketplaceOrder.objects.filter(
            company=company, channel=channel, order_id__in=order_ids
        ).values_list("order_id", flat=True)
    )
    duplicate_ids = [oid for oid in order_ids if oid in existing]
    new_ids = [oid for oid in order_ids if oid not in existing]

    mappings = load_mappings(company, channel)
    # Primary key is FSN; a row is unmapped only if NEITHER its FSN nor its SKU is
    # in the master. Report the FSN (the key it should map on) when present.
    unmapped_keys = set()
    for order_rows in by_order.values():
        for r in order_rows:
            fsn = r["fsn"].strip()
            sku = r["sku"].strip()
            if not (fsn or sku):
                continue
            if (fsn and fsn.upper() in mappings) or (sku and sku.upper() in mappings):
                continue
            unmapped_keys.add(fsn or sku)
    unmapped = sorted(unmapped_keys)

    return {
        "row_count": len(rows),
        "total_orders": len(order_ids),
        "new_count": len(new_ids),
        "duplicate_count": len(duplicate_ids),
        "skipped_rows": skipped,
        "new_order_ids": new_ids,
        "duplicate_order_ids": duplicate_ids,
        "unmapped_skus": unmapped,
        "has_duplicates": bool(duplicate_ids),
    }


@transaction.atomic
def ingest(
    company, *, text=None, content=None, filename="", content_type="", user=None,
    channel=MarketplaceChannel.FLIPKART, skip_duplicates=False,
):
    """Parse a sheet (``text`` for CSV, or ``content`` bytes for CSV/xlsx) and
    create this batch's orders for ``channel``. Returns the batch + counts.

    EVERY order the sheet lists is imported into it. A sheet is an independent
    scanning session: an order already present on an earlier sheet gets a SECOND row
    here, with its own lines, dispatch and scans, and is scanned and confirmed here
    on its own — the earlier sheet is left exactly as it was and stays fully live.

    Set-based (a handful of bulk queries regardless of sheet size) so a full
    export imports fast even against a remote database. Parsing is channel-specific
    (see :func:`parse_rows_for`), everything after it is shared and channel-scoped.

    ``skip_duplicates`` is accepted for API compatibility and ignored: nothing is
    a duplicate now, because sheets no longer share order rows.
    """
    rows = parse_rows_for(channel, text=text, content=content,
                          filename=filename, content_type=content_type)
    batch = OrderImportBatch.objects.create(
        company=company, channel=channel, filename=filename,
        row_count=len(rows), created_by=user,
    )

    # Retain the original sheet (the model documents this) so a sheet's carried-over
    # skips can be re-derived later and for audit/export. Best-effort — a storage
    # hiccup must never block the import.
    raw_bytes = content if content is not None else (text.encode("utf-8") if text else None)
    if raw_bytes:
        try:
            from django.core.files.base import ContentFile
            batch.raw_file.save(
                filename or f"import-{batch.id}.csv",
                ContentFile(raw_bytes), save=True,
            )
        except Exception:  # noqa: BLE001 — retention is best-effort
            logger.warning("Could not retain raw sheet for batch %s", batch.id)

    by_order, skipped = _group_by_order(rows)

    # THIS SHEET IS ITS OWN SCANNING SESSION. Every order the sheet lists is created
    # fresh against this batch — including one that already exists on an earlier
    # sheet, which keeps its own row, lines, dispatch and scans over there. Nothing
    # from a previous sheet is moved, rewritten, hidden or skipped, so an order can
    # read CONFIRMED on the sheet it shipped on and PENDING here, and be scanned and
    # confirmed again here. Both sheets stay live and independent in both directions.
    #
    # The one thing NOT repeated is the SAP delivery note: the goods only left
    # inventory once, so confirming the repeat marks it NOT_REQUIRED rather than
    # cutting a second note (see confirm_service._already_shipped_elsewhere).
    #
    # One query says which of this sheet's orders have been seen before — purely so
    # the summary can report it. The import itself does not branch on the answer.
    repeat_ids = set(
        MarketplaceOrder.objects.filter(
            company=company, channel=channel, order_id__in=list(by_order)
        )
        .exclude(import_batch=batch)
        .values_list("order_id", flat=True)
    )

    to_create = []
    for oid, order_rows in by_order.items():
        cancelled = all(_is_cancelled(r["order_state"]) for r in order_rows)
        # Cancellation is tracked by is_cancelled (set via fields); the order keeps
        # status OPEN so a later re-approval sheet recovers it cleanly.
        to_create.append(MarketplaceOrder(
            company=company, channel=channel, order_id=oid, import_batch=batch,
            created_by=user, updated_by=user, **_order_fields(order_rows[0], cancelled),
        ))
    created_objs = MarketplaceOrder.objects.bulk_create(to_create, batch_size=500)
    orders_by_id = {o.order_id: o for o in created_objs}

    line_objs = []
    blank_sku_skipped = 0  # rows dropped because the SKU column was blank
    for oid, order_rows in by_order.items():
        order = orders_by_id[oid]
        for row in order_rows:
            if not row["sku"].strip():
                blank_sku_skipped += 1
                continue
            line_objs.append(_line_from_row(order, row))
    MarketplaceOrderLine.objects.bulk_create(line_objs, batch_size=1000)

    created, line_count = len(created_objs), len(line_objs)
    batch.order_count = created
    batch.line_count = line_count
    # Row arithmetic reconciles as:
    #   row_count = lines + blank_sku_skipped + skipped (rows carrying no order id)
    # The carry-over keys are kept at zero rather than dropped: no order is left
    # behind or moved any more, but batches imported under the old behaviour still
    # hold real values there and mp_backfill_import_skips reads them.
    batch.summary = {
        "created": created, "updated": 0, "skipped": skipped,
        # Orders on this sheet that also exist on an earlier one. Informational —
        # they were imported here exactly like any other order.
        "repeat_orders": len(repeat_ids),
        "duplicates_skipped": 0,
        "dispatched_skipped": 0,
        "retracked": 0,
        "blank_sku_skipped": blank_sku_skipped,
        "skipped_order_rows": 0,
        "retracked_rows": 0,
        "retracked_lines": 0,
        "orders": created,
        "lines": line_count,
    }
    batch.save(update_fields=["order_count", "line_count", "summary"])
    return batch
