"""Settle barcode Box/Pallet lifecycle for stock dispatched through the
gate_core docking flow.

Unlike the barcode DispatchSession flow (``DispatchService``), the docking /
gatepass flow records box scans (``SalesDispatchBoxScan``) purely for
reconciliation and never marks the underlying ``Box`` / ``Pallet`` dispatched.
The result is stranded stock: boxes stay ACTIVE and their pallets keep occupying
their Warehouse Ops bins after the truck has left.

``settle_dispatched_boxes`` closes that gap. It flips the still-active boxes to
DISPATCHED, records a DISPATCH movement, and recalculates each affected pallet —
which, via ``recalculate_pallet_state``, marks an emptied pallet DISPATCHED and
lets ``reconcile_pallet_to_wms`` free its bin. It is idempotent: boxes already
DISPATCHED (e.g. dispatched through the barcode flow) are skipped, so it is safe
to run live on every dispatch and to backfill historical dockings.

Kept in the ``barcode`` app (it only touches barcode models) so ``gate_core``
depends on ``barcode`` and not the reverse.
"""
import logging
from decimal import Decimal

from django.utils import timezone

from ..models import Box, BoxMovement, BoxMovementType, BoxStatus
from .pallet_state import recalculate_pallet_state

logger = logging.getLogger(__name__)


def settle_dispatched_boxes(company, boxes, user, *, note="Dispatched") -> dict:
    """Mark ``boxes`` dispatched and reconcile their pallets (+ WMS bins).

    ``boxes`` is any iterable of ``Box`` instances (duplicates tolerated). Only
    boxes currently ACTIVE/PARTIAL are settled; already-DISPATCHED boxes are left
    untouched, so calling this repeatedly — or on a load whose boxes partly went
    through the barcode flow — is safe.

    Returns ``{"boxes_dispatched": int, "pallets_reconciled": int}``.
    """
    now = timezone.now()
    seen_box_ids: set[int] = set()
    affected_pallets: dict[int, "Box"] = {}
    dispatched = 0

    for box in boxes:
        if box is None or box.id in seen_box_ids:
            continue
        seen_box_ids.add(box.id)
        if box.status not in (BoxStatus.ACTIVE, BoxStatus.PARTIAL):
            # Already dispatched (e.g. via the barcode flow) — idempotent skip.
            # Still reconcile its pallet in case that step was missed before.
            if box.pallet_id and box.pallet_id not in affected_pallets:
                affected_pallets[box.pallet_id] = box.pallet
            continue

        old_pallet = box.pallet
        from_warehouse = box.current_warehouse
        box.status = BoxStatus.DISPATCHED
        box.dispatched_at = now
        box.qty = Decimal("0")
        box.save(update_fields=["status", "dispatched_at", "qty", "updated_at"])

        BoxMovement.objects.create(
            company=company,
            box=box,
            movement_type=BoxMovementType.DISPATCH,
            from_warehouse=from_warehouse,
            from_pallet=old_pallet,
            performed_by=user,
        )
        dispatched += 1
        if old_pallet and old_pallet.id not in affected_pallets:
            affected_pallets[old_pallet.id] = old_pallet

    for pallet in affected_pallets.values():
        recalculate_pallet_state(company, pallet)

    return {
        "boxes_dispatched": dispatched,
        "pallets_reconciled": len(affected_pallets),
    }
