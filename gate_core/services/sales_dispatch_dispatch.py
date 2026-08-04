"""Dispatch a docking out the gate (status -> DISPATCHED).

Factored out of ``SalesDispatchMarkDispatchedView`` so the cross-company arrival
dispatch can run the exact same per-docking settlement for every company's docking
on one physical truck.
"""

import logging

from django.db import transaction
from django.utils import timezone

from barcode.models import Box
from barcode.services.dispatch_settlement import settle_dispatched_boxes
from dispatch_plans.models import DispatchPlan, DispatchPlanStatus
from gate_core.models import SalesDispatchGateOutStatus
from gate_core.services.empty_vehicle_dispatch import (
    consume_covers_for_dispatched_plans,
)

logger = logging.getLogger(__name__)


def settle_docking_stock(entry, user):
    """Mark the docking's scanned boxes dispatched and free their WMS bins.

    The docking flow records ``SalesDispatchBoxScan`` rows for reconciliation but
    never changes the underlying barcode stock, so without this the boxes stay
    ACTIVE and their pallets keep occupying their Warehouse Ops bins after the
    truck leaves. Resolves each active scan to its ``Box`` (by FK, else barcode)
    and hands them to the shared settlement helper. Idempotent — safe to re-run.
    """
    scans = (
        entry.box_scans.filter(is_active=True)
        .select_related("box", "box__pallet")
    )
    boxes = []
    missing_barcodes = []
    for scan in scans:
        box = scan.box
        if box is None and scan.box_barcode:
            box = Box.objects.filter(
                company=entry.company, box_barcode=scan.box_barcode
            ).select_related("pallet").first()
        if box is None:
            missing_barcodes.append(scan.box_barcode)
            continue
        boxes.append(box)

    if missing_barcodes:
        logger.warning(
            "Docking %s dispatch: %d scanned box(es) could not be resolved to a "
            "barcode Box and were not settled: %s",
            entry.id, len(missing_barcodes), ", ".join(missing_barcodes[:20]),
        )
    return settle_dispatched_boxes(
        entry.company, boxes, user,
        note=f"Dispatched — Docking {entry.entry_no}",
    )


def dispatch_tare_weight(entry):
    """The canonical tare for a docking.

    A physical truck is weighed empty ONCE; that single tare lives on its
    ``VehicleArrival``. Each per-company gate-in and the docking then keep their own
    ``Weighment.tare_weight`` copy, which operators can edit independently and which
    drifts (a 3-company truck stores the same tare four times). Reading the arrival's
    tare when the docking belongs to one makes every company chain on the truck
    reconcile against the same empty weight; single-company / legacy dockings (no
    arrival) keep their own weighment tare, so their behaviour is unchanged.
    """
    arrival = getattr(entry, "arrival", None)
    if arrival is not None and arrival.tare_weight is not None:
        return arrival.tare_weight
    weighment = getattr(getattr(entry, "vehicle_entry", None), "weighment", None)
    return getattr(weighment, "tare_weight", None) if weighment else None


def dispatch_gross_weight(entry):
    """The loaded-truck gross for a docking.

    A physical truck is weighed loaded ONCE at the gate, but each per-company
    docking keeps its own ``Weighment.gross_weight`` and the operator records it on
    just one of them. Read the gross from any of the truck's dockings (via the
    shared arrival) so every company chain on the truck reconciles against the same
    loaded weight -- record it once, dispatch the whole truck. Single-company /
    legacy dockings (no arrival) fall back to their own weighment, unchanged.
    """
    weighment = getattr(getattr(entry, "vehicle_entry", None), "weighment", None)
    own_gross = getattr(weighment, "gross_weight", None) if weighment else None
    if own_gross is not None and own_gross > 0:
        return own_gross
    arrival = getattr(entry, "arrival", None)
    if arrival is None:
        return own_gross
    from weighment.models import Weighment
    from gate_core.models import SalesDispatchGateOut

    sibling_ve_ids = SalesDispatchGateOut.objects.filter(
        arrival_id=arrival.id, is_active=True
    ).values_list("vehicle_entry_id", flat=True)
    sibling_gross = (
        Weighment.objects.filter(vehicle_entry_id__in=list(sibling_ve_ids), gross_weight__gt=0)
        .order_by("-gross_weight")
        .values_list("gross_weight", flat=True)
        .first()
    )
    return sibling_gross if sibling_gross is not None else own_gross


