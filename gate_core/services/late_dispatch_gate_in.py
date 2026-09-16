"""The evening cutoff on DISPATCH empty-vehicle gate-ins, and its approval.

A truck that turns up to *load* after the cutoff cannot be loaded, docked,
weighed and gate-passed the same evening, so somebody has to own the decision to
let it in. Past the cutoff the gate cannot start a DISPATCH empty-vehicle entry
until a ``LateDispatchGateInApproval`` for that truck and date has been approved.

The decision is dispatch's. Dispatch booked the truck and is the only side that
can say whether this load is worth keeping a loading crew back for, so dispatch
raises the request from Dispatch > Vehicle Linking, ahead of the truck arriving.
The gate only enforces the answer: past the cutoff it refuses the entry and says
who to go to. It has no way to ask on its own behalf, deliberately.

Only DISPATCH is time-bound: a repair movement, job work or other reason is not
loading anything, and has never been held to a cutoff.

Everything the rule needs lives here -- the cutoff itself, the lateness test, the
load snapshot shown to the approver, and the lookup/consume pair -- so the gate
create endpoint, the request endpoint and the tests all read the same rule.
"""

import datetime as dt
import logging

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

DEFAULT_CUTOFF = dt.time(17, 0)

# Frontend route for the approver's queue (FactoryFlow admin module).
APPROVALS_URL = "/admin/late-dispatch-approvals"
# Where dispatch raises the request and reads the verdict.
VEHICLE_LINKING_URL = "/dispatch/vehicle-linking"
# Codename only -- NotificationService matches on the bare permission codename.
PERM_APPROVE_CODENAME = "can_approve_late_dispatch_gate_in"


def cutoff_time():
    """The hour after which a dispatch truck needs an approval to be gated in.

    Read from settings on every call rather than captured at import, so the site
    can move it (and tests can override it) without a restart.
    """
    configured = getattr(settings, "LATE_DISPATCH_GATE_IN_CUTOFF", None)
    if isinstance(configured, dt.time):
        return configured
    if isinstance(configured, str) and configured.strip():
        try:
            return dt.time.fromisoformat(configured.strip())
        except ValueError:
            logger.warning(
                "Ignoring unparseable LATE_DISPATCH_GATE_IN_CUTOFF %r", configured
            )
    return DEFAULT_CUTOFF


def is_late_dispatch_gate_in(gate_in_date, in_time, now=None):
    """True when this gate-in is being recorded past the cutoff.

    Judged on the *later* of the two clocks that matter. The typed ``in_time`` is
    the recorded arrival and normally decides it; but the field is editable, so an
    entry started at 8 PM with "16:00" typed into it would otherwise walk straight
    past the rule. For an entry dated today the wall clock therefore also counts.
    A back-dated entry has no live clock to appeal to and rests on ``in_time``.
    """
    cutoff = cutoff_time()
    if in_time >= cutoff:
        return True
    now = now or timezone.localtime()
    return gate_in_date == now.date() and now.time() >= cutoff


def booked_plans_for_vehicle(vehicle, company_ids):
    """The truck's booked, not-yet-gated-in dispatch plans across the user's companies.

    Same set ``record_dispatch_covers`` snapshots as the gate-in's covers, so the
    load the approver is shown is the load the truck will actually be let in with.
    """
    from dispatch_plans.models import DispatchPlan, DispatchPlanStatus

    return list(
        DispatchPlan.objects.filter(
            company_id__in=company_ids,
            is_active=True,
            booking_status=DispatchPlanStatus.BOOKED,
            vehicle=vehicle,
            linked_vehicle_entry__isnull=True,
        )
        .select_related("company")
        # Ordered so the snapshot the approver reads lists the bills the same way
        # twice running, rather than in whatever order the rows came back.
        .order_by("sap_invoice_doc_entry")
    )


def resolve_dispatch_company(company_ids, active_company, vehicle):
    """Owning company for a DISPATCH record on this truck.

    The company whose booked bills the truck carries, not the active Company-Code
    (the selector is a decorator at the gate). Prefers the active company when it
    has bills -- no surprise for the operator -- else the company that actually
    does; falls back to the active company when there are no bills yet.

    Shared by the gate-in create endpoint and the late-approval request so an
    approval is always filed in the same company as the gate-in it will let through.
    """
    from company.models import Company
    from dispatch_plans.models import DispatchPlan, DispatchPlanStatus

    booked = DispatchPlan.objects.filter(
        company_id__in=company_ids,
        vehicle=vehicle,
        booking_status=DispatchPlanStatus.BOOKED,
        linked_vehicle_entry__isnull=True,
        is_active=True,
    )
    if booked.filter(company_id=active_company.id).exists():
        return active_company
    company_id = (
        booked.values_list("company_id", flat=True).order_by("company_id").first()
    )
    if company_id is None:
        return active_company
    return Company.objects.get(id=company_id)


def load_snapshot(plans):
    """Bill numbers / customers / count for the approver, from the truck's plans."""
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


def usable_approval(vehicle, gate_in_date, company_ids):
    """An approved, unspent approval that lets this truck in on this date, or None."""
    from gate_core.models import (
        LateDispatchGateInApproval,
        LateDispatchGateInApprovalStatus,
    )

    return (
        LateDispatchGateInApproval.objects.filter(
            vehicle=vehicle,
            gate_in_date=gate_in_date,
            company_id__in=company_ids,
            status=LateDispatchGateInApprovalStatus.APPROVED,
            consumed_at__isnull=True,
            is_active=True,
        )
        .order_by("-reviewed_at", "-id")
        .first()
    )


