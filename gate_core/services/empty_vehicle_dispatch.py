"""Bill-accurate covers + retirement for dispatch empty-vehicle gate-ins.

A DISPATCH empty-in records the exact bills (``EmptyVehicleGateInCover``) it is
gated in to carry. Matching and docking eligibility read these covers so a reused
truck's new bills can never ride on an old/foreign gate-in. A gate-in is
*retired* once every cover is consumed (its bills dispatched) or the truck leaves
empty — a retired gate-in stops making any of its bills dockable.

These helpers are shared by the per-company gate-in completion flow and the
cross-company arrival flow (Part B), so the snapshot/consume/retire rules live in
one place. Imports are lazy to avoid app-load circular imports.
"""

from django.utils import timezone


def record_dispatch_covers(gate_in, user, sap_doc_entries=None):
    """Link the vehicle's booked, unlinked bills to ``gate_in`` and record covers.

    Snapshots the plans the planner booked to this vehicle (optionally constrained
    to ``sap_doc_entries``) as the gate-in's covers, and points each plan's
    ``linked_vehicle_entry`` at the gate-in's vehicle entry. Returns the linked
    ``DispatchPlan`` list. Idempotent: a cover already present for a bill is left
    as-is.
    """
    from dispatch_plans.models import DispatchPlan, DispatchPlanStatus
    from gate_core.models import EmptyVehicleGateInCover

    plans_qs = DispatchPlan.objects.filter(
        company=gate_in.company,
        is_active=True,
        booking_status=DispatchPlanStatus.BOOKED,
        vehicle=gate_in.vehicle,
        linked_vehicle_entry__isnull=True,
    )
    if sap_doc_entries is not None:
        plans_qs = plans_qs.filter(sap_invoice_doc_entry__in=list(sap_doc_entries))
    plans = list(plans_qs)
    if not plans:
        return []

    now = timezone.now()
    user_id = getattr(user, "id", None)
    DispatchPlan.objects.filter(id__in=[p.id for p in plans]).update(
        linked_vehicle_entry=gate_in.vehicle_entry,
        updated_by_id=user_id,
        updated_at=now,
    )

    already = set(
        EmptyVehicleGateInCover.objects.filter(
            empty_vehicle_gate_in=gate_in
        ).values_list("sap_doc_entry", flat=True)
    )
    new_covers = [
        EmptyVehicleGateInCover(
            empty_vehicle_gate_in=gate_in,
            dispatch_plan=plan,
            sap_doc_entry=plan.sap_invoice_doc_entry,
            sap_doc_num=plan.sap_invoice_doc_num or "",
            created_by_id=user_id,
            updated_by_id=user_id,
        )
        for plan in plans
        if plan.sap_invoice_doc_entry not in already
    ]
    if new_covers:
        EmptyVehicleGateInCover.objects.bulk_create(new_covers)
    return plans


def _retire_if_fully_consumed(gate_in_id, user, reason, when):
    """Retire a gate-in once every one of its covers is consumed (≥1 cover)."""
    from gate_core.models import EmptyVehicleGateIn, EmptyVehicleGateInCover

    covers = EmptyVehicleGateInCover.objects.filter(
        empty_vehicle_gate_in_id=gate_in_id, is_active=True
    )
    if not covers.exists() or covers.filter(consumed_at__isnull=True).exists():
        return  # zero covers, or some still open -> not fully consumed
    EmptyVehicleGateIn.objects.filter(id=gate_in_id, retired_at__isnull=True).update(
        retired_at=when,
        retired_reason=reason,
        updated_by_id=getattr(user, "id", None),
        updated_at=when,
    )


def _depart_arrival_if_complete(arrival_id, user, when):
    """Close an arrival once its whole load has left (every gate-in retired).

    Mirrors ``VehicleArrivalDepartView``'s completion rule, but runs automatically
    when the last live gate-in retires on dispatch. Without this an arrival stays
    ``LOADING`` forever after the truck physically leaves; the next time the same
    truck is gated in, the reuse logic attaches the new gate-in to that stale trip,
    so the truck looks perpetually "inside" and a fresh bill can never surface it as
    an expected arrival. For a multi-company truck this only fires once *all*
    companies have dispatched (the "one deliberate exit" rule is preserved).
    """
    from gate_core.models import VehicleArrival, VehicleArrivalStatus

    if arrival_id is None:
        return
    arrival = VehicleArrival.objects.filter(id=arrival_id, is_active=True).first()
    if arrival is None:
        return
    if arrival.status in (
        VehicleArrivalStatus.DEPARTED,
        VehicleArrivalStatus.CANCELLED,
    ):
        return
    if arrival.gate_ins.filter(is_active=True, retired_at__isnull=True).exists():
        return  # a company chain is still inside -> not a full exit yet
    VehicleArrival.objects.filter(id=arrival_id).exclude(
        status__in=[VehicleArrivalStatus.DEPARTED, VehicleArrivalStatus.CANCELLED]
    ).update(
        status=VehicleArrivalStatus.DEPARTED,
        gate_out_date=timezone.localdate(),
        out_time=timezone.localtime().time().replace(microsecond=0),
        departed_at=when,
        departed_by_id=getattr(user, "id", None),
        updated_by_id=getattr(user, "id", None),
        updated_at=when,
    )


