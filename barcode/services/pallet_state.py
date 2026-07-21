"""Recalculate a pallet's derived counts/status from its boxes, and keep the
Warehouse Ops (``wms``) map in sync.

Extracted from ``DispatchService._recalculate_pallet_state`` so both the barcode
DispatchSession flow and the gate_core docking flow settle a pallet the exact
same way — counts, status transition, and the WMS reconcile that empties a freed
bin — from a single source of truth. Takes a plain ``company`` so callers don't
need to construct the heavier ``DispatchService`` (SAP adapter, scan service).
"""
import logging
from decimal import Decimal

from django.utils import timezone

from ..models import Box, BoxStatus, Pallet, PalletBoxHistory, PalletStatus
from .wms_sync import reconcile_pallet_to_wms

logger = logging.getLogger(__name__)


def recalculate_pallet_state(company, pallet: Pallet) -> None:
    """Recompute ``pallet``'s box counts / status from its boxes and sync WMS."""
    boxes = list(Box.objects.filter(company=company, pallet=pallet))
    active_boxes = [box for box in boxes if box.status in (BoxStatus.ACTIVE, BoxStatus.PARTIAL)]
    dispatched_boxes = [box for box in boxes if box.status == BoxStatus.DISPATCHED]
    removed_box_count = PalletBoxHistory.objects.filter(
        company=company,
        pallet=pallet,
        action="BOX_DISPATCHED_SEPARATELY",
    ).values("box_id").distinct().count()
    pallet.total_boxes = len(boxes) + removed_box_count
    pallet.available_boxes = len(active_boxes)
    pallet.dispatched_boxes = len(dispatched_boxes) + removed_box_count
    pallet.box_count = len(active_boxes)
    pallet.total_qty = sum((box.qty for box in active_boxes), Decimal("0"))
    if pallet.status != PalletStatus.DISPATCHED:
        if not active_boxes and (dispatched_boxes or removed_box_count):
            pallet.status = PalletStatus.DISPATCHED
            pallet.dispatch_session = (
                dispatched_boxes[0].dispatch_session
                if dispatched_boxes and dispatched_boxes[0].dispatch_session_id
                else pallet.dispatch_session
            )
            pallet.dispatched_at = pallet.dispatched_at or timezone.now()
        elif active_boxes and (dispatched_boxes or removed_box_count):
            pallet.status = PalletStatus.PARTIAL
        elif not active_boxes:
            pallet.status = PalletStatus.EMPTY
        else:
            pallet.status = PalletStatus.ACTIVE
    pallet.save(update_fields=[
        "status",
        "dispatch_session",
        "dispatched_at",
        "box_count",
        "total_boxes",
        "available_boxes",
        "dispatched_boxes",
        "total_qty",
        "updated_at",
    ])
    # Keep the Warehouse Ops map in sync: a dispatched/emptied pallet is removed
    # from its bin (and a partial dispatch decrements it) so the map never shows
    # phantom stock. Best-effort — never block a dispatch on it.
    try:
        reconcile_pallet_to_wms(company, pallet)
    except Exception:  # noqa: BLE001 - WMS sync must not break dispatch
        logger.exception("WMS reconcile failed for pallet %s", pallet.pallet_id)
