"""
Access control for Request Labour.

Three separate rights, because three different people are involved: anyone who
plans around tomorrow's headcount may *see* the board, a department head *raises*
its own ask, and whoever is accountable for the labour bill *decides* it. None
of the three implies another.
"""

from rest_framework.permissions import BasePermission

VIEW_PERMISSION = "labour_request.can_view_labour_request"
RAISE_PERMISSION = "labour_request.can_raise_labour_request"
DECIDE_PERMISSION = "labour_request.can_decide_labour_request"


class CanViewLabourRequest(BasePermission):
    """Open the board. Raising or deciding implies being able to read it."""

    def has_permission(self, request, view):
        user = request.user
        return bool(user and user.is_authenticated) and (
            user.has_perm(VIEW_PERMISSION)
            or user.has_perm(RAISE_PERMISSION)
            or user.has_perm(DECIDE_PERMISSION)
        )


class CanRaiseLabourRequest(BasePermission):
    """Raise, edit, delete or restore a department's request."""

    def has_permission(self, request, view):
        user = request.user
        return bool(user and user.is_authenticated) and user.has_perm(RAISE_PERMISSION)


class CanDecideLabourRequest(BasePermission):
    """Approve, reject or reopen a request."""

    def has_permission(self, request, view):
        user = request.user
        return bool(user and user.is_authenticated) and user.has_perm(DECIDE_PERMISSION)