def consume_covers_for_dispatched_plans(plans, user):
    """Mark dispatched plans' covers consumed, then retire fully-consumed gate-ins.

    Called when a docking is marked dispatched: each bill that physically left is
    consumed; once all of a gate-in's bills are consumed the truck is gone, so the
    gate-in retires and stops making anything eligible.
    """
    from gate_core.models import (
        EmptyVehicleGateIn,
        EmptyVehicleGateInCover,
        EmptyVehicleGateInRetireReason,
    )

    plan_ids = [p.id for p in plans]
    if not plan_ids:
        return
    now = timezone.now()
    gate_in_ids = set(
        EmptyVehicleGateInCover.objects.filter(
            is_active=True, consumed_at__isnull=True, dispatch_plan_id__in=plan_ids
        ).values_list("empty_vehicle_gate_in_id", flat=True)
    )
    if not gate_in_ids:
        return
    EmptyVehicleGateInCover.objects.filter(
        is_active=True, consumed_at__isnull=True, dispatch_plan_id__in=plan_ids
    ).update(consumed_at=now, updated_by_id=getattr(user, "id", None), updated_at=now)
    for gate_in_id in gate_in_ids:
        _retire_if_fully_consumed(
            gate_in_id, user, EmptyVehicleGateInRetireReason.DISPATCHED, now
        )
    # The truck physically leaves once every company chain on its arrival is
    # retired; close those arrivals so they don't linger and get reused next visit.
    arrival_ids = set(
        EmptyVehicleGateIn.objects.filter(
            id__in=gate_in_ids, arrival__isnull=False
        ).values_list("arrival_id", flat=True)
    )
    for arrival_id in arrival_ids:
        _depart_arrival_if_complete(arrival_id, user, now)


def unconsume_covers_for_plans(plans, user):
    """Reverse consumption for these plans' covers and un-retire DISPATCHED-retired
    gate-ins (a docking was rejected/cancelled/un-docked after dispatch)."""
    from gate_core.models import EmptyVehicleGateIn, EmptyVehicleGateInCover

    plan_ids = [p.id for p in plans]
    if not plan_ids:
        return
    now = timezone.now()
    gate_in_ids = set(
        EmptyVehicleGateInCover.objects.filter(
            is_active=True, consumed_at__isnull=False, dispatch_plan_id__in=plan_ids
        ).values_list("empty_vehicle_gate_in_id", flat=True)
    )
    if not gate_in_ids:
        return
    EmptyVehicleGateInCover.objects.filter(
        is_active=True, consumed_at__isnull=False, dispatch_plan_id__in=plan_ids
    ).update(consumed_at=None, updated_by_id=getattr(user, "id", None), updated_at=now)
    # A gate-in retired only because its bills dispatched should reopen.
    reopened = set(
        EmptyVehicleGateIn.objects.filter(
            id__in=gate_in_ids, retired_reason="DISPATCHED"
        ).values_list("id", flat=True)
    )
    EmptyVehicleGateIn.objects.filter(id__in=reopened).update(
        retired_at=None,
        retired_reason="",
        updated_by_id=getattr(user, "id", None),
        updated_at=now,
    )
    # If the truck had auto-departed on full dispatch, un-dispatching brings a chain
    # back inside -> reopen the arrival so it isn't DEPARTED with a live gate-in.
    _reopen_departed_arrivals_for_gate_ins(reopened, user, now)


def _reopen_departed_arrivals_for_gate_ins(gate_in_ids, user, when):
    """Reopen arrivals that auto-departed but now have a live (un-retired) gate-in."""
    from gate_core.models import EmptyVehicleGateIn, VehicleArrival, VehicleArrivalStatus

    if not gate_in_ids:
        return
    arrival_ids = set(
        EmptyVehicleGateIn.objects.filter(
            id__in=gate_in_ids, arrival__isnull=False, retired_at__isnull=True
        ).values_list("arrival_id", flat=True)
    )
    if not arrival_ids:
        return
    VehicleArrival.objects.filter(
        id__in=arrival_ids, is_active=True, status=VehicleArrivalStatus.DEPARTED
    ).update(
        status=VehicleArrivalStatus.LOADING,
        gate_out_date=None,
        out_time=None,
        departed_at=None,
        departed_by_id=None,
        updated_by_id=getattr(user, "id", None),
        updated_at=when,
    )


