"""
dispatch_plans/freight_approval_service.py

A linked truck's freight against its benchmark (see `models_freight_approval`):
working the benchmark out, recording it when the truck is linked, the gate's
refusal while it waits, and the approver's decision.

The benchmark for a truck is the destination's rate on the chosen slab:

  - a PER_TRIP rate is the benchmark as it stands;
  - a PER_KG rate (Delhi NCR's 5,001-8,000 kg band) is multiplied by the load --
    the linked bills' invoice weight, which is kg -- and by the vehicle's
    capacity only when the bills carry no weight, so a part-load is not
    benchmarked as a full one.

The slab is suggested from the vehicle's capacity: the destination's rated slab
whose band holds it. The operator may pick another (a truck registered at 9 T
running as a 10 MT load), and the approver is shown when that happened.
"""

import logging
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Dict, Iterable, List, Optional

from django.db import transaction
from django.utils import timezone

from .models_freight_approval import (
    CLEARED_FREIGHT_APPROVAL_STATUSES,
    LIVE_FREIGHT_APPROVAL_STATUSES,
    DispatchFreightApproval,
    FreightApprovalStatus,
)
from .models_freight_benchmark import (
    FreightBenchmark,
    FreightDestination,
    FreightRateBasis,
    FreightSlab,
)

logger = logging.getLogger(__name__)

# Frontend routes (FactoryFlow).
APPROVALS_URL = "/admin/freight-approvals"
VEHICLE_LINKING_URL = "/dispatch/vehicle-linking"
# Codename only -- NotificationService matches on the bare permission codename.
PERM_APPROVE_CODENAME = "can_approve_freight_approvals"

REFUSAL_CODE = "FREIGHT_APPROVAL_REQUIRED"

PAISE = Decimal("0.01")


class FreightApprovalError(Exception):
    """A link or a decision the flow refuses, with the reason in words."""


def capacity_kg(vehicle) -> Optional[int]:
    if vehicle is None or not vehicle.capacity_ton:
        return None
    return int((Decimal(vehicle.capacity_ton) * 1000).to_integral_value(ROUND_HALF_UP))


def suggested_slab(destination: FreightDestination, capacity: Optional[int]):
    """The destination's rated slab whose band holds the vehicle's capacity."""
    if capacity is None:
        return None
    for benchmark in destination.benchmarks.select_related("slab"):
        slab = benchmark.slab
        if slab.is_active and slab.above_kg < capacity <= slab.up_to_kg:
            return slab
    return None


def load_kg_of(plans: Iterable) -> Optional[Decimal]:
    total = sum((Decimal(p.invoice_weight) for p in plans if p.invoice_weight), Decimal(0))
    return total if total > 0 else None


def benchmark_for(
    destination: FreightDestination,
    slab: FreightSlab,
    *,
    load_kg: Optional[Decimal],
    capacity: Optional[int],
) -> Dict[str, Any]:
    """The rate on (destination, slab) and the freight it comes to."""
    benchmark = FreightBenchmark.objects.filter(destination=destination, slab=slab).first()
    if benchmark is None:
        return {"basis": "", "rate": None, "load_kg": None, "freight": None}
    if benchmark.basis == FreightRateBasis.PER_KG:
        weight = load_kg if load_kg else (Decimal(capacity) if capacity else None)
        freight = (benchmark.amount * weight).quantize(PAISE) if weight else None
        return {
            "basis": benchmark.basis,
            "rate": benchmark.amount,
            "load_kg": weight,
            "freight": freight,
        }
    return {
        "basis": benchmark.basis,
        "rate": benchmark.amount,
        "load_kg": None,
        "freight": benchmark.amount,
    }


def _snapshot(plans: List) -> Dict[str, Any]:
    doc_nums, customers = [], []
    for plan in plans:
        doc_num = plan.sap_invoice_doc_num or str(plan.sap_invoice_doc_entry or "")
        if doc_num and doc_num not in doc_nums:
            doc_nums.append(doc_num)
        customer = (plan.customer_name or "").strip()
        if customer and customer not in customers:
            customers.append(customer)
    return {
        "bill_doc_nums": ", ".join(doc_nums),
        "customer_names": ", ".join(customers),
        "bill_count": len(plans),
    }


