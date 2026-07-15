"""Dispatch a docking out the gate (status -> DISPATCHED).

Factored out of ``SalesDispatchMarkDispatchedView`` so the cross-company arrival
dispatch can run the exact same per-docking settlement for every company's docking
on one physical truck.
"""

from django.db import transaction
from django.utils import timezone

from dispatch_plans.models import DispatchPlan, DispatchPlanStatus
from gate_core.models import SalesDispatchGateOutStatus
from gate_core.services.empty_vehicle_dispatch import (
    consume_covers_for_dispatched_plans,
)


def get_dispatch_weight_error(entry):
    """Human message if the load can't be dispatched on weight grounds, else ''."""
    weighment = getattr(entry.vehicle_entry, "weighment", None)
    if not weighment:
        return "Gross and tare weighment are required before marking Docking as dispatched."

    gross_weight = weighment.gross_weight
    tare_weight = weighment.tare_weight
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
    """Dispatch every in-flight docking on ``entry``'s physical vehicle together.

    The reliable "one truck, one exit" grouping: a vehicle is physically inside
    only once at a time, so all of its in-flight dockings -- across companies,
    whether or not they were ever threaded onto a shared ``VehicleArrival`` -- are
    this one truck's load and must leave together. This is what the arrival FK was
    meant to capture but frequently misses (many gate-ins carry no arrival), which
    let one company's bill dispatch while its sibling on the same truck stayed
    stuck. Falls back to just ``entry`` when the vehicle is unknown.
    """
    from gate_core.models import SalesDispatchGateOut

    dockings = []
    if entry.vehicle_id:
        dockings = list(
            SalesDispatchGateOut.objects.filter(
                vehicle_id=entry.vehicle_id,
                is_active=True,
                status__in=_IN_FLIGHT_DOCKING_STATUSES,
            ).select_related("company")
        )
    # Guarantee the acted-on docking is in the set even if its status/flags are an
    # edge case, so this action always at least dispatches ``entry`` itself.
    if all(d.id != entry.id for d in dockings):
        dockings.append(entry)
    return _dispatch_docking_set(dockings, user)
