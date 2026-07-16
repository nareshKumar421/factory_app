"""Packing: print item labels, then release the order to Outward.

Flow: warehouse issue → PACKING (print item labels) → PACKED → order becomes
dispatchable in Outward. We no longer mint our own barcodes: every label — and
every Packing / Outward / Return scan — uses the Flipkart Tracking ID that is
already printed on the shipping label. See MARKETPLACE_FLIPKART_SHEET_FLOW.md — Packing.
"""
from django.db import transaction
from django.utils import timezone

from ..models import (
    MarketplaceOrderStatus,
    MarketplacePackBarcode,
    MarketplacePacking,
    MarketplacePackingStatus,
)
from .dispatch_gate import order_is_issued, order_is_packed
from .errors import MarketplaceError
from .resolve_service import fg_lines, load_mappings, resolve_order


def orders_ready_to_pack(company, channel):
    """Issued orders that have not been packed yet (and are not cancelled)."""
    from ..models import MarketplaceOrder

    orders = (
        MarketplaceOrder.objects.filter(company=company, channel=channel)
        .exclude(status=MarketplaceOrderStatus.RETURNED)
        .filter(is_cancelled=False)
        .prefetch_related("lines")
    )
    return [o for o in orders if order_is_issued(o) and not order_is_packed(o)]


def packing_queue(company, channel):
    """Orders for the Packing screen — those still to pack AND those already
    packed (kept so their item labels can be reprinted).

    Issued (materials handed to packing), not cancelled, not returned. Returned as
    a queryset — ordered unpacked-first, then newest — so the view can paginate it.
    Each order is annotated with ``line_count`` and its ``packing`` is preloaded.
    """
    from django.db.models import Count, Q

    from ..models import MarketplaceOrder
    from .dispatch_gate import issued_subquery, packed_subquery

    return (
        MarketplaceOrder.objects.filter(company=company, channel=channel)
        .exclude(status=MarketplaceOrderStatus.RETURNED)
        .filter(is_cancelled=False)
        .annotate(issued_flag=issued_subquery(), packed_flag=packed_subquery())
        .filter(Q(issued_flag=True) | Q(packed_flag=True))
        .select_related("packing")
        .annotate(line_count=Count("lines", distinct=True))
        .order_by("packed_flag", "-created_at")
    )


def _pending_orders(company, channel):
    """Issued, not-cancelled, not-yet-PACKED orders — the packing work-list."""
    from ..models import MarketplaceOrder

    orders = (
        MarketplaceOrder.objects.filter(company=company, channel=channel)
        .exclude(status=MarketplaceOrderStatus.RETURNED)
        .filter(is_cancelled=False)
        .prefetch_related("lines")
    )
    return [o for o in orders if order_is_issued(o) and not order_is_packed(o)]


def packing_summary(company, channel):
    """Group the packing work-list BY finished-good item instead of per order.

    Returns ``{"items": [{item_code, item_name, order_count}], "total_orders": N,
    "unmapped_orders": M}`` sorted by item code. Mappings are loaded once and
    reused across every order (mirror of ``batch_resolve_service.build_stock_list``)
    so this stays fast on the remote DB. Orders with an unmapped SKU can't be
    grouped and are surfaced as a count so they aren't silently dropped.
    """
    mappings = load_mappings(company, channel)
    groups = {}  # item_code -> {item_code, item_name, order_count}
    total = 0
    unmapped = 0
    for order in _pending_orders(company, channel):
        resolved = resolve_order(order, mappings)
        if resolved["unmapped_skus"]:
            unmapped += 1
            continue
        total += 1
        seen = set()
        for line in fg_lines(resolved["resolved_lines"]):
            code = line["item_code"]
            if code in seen:
                continue  # count each order once per item
            seen.add(code)
            g = groups.get(code)
            if g is None:
                groups[code] = {"item_code": code, "item_name": line["item_name"], "order_count": 1}
            else:
                g["order_count"] += 1
                if not g["item_name"] and line["item_name"]:
                    g["item_name"] = line["item_name"]
    items = sorted(groups.values(), key=lambda g: g["item_code"])
    return {"items": items, "total_orders": total, "unmapped_orders": unmapped}