def _same_question(approval, *, vehicle, destination, slab, actual) -> bool:
    """Is this the same truck, going to the same place, at the same price?

    The bills are left out on purpose. A bill added to a truck whose freight has
    not moved does not change what the approver agreed to -- the price of that
    truck to that place -- and sending it back to the queue for it would hold a
    cleared truck at the gate over a question already answered.
    """
    return (
        approval.vehicle_id == vehicle.pk
        and approval.destination_id == destination.pk
        and approval.slab_id == slab.pk
        and approval.actual_freight == actual
    )


# ---------------------------------------------------------------------- #
# linking
# ---------------------------------------------------------------------- #
def record_link_freight(
    *,
    plans: List,
    truck: List,
    vehicle,
    transporter,
    destination: FreightDestination,
    slab: FreightSlab,
    actual_freight,
    reason: str = "",
    user=None,
) -> DispatchFreightApproval:
    """
    Hold a truck's freight against its benchmark.

    `plans` are the bills the freight is for; they share one row. `truck` is
    every bill the truck is booked to carry and has not been gated in with, so a
    truck only ever has one live freight: any other row on it is superseded, and
    a bill that was under the old freight but is not under this one comes off it
    with its share cleared.

    Re-saving the same truck at the same freight, destination and slab keeps the
    row it has, re-pointed at the bills as they now stand -- a linking save also
    carries bilty and remarks edits, and bills get added to a truck, and neither
    must send a cleared truck back to the queue. Anything else supersedes the old
    row and asks afresh.

    A reason is asked for by the linking form whenever the freight is over. It is
    not refused here: the form works a per-kg benchmark out from the bills it
    shows, and a link must not half-fail over the difference.
    """
    if not plans:
        raise FreightApprovalError("There are no bills to hold the freight against.")
    if not destination.is_active:
        raise FreightApprovalError(f"{destination.name} is out of use on the Freight Benchmarks.")
    actual = Decimal(actual_freight).quantize(PAISE)
    if actual < 0:
        raise FreightApprovalError("Freight cannot be negative.")

    plan_ids = {plan.pk for plan in plans}
    plan_model = type(plans[0])
    live = {
        plan.freight_approval
        for plan in truck
        if plan.freight_approval_id
        and plan.freight_approval.vehicle_id == vehicle.pk
        and plan.freight_approval.status in LIVE_FREIGHT_APPROVAL_STATUSES
    }

    def take_over(approval, replaced_ids):
        """Point this freight's bills at `approval`; free the ones left behind."""
        plan_model.objects.filter(pk__in=plan_ids).update(freight_approval=approval)
        left_behind = [
            plan.pk
            for plan in truck
            if plan.pk not in plan_ids and plan.freight_approval_id in replaced_ids
        ]
        if left_behind:
            # Their share was part of a freight that no longer covers them;
            # left in place it would be paid on top of the new one.
            plan_model.objects.filter(pk__in=left_behind).update(
                freight_approval=None, freight=None, total_freight=None
            )
        for plan in plans:
            plan.freight_approval = approval

    if len(live) == 1:
        (only,) = live
        if _same_question(only, vehicle=vehicle, destination=destination, slab=slab, actual=actual):
            snapshot = _snapshot(plans)
            with transaction.atomic():
                DispatchFreightApproval.objects.filter(pk=only.pk).update(**snapshot)
                for field, value in snapshot.items():
                    setattr(only, field, value)
                take_over(only, {only.pk})
            return only

    capacity = capacity_kg(vehicle)
    suggested = suggested_slab(destination, capacity)
    quote = benchmark_for(destination, slab, load_kg=load_kg_of(plans), capacity=capacity)
    within = quote["freight"] is not None and actual <= quote["freight"]
    reason = (reason or "").strip()

    with transaction.atomic():
        now = timezone.now()
        replaced = {a.pk for a in live}
        DispatchFreightApproval.objects.filter(pk__in=replaced).update(
            status=FreightApprovalStatus.SUPERSEDED, superseded_at=now
        )
        approval = DispatchFreightApproval.objects.create(
            company=plans[0].company,
            vehicle=vehicle,
            transporter=transporter,
            destination=destination,
            destination_label=f"{destination.name}, {destination.state}",
            slab=slab,
            slab_label=slab.label,
            suggested_slab=suggested,
            vehicle_capacity_kg=capacity,
            rate_basis=quote["basis"],
            rate_amount=quote["rate"],
            load_kg=quote["load_kg"],
            benchmark_freight=quote["freight"],
            actual_freight=actual,
            status=(
                FreightApprovalStatus.WITHIN_BENCHMARK
                if within
                else FreightApprovalStatus.PENDING
            ),
            reason="" if within else reason,
            requested_by=user,
            requested_at=now,
            **_snapshot(plans),
        )
        take_over(approval, replaced)
        if approval.status == FreightApprovalStatus.PENDING:
            transaction.on_commit(lambda: notify_approvers_of_new_request(approval))
    return approval


