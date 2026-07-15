"""Packing: generate + print item barcodes, then release the order to Outward.

Flow: warehouse issue → PACKING (generate unique item barcodes, print them) →
PACKED → order becomes dispatchable in Outward. Barcode string format follows the
Barcode Module convention (``PREFIX-YYYYMMDD-…-NNN``); here ``PACK-<date>-<order>-<seq>``.
See MARKETPLACE_FLIPKART_SHEET_FLOW.md — Packing.
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
from .resolve_service import fg_lines, resolve_order


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


def _barcode_value(order, seq, today):
    return f"PACK-{today:%Y%m%d}-{order.pk}-{seq:03d}"


@transaction.atomic
def generate_barcodes(packing, *, user=None):
    """Create one unique barcode per finished-good line (idempotent)."""
    if packing.status == MarketplacePackingStatus.PACKED:
        raise MarketplaceError("Order is already packed.", code="ALREADY_PACKED", status_code=409)
    if packing.barcodes.exists():
        return list(packing.barcodes.all())  # idempotent — already generated

    resolved = resolve_order(packing.order)
    if resolved["unmapped_skus"]:
        raise MarketplaceError(
            "Order has unmapped SKUs; resolve them before packing.",
            code="UNMAPPED_SKUS", status_code=409, detail=resolved["unmapped_skus"],
        )
    lines = fg_lines(resolved["resolved_lines"])
    if not lines:
        raise MarketplaceError("Nothing to pack for this order.", code="EMPTY", status_code=400)

    today = timezone.localdate()
    created = []
    for seq, line in enumerate(lines, start=1):
        created.append(MarketplacePackBarcode.objects.create(
            company=packing.company,
            packing=packing,
            order=packing.order,
            barcode=_barcode_value(packing.order, seq, today),
            item_code=line["item_code"],
            item_name=line["item_name"],
            quantity=line["required_quantity"],
            uom=line["uom"],
            source_sku=(line["source_skus"][0] if line["source_skus"] else ""),
        ))

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
