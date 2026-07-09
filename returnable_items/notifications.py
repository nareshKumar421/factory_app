"""Notification fan-out for the Returnable Items module.

Every transition notifies the *other* side of the handoff, so nobody has to poll
a list to find out that work has arrived. All helpers swallow their exceptions —
a notification failure must never roll back the transition that triggered it, the
same contract ``maintenance/jobs.py`` follows for work-permit expiry.

Deep links point at the screen the recipient needs, not a generic list:
department users land on the pass detail, gate users land on their queue item.
"""

import logging

from notifications.models import NotificationType
from notifications.services import NotificationService

logger = logging.getLogger(__name__)

REFERENCE_TYPE = "returnable_gatepass"

#: Permission codenames used as notification audiences.
APPROVE_PERM = "can_approve_returnable_gatepass"
GATE_OUT_PERM = "can_gate_out_returnable"
GATE_IN_PERM = "can_gate_in_returnable"
ACKNOWLEDGE_PERM = "can_acknowledge_returnable"


def _department_url(gate_pass):
    return f"/maintenance/returnable/{gate_pass.id}"


def _gate_out_url(gate_pass):
    return f"/gate/return-out/{gate_pass.id}"


def _gate_in_url(gate_pass):
    return f"/gate/return-in/{gate_pass.id}"


def _safe(fn, gate_pass, label):
    try:
        fn()
    except Exception as exc:  # notifications never block a transition
        logger.error(
            "[Returnable] Failed to send %s notification for pass %s: %s",
            label,
            gate_pass.pass_no,
            exc,
            exc_info=True,
        )


def _by_permission(codename, gate_pass, title, body, ntype, url, actor=None):
    NotificationService.send_notification_by_permission(
        permission_codename=codename,
        title=title,
        body=body,
        notification_type=ntype,
        click_action_url=url,
        reference_type=REFERENCE_TYPE,
        reference_id=gate_pass.id,
        company=gate_pass.company,
        created_by=actor,
    )


def _to_user(user, gate_pass, title, body, ntype, url, actor=None):
    if not user:
        return
    NotificationService.send_notification_to_user(
        user=user,
        title=title,
        body=body,
        notification_type=ntype,
        click_action_url=url,
        reference_type=REFERENCE_TYPE,
        reference_id=gate_pass.id,
        company=gate_pass.company,
        created_by=actor,
    )


def _owner(gate_pass):
    """Whoever should hear about this pass — the submitter, else the creator."""
    return gate_pass.submitted_by or gate_pass.created_by


# ---------------------------------------------------------------------------
# Transition notifications
# ---------------------------------------------------------------------------


def notify_submitted(gate_pass, actor=None):
    """Department submitted the pass → tell the approvers, not the gate.

    The gate hears nothing until the pass is approved.
    """

    item_count = gate_pass.items.count()
    title = "Returnable gate pass awaiting your approval"
    body = (
        f"{gate_pass.pass_no}: {item_count} item(s) for {gate_pass.get_purpose_display().lower()} "
        f"to {gate_pass.party_name}. Expected back by "
        f"{gate_pass.expected_return_date:%d %b %Y}."
    )
    _safe(
        lambda: _by_permission(
            APPROVE_PERM,
            gate_pass,
            title,
            body,
            NotificationType.RETURNABLE_SUBMITTED,
            _department_url(gate_pass),
            actor,
        ),
        gate_pass,
        "submitted",
    )


