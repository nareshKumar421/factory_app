"""Recalculate a pallet's derived counts/status from its boxes, and keep the
Warehouse Ops (``wms``) map in sync.

Extracted from ``DispatchService._recalculate_pallet_state`` so both the barcode
DispatchSession flow and the gate_core docking flow settle a pallet the exact
same way — counts, status transition, and the WMS reconcile that empties a freed
bin — from a single source of truth. Takes a plain ``company`` so callers don't
need to construct the heavier ``DispatchService`` (SAP adapter, scan service).

A pallet whose remaining (non-dispatched) boxes are all INSIDE_VEHICLE is itself
INSIDE_VEHICLE: physically in the truck, no longer at a warehouse location. The
transition into/out of INSIDE_VEHICLE is recorded as a PalletMovement so the
pallet's trail shows the load/unload, and the WMS reconcile frees the location
while the truck is loaded (and deletes the WMS pallet only once dispatched).
"""
import logging
from decimal import Decimal

from django.utils import timezone

from ..models import (
    Box,
    BoxStatus,
    Pallet,
    PalletBoxHistory,
    PalletMovement,
    PalletMovementType,
    PalletStatus,
)
from .wms_sync import reconcile_pallet_to_wms

logger = logging.getLogger(__name__)


def recalculate_pallet_state(company, pallet: Pallet, user=None, note="", trigger="") -> None:
    """Recompute ``pallet``'s box counts / status from its boxes and sync WMS.

    ``trigger`` labels what caused the recalculation ("load" / "unload" from the
    vehicle_load service, "" otherwise) so the pallet-level trail only records
    *real* vehicle events: a pallet loaded in several scan chunks flips
    PARTIAL ⇄ INSIDE_VEHICLE repeatedly as more of its boxes surface to scan,
    and without this the trail filled up with LOAD/UNLOAD pairs even though no
    box ever left a truck.
    """
    boxes = list(Box.objects.filter(company=company, pallet=pallet))
    active_boxes = [box for box in boxes if box.status in (BoxStatus.ACTIVE, BoxStatus.PARTIAL)]
    loaded_boxes = [box for box in boxes if box.status == BoxStatus.INSIDE_VEHICLE]
    dispatched_boxes = [box for box in boxes if box.status == BoxStatus.DISPATCHED]
    removed_box_count = PalletBoxHistory.objects.filter(
        company=company,
        pallet=pallet,
        action="BOX_DISPATCHED_SEPARATELY",
    ).values("box_id").distinct().count()
    old_status = pallet.status
    old_bin = pallet.current_bin
    pallet.total_boxes = len(boxes) + removed_box_count
    pallet.available_boxes = len(active_boxes)
    pallet.dispatched_boxes = len(dispatched_boxes) + removed_box_count
    pallet.box_count = len(active_boxes)
    pallet.total_qty = sum((box.qty for box in active_boxes), Decimal("0"))
    if pallet.status != PalletStatus.DISPATCHED:
        if not active_boxes and loaded_boxes:
            # Everything still on the pallet is in the truck — the pallet itself
            # is inside the vehicle and no longer occupies a warehouse location.
            pallet.status = PalletStatus.INSIDE_VEHICLE
            pallet.current_bin = ""
        elif not active_boxes and (dispatched_boxes or removed_box_count):
            pallet.status = PalletStatus.DISPATCHED
            pallet.dispatch_session = (
                dispatched_boxes[0].dispatch_session
                if dispatched_boxes and dispatched_boxes[0].dispatch_session_id
                else pallet.dispatch_session
            )
            pallet.dispatched_at = pallet.dispatched_at or timezone.now()
        elif active_boxes and (dispatched_boxes or loaded_boxes or removed_box_count):
            pallet.status = PalletStatus.PARTIAL
        elif not active_boxes:
            pallet.status = PalletStatus.EMPTY
        else:
            pallet.status = PalletStatus.ACTIVE
    pallet.save(update_fields=[
        "status",
        "current_bin",
        "dispatch_session",
        "dispatched_at",
        "box_count",
        "total_boxes",
        "available_boxes",
        "dispatched_boxes",
        "total_qty",
        "updated_at",
    ])
    # Trail the pallet-level load/unload so the detail page shows when the whole
    # pallet went into (or came back out of) a truck, not just its boxes.
    if old_status != pallet.status:
        if pallet.status == PalletStatus.INSIDE_VEHICLE:
            # Collapse chunked loading: if the pallet's last vehicle event is
            # already a LOAD for the same docking (no real unload in between),
            # this flip is just another chunk of the same load — don't re-log it.
            last_vehicle_movement = (
                PalletMovement.objects.filter(
                    pallet=pallet,
                    movement_type__in=(
                        PalletMovementType.LOAD_VEHICLE,
                        PalletMovementType.UNLOAD_VEHICLE,
                    ),
                )
                .order_by("-performed_at", "-id")
                .first()
            )
            already_logged = (
                last_vehicle_movement is not None
                and last_vehicle_movement.movement_type == PalletMovementType.LOAD_VEHICLE
                and last_vehicle_movement.notes == note
            )
            if not already_logged:
                PalletMovement.objects.create(
                    company=company,
                    pallet=pallet,
                    movement_type=PalletMovementType.LOAD_VEHICLE,
                    from_warehouse=pallet.current_warehouse,
                    from_bin=old_bin,
                    performed_by=user,
                    notes=note,
                )
        elif (
            old_status == PalletStatus.INSIDE_VEHICLE
            and pallet.status in (PalletStatus.ACTIVE, PalletStatus.PARTIAL)
            and trigger == "unload"
        ):
            # Only a real unscan is an unload; a fully-loaded pallet gaining new
            # to-scan boxes (chunked loading) is not a vehicle event.
            PalletMovement.objects.create(
                company=company,
                pallet=pallet,
                movement_type=PalletMovementType.UNLOAD_VEHICLE,
                to_warehouse=pallet.current_warehouse,
                performed_by=user,
                notes=note,
            )
    # Keep the Warehouse Ops map in sync: a dispatched/emptied pallet is removed
    # from its bin (a loaded one is un-located but kept, and a partial dispatch
    # decrements it) so the map never shows phantom stock. Best-effort — never
    # block a dispatch on it.
    try:
        reconcile_pallet_to_wms(company, pallet, note=note or "Dispatched")
    except Exception:  # noqa: BLE001 - WMS sync must not break dispatch
        logger.exception("WMS reconcile failed for pallet %s", pallet.pallet_id)