def latest_approval(vehicle, gate_in_date, company_ids):
    """The most recent approval of any status for this truck and date, or None.

    Drives the message the gate is refused with: "waiting with the approver" reads
    very differently from "nobody has been asked yet".
    """
    from gate_core.models import LateDispatchGateInApproval

    return (
        LateDispatchGateInApproval.objects.filter(
            vehicle=vehicle,
            gate_in_date=gate_in_date,
            company_id__in=company_ids,
            is_active=True,
        )
        .order_by("-requested_at", "-id")
        .first()
    )


def consume_approval(approval, gate_in, user):
    """Spend an approval on the gate-in it let through.

    The entry's own ``in_time`` is stamped onto the approval as it is spent. Nobody
    knows it when the request is raised -- dispatch asks hours before the truck
    turns up -- so this is the only moment the hour the truck actually came in can
    be recorded against the clearance that allowed it.
    """
    approval.empty_vehicle_gate_in = gate_in
    approval.in_time = getattr(gate_in, "in_time", None)
    approval.consumed_at = timezone.now()
    approval.updated_by = user
    approval.save(
        update_fields=[
            "empty_vehicle_gate_in",
            "in_time",
            "consumed_at",
            "updated_by",
            "updated_at",
        ]
    )


def format_cutoff():
    """The cutoff as the gate reads it on screen, e.g. ``5:00 PM``."""
    return cutoff_time().strftime("%I:%M %p").lstrip("0")


def refusal_payload(vehicle, gate_in_date, company_ids):
    """The 400 body for a late gate-in with no approval behind it.

    Every wording sends the operator to the same place -- dispatch -- because the
    gate cannot fix any of these itself. ``code`` is what the client branches on,
    turning the refusal into a proper notice on the Empty Vehicle In board rather
    than a bare red toast.
    """
    approval = latest_approval(vehicle, gate_in_date, company_ids)
    cutoff = format_cutoff()
    approval_status = approval.status if approval else "NONE"

    if approval_status == "PENDING":
        detail = (
            f"{vehicle.vehicle_number} is being gated in for dispatch after {cutoff}. "
            "Dispatch has asked for it to be allowed, but the request is still "
            "waiting with the approver."
        )
    elif approval_status == "REJECTED":
        note = approval.review_notes or "No reason given."
        detail = (
            f"{vehicle.vehicle_number} was refused a late dispatch gate-in for "
            f"{gate_in_date}. Reason: {note}"
        )
    else:
        detail = (
            f"{vehicle.vehicle_number} is being gated in for dispatch after {cutoff}, "
            "which needs an approval. Ask dispatch to raise it from Vehicle Linking; "
            "the entry can be started once it is approved."
        )

    return {
        "detail": detail,
        "code": "LATE_DISPATCH_APPROVAL_REQUIRED",
        "approval_status": approval_status,
        "approval_id": approval.id if approval else None,
        "cutoff": cutoff_time().isoformat(timespec="minutes"),
    }


# ---------------------------------------------------------------------------
# Notifications (best-effort: a notification failure never blocks the gate)
# ---------------------------------------------------------------------------


def notify_approvers_of_new_request(approval):
    """Tell everyone who can clear a late gate-in that a truck is waiting."""
    from gate_core.serializers_sales_dispatch import user_display_name
    from notifications.models import NotificationType
    from notifications.services import NotificationService

    requester = user_display_name(approval.requested_by) or "Dispatch"
    bills = approval.bill_doc_nums or "no booked bill"
    try:
        NotificationService.send_notification_by_permission(
            permission_codename=PERM_APPROVE_CODENAME,
            title="Late dispatch gate-in requested",
            body=(
                f"{requester} wants {approval.vehicle.vehicle_number} let in for "
                f"dispatch after {format_cutoff()} on {approval.gate_in_date} "
                f"(bills {bills}). Reason: {approval.reason}"
            ),
            notification_type=NotificationType.LATE_DISPATCH_GATE_IN_REQUESTED,
            click_action_url=APPROVALS_URL,
            company=approval.company,
            extra_data={
                "late_dispatch_approval_id": approval.id,
                "vehicle_id": approval.vehicle_id,
            },
            created_by=approval.requested_by,
        )
    except Exception as exc:  # best-effort: never block the request
        logger.error(
            f"Failed to notify approvers of late dispatch request {approval.id}: {exc}"
        )


def notify_requester_of_review(approval):
    """Tell dispatch whether the truck may come in."""
    from notifications.models import NotificationType
    from notifications.services import NotificationService

    recipient = approval.requested_by
    if not recipient:
        return

    approved = approval.is_approved
    vehicle_no = approval.vehicle.vehicle_number
    if approved:
        body = (
            f"{vehicle_no} is cleared for a late dispatch gate-in on "
            f"{approval.gate_in_date}. The gate can start its entry now."
        )
    else:
        note = approval.review_notes or "No reason provided."
        body = (
            f"{vehicle_no} was refused a late dispatch gate-in on "
            f"{approval.gate_in_date}. Reason: {note}"
        )

    try:
        NotificationService.send_notification_to_user(
            user=recipient,
            title=f"Late dispatch gate-in {'approved' if approved else 'rejected'}",
            body=body,
            notification_type=NotificationType.LATE_DISPATCH_GATE_IN_REVIEWED,
            click_action_url=VEHICLE_LINKING_URL,
            reference_type="late_dispatch_gate_in_approval",
            reference_id=approval.id,
            company=approval.company,
            created_by=approval.reviewed_by,
        )
    except Exception as exc:  # best-effort: never block the review
        logger.error(
            f"Failed to notify requester of late dispatch review {approval.id}: {exc}"
        )
