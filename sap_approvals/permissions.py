"""
Access control for SAP Approvals.

One class per right. SAP Portal had a single ``sap-approvals`` module that let
anyone mapped to a SAP user approve, reject and withdraw; here the three are
separate rights, so a person who only raises documents can withdraw their own
without being handed the approve button. Deciding and withdrawing each imply
viewing: nobody should act on a list they cannot read.

None of these is sufficient on its own. SAP still takes a decision only from
the authorizer it named on the request's current stage, and a withdrawal only
from the request's originator — the views check the caller IS that SAP user
(``SapApproverIdentity``) before anything reaches SAP.
"""

from rest_framework.permissions import BasePermission

VIEW_PERMISSION = "sap_approvals.can_view_sap_approval_inbox"
DECIDE_PERMISSION = "sap_approvals.can_decide_sap_approvals"
WITHDRAW_PERMISSION = "sap_approvals.can_withdraw_own_sap_approvals"
HISTORY_PERMISSION = "sap_approvals.can_view_sap_rejection_history"


def _authenticated(request):
    user = request.user
    return bool(user and user.is_authenticated)


class CanViewSapApprovalInbox(BasePermission):
    """The inbox, one request, the badge. Deciding or withdrawing implies it."""

    message = "You do not have permission to view SAP approvals."

    def has_permission(self, request, view):
        user = request.user
        return _authenticated(request) and (
            user.has_perm(VIEW_PERMISSION)
            or user.has_perm(DECIDE_PERMISSION)
            or user.has_perm(WITHDRAW_PERMISSION)
        )


class CanDecideSapApprovals(BasePermission):
    """Approve or reject, signed as the caller's own SAP account."""

    message = "You do not have permission to approve or reject SAP approval requests."

    def has_permission(self, request, view):
        return _authenticated(request) and request.user.has_perm(DECIDE_PERMISSION)


class CanWithdrawOwnSapApprovals(BasePermission):
    """Withdraw a pending request the caller raised in SAP."""

    message = "You do not have permission to withdraw SAP approval requests."

    def has_permission(self, request, view):
        return _authenticated(request) and request.user.has_perm(WITHDRAW_PERMISSION)


class CanViewSapRejectionHistory(BasePermission):
    """Every rejection in the company and who raised it — not only the caller's.

    A right of its own: the inbox shows a person what involves them, this shows
    everybody's mistakes side by side, which is for whoever reviews the desk.
    """

    message = "You do not have permission to view the SAP rejection history."

    def has_permission(self, request, view):
        return _authenticated(request) and request.user.has_perm(HISTORY_PERMISSION)
