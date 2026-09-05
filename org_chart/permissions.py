"""
DRF permissions for the ownership chart.

Reading is separate from editing on purpose: the chart is meant to be looked up
by anyone who needs to know whom to ask, while changing who owns a function is
an HR-level edit.
"""

from rest_framework.permissions import BasePermission

WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

VIEW_PERMISSION = "org_chart.can_view_org_chart"
MANAGE_PERMISSION = "org_chart.can_manage_org_chart"


class OrgChartPermission(BasePermission):
    """Read needs view OR manage; writing needs manage."""

    def has_permission(self, request, view):
        user = request.user
        if not user or not user.is_authenticated:
            return False
        if request.method in WRITE_METHODS:
            return user.has_perm(MANAGE_PERMISSION)
        return user.has_perm(VIEW_PERMISSION) or user.has_perm(MANAGE_PERMISSION)
