"""
Two rights, one per page: putting an expense in, and approving one.
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
    message = "You are not an expense approver."

    def has_permission(self, request, view):
        return _has(request.user, APPROVE_PERMISSION)