def get_dispatch_weight_error(entry):
    """Human message if the load can't be dispatched on weight grounds, else ''."""
    weighment = getattr(entry.vehicle_entry, "weighment", None)
    if not weighment:
        return "Gross and tare weighment are required before marking Docking as dispatched."

    # Gross is the truck's single loaded weight (recorded on any one of its
    # dockings), mirroring the shared arrival tare below.
    gross_weight = dispatch_gross_weight(entry)
    # Tare is the truck's empty weight from the weighbridge at the gate -- the single
    # arrival tare -- not each per-company docking weighment's editable copy. So every
    # chain on one physical truck reconciles against the same empty weight; a drifted
    # docking-weighment tare can no longer let a bad load pass (or block a good one).
    # Falls back to this weighment for legacy / single-company dockings with no arrival.
    tare_weight = dispatch_tare_weight(entry)
    if gross_weight is None or gross_weight <= 0:
        return "Gross weight is required before marking Docking as dispatched."
    if tare_weight is None or tare_weight < 0:
        return "Tare weight from empty vehicle in is required before marking Docking as dispatched."
    if tare_weight > gross_weight:
        return "Tare weight cannot be greater than gross weight."

    return ""


def mark_docking_dispatched(entry, user):
    """Move a PRINT_COMMITTED docking to DISPATCHED and settle its plans/covers.

    Raises ``ValueError`` if the docking isn't ready (wrong status, missing gatepass
    commit, or invalid weight). Consuming the dispatched plans' covers retires any
    gate-in whose bills have all left, so the truck stops counting as inside.
    """
    if entry.status != SalesDispatchGateOutStatus.PRINT_COMMITTED:
        raise ValueError("Print must be committed before marking Docking as dispatched.")
    if not entry.gatepass_no or not entry.print_committed_at:
        raise ValueError(
            "Gatepass number and final print commit timestamp are required "
            "before marking Docking as dispatched."
        )
    weight_error = get_dispatch_weight_error(entry)
    if weight_error:
        raise ValueError(weight_error)

    with transaction.atomic():
        entry.status = SalesDispatchGateOutStatus.DISPATCHED
        entry.gate_out_date = timezone.localdate()
        entry.out_time = timezone.localtime().time().replace(microsecond=0)
        entry.dispatched_by = user
        entry.dispatched_at = timezone.now()
        entry.updated_by = user
        entry.save(
            update_fields=[
                "status",
                "gate_out_date",
                "out_time",
                "dispatched_by",
                "dispatched_at",
                "updated_by",
                "updated_at",
            ]
        )
        entry.vehicle_entry.status = "COMPLETED"
        entry.vehicle_entry.updated_by = user
        entry.vehicle_entry.save(update_fields=["status", "updated_by", "updated_at"])

        dispatch_plans = list(
            DispatchPlan.objects.filter(
                sales_dispatch_gate_out_documents__sales_dispatch=entry
            ).distinct()
        )
        if entry.dispatch_plan_id:
            dispatch_plans.append(entry.dispatch_plan)
        seen_plan_ids = set()
        dispatched_plans = []
        for dispatch_plan in dispatch_plans:
            if dispatch_plan.id in seen_plan_ids:
                continue
            seen_plan_ids.add(dispatch_plan.id)
            dispatched_plans.append(dispatch_plan)
            dispatch_plan.booking_status = DispatchPlanStatus.DISPATCHED
            dispatch_plan.updated_by = user
            dispatch_plan.save(
                update_fields=["booking_status", "updated_by", "updated_at"]
            )
        consume_covers_for_dispatched_plans(dispatched_plans, user)
        # The docking flow only records box scans; settling them here marks the
        # scanned boxes DISPATCHED and recalculates their pallets, which frees the
        # emptied bins on the Warehouse Ops map (they otherwise linger as phantom
        # stock). In the same transaction so stock state is atomic with dispatch.
        settle_docking_stock(entry, user)
    return entry