# Once the truck photo is attached at docking, the physical load is fixed and no
# further bills may join it.
_LOAD_LOCKED_DOCKING_STATUSES = (
    "PHOTO_ATTACHED",
    "READY_FOR_GATEPASS",
    "GATEPASS_PRINTED",
    "PRINT_COMMITTED",
)


def attach_bill_to_inside_vehicle(plan, user):
    """Add a just-booked bill to the load of a vehicle that is already inside.

    If the plan's vehicle has a live (COMPLETED, non-retired) dispatch gate-in and
    its load has not yet been photo-locked at docking, add this bill to that
    gate-in (cover + link) so it joins the current load — instead of asking the
    gate to register the same physical truck again. Only *live* gate-ins qualify,
    so a departed truck's retired gate-in can't grab it. Returns True if attached.
    """
    from gate_core.models import (
        EmptyVehicleGateIn,
        EmptyVehicleGateInCover,
        SalesDispatchGateOut,
    )

    if not plan.vehicle_id:
        return False
    gate_in = (
        EmptyVehicleGateIn.objects.filter(
            company_id=plan.company_id,
            is_active=True,
            reason="DISPATCH",
            vehicle_id=plan.vehicle_id,
            vehicle_entry__status="COMPLETED",
            retired_at__isnull=True,
        )
        .order_by("-vehicle_entry__updated_at")
        .first()
    )
    if gate_in is None:
        # No gate-in for this bill's company yet. If the truck is already inside
        # under an open cross-company arrival whose load isn't photo-locked, add a
        # gate-in for this company under that same arrival (one truck, one trip,
        # many companies).
        return _attach_bill_via_arrival(plan, user)
    # Cutoff: the load is fixed once *this gate-in's* truck photo is attached at
    # docking. Scope to dockings of bills linked to this gate-in — a stale,
    # photo-locked docking from an earlier trip of the same vehicle must not block.
    from django.db.models import Q

    if (
        SalesDispatchGateOut.objects.filter(
            company_id=plan.company_id,
            is_active=True,
            status__in=_LOAD_LOCKED_DOCKING_STATUSES,
        )
        .filter(
            Q(dispatch_plan__linked_vehicle_entry_id=gate_in.vehicle_entry_id)
            | Q(documents__dispatch_plan__linked_vehicle_entry_id=gate_in.vehicle_entry_id)
        )
        .exists()
    ):
        return False

    now = timezone.now()
    user_id = getattr(user, "id", None)
    EmptyVehicleGateInCover.objects.get_or_create(
        empty_vehicle_gate_in=gate_in,
        sap_doc_entry=plan.sap_invoice_doc_entry,
        defaults={
            "dispatch_plan": plan,
            "sap_doc_num": plan.sap_invoice_doc_num or "",
            "created_by_id": user_id,
            "updated_by_id": user_id,
        },
    )
    type(plan).objects.filter(id=plan.id).update(
        linked_vehicle_entry_id=gate_in.vehicle_entry_id,
        updated_by_id=user_id,
        updated_at=now,
    )
    plan.linked_vehicle_entry_id = gate_in.vehicle_entry_id
    return True