def complete_item_group(company, channel, *, item_code, user=None):
    """Mark every pending order that consists solely of ``item_code`` as PACKED,
    so those orders flow to Outward.

    Single-FG-line orders (the marketplace's real data — one RAW SKU → one SL
    sale-BOM code) pack cleanly here. A rare multi-FG-line order is left untouched
    (its other item groups must be completed too) and reported in ``skipped`` so
    nothing is packed half-resolved.
    """
    want = (item_code or "").strip().upper()
    if not want:
        raise MarketplaceError("item_code is required.", status_code=400)
    mappings = load_mappings(company, channel)
    completed_ids, skipped_ids = [], []
    with transaction.atomic():
        for order in _pending_orders(company, channel):
            resolved = resolve_order(order, mappings)
            if resolved["unmapped_skus"]:
                continue
            codes = {l["item_code"].strip().upper() for l in fg_lines(resolved["resolved_lines"])}
            if codes != {want}:
                if want in codes:
                    skipped_ids.append(order.order_id)  # multi-item order — needs its others too
                continue
            packing, _ = MarketplacePacking.objects.get_or_create(
                company=company, order=order, defaults={"channel": channel}
            )
            if packing.status != MarketplacePackingStatus.PACKED:
                packing.status = MarketplacePackingStatus.PACKED
                packing.packed_by = user
                packing.packed_at = timezone.now()
                packing.updated_by = user
                packing.save(update_fields=[
                    "status", "packed_by", "packed_at", "updated_by", "updated_at",
                ])
            completed_ids.append(order.order_id)
    return {
        "item_code": want,
        "completed_count": len(completed_ids),
        "completed_order_ids": completed_ids,
        "skipped_order_ids": skipped_ids,
    }


@transaction.atomic
def scan_pack(company, channel, *, barcode, user=None):
    """Pack an order by scanning its Flipkart Tracking ID barcode.

    The tracking ID is already printed on the shipping label, so no internal
    barcode is generated. Resolves the order by ``tracking_id``, marks its packing
    PACKED (recording who/when + the scanned barcode), and the order then becomes
    dispatchable in Outward. Returns ``(packing, already_packed)``.
    """
    from ..models import MarketplaceOrder

    code = (barcode or "").strip()
    if not code:
        raise MarketplaceError("Scan a tracking ID.", code="EMPTY", status_code=400)

    order = (
        MarketplaceOrder.objects.filter(
            company=company, channel=channel, tracking_id=code
        )
        .exclude(status=MarketplaceOrderStatus.RETURNED)
        .order_by("-created_at")
        .first()
    )
    if order is None:
        raise MarketplaceError(
            f"No order found for tracking ID {code}.",
            code="NOT_FOUND", status_code=404,
        )
    if order.is_cancelled:
        raise MarketplaceError(
            "Order is cancelled on the marketplace; cannot pack.",
            code="ORDER_CANCELLED", status_code=409,
        )
    if not order_is_issued(order):
        raise MarketplaceError(
            "Order materials have not been issued from the warehouse yet.",
            code="NOT_ISSUED", status_code=409,
        )

    packing, _ = MarketplacePacking.objects.get_or_create(
        order=order,
        defaults={"company": order.company, "channel": order.channel, "created_by": user},
    )
    already_packed = packing.status == MarketplacePackingStatus.PACKED
    if not already_packed:
        packing.status = MarketplacePackingStatus.PACKED
        packing.packed_by = user
        packing.packed_at = timezone.now()
    packing.pack_barcode = code
    packing.updated_by = user
    packing.save()
    return packing, already_packed