def notify_approved(gate_pass, actor=None):
    """Approver signed off → the pass reaches the gate, and the creator is told."""

    item_count = gate_pass.items.count()
    approver = getattr(gate_pass.approved_by, "full_name", "") or "the approver"

    def send():
        # The gate now has work to do.
        _by_permission(
            GATE_OUT_PERM,
            gate_pass,
            "Returnable gate pass ready for gate out",
            (
                f"{gate_pass.pass_no}: {item_count} item(s) for "
                f"{gate_pass.get_purpose_display().lower()} to {gate_pass.party_name}. "
                f"Expected back by {gate_pass.expected_return_date:%d %b %Y}."
            ),
            NotificationType.RETURNABLE_APPROVED,
            _gate_out_url(gate_pass),
            actor,
        )
        # The department knows it cleared approval.
        _to_user(
            _owner(gate_pass),
            gate_pass,
            "Returnable gate pass approved",
            f"{gate_pass.pass_no} was approved by {approver} and is now with the gate.",
            NotificationType.RETURNABLE_APPROVED,
            _department_url(gate_pass),
            actor,
        )

    _safe(send, gate_pass, "approved")


def notify_approval_rejected(gate_pass, reason, actor=None):
    """Approver sent it back → only the department hears; the gate never saw it."""

    title = "Returnable gate pass rejected by approver"
    body = f"{gate_pass.pass_no} was sent back to draft. Reason: {reason}"
    _safe(
        lambda: _to_user(
            _owner(gate_pass),
            gate_pass,
            title,
            body,
            NotificationType.RETURNABLE_APPROVAL_REJECTED,
            _department_url(gate_pass),
            actor,
        ),
        gate_pass,
        "approval_rejected",
    )


def notify_gate_out(gate_pass, actor=None):
    """Vehicle has left → tell the department who raised the pass."""

    vehicle = gate_pass.vehicle.vehicle_number if gate_pass.vehicle_id else gate_pass.vehicle_number_manual
    title = "Returnable items have left the gate"
    body = (
        f"{gate_pass.pass_no} was gated out on vehicle {vehicle or 'N/A'} "
        f"to {gate_pass.party_name}. Expected back by "
        f"{gate_pass.expected_return_date:%d %b %Y}."
    )
    _safe(
        lambda: _to_user(
            _owner(gate_pass),
            gate_pass,
            title,
            body,
            NotificationType.RETURNABLE_GATE_OUT,
            _department_url(gate_pass),
            actor,
        ),
        gate_pass,
        "gate_out",
    )


def notify_rejected_at_gate(gate_pass, reason, actor=None):
    """Gate found a mismatch → send it back to the department with the reason."""

    title = "Returnable gate pass rejected at the gate"
    body = f"{gate_pass.pass_no} was returned to draft by the gate. Reason: {reason}"
    _safe(
        lambda: _to_user(
            _owner(gate_pass),
            gate_pass,
            title,
            body,
            NotificationType.RETURNABLE_REJECTED_AT_GATE,
            _department_url(gate_pass),
            actor,
        ),
        gate_pass,
        "rejected_at_gate",
    )


def notify_return_recorded(gate_pass, event, actor=None):
    """Gate accepted a return trip → tell the department to come and collect."""

    pending = gate_pass.pending_return_qty
    fully = pending <= 0
    title = "Returnable items are back at the gate"
    body = (
        f"{gate_pass.pass_no}: return {event.event_ref or event.event_no} recorded. "
        + (
            "All items are back. Collect them from the gate and close the pass."
            if fully
            else f"{pending} unit(s) still pending. Collect the returned items from the gate."
        )
    )

    def send():
        _to_user(
            _owner(gate_pass),
            gate_pass,
            title,
            body,
            NotificationType.RETURNABLE_RETURN_RECORDED,
            _department_url(gate_pass),
            actor,
        )
        _by_permission(
            ACKNOWLEDGE_PERM,
            gate_pass,
            title,
            body,
            NotificationType.RETURNABLE_RETURN_RECORDED,
            _department_url(gate_pass),
            actor,
        )

    _safe(send, gate_pass, "return_recorded")


def notify_acknowledged(gate_pass, event, actor=None):
    """Department collected the material → let the gate stop holding it."""

    title = "Returned items collected by the department"
    body = (
        f"{gate_pass.pass_no}: return {event.event_ref or event.event_no} has been "
        f"collected from the gate by the department."
    )
    _safe(
        lambda: _by_permission(
            GATE_IN_PERM,
            gate_pass,
            title,
            body,
            NotificationType.RETURNABLE_ACKNOWLEDGED,
            _gate_in_url(gate_pass),
            actor,
        ),
        gate_pass,
        "acknowledged",
    )


