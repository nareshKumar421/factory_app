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


def consume_covers_for_dispatched_plans(plans, user):
    """Mark dispatched plans' covers consumed, then retire fully-consumed gate-ins.

    Called when a docking is marked dispatched: each bill that physically left is
    consumed; once all of a gate-in's bills are consumed the truck is gone, so the
    gate-in retires and stops making anything eligible.
    """
    from gate_core.models import EmptyVehicleGateInCover, EmptyVehicleGateInRetireReason

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
    EmptyVehicleGateIn.objects.filter(id__in=gate_in_ids, retired_reason="DISPATCHED").update(
        retired_at=None,
        retired_reason="",
        updated_by_id=getattr(user, "id", None),
        updated_at=now,
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
        return False
    # Cutoff: the load is fixed once the truck photo is attached at docking.
    if SalesDispatchGateOut.objects.filter(
        company_id=plan.company_id,
        is_active=True,
        vehicle_id=plan.vehicle_id,
        status__in=_LOAD_LOCKED_DOCKING_STATUSES,
    ).exists():
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
            record_dispatch_covers(gate_in, user)
    return arrival