@transaction.atomic
def start_or_get(order, *, user=None):
    """Open (or fetch) the packing session for an issued order."""
    if not order_is_issued(order):
        raise MarketplaceError(
            "Order materials have not been issued from the warehouse yet.",
            code="NOT_ISSUED", status_code=409,
        )
    packing, _created = MarketplacePacking.objects.get_or_create(
        order=order,
        defaults={"company": order.company, "channel": order.channel, "created_by": user},
    )
    return packing


@transaction.atomic
def generate_barcodes(packing, *, user=None):
    """Materialise one printable label per finished-good line, all carrying the
    order's Flipkart Tracking ID as their scannable barcode (idempotent).

    We no longer mint our own barcodes — the Flipkart Tracking ID already printed
    on the shipping label is the single barcode used across Packing, Outward and
    Returns. Works whether the order is still being packed or already PACKED, so
    labels can be printed (or reprinted) at any point.
    """
    if packing.barcodes.exists():
        return list(packing.barcodes.all())  # idempotent — already generated

    tracking_id = (packing.order.tracking_id or "").strip()
    if not tracking_id:
        raise MarketplaceError(
            "Order has no Flipkart Tracking ID yet; cannot print labels.",
            code="NO_TRACKING_ID", status_code=409,
        )

    resolved = resolve_order(packing.order)
    if resolved["unmapped_skus"]:
        raise MarketplaceError(
            "Order has unmapped SKUs; resolve them before packing.",
            code="UNMAPPED_SKUS", status_code=409, detail=resolved["unmapped_skus"],
        )
    lines = fg_lines(resolved["resolved_lines"])
    if not lines:
        raise MarketplaceError("Nothing to pack for this order.", code="EMPTY", status_code=400)

    created = []
    for line in lines:
        created.append(MarketplacePackBarcode.objects.create(
            company=packing.company,
            packing=packing,
            order=packing.order,
            barcode=tracking_id,
            item_code=line["item_code"],
            item_name=line["item_name"],
            quantity=line["required_quantity"],
            uom=line["uom"],
            source_sku=(line["source_skus"][0] if line["source_skus"] else ""),
        ))

    # Generating labels never regresses an already-PACKED order.
    if packing.status != MarketplacePackingStatus.PACKED:
        packing.status = MarketplacePackingStatus.PACKING
        packing.updated_by = user
        packing.save(update_fields=["status", "updated_by", "updated_at"])
    return created


def label_data(barcode_obj, *, mark_printed=True):
    """Return the printable label fields (frontend renders the symbology)."""
    if mark_printed and not barcode_obj.printed:
        barcode_obj.printed = True
        barcode_obj.printed_at = timezone.now()
        barcode_obj.save(update_fields=["printed", "printed_at"])
    order = barcode_obj.order
    return {
        "type": "PACK",
        "barcode": barcode_obj.barcode,
        "qr_payload": barcode_obj.barcode,
        "order_id": order.order_id,
        "buyer_name": order.buyer_name,
        "item_code": barcode_obj.item_code,
        "item_name": barcode_obj.item_name,
        "quantity": str(barcode_obj.quantity),
        "uom": barcode_obj.uom,
        "source_sku": barcode_obj.source_sku,
    }


@transaction.atomic
def complete(packing, *, user=None):
    """Finish packing → PACKED. The order is now dispatchable in Outward."""
    if packing.status == MarketplacePackingStatus.PACKED:
        return packing
    if not packing.barcodes.exists():
        raise MarketplaceError(
            "Generate item barcodes before completing packing.",
            code="NO_BARCODES", status_code=409,
        )
    packing.status = MarketplacePackingStatus.PACKED
    packing.packed_by = user
    packing.packed_at = timezone.now()
    packing.updated_by = user
    packing.save(update_fields=["status", "packed_by", "packed_at", "updated_by", "updated_at"])
    return packing