# Dockings still in-flight (loaded but not yet gone). A vehicle is physically
# inside only once at a time, so all of its in-flight dockings are the one truck's
# current load. Excludes DISPATCHED (already gone) and REJECTED/CANCELLED.
_IN_FLIGHT_DOCKING_STATUSES = (
    SalesDispatchGateOutStatus.DOCKED,
    SalesDispatchGateOutStatus.PHOTO_ATTACHED,
    SalesDispatchGateOutStatus.READY_FOR_GATEPASS,
    SalesDispatchGateOutStatus.GATEPASS_PRINTED,
    SalesDispatchGateOutStatus.PRINT_COMMITTED,
)


def _dispatch_docking_set(dockings, user):
    """Dispatch a set of dockings atomically -- one truck, one exit.

    Each must be ready (PRINT_COMMITTED with a valid weight); a not-yet-ready
    sibling rolls the whole dispatch back, naming the blocking company, so the
    truck can never go out for one company while another is still loading.
    Already-dispatched dockings are skipped, so it is idempotent.
    """
    with transaction.atomic():
        for docking in dockings:
            if docking.status == SalesDispatchGateOutStatus.DISPATCHED:
                continue
            try:
                mark_docking_dispatched(docking, user)
            except ValueError as exc:
                raise ValueError(f"{docking.company.code}: {exc}") from exc
    return dockings


def dispatch_arrival(arrival, user):
    """Dispatch EVERY company's docking on one physical truck in a single action.

    One truck, one exit: each docking must be ready (PRINT_COMMITTED with a valid
    weight), and the loop is atomic so a not-yet-ready company rolls the whole
    dispatch back -- the truck can never go out for one company while another is
    still inside. Already-dispatched dockings are skipped, so it is idempotent.
    Raises ``ValueError`` naming the blocking company if a sibling isn't ready.
    """
    from gate_core.services.arrival_gatepass import arrival_dockings

    dockings = arrival_dockings(arrival)
    if not dockings:
        raise ValueError("No dockings to dispatch on this trip.")
    return _dispatch_docking_set(dockings, user)


def dispatch_vehicle_trip(entry, user):
    """Dispatch every in-flight docking on ``entry``'s *current trip* together.

    "One truck, one exit": cross-company siblings on the same physical trip leave
    together. The trip is scoped to the dockings that share ``entry``'s arrival,
    plus genuinely *untethered* (null-arrival) in-flight dockings on the same
    vehicle -- the cross-company siblings that were never threaded onto the
    arrival (the case the arrival FK misses). Dockings that belong to a
    *different* arrival -- a separate/older trip of the same vehicle -- are
    deliberately excluded, so a stale docking left over from a prior trip can't
    block (or wrongly ride along with) the current dispatch. Falls back to just
    ``entry`` when the vehicle is unknown.
    """
    from django.db.models import Q

    from gate_core.models import SalesDispatchGateOut

    dockings = []
    if entry.vehicle_id:
        dockings = list(
            SalesDispatchGateOut.objects.filter(
                vehicle_id=entry.vehicle_id,
                is_active=True,
                status__in=_IN_FLIGHT_DOCKING_STATUSES,
            )
            # same trip (shared arrival) or an untethered sibling -- never another
            # arrival's docking.
            .filter(Q(arrival_id=entry.arrival_id) | Q(arrival_id__isnull=True))
            .select_related("company")
        )
    # Guarantee the acted-on docking is in the set even if its status/flags are an
    # edge case, so this action always at least dispatches ``entry`` itself.
    if all(d.id != entry.id for d in dockings):
        dockings.append(entry)
    return _dispatch_docking_set(dockings, user)
