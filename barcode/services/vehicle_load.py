"""Mark barcode boxes as loaded into (or unloaded from) a vehicle.

A box scanned on a gate_core docking is physically inside the truck long before
the truck leaves. Loading flips it to ``INSIDE_VEHICLE`` and clears its bin so
the warehouse no longer shows it at a location, and records a LOAD_VEHICLE
movement so the trail shows when (and for which docking) it went into the truck.
Removing the scan reverses all of that via ``unload_boxes_from_vehicle``.

The box keeps its ``current_warehouse`` (settlement still needs the source
warehouse) and its pallet FK (the pallet's counts/status are derived by
``recalculate_pallet_state``, which also frees the pallet's Warehouse Ops
location once every box is in the truck). The pre-load status/bin are stashed on
the box itself so an unscan restores exactly what loading changed.
"""
import logging

from ..models import Box, BoxMovement, BoxMovementType, BoxStatus
from .pallet_state import recalculate_pallet_state

logger = logging.getLogger(__name__)

# Statuses a box can be loaded from (mirrors the docking scan validation).
LOADABLE_STATUSES = (BoxStatus.ACTIVE, BoxStatus.PARTIAL)


def load_boxes_into_vehicle(company, boxes, user, *, reference="") -> int:
    """Flip ``boxes`` to INSIDE_VEHICLE and clear their bin. Returns count loaded.

    Boxes not currently ACTIVE/PARTIAL are skipped (idempotent — a box already
    INSIDE_VEHICLE on this docking stays as it is). ``reference`` is a human
    label for the trail, e.g. ``"Docking GP/25-26/000123 (PB11AB1234)"``.
    """
    affected_pallets = {}
    loaded = 0
    seen_ids = set()
    for box in boxes:
        if box is None or box.id in seen_ids:
            continue
        seen_ids.add(box.id)
        if box.status not in LOADABLE_STATUSES:
            continue
        from_bin = box.current_bin
        box.pre_load_status = box.status
        box.pre_load_bin = from_bin
        box.status = BoxStatus.INSIDE_VEHICLE
        box.current_bin = ""
        box.save(update_fields=[
            "status", "current_bin", "pre_load_status", "pre_load_bin", "updated_at",
        ])
        BoxMovement.objects.create(
            company=company,
            box=box,
            movement_type=BoxMovementType.LOAD_VEHICLE,
            from_warehouse=box.current_warehouse,
            from_bin=from_bin,
            from_pallet=box.pallet,
            performed_by=user,
            notes=reference,
        )
        loaded += 1
        if box.pallet_id and box.pallet_id not in affected_pallets:
            affected_pallets[box.pallet_id] = box.pallet

    for pallet in affected_pallets.values():
        recalculate_pallet_state(company, pallet, user=user, note=reference or "Loaded into vehicle")
    return loaded


def unload_boxes_from_vehicle(company, boxes, user, *, reference="") -> int:
    """Reverse :func:`load_boxes_into_vehicle` for boxes still INSIDE_VEHICLE.

    Restores the stashed pre-load status and bin, records an UNLOAD_VEHICLE
    movement, and recalculates the affected pallets (which restores the pallet's
    status and Warehouse Ops counts). Boxes in any other status — including ones
    already settled DISPATCHED — are left untouched. Returns count unloaded.
    """
    affected_pallets = {}
    unloaded = 0
    seen_ids = set()
    for box in boxes:
        if box is None or box.id in seen_ids:
            continue
        seen_ids.add(box.id)
        if box.status != BoxStatus.INSIDE_VEHICLE:
            continue
        restored_status = (
            box.pre_load_status
            if box.pre_load_status in LOADABLE_STATUSES
            else BoxStatus.ACTIVE
        )
        restored_bin = box.pre_load_bin
        box.status = restored_status
        box.current_bin = restored_bin
        box.pre_load_status = ""
        box.pre_load_bin = ""
        box.save(update_fields=[
            "status", "current_bin", "pre_load_status", "pre_load_bin", "updated_at",
        ])
        BoxMovement.objects.create(
            company=company,
            box=box,
            movement_type=BoxMovementType.UNLOAD_VEHICLE,
            to_warehouse=box.current_warehouse,
            to_bin=restored_bin,
            to_pallet=box.pallet,
            performed_by=user,
            notes=reference,
        )
        unloaded += 1
        if box.pallet_id and box.pallet_id not in affected_pallets:
            affected_pallets[box.pallet_id] = box.pallet

    for pallet in affected_pallets.values():
        recalculate_pallet_state(company, pallet, user=user, note=reference or "Unloaded from vehicle")
    return unloaded


def resolve_scan_boxes(company, scans):
    """Resolve gate_core box scans to their barcode ``Box`` rows (best-effort).

    Follows the scan's ``box`` FK, falling back to a barcode lookup for legacy
    scans that were never linked. Unresolvable scans are skipped.
    """
    boxes = []
    for scan in scans:
        box = getattr(scan, "box", None)
        if box is None and getattr(scan, "box_barcode", ""):
            box = (
                Box.objects.filter(company=company, box_barcode=scan.box_barcode)
                .select_related("pallet")
                .first()
            )
        if box is not None:
            boxes.append(box)
    return boxes
