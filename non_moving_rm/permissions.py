"""
non_moving_rm/permissions.py

Permission-based access control for Non-Moving RM Dashboard module.
"""

from rest_framework.permissions import BasePermission


class CanViewNonMovingRM(BasePermission):
    """Permission to view the Non-Moving RM Dashboard."""

    def has_permission(self, request, view):
        return request.user.has_perm("non_moving_rm.can_view_non_moving_rm")
