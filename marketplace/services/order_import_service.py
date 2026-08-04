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
from django.utils import timezone

logger = logging.getLogger(__name__)

from ..models import (
    ImportSkipReason,
    MarketplaceChannel,
    MarketplaceDispatch,
    MarketplaceDispatchStatus,
    MarketplaceImportSkip,
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


def _retrack_carried_over(order, order_rows, dispatch=None):
    """Refresh a carried-over order's tracking IDs from a newer sheet.

    Flipkart sometimes re-manifests an order and re-lists it (in a later CSV) with a
    DIFFERENT Tracking ID. Such an order is kept on its original sheet (it already has
    a live dispatch), but its stored tracking would no longer match the parcel the
    operator now holds. We match each new-sheet row to an existing line by ORDER ITEM
    ID (then SKU) and update the line's ``tracking_id``.

    Returns ``True`` if any tracking changed. Because scan-completeness is recomputed
    from the line tracking IDs (see ``dispatch_is_fully_scanned`` / the Outward board),
    a change drops the order back into "to scan" and re-blocks confirm until the new
    tracking is scanned — exactly the desired re-open behaviour. Scans made against the
    OLD tracking are deactivated so they don't double-count at confirm ("Scan counts
    deviate from the order") once the new tracking is scanned.
    """
    by_item, by_sku = {}, {}
    for r in order_rows:
        tid = (r.get("tracking") or "").strip()
        if not tid:
            continue
        iid = _clean_id(r.get("order_item_id"))
        if iid:
            by_item.setdefault(iid, tid)
        sku = (r.get("sku") or "").strip().upper()
        if sku:
            by_sku.setdefault(sku, tid)

    changed = []
    for line in order.lines.all():
        new_tid = (by_item.get(_clean_id(line.order_item_id))
                   or by_sku.get((line.marketplace_sku or "").upper()))
        if new_tid and new_tid != (line.tracking_id or "").strip():
            line.tracking_id = new_tid[:120]
            changed.append(line)
    if not changed:
        return False

    MarketplaceOrderLine.objects.bulk_update(changed, ["tracking_id"])
    # Keep the order-level tracking (legacy/return-scan fallback) in sync.
    current = {(l.tracking_id or "").strip() for l in order.lines.all() if (l.tracking_id or "").strip()}
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


# Fields written on update (kept in sync with _order_fields + audit/status).
_UPDATE_FIELDS = [
    "order_date", "buyer_name", "ship_to_name", "flipkart_shipment_id", "order_type",
    "address_line1", "address_line2", "city", "state", "pin_code", "dispatch_by",
    "tracking_id", "is_cancelled", "import_batch", "status", "updated_by", "updated_at",
]


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
    """Dry-run: report which orders are new vs already-present (duplicates), plus
    any unmapped SKUs — WITHOUT writing anything. Drives the pre-import review so
    the user can acknowledge duplicates before they are re-imported.
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
    create/refresh orders for ``channel``. Returns the batch + counts.

    Set-based (a handful of bulk queries regardless of sheet size) so a full
    export imports fast even against a remote database. Parsing is channel-specific
    (see :func:`parse_rows_for`), everything after it is shared and channel-scoped.

    ``skip_duplicates=True`` imports only orders that are NOT already present
    (the user chose not to re-import existing ones); existing orders are left
    untouched and reported under ``summary.duplicates_skipped``.
    """
    rows = parse_rows_for(channel, text=text, content=content,
                          filename=filename, content_type=content_type)
    now = timezone.now()

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

    # One query for every order that already exists.
    existing = {
        o.order_id: o
        for o in MarketplaceOrder.objects.filter(
            company=company, channel=channel, order_id__in=list(by_order)
        )
    }

    # Orders that already have a live (non-cancelled) dispatch are being worked in
    # their ORIGINAL sheet. Re-uploading an overlapping CSV must not drag them into
    # this new sheet (nor replace their lines) — doing so made them appear in the
    # new sheet already "done" without anyone scanning it. Such orders are left
    # completely untouched, exactly where they were first imported.
    existing_ids = [o.id for o in existing.values()]
    live_dispatch = {}  # order pk -> latest non-cancelled dispatch
    if existing_ids:
        for d in (
            MarketplaceDispatch.objects.filter(company=company, order_id__in=existing_ids)
            .exclude(status=MarketplaceDispatchStatus.CANCELLED)
            .order_by("order_id", "-created_at", "-id")
        ):
            live_dispatch.setdefault(d.order_id, d)
    dispatched_ids = set(live_dispatch)

    to_create, to_update = [], []
    duplicates_skipped = 0
    dispatched_skipped = 0
    retracked = 0
    # (oid, reason, existing_order, order_rows) for orders present in the CSV but
    # left on their original sheet — recorded so the board can explain the skip.
    skip_records = []
    # Orders we actually write (new + refreshed) — only these get their lines replaced.
    processed_rows = {}
    for oid, order_rows in by_order.items():
        cancelled = all(_is_cancelled(r["order_state"]) for r in order_rows)
        fields = _order_fields(order_rows[0], cancelled)
        obj = existing.get(oid)
        if obj is None:
            obj = MarketplaceOrder(
                company=company, channel=channel, order_id=oid,
                import_batch=batch, created_by=user, updated_by=user, **fields,
            )
            # Cancellation is tracked by is_cancelled (set via fields); the order
            # keeps status OPEN so a later re-approval sheet recovers it cleanly.
            to_create.append(obj)
            processed_rows[oid] = order_rows
        elif skip_duplicates:
            # Existing order the user chose NOT to re-import — leave untouched.
            duplicates_skipped += 1
            skip_records.append((oid, ImportSkipReason.DUPLICATE, obj, order_rows))
            continue
        elif obj.id in dispatched_ids:
            # Already being worked under its original sheet. The Tracking ID — not the
            # order id — is the parcel's identity: if this sheet re-lists the order with
            # a CHANGED tracking id (Flipkart re-manifested it), the operator now holds a
            # DIFFERENT parcel that must be scanned here, so pull it onto THIS sheet and
            # show it DIRECTLY in "To scan" rather than as a "carried over" note. Only a
            # same-tracking re-list is a true carry-over left on its original sheet.
            d = live_dispatch.get(obj.id)
            if d is not None and d.status != MarketplaceDispatchStatus.CONFIRMED:
                # Live but not yet shipped: re-track in place and reuse its dispatch
                # (its stale scans are retired so they don't double-count at confirm).
                if _retrack_carried_over(obj, order_rows, d):
                    obj.import_batch = batch
                    obj.updated_by = user
                    obj.updated_at = now
                    obj.save(update_fields=["import_batch", "tracking_id", "updated_by", "updated_at"])
                    retracked += 1
                    continue
            elif d is not None:  # d.status == CONFIRMED
                # The first parcel already shipped (delivery note posted). A changed
                # tracking is a brand-new parcel, NOT the shipped one — so re-track the
                # lines to the new tracking (leaving the CONFIRMED dispatch and its scans
                # intact as history) and open a fresh DRAFT dispatch so the new parcel
                # surfaces in "To scan" here and can be scanned + dispatched on its own.
                if _retrack_carried_over(obj, order_rows, dispatch=None):
                    MarketplaceDispatch.objects.create(
                        company=company, channel=channel, order=obj,
                        sap_warehouse_code=obj.sap_warehouse_code or "",
                        status=MarketplaceDispatchStatus.DRAFT,
                        created_by=user, updated_by=user,
                    )
                    obj.import_batch = batch
                    obj.updated_by = user
                    obj.updated_at = now
                    obj.save(update_fields=["import_batch", "tracking_id", "updated_by", "updated_at"])
                    retracked += 1
                    continue
            dispatched_skipped += 1
            skip_records.append((oid, ImportSkipReason.DISPATCHED, obj, order_rows))
            continue
        else:
            for key, value in fields.items():
                setattr(obj, key, value)
            obj.import_batch = batch
            obj.updated_by = user
            obj.updated_at = now
            # Un-cancel recovery: a previously cancelled-at-import order (legacy
            # RETURNED, no dispatch — dispatched orders are skipped above) that is
            # now re-approved returns to OPEN so it can be processed again.
            if not cancelled and obj.status == MarketplaceOrderStatus.RETURNED:
                obj.status = MarketplaceOrderStatus.OPEN
            to_update.append(obj)
            processed_rows[oid] = order_rows

    created_objs = MarketplaceOrder.objects.bulk_create(to_create, batch_size=500)
    if to_update:
        MarketplaceOrder.objects.bulk_update(to_update, _UPDATE_FIELDS, batch_size=500)

    orders_by_id = {o.order_id: o for o in to_update}
    for obj in created_objs:
        orders_by_id[obj.order_id] = obj

    # Replace lines only for orders we actually wrote (idempotent snapshot).
    if orders_by_id:
        MarketplaceOrderLine.objects.filter(order__in=orders_by_id.values()).delete()
    line_objs = []
    blank_sku_skipped = 0  # rows dropped because the SKU column was blank
    for oid, order_rows in processed_rows.items():
        order = orders_by_id[oid]
        for row in order_rows:
            sku = row["sku"].strip()
            if not sku:
                blank_sku_skipped += 1
                continue
            line_objs.append(MarketplaceOrderLine(
                order=order,
                marketplace_sku=sku[:120],
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
            ))
    MarketplaceOrderLine.objects.bulk_create(line_objs, batch_size=1000)

    # Persist the carried-over orders so the board can explain the skip.
    if skip_records:
        MarketplaceImportSkip.objects.bulk_create([
            MarketplaceImportSkip(
                company=company, import_batch=batch, kept_order=obj, order_id=oid, reason=reason,
                row_count=len(order_rows),
                tracking_ids=[r["tracking"].strip() for r in order_rows if r["tracking"].strip()],
            )
            for (oid, reason, obj, order_rows) in skip_records
        ], batch_size=500)

    created, updated, line_count = len(to_create), len(to_update), len(line_objs)
    skipped_order_rows = sum(len(rows) for (_oid, _r, _o, rows) in skip_records)

    batch.order_count = created + updated
    batch.line_count = line_count
    # Existing integer keys are kept intact (other code + the serializer read them);
    # blank_sku_skipped and skipped_order_rows are added so row arithmetic reconciles:
    #   row_count = lines + blank_sku_skipped + skipped_order_rows + skipped(no order id)
    batch.summary = {
        "created": created, "updated": updated, "skipped": skipped,
        "duplicates_skipped": duplicates_skipped,
        "dispatched_skipped": dispatched_skipped,
        "retracked": retracked,
        "blank_sku_skipped": blank_sku_skipped,
        "skipped_order_rows": skipped_order_rows,
        "orders": created + updated, "lines": line_count,
    }
    batch.save(update_fields=["order_count", "line_count", "summary"])
    return batch
