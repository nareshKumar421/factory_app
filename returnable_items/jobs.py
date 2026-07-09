"""Background jobs for the Returnable Items module.

Two time-based nudges, both idempotent so they can run on a short interval:

* ``notify_due_returnables`` — the expected return date has arrived. Warns the
  gate (expect the vehicle) and the department (chase the vendor). Once per pass.
* ``flag_overdue_returnables`` — the date has passed and material is still out.
  Sets ``is_overdue`` and notifies the department, the gate and the admins. Once
  per pass.

Guarded by ``due_notified_at`` / ``overdue_notified_at`` rather than by status,
because overdue is a flag here, not a state transition.
"""

import logging

from django.utils import timezone

from . import notifications as notify
from .constants import OUTSTANDING_STATUSES, ReturnableLogAction
from .models import ReturnableGatePass

logger = logging.getLogger(__name__)


def notify_due_returnables() -> int:
    """Notify gate + department for passes whose return is due today. Returns count."""
    today = timezone.localdate()
    candidates = ReturnableGatePass.objects.filter(
        status__in=OUTSTANDING_STATUSES,
        is_active=True,
        expected_return_date=today,
        due_notified_at__isnull=True,
    ).select_related("company", "submitted_by", "created_by")

    notified = 0
    for gate_pass in candidates:
        notify.notify_due_today(gate_pass)
        gate_pass.due_notified_at = timezone.now()
        gate_pass.save(update_fields=["due_notified_at", "updated_at"])
        gate_pass.log(
            ReturnableLogAction.DUE_TODAY,
            note=f"Return due today ({gate_pass.expected_return_date:%d %b %Y}).",
        )
        notified += 1

    logger.info("[Returnable] Due-today notification sent for %s gate pass(es).", notified)
    return notified


def flag_overdue_returnables() -> int:
    """Flag passes past their expected return date and notify. Returns count."""
    today = timezone.localdate()
    candidates = ReturnableGatePass.objects.filter(
        status__in=OUTSTANDING_STATUSES,
        is_active=True,
        expected_return_date__lt=today,
        overdue_notified_at__isnull=True,
    ).select_related("company", "submitted_by", "created_by")

    flagged = 0
    for gate_pass in candidates:
        gate_pass.is_overdue = True
        gate_pass.overdue_notified_at = timezone.now()
        gate_pass.save(update_fields=["is_overdue", "overdue_notified_at", "updated_at"])
        gate_pass.log(
            ReturnableLogAction.OVERDUE_FLAGGED,
            note=f"{gate_pass.days_overdue} day(s) past the expected return date.",
        )
        notify.notify_overdue(gate_pass)
        flagged += 1

    logger.info("[Returnable] Flagged %s overdue gate pass(es).", flagged)
    return flagged


def run_returnable_checks() -> dict:
    """Both checks, in the order a day actually unfolds. Used by the scheduler."""
    return {
        "due_notified": notify_due_returnables(),
        "overdue_flagged": flag_overdue_returnables(),
    }
