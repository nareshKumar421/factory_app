# labour_count/permissions.py
"""
Permission-based access control for the Labour Count module.
"""
from rest_framework.permissions import BasePermission


class CanViewLabourCount(BasePermission):
    """Permission to view labour count sheets (department + gate)."""

    def has_permission(self, request, view):
        return request.user.has_perm("labour_count.view_labourcountsheet")


class CanSubmitLabourCount(BasePermission):
    """Permission for a department supervisor to enter/submit labour counts."""

    def has_permission(self, request, view):
        return request.user.has_perm("labour_count.can_submit_labour_count")


class CanVerifyLabourCount(BasePermission):
    """Permission for a gate operator to verify (OK) submitted counts."""

    def has_permission(self, request, view):
        return request.user.has_perm("labour_count.can_verify_labour_count")