def notify_due_today(gate_pass):
    """The expected return date has arrived → warn the gate AND the department.

    The gate needs to know to expect the vehicle; the department needs to chase
    the vendor. Sent once per pass, guarded by ``due_notified_at``.
    """

    title = "Returnable items due back today"
    body = (
        f"{gate_pass.pass_no}: {gate_pass.pending_return_qty} unit(s) from "
        f"{gate_pass.party_name} are due back today."
    )

    def send():
        _by_permission(
            GATE_IN_PERM,
            gate_pass,
            title,
            body,
            NotificationType.RETURNABLE_DUE_TODAY,
            _gate_in_url(gate_pass),
        )
        _to_user(
            _owner(gate_pass),
            gate_pass,
            title,
            f"{body} Follow up with the party if they have not dispatched.",
            NotificationType.RETURNABLE_DUE_TODAY,
            _department_url(gate_pass),
        )

    _safe(send, gate_pass, "due_today")


def notify_overdue(gate_pass):
    """Past the expected return date and still not back → department, gate, admins."""

    days = gate_pass.days_overdue
    title = "Returnable items overdue"
    body = (
        f"{gate_pass.pass_no}: {gate_pass.pending_return_qty} unit(s) from "
        f"{gate_pass.party_name} are {days} day(s) overdue "
        f"(expected {gate_pass.expected_return_date:%d %b %Y})."
    )

    def send():
        _to_user(
            _owner(gate_pass),
            gate_pass,
            title,
            body,
            NotificationType.RETURNABLE_OVERDUE,
            _department_url(gate_pass),
        )
        _by_permission(
            GATE_IN_PERM,
            gate_pass,
            title,
            body,
            NotificationType.RETURNABLE_OVERDUE,
            _gate_in_url(gate_pass),
        )
        NotificationService.send_notification_by_auth_group(
            group_name="returnable_admin",
            title=title,
            body=body,
            notification_type=NotificationType.RETURNABLE_OVERDUE,
            click_action_url=_department_url(gate_pass),
            reference_type=REFERENCE_TYPE,
            reference_id=gate_pass.id,
            company=gate_pass.company,
        )

    _safe(send, gate_pass, "overdue")


def notify_closed(gate_pass, short_closed=False, actor=None):
    title = "Returnable gate pass short closed" if short_closed else "Returnable gate pass closed"
    body = (
        f"{gate_pass.pass_no} for {gate_pass.party_name} has been "
        + (
            f"short closed with {gate_pass.pending_return_qty} unit(s) never returned. "
            f"Reason: {gate_pass.short_close_reason}"
            if short_closed
            else "closed. All items are accounted for."
        )
    )
    _safe(
        lambda: _to_user(
            _owner(gate_pass),
            gate_pass,
            title,
            body,
            NotificationType.RETURNABLE_CLOSED,
            _department_url(gate_pass),
            actor,
        ),
        gate_pass,
        "closed",
    )


def notify_cancelled(gate_pass, actor=None):
    """Tell the gate to stop expecting this pass — otherwise it sits in their queue."""

    title = "Returnable gate pass cancelled"
    body = f"{gate_pass.pass_no} for {gate_pass.party_name} was cancelled. Reason: {gate_pass.cancel_reason}"

    def send():
        _by_permission(
            GATE_OUT_PERM,
            gate_pass,
            title,
            body,
            NotificationType.RETURNABLE_CANCELLED,
            _gate_out_url(gate_pass),
            actor,
        )
        _by_permission(
            GATE_IN_PERM,
            gate_pass,
            title,
            body,
            NotificationType.RETURNABLE_CANCELLED,
            _gate_in_url(gate_pass),
            actor,
        )

    _safe(send, gate_pass, "cancelled")