def truck_plans(vehicle, company_ids) -> List:
    """The bills a truck is booked to carry and has not yet been gated in with.

    The same set the gate snapshots as the gate-in's covers
    (`booked_plans_for_vehicle`). It can hold more than the desk is looking at:
    a bill booked onto the truck weeks ago and never dispatched is still in it.
    That is why the freight is taken over the bills the desk names, not over
    this whole set.
    """
    from gate_core.services.late_dispatch_gate_in import booked_plans_for_vehicle

    from .models import DispatchPlan

    ids = [plan.pk for plan in booked_plans_for_vehicle(vehicle, company_ids)]
    return list(
        DispatchPlan.objects.filter(pk__in=ids)
        .select_related("company", "transporter", "freight_approval")
        .order_by("company_id", "sap_invoice_doc_entry")
    )


# The bases a truck's freight is split on, in order of preference. One basis
# for every bill: splitting one bill by litres and the next by rupees of
# invoice value hands nearly the whole freight to the bill with no litres.
SPLIT_BASES = ("total_litres", "invoice_weight", "invoice_amount")


def split_freight(plans: List, total: Decimal) -> Dict[int, Decimal]:
    """The truck's freight over its bills: by litres if every bill has litres,
    else by weight if every bill has weight, else by value, else equally. The
    last bill takes the rounding, so the shares add up to the paisa."""
    weights = [Decimal(1)] * len(plans)
    for basis in SPLIT_BASES:
        candidate = [Decimal(getattr(plan, basis) or 0) for plan in plans]
        if all(weight > 0 for weight in candidate):
            weights = candidate
            break
    whole = sum(weights, Decimal(0))
    shares, running = {}, Decimal(0)
    for index, plan in enumerate(plans):
        if index == len(plans) - 1:
            share = total - running
        else:
            share = (total * weights[index] / whole).quantize(PAISE, rounding=ROUND_HALF_UP)
            running += share
        shares[plan.pk] = share
    return shares


def record_truck_freight(
    *,
    vehicle,
    company_ids,
    bills: List,
    extend: bool = False,
    destination: FreightDestination,
    slab: FreightSlab,
    actual_freight,
    reason: str = "",
    user=None,
) -> Dict[str, Any]:
    """
    The truck's freight, entered once for the whole truck at vehicle linking.

    `bills` are `(company_code, sap_invoice_doc_entry)` pairs the desk names:
    each must be booked on this truck and not yet gated in. With `extend` the
    freight also keeps every bill the truck's current freight already covers --
    what the linking sheet sends, since it only knows the bills it is adding.
    Without it the named bills are the whole of it -- what the truck card sends,
    since it shows the truck's bills.

    Held against the benchmark, then split over those bills (`split_freight`)
    into each plan's `freight` and `total_freight`, which is what the Service
    GRPO later pays the transporter from.
    """
    truck = truck_plans(vehicle, company_ids)
    by_key = {(plan.company.code, plan.sap_invoice_doc_entry): plan for plan in truck}
    missing = [str(doc_entry) for (_, doc_entry) in bills if (_, doc_entry) not in by_key]
    if missing:
        raise FreightApprovalError(
            f"{', '.join(missing)} {'is' if len(missing) == 1 else 'are'} not booked on "
            f"{vehicle.vehicle_number} waiting to be gated in."
        )
    chosen = {by_key[key].pk: by_key[key] for key in bills}
    if extend:
        for plan in truck:
            approval = plan.freight_approval
            if (
                approval is not None
                and approval.vehicle_id == vehicle.pk
                and approval.status in LIVE_FREIGHT_APPROVAL_STATUSES
            ):
                chosen.setdefault(plan.pk, plan)
    if not chosen:
        raise FreightApprovalError("Name the bills the freight is for.")
    plans = sorted(chosen.values(), key=lambda p: (p.company_id, p.sap_invoice_doc_entry))

    transporter = next(
        (plan.transporter for plan in plans if plan.transporter_id), vehicle.transporter
    )
    with transaction.atomic():
        approval = record_link_freight(
            plans=plans,
            truck=truck,
            vehicle=vehicle,
            transporter=transporter,
            destination=destination,
            slab=slab,
            actual_freight=actual_freight,
            reason=reason,
            user=user,
        )
        shares = split_freight(plans, approval.actual_freight)
        plan_model = type(plans[0])
        for plan in plans:
            share = shares[plan.pk]
            # Written with update(), not save(): a plan save sends the BOOKED
            # notification, and a freight entry is not a new booking.
            plan_model.objects.filter(pk=plan.pk).update(freight=share, total_freight=share)
            plan.freight = plan.total_freight = share
    return {"approval": approval, "plans": plans}


