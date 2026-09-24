"""
Telling the right person that something needs them, or happened to them.

Two messages: the approver learns a request is waiting, the applicant learns
what was decided. Both go through the existing
:class:`notifications.services.NotificationService`, so they land in the bell
menu and on a device without this module knowing anything about FCM.

**Everything here is best effort, and that is deliberate.** A push that fails
must never roll back the decision it describes -- an approval that worked and a
notification that did not is a far better outcome than an approval undone
because Google was unreachable. So every function swallows its exceptions and
logs them, and none of them is called inside the transaction that moves the
status.

**Both messages can find nobody, and that is normal, not an error.** Roughly
half the workforce has no login at all, and the approver the reporting tree
names may be one of them. A request raised for somebody with no login simply
has nobody to notify; the queue still shows it to whoever can act on it.
"""

import logging

from notifications.models import NotificationType
from notifications.services import NotificationService

from .routing import responsible_manager

logger = logging.getLogger(__name__)


def _login_for(employee):
    """The app login an employee is, or ``None`` if they have none."""
    return getattr(employee, "user", None) if employee is not None else None


def notify_approver(request):
    """Tell the person the tree says should decide this that it is waiting.

    Falls silent when the responsible manager has no login -- see the module
    docstring. Never raises.
    """
    try:
        manager = responsible_manager(request.employee)
        recipient = _login_for(manager)
        if recipient is None:
            logger.info(
                "leave: no login to notify for request %s (approver=%s)",
                request.pk,
                manager.employee_code if manager else None,
            )
            return None

        dates = (
            str(request.from_date)
            if request.from_date == request.to_date
            else f"{request.from_date} to {request.to_date}"
        )
        return NotificationService.send_notification_to_user(
            user=recipient,
            title="Leave request waiting",
            body=(
                f"{request.employee.full_name} has applied for "
                f"{request.leave_type.name} ({dates}, {request.total_days} day(s))."
            ),
            notification_type=NotificationType.LEAVE_REQUESTED,
            click_action_url="/organization/leave/approvals",
            reference_type="leave_request",
            reference_id=request.pk,
            company=request.company,
            created_by=request.applied_by,
        )
    except Exception:  # pragma: no cover - defensive, see module docstring
        logger.exception("leave: failed to notify the approver for request %s", request.pk)
        return None


def notify_applicant(request, *, decided_by=None):
    """Tell whoever applied what was decided.

    Prefers the employee's own login and falls back to the login that submitted
    the form -- so when the time office raises a request for somebody with no
    login, the time office is the one told the answer.
    """
    try:
        recipient = _login_for(request.employee) or request.applied_by
        if recipient is None:
            return None

        verdict = request.get_status_display()
        note = request.decision_note.strip()
        return NotificationService.send_notification_to_user(
            user=recipient,
            title=f"Leave {verdict.lower()}",
            body=(
                f"{request.leave_type.name} {request.from_date} to {request.to_date}: "
                f"{verdict}." + (f" {note}" if note else "")
            ),
            notification_type=NotificationType.LEAVE_DECIDED,
            click_action_url="/organization/leave",
            reference_type="leave_request",
            reference_id=request.pk,
            company=request.company,
            created_by=decided_by,
        )
    except Exception:  # pragma: no cover - defensive, see module docstring
        logger.exception("leave: failed to notify the applicant for request %s", request.pk)
        return None
