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
