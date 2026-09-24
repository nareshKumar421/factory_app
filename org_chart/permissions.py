"""
DRF permissions for the ownership chart.

Reading is separate from editing on purpose: the chart is meant to be looked up
by anyone who needs to know whom to ask, while changing who owns a function is
an HR-level edit.

So reading is open to every signed-in user, with no grant at all. It holds names
and who owns which function, nothing personal and no pay. ``can_view_org_chart``
is still defined and still sits in the groups. It no longer decides anything,
but removing it would mean a migration and group edits for no change in access.
"""

from rest_framework.permissions import BasePermission

WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

VIEW_PERMISSION = "org_chart.can_view_org_chart"
MANAGE_PERMISSION = "org_chart.can_manage_org_chart"


class OrgChartPermission(BasePermission):
    """Any signed-in user may read; writing needs manage."""

    def has_permission(self, request, view):
        user = request.user
        if not user or not user.is_authenticated:
            return False
        if request.method in WRITE_METHODS:
            return user.has_perm(MANAGE_PERMISSION)
        return True
