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


# A bill's load is committed (and must not be detached) once boxes are scanned or
# its docking is photo-locked / dispatched.
_COMMITTED_DOCKING_STATUSES = _LOAD_LOCKED_DOCKING_STATUSES + ("DISPATCHED",)


def bill_commit_reason(plan):
    """Reason a bill's load is committed (so it can't be detached), or None.

    Mirrors the add cut-off from the other direction: once loading has started
    (a box scan) or the docking is photo-locked/dispatched, the physical load is
    fixed and the bill must not be pulled off the vehicle from the console.
    """
    if plan is None:
        return None
    from gate_core.models import (
        SalesDispatchBoxScan,
        SalesDispatchGateOut,
        SalesDispatchGateOutDocument,
    )

    # Scans attributed to THIS bill via its own document -- works whether the bill is the
    # docking's primary or a *secondary* document on a shared load. (Scoping by
    # ``sales_dispatch__dispatch_plan`` alone missed a scanned secondary bill, because a
    # shared docking's primary FK points at a different bill.)
    if SalesDispatchBoxScan.objects.filter(
        is_active=True, document__dispatch_plan=plan
    ).exists():
        return "loading has started (boxes scanned)"
    # Legacy scans not yet attributed to a document: fall back to the docking's primary.
    if SalesDispatchBoxScan.objects.filter(
        is_active=True, document__isnull=True, sales_dispatch__dispatch_plan=plan
    ).exists():
        return "loading has started (boxes scanned)"
    # This bill's document rides a committed/dispatched load (primary or secondary).
    if SalesDispatchGateOutDocument.objects.filter(
        dispatch_plan=plan,
        is_active=True,
        sales_dispatch__is_active=True,
        sales_dispatch__status__in=_COMMITTED_DOCKING_STATUSES,
    ).exists():
        return "its docking is already committed"
    # Legacy single-document docking (no document rows) whose primary is this plan.
    if SalesDispatchGateOut.objects.filter(
        dispatch_plan=plan, is_active=True, status__in=_COMMITTED_DOCKING_STATUSES
    ).exists():
        return "its docking is already committed"
    return None


# A docking in one of these states is finalized: committed/loaded, gone, or
# already unwound. Detaching a bill must leave those alone (a committed one is
# blocked outright by ``bill_commit_reason``); only a "docked but not loaded"
# gate-out is safe to cancel when the bill comes off the vehicle.
_FINALIZED_DOCKING_STATUSES = _COMMITTED_DOCKING_STATUSES + ("REJECTED", "CANCELLED")


def cancel_unscanned_dockings_for_plan(plan, user, now=None):
    """Cancel a plan's un-scanned, non-finalized docking gate-outs.

    A docking (``SalesDispatchGateOut``) is a record separate from the gate-in
    cover, so pulling a bill off the vehicle must also unwind any docking the bill
    created -- otherwise the docking lingers on the dock board pointing at a plan
    that is no longer linked to the truck.

    Finds every active docking carrying this bill by the bill's own *document* row,
    not just the docking's primary ``dispatch_plan`` FK, so a bill that rode as a
    *secondary* document on a shared docking is handled too (previously it was
    missed, leaving its document + box scans + the header stale on the load). A
    shared docking keeps its other bills -- only this bill's document is unwound via
    ``remove_document_from_docking``; a single-bill docking is cancelled outright.
    Committed/scanned loads are left for ``bill_commit_reason`` to block the detach.
    Returns the number of dockings affected.
    """
    from gate_core.models import (
        SalesDispatchGateOut,
        SalesDispatchGateOutDocument,
        SalesDispatchGateOutStatus,
    )
    from gate_core.services.sales_dispatch_docking import remove_document_from_docking

    if plan is None:
        return 0
    now = now or timezone.now()

    docking_ids = set(
        SalesDispatchGateOutDocument.objects.filter(
            dispatch_plan=plan, is_active=True, sales_dispatch__is_active=True
        ).values_list("sales_dispatch_id", flat=True)
    )
    docking_ids |= set(
        SalesDispatchGateOut.objects.filter(
            dispatch_plan=plan, is_active=True
        ).values_list("id", flat=True)
    )

    affected = 0
    for docking in (
        SalesDispatchGateOut.objects.filter(id__in=docking_ids)
        .exclude(status__in=_FINALIZED_DOCKING_STATUSES)
        .select_related("vehicle_entry")
    ):
        active_docs = list(
            SalesDispatchGateOutDocument.objects.filter(
                sales_dispatch=docking, is_active=True
            )
        )
        this_doc = next(
            (
                d
                for d in active_docs
                if d.dispatch_plan_id == plan.id
                or d.sap_doc_entry == plan.sap_invoice_doc_entry
            ),
            None,
        )
        if len(active_docs) > 1 and this_doc is not None:
            # Shared load: pull just this bill off, keeping the rest (including any
            # scanned/committed bills) intact. This bill itself is un-scanned here --
            # ``bill_commit_reason`` already blocks the detach once its boxes are scanned.
            remove_document_from_docking(docking, this_doc, user)
            affected += 1
            continue
        # Single-bill docking (or a legacy docking with no document rows): cancel it,
        # unless it already carries scans (mirrors the "docked but not loaded" rule).
        if docking.box_scans.filter(is_active=True).exists():
            continue
        docking.status = SalesDispatchGateOutStatus.CANCELLED
        docking.cancel_reason = "Bill removed from inside vehicle before loading."
        docking.cancelled_by = user
        docking.cancelled_at = now
        docking.updated_by = user
        docking.save(
            update_fields=[
                "status",
                "cancel_reason",
                "cancelled_by",
                "cancelled_at",
                "updated_by",
                "updated_at",
            ]
        )
        dock_entry = docking.vehicle_entry
        if dock_entry is not None and dock_entry.status != "CANCELLED":
            dock_entry.status = "CANCELLED"
            dock_entry.updated_by = user
            dock_entry.save(update_fields=["status", "updated_by", "updated_at"])
        affected += 1
    return affected