def truck_freight_board(company_ids) -> List[Dict[str, Any]]:
    """Every booked, not-yet-gated truck with its current freight approval and
    the bills that freight covers.

    One read for the whole Vehicle Linking board, so it can badge each truck
    without asking per truck. The board works out for itself which of the bills
    it shows are not covered: it shows a window of bills, and a count taken over
    every booking on the truck would name bills nobody can see.
    """
    from .models import DispatchPlan, DispatchPlanStatus

    plans = (
        DispatchPlan.objects.filter(
            company_id__in=company_ids,
            is_active=True,
            booking_status=DispatchPlanStatus.BOOKED,
            vehicle__isnull=False,
            linked_vehicle_entry__isnull=True,
        )
        .select_related("freight_approval", "vehicle", "company")
        .order_by("vehicle_id", "sap_invoice_doc_entry")
    )
    trucks: Dict[int, Dict[str, Any]] = {}
    for plan in plans:
        truck = trucks.setdefault(
            plan.vehicle_id, {"vehicle": plan.vehicle, "covered": {}, "approvals": {}}
        )
        approval = plan.freight_approval
        if (
            approval is not None
            and approval.vehicle_id == plan.vehicle_id
            and approval.status in LIVE_FREIGHT_APPROVAL_STATUSES
        ):
            truck["approvals"][approval.pk] = approval
            truck["covered"].setdefault(approval.pk, []).append(
                {"company_code": plan.company.code, "doc_entry": plan.sap_invoice_doc_entry}
            )
    board = []
    for vehicle_id, truck in trucks.items():
        approvals = sorted(truck["approvals"].values(), key=lambda a: a.requested_at)
        latest = approvals[-1] if approvals else None
        board.append(
            {
                "vehicle_id": vehicle_id,
                "vehicle_no": truck["vehicle"].vehicle_number,
                "approval": latest,
                "covered_bills": truck["covered"].get(latest.pk, []) if latest else [],
            }
        )
    return board


# ---------------------------------------------------------------------- #
# the gate
# ---------------------------------------------------------------------- #
def blocking_approvals(plans: Iterable, vehicle=None) -> List[DispatchFreightApproval]:
    """The truck's live freight approvals that do not yet let it in.

    An approval taken for another vehicle -- a bill moved trucks -- says nothing
    about this one, and is left out.
    """
    seen, blocking = set(), []
    for plan in plans:
        approval = plan.freight_approval
        if approval is None or approval.pk in seen:
            continue
        if vehicle is not None and approval.vehicle_id != vehicle.pk:
            continue
        seen.add(approval.pk)
        if approval.status in (FreightApprovalStatus.PENDING, FreightApprovalStatus.REJECTED):
            blocking.append(approval)
    return blocking


def gate_refusal(vehicle, plans: Iterable) -> Optional[Dict[str, Any]]:
    """
    The 400 body for a DISPATCH gate-in while the truck's freight waits, or None.

    A bill linked before this check existed has no approval at all and is let
    through: the rule is about freight somebody agreed from now on, not a
    retrospective audit of every truck already on the road.
    """
    blocking = blocking_approvals(list(plans), vehicle)
    if not blocking:
        return None
    approval = blocking[0]
    if approval.status == FreightApprovalStatus.REJECTED:
        note = approval.review_notes or "No reason given."
        detail = (
            f"{vehicle.vehicle_number}'s freight of ₹{approval.actual_freight:,.2f} to "
            f"{approval.destination_label} was refused. Reason: {note} Dispatch has to "
            "relink it at a freight that can be cleared before the truck comes in."
        )
    else:
        detail = (
            f"{vehicle.vehicle_number}'s freight of ₹{approval.actual_freight:,.2f} to "
            f"{approval.destination_label} is over its benchmark and still waiting "
            "for approval. The entry can be started once it is approved in Admin > "
            "Freight Approvals."
        )
    return {
        "detail": detail,
        "code": REFUSAL_CODE,
        "approval_status": approval.status,
        "approval_id": approval.pk,
    }


