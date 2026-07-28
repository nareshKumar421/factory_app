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
from datetime import datetime
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone

from ..models import (
    ImportSkipReason,
    MarketplaceChannel,
    MarketplaceDispatch,
    MarketplaceDispatchStatus,
    MarketplaceImportSkip,
    MarketplaceOrder,
    MarketplaceOrderLine,
    MarketplaceOrderStatus,
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
_DATE_FORMATS = ("%b %d, %Y %H:%M:%S", "%b %d, %Y", "%m/%d/%y", "%m/%d/%Y")


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


def analyze(company, *, text, channel=MarketplaceChannel.FLIPKART):
    """Dry-run: report which orders are new vs already-present (duplicates), plus
    any unmapped SKUs — WITHOUT writing anything. Drives the pre-import review so
    the user can acknowledge duplicates before they are re-imported.
    """
    from .resolve_service import load_mappings

    rows = parse_rows(text)
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
    company, *, text, filename="", user=None,
    channel=MarketplaceChannel.FLIPKART, skip_duplicates=False,
):
    """Parse ``text`` and create/refresh orders. Returns the batch + counts.

    Set-based (a handful of bulk queries regardless of sheet size) so a full
    Flipkart export imports fast even against a remote database.

    ``skip_duplicates=True`` imports only orders that are NOT already present
    (the user chose not to re-import existing ones); existing orders are left
    untouched and reported under ``summary.duplicates_skipped``.
    """
    rows = parse_rows(text)
    now = timezone.now()

    batch = OrderImportBatch.objects.create(
        company=company, channel=channel, filename=filename,
        row_count=len(rows), created_by=user,
    )

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
    dispatched_ids = set(
        MarketplaceDispatch.objects.filter(company=company, order_id__in=existing_ids)
        .exclude(status=MarketplaceDispatchStatus.CANCELLED)
        .values_list("order_id", flat=True)
    ) if existing_ids else set()

    to_create, to_update = [], []
    duplicates_skipped = 0
    dispatched_skipped = 0
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
            # Already dispatched under its original sheet — keep it there.
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
        "blank_sku_skipped": blank_sku_skipped,
        "skipped_order_rows": skipped_order_rows,
        "orders": created + updated, "lines": line_count,
    }
    batch.save(update_fields=["order_count", "line_count", "summary"])
    return batch
