"""
Two pushes: one to every expense approver when a claim is waiting, one back to
the submitter when it is decided.

Best-effort, like every other module's: a push that fails is logged and
swallowed, because the transition it describes has already committed.
"""

import logging

from notifications.models import NotificationType
from notifications.services import NotificationService

from .constants import APPROVE_PERMISSION

logger = logging.getLogger(__name__)

REFERENCE_TYPE = "expense_claim"
APPROVE_CODENAME = APPROVE_PERMISSION.split(".", 1)[1]

APPROVAL_URL = "/accounts/expense-approval"
ENTRY_URL = "/accounts/expense-entry"


def _who(user):
    if user is None:
        return "Somebody"
    return user.full_name or user.email


def _send(user, claim, title, body, ntype, url, actor):
    if user is None:
        return
    try:
        NotificationService.send_notification_to_user(
            user=user,
            title=title,
            body=body,
            notification_type=ntype,
            click_action_url=url,
            reference_type=REFERENCE_TYPE,
            reference_id=claim.id,
            company=claim.company,
            created_by=actor,
        )
    except Exception as exc:  # a push never undoes the decision it reports
        logger.error(
            "[Expense claims] Could not notify %s about claim %s: %s",
            user.pk,
            claim.id,
            exc,
            exc_info=True,
        )


def waiting(claim, *, actor):
    """Tell every expense approver that an expense is waiting for them."""
    try:
        NotificationService.send_notification_by_permission(
            permission_codename=APPROVE_CODENAME,
            title="Expense waiting for approval",
            body=f"{_who(claim.created_by)}: \u20b9{claim.amount} -- {claim.comment[:120]}",
            notification_type=NotificationType.EXPENSE_CLAIM_SENT,
            click_action_url=APPROVAL_URL,
            reference_type=REFERENCE_TYPE,
            reference_id=claim.id,
            # Expenses are common to every company, so is the approver list.
            company=None,
            created_by=actor,
        )
    except Exception as exc:  # a push never undoes the expense it reports
        logger.error(
            "[Expense claims] Could not notify approvers about claim %s: %s",
            claim.id,
            exc,
            exc_info=True,
        )


def decided(claim, *, actor):
    verdict = "approved" if claim.status == "APPROVED" else "rejected"
    body = f"₹{claim.amount} -- {claim.comment[:120]}"
    if claim.decision_note:
        body += f" ({claim.decision_note[:120]})"
    _send(
        claim.created_by,
        claim,
        title=f"Your expense was {verdict} by {_who(actor)}",
        body=body,
        ntype=NotificationType.EXPENSE_CLAIM_DECIDED,
        url=ENTRY_URL,
        actor=actor,
    )