def _attach_bill_via_arrival(plan, user):
    """Add ``plan``'s company to a truck already inside under an open arrival.

    When a bill is booked for a company that has no gate-in yet, but the truck is
    already inside under an open (INSIDE/LOADING) cross-company arrival whose load
    is not photo-locked, create a gate-in for ``plan.company`` under that same
    arrival (reusing its shared tare). Returns True if attached.
    """
    from django.db import transaction

    from gate_core.models import SalesDispatchGateOut, VehicleArrival, VehicleArrivalStatus

    arrival = (
        VehicleArrival.objects.filter(
            vehicle_id=plan.vehicle_id,
            is_active=True,
            status__in=[VehicleArrivalStatus.INSIDE, VehicleArrivalStatus.LOADING],
            # Only a genuinely live trip (≥1 unretired gate-in). A stale arrival
            # whose chains all retired must not adopt a new bill.
            gate_ins__is_active=True,
            gate_ins__retired_at__isnull=True,
        )
        .order_by("-created_at")
        .distinct()
        .first()
    )
    if arrival is None:
        return False
    # The truck's load is fixed once any docking on the trip is photo-locked.
    if SalesDispatchGateOut.objects.filter(
        arrival=arrival, is_active=True, status__in=_LOAD_LOCKED_DOCKING_STATUSES
    ).exists():
        return False
    # Don't add a second live gate-in for a company already represented.
    if arrival.gate_ins.filter(
        company_id=plan.company_id, is_active=True, retired_at__isnull=True
    ).exists():
        return False

    with transaction.atomic():
        gate_in = _create_company_gate_in(
            arrival,
            plan.company,
            arrival.vehicle,
            arrival.driver,
            arrival.gate_in_date,
            arrival.in_time,
            user,
            tare_weight=arrival.tare_weight,
            weighbridge_slip_no=arrival.weighbridge_slip_no,
            security_name=arrival.security_name,
            sap_doc_entries=[plan.sap_invoice_doc_entry],
        )
    plan.linked_vehicle_entry_id = gate_in.vehicle_entry_id
    return True


def retire_empty_in(gate_in, reason, user):
    """Explicitly retire a gate-in (e.g. the truck left empty)."""
    from gate_core.models import EmptyVehicleGateIn

    if gate_in is None or getattr(gate_in, "retired_at", None) is not None:
        return
    now = timezone.now()
    EmptyVehicleGateIn.objects.filter(id=gate_in.id, retired_at__isnull=True).update(
        retired_at=now,
        retired_reason=reason,
        updated_by_id=getattr(user, "id", None),
        updated_at=now,
    )


def create_vehicle_arrival(
    *,
    vehicle,
    driver,
    company_ids,
    gate_in_date,
    in_time,
    user,
    tare_weight=None,
    weighbridge_slip_no="",
    security_name="",
    remarks="",
):
    """Create one cross-company physical arrival for ``vehicle``.

    For each of ``company_ids`` that has booked, unlinked bills for this vehicle,
    creates a COMPLETED dispatch gate-in (with covers + the single shared tare)
    under one ``VehicleArrival``. Returns the arrival, or ``None`` if no company
    has bills. The caller should guard against an already-open arrival first.
    """
    from django.db import transaction

    from company.models import Company
    from dispatch_plans.models import DispatchPlan, DispatchPlanStatus
    from driver_management.models import VehicleEntry
    from gate_core.models import EmptyVehicleGateIn, VehicleArrival
    from weighment.models import Weighment

    companies_with_bills = list(
        DispatchPlan.objects.filter(
            company_id__in=list(company_ids),
            vehicle=vehicle,
            booking_status=DispatchPlanStatus.BOOKED,
            linked_vehicle_entry__isnull=True,
            is_active=True,
        )
        .values_list("company_id", flat=True)
        .distinct()
    )
    if not companies_with_bills:
        return None

    with transaction.atomic():
        arrival = VehicleArrival.objects.create(
            arrival_no=VehicleArrival.generate_arrival_no(),
            vehicle=vehicle,
            driver=driver,
            gate_in_date=gate_in_date,
            in_time=in_time,
            tare_weight=tare_weight,
            weighbridge_slip_no=weighbridge_slip_no,
            security_name=security_name,
            remarks=remarks,
            created_by=user,
            updated_by=user,
        )
        for company in Company.objects.filter(id__in=companies_with_bills):
            _create_company_gate_in(
                arrival,
                company,
                vehicle,
                driver,
                gate_in_date,
                in_time,
                user,
                tare_weight=tare_weight,
                weighbridge_slip_no=weighbridge_slip_no,
                security_name=security_name,
            )
    return arrival