def detach_bill_from_gate_in(gate_in, sap_doc_entry, user, reset_plan=True):
    """Remove a bill's cover from an inside gate-in, cancel its docking, unlink it.

    The database-surgery-free way to pull a wrongly/duplicately attached bill off
    an inside vehicle (what previously needed a manual cover delete + plan
    unlink): deletes the active cover, cancels any un-scanned docking the bill
    created, and -- when ``reset_plan`` -- returns the plan to ``PENDING`` with no
    vehicle, the state every bill has before vehicle linking, so it re-enters the
    unbooked pool cleanly. ``Move`` passes ``reset_plan=False`` because it re-books
    the plan onto the destination truck immediately. Refuses if the bill is
    already dispatched or its load is committed. Returns ``(ok, detail)``.
    """
    from dispatch_plans.models import DispatchPlan, DispatchPlanStatus
    from gate_core.models import EmptyVehicleGateInCover

    cover = EmptyVehicleGateInCover.objects.filter(
        empty_vehicle_gate_in=gate_in, sap_doc_entry=sap_doc_entry, is_active=True
    ).first()
    if cover is None:
        return False, "That bill is not on this vehicle."
    if cover.consumed_at is not None:
        return False, "That bill has already been dispatched."

    plan = DispatchPlan.objects.filter(
        company=gate_in.company, sap_invoice_doc_entry=sap_doc_entry
    ).first()
    reason = bill_commit_reason(plan)
    if reason:
        return False, f"Cannot remove: {reason}."

    now = timezone.now()
    user_id = getattr(user, "id", None)
    cover.delete()
    if plan is not None:
        # The docking is a separate record from the cover -- cancel it too so it
        # stops showing on the dock board (this was the missing step that left
        # "docked but not loaded" bills stranded after a remove).
        cancel_unscanned_dockings_for_plan(plan, user, now)
        if reset_plan:
            DispatchPlan.objects.filter(id=plan.id).update(
                booking_status=DispatchPlanStatus.PENDING,
                linked_vehicle_entry=None,
                vehicle=None,
                driver=None,
                transporter=None,
                updated_by_id=user_id,
                updated_at=now,
            )
        elif plan.linked_vehicle_entry_id == gate_in.vehicle_entry_id:
            DispatchPlan.objects.filter(id=plan.id).update(
                linked_vehicle_entry=None, updated_by_id=user_id, updated_at=now
            )
    # Removing this bill may have left the chain with nothing open to dispatch.
    # Close it out (and depart the truck once every chain is closed) so an emptied
    # gate-in can't keep the vehicle perpetually "inside" -- the 0-cover-phantom
    # bug. Only when we actually pulled the bill off (reset_plan); Move re-attaches
    # the plan elsewhere and manages the source itself.
    if reset_plan and not EmptyVehicleGateInCover.objects.filter(
        empty_vehicle_gate_in=gate_in, is_active=True, consumed_at__isnull=True
    ).exists():
        from gate_core.models import EmptyVehicleGateInRetireReason

        retire_empty_in(gate_in, EmptyVehicleGateInRetireReason.EMPTY_OUT, user)
    return True, f"Bill {cover.sap_doc_num or sap_doc_entry} removed from the vehicle."


def retire_empty_in(gate_in, reason, user):
    """Explicitly retire a gate-in (e.g. the truck left empty).

    The truck is leaving with these bills still un-dispatched, so its remaining
    (un-consumed) covers are released too. Otherwise they linger *active* on a
    dead gate-in and keep re-attaching the bill to later trips of the same truck
    -- the cover leak behind bills that reappear on the dock across arrivals.
    Consumed covers are left untouched as the dispatch audit trail (the
    ``_retire_if_fully_consumed`` / un-dispatch path relies on them).
    """
    from gate_core.models import EmptyVehicleGateIn, EmptyVehicleGateInCover

    if gate_in is None or getattr(gate_in, "retired_at", None) is not None:
        return
    now = timezone.now()
    user_id = getattr(user, "id", None)
    EmptyVehicleGateIn.objects.filter(id=gate_in.id, retired_at__isnull=True).update(
        retired_at=now,
        retired_reason=reason,
        updated_by_id=user_id,
        updated_at=now,
    )
    EmptyVehicleGateInCover.objects.filter(
        empty_vehicle_gate_in_id=gate_in.id, is_active=True, consumed_at__isnull=True
    ).update(is_active=False, updated_by_id=user_id, updated_at=now)
    # If this was the truck's last live chain, it has physically left -- mark the
    # arrival DEPARTED, exactly as the dispatch path does. Without this, a truck
    # whose final chain leaves via empty-out (not dispatch) stays stuck INSIDE /
    # LOADING forever, and the next visit reuses that stale trip.
    _depart_arrival_if_complete(getattr(gate_in, "arrival_id", None), user, now)


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
