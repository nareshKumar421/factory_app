"""Gate: an order may only be dispatched once its warehouse issue request has
been ISSUED (or RECEIVED) — i.e. the stock was physically handed to packing.

Orders whose materials were not issued are hidden from Outward and blocked from
dispatch. See MARKETPLACE_FLIPKART_SHEET_FLOW.md §5.6.
"""
from django.db.models import Exists, OuterRef

from ..models import (
    MarketplaceIssueRequest,
    MarketplaceIssueStatus,
    MarketplacePacking,
    MarketplacePackingStatus,
)

READY_ISSUE_STATUSES = (MarketplaceIssueStatus.ISSUED, MarketplaceIssueStatus.RECEIVED)


def order_is_issued(order):
    """Whether the order is cleared for dispatch.

    The gate applies to sheet-imported orders: such an order is dispatchable only
    once its import batch has an ISSUED/RECEIVED issue request. Orders that did
    NOT come from a sheet (no ``import_batch`` — e.g. manual/legacy) are not part
    of this flow and are not gated.
    """
    if not order.import_batch_id:
        return True
    return MarketplaceIssueRequest.objects.filter(
        batch_id=order.import_batch_id, status__in=READY_ISSUE_STATUSES
    ).exists()


def order_is_packed(order):
    """Whether the order is cleared for dispatch (packed).

    Sheet-imported orders must be PACKED. Orders not from a sheet (no
    ``import_batch`` — manual/legacy) are not part of this flow and pass through.
    """
    if not order.import_batch_id:
        return True
    return MarketplacePacking.objects.filter(
        order=order, status=MarketplacePackingStatus.PACKED
    ).exists()


def issued_subquery():
    """``Exists`` subquery: the order's batch has an issued/received request."""
    return Exists(
        MarketplaceIssueRequest.objects.filter(
            batch_id=OuterRef("import_batch_id"), status__in=READY_ISSUE_STATUSES
        )
    )


def packed_subquery():
    """``Exists`` subquery: the order has a completed packing session."""
    return Exists(
        MarketplacePacking.objects.filter(
            order_id=OuterRef("pk"), status=MarketplacePackingStatus.PACKED
        )
    )


# Outward dispatch is gated on PACKED (packing generates the item barcodes).
def dispatch_ready_subquery():
    return packed_subquery()