def replicate_dispatch_gate_in_across_companies(gate_in, user, company_ids):
    """Mirror a just-completed DISPATCH gate-in onto every other company that has
    booked bills for the same physical truck.

    One empty-vehicle-in (done under any single company) should mark the truck in
    across all the companies whose bills it carries. This wraps the gate-in in a
    ``VehicleArrival`` (so the truck becomes one cross-company trip, and future
    late bills can attach), then creates a sibling COMPLETED gate-in -- an exact
    copy: same driver, tare, date/time, with its own covers -- for each other
    company in ``company_ids`` that has booked, unlinked bills for the vehicle.

    Idempotent: a company already represented by a live gate-in under the arrival
    is skipped, so an arrival-backed gate-in (already cross-company) is a no-op.
    Returns the arrival (or ``None`` for a non-dispatch gate-in).
    """
    from django.db import transaction

    from company.models import Company
    from dispatch_plans.models import DispatchPlan, DispatchPlanStatus
    from gate_core.models import VehicleArrival, VehicleArrivalStatus
    from weighment.models import Weighment

    if gate_in.reason != "DISPATCH" or not gate_in.vehicle_id:
        return None

    vehicle = gate_in.vehicle
    weighment = Weighment.objects.filter(vehicle_entry=gate_in.vehicle_entry).first()
    tare = weighment.tare_weight if weighment else None
    slip = weighment.weighbridge_slip_no if weighment else ""
    security_name = gate_in.security_name or ""

    with transaction.atomic():
        arrival = gate_in.arrival
        if arrival is None:
            arrival = (
                VehicleArrival.objects.filter(
                    vehicle=vehicle,
                    is_active=True,
                    status__in=[VehicleArrivalStatus.INSIDE, VehicleArrivalStatus.LOADING],
                    # Reuse only a live trip; a stale arrival whose chains all
                    # retired must not absorb this fresh gate-in (it would keep the
                    # truck "inside" across visits). The fresh gate-in is unattached
                    # here (arrival is None), so it can't be a candidate's live chain.
                    gate_ins__is_active=True,
                    gate_ins__retired_at__isnull=True,
                )
                .order_by("-created_at")
                .distinct()
                .first()
            )
        if arrival is None:
            arrival = VehicleArrival.objects.create(
                arrival_no=VehicleArrival.generate_arrival_no(),
                vehicle=vehicle,
                driver=gate_in.driver,
                gate_in_date=gate_in.gate_in_date,
                in_time=gate_in.in_time,
                tare_weight=tare,
                weighbridge_slip_no=slip,
                security_name=security_name,
                status=VehicleArrivalStatus.INSIDE,
                created_by=user,
                updated_by=user,
            )
        if gate_in.arrival_id != arrival.id:
            gate_in.arrival = arrival
            gate_in.updated_by = user
            gate_in.save(update_fields=["arrival", "updated_by", "updated_at"])

        other_company_ids = (
            DispatchPlan.objects.filter(
                company_id__in=[c for c in company_ids if c != gate_in.company_id],
                vehicle=vehicle,
                booking_status=DispatchPlanStatus.BOOKED,
                linked_vehicle_entry__isnull=True,
                is_active=True,
            )
            .values_list("company_id", flat=True)
            .distinct()
        )
        for company in Company.objects.filter(id__in=list(other_company_ids)):
            if arrival.gate_ins.filter(
                company=company, is_active=True, retired_at__isnull=True
            ).exists():
                continue
            _create_company_gate_in(
                arrival,
                company,
                vehicle,
                gate_in.driver,
                gate_in.gate_in_date,
                gate_in.in_time,
                user,
                tare_weight=tare,
                weighbridge_slip_no=slip,
                security_name=security_name,
            )
    return arrival


def _create_company_gate_in(
    arrival,
    company,
    vehicle,
    driver,
    gate_in_date,
    in_time,
    user,
    *,
    tare_weight=None,
    weighbridge_slip_no="",
    security_name="",
    sap_doc_entries=None,
):
    """Create one company's COMPLETED dispatch gate-in under ``arrival``.

    Records covers (optionally constrained to ``sap_doc_entries``) and copies the
    arrival's shared tare. Shared by the initial arrival creation and the
    late-bill path (a bill of a new company joining a truck already inside).
    """
    from driver_management.models import VehicleEntry
    from gate_core.models import EmptyVehicleGateIn
    from weighment.models import Weighment

    entry_no = EmptyVehicleGateIn.generate_entry_no()
    vehicle_entry = VehicleEntry.objects.create(
        entry_no=entry_no,
        company=company,
        vehicle=vehicle,
        driver=driver,
        entry_type="EMPTY_VEHICLE",
        status="COMPLETED",
        created_by=user,
        updated_by=user,
    )
    gate_in = EmptyVehicleGateIn.objects.create(
        company=company,
        entry_no=entry_no,
        vehicle_entry=vehicle_entry,
        vehicle=vehicle,
        driver=driver,
        reason="DISPATCH",
        gate_in_date=gate_in_date,
        in_time=in_time,
        security_name=security_name,
        arrival=arrival,
        created_by=user,
        updated_by=user,
    )
    if tare_weight is not None:
        Weighment.objects.create(
            vehicle_entry=vehicle_entry,
            tare_weight=tare_weight,
            weighbridge_slip_no=weighbridge_slip_no,
            created_by=user,
            updated_by=user,
        )
    record_dispatch_covers(gate_in, user, sap_doc_entries=sap_doc_entries)
    return gate_in