# ---------------------------------------------------------------------- #
# review
# ---------------------------------------------------------------------- #
def review(approval: DispatchFreightApproval, *, approve: bool, reviewer, notes: str = ""):
    notes = (notes or "").strip()
    if approval.status != FreightApprovalStatus.PENDING:
        raise FreightApprovalError(
            f"This freight is already {approval.get_status_display().lower()}."
        )
    if not approve and not notes:
        raise FreightApprovalError("Say why the freight is refused; dispatch reads it.")
    approval.mark_reviewed(
        status=FreightApprovalStatus.APPROVED if approve else FreightApprovalStatus.REJECTED,
        reviewer=reviewer,
        notes=notes,
    )
    transaction.on_commit(lambda: notify_requester_of_review(approval))
    return approval


def is_cleared(approval: Optional[DispatchFreightApproval]) -> bool:
    return approval is None or approval.status in CLEARED_FREIGHT_APPROVAL_STATUSES


# ---------------------------------------------------------------------- #
# notifications (best-effort: a failure never blocks a link or a decision)
# ---------------------------------------------------------------------- #
def _display_name(user) -> str:
    if user is None:
        return ""
    return getattr(user, "full_name", "") or getattr(user, "email", "")


def notify_approvers_of_new_request(approval: DispatchFreightApproval):
    from notifications.models import NotificationType
    from notifications.services import NotificationService

    requester = _display_name(approval.requested_by) or "Dispatch"
    if approval.benchmark_freight is None:
        against = f"no benchmark on {approval.slab_label}"
    else:
        against = f"benchmark ₹{approval.benchmark_freight:,.0f} on {approval.slab_label}"
    try:
        NotificationService.send_notification_by_permission(
            permission_codename=PERM_APPROVE_CODENAME,
            title="Freight over benchmark",
            body=(
                f"{requester} linked {approval.vehicle.vehicle_number} to "
                f"{approval.destination_label} at ₹{approval.actual_freight:,.0f} "
                f"({against}). Reason: {approval.reason}"
            ),
            notification_type=NotificationType.DISPATCH_FREIGHT_APPROVAL_REQUESTED,
            click_action_url=APPROVALS_URL,
            company=approval.company,
            extra_data={
                "freight_approval_id": approval.pk,
                "vehicle_id": approval.vehicle_id,
            },
            created_by=approval.requested_by,
        )
    except Exception as exc:  # best-effort
        logger.error(f"Failed to notify approvers of freight approval {approval.pk}: {exc}")


def notify_requester_of_review(approval: DispatchFreightApproval):
    from notifications.models import NotificationType
    from notifications.services import NotificationService

    if not approval.requested_by:
        return
    vehicle_no = approval.vehicle.vehicle_number
    approved = approval.status == FreightApprovalStatus.APPROVED
    if approved:
        body = (
            f"{vehicle_no}'s freight of ₹{approval.actual_freight:,.0f} to "
            f"{approval.destination_label} is approved. The gate can let it in."
        )
    else:
        body = (
            f"{vehicle_no}'s freight of ₹{approval.actual_freight:,.0f} to "
            f"{approval.destination_label} was refused: "
            f"{approval.review_notes or 'no reason given'}. Relink it at a freight "
            "that can be cleared."
        )
    try:
        NotificationService.send_notification_to_user(
            user=approval.requested_by,
            title=f"Freight {'approved' if approved else 'refused'}",
            body=body,
            notification_type=NotificationType.DISPATCH_FREIGHT_APPROVAL_REVIEWED,
            click_action_url=VEHICLE_LINKING_URL,
            reference_type="dispatch_freight_approval",
            reference_id=approval.pk,
            company=approval.company,
            created_by=approval.reviewed_by,
        )
    except Exception as exc:  # best-effort
        logger.error(f"Failed to notify requester of freight review {approval.pk}: {exc}")
