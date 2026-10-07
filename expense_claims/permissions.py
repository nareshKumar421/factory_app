"""
Putting an expense in is this app's own right. Deciding one needs no right --
only to be the person it was sent to. Seeing every expense is the cash book
approvers'.
"""

from rest_framework.permissions import BasePermission

from .constants import APPROVE_PERMISSION, SUBMIT_PERMISSION


def _has(user, code) -> bool:
    return bool(user and user.is_authenticated and user.has_perm(code))


class CanSubmitExpenseClaim(BasePermission):
    message = "You are not allowed to put in expenses."

    def has_permission(self, request, view):
        return _has(request.user, SUBMIT_PERMISSION)


class CanApproveExpenseClaims(BasePermission):
    message = "Only a cash book approver can see every expense."

    def has_permission(self, request, view):
        return _has(request.user, APPROVE_PERMISSION)
