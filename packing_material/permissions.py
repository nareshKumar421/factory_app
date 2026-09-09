"""
packing_material/permissions.py

Who may read the packing-material board.

Either of two rights opens it:

``packing_material.can_view_packing_material``
    The dedicated right. Declared by this app's sentinel model, and the one to
    grant the day packaging stock and spend need restricting independently of
    the production reports. It does NOT exist in a database until
    ``manage.py sync_packing_material_permission`` has been run there.

``production_execution.can_view_reports``
    The production reports right, which already exists and is already granted.
    Accepted because this board IS a production report -- it reads goods issues
    against bills of material -- and because requiring the dedicated right
    first would leave the dashboard invisible on an installation where nobody
    had run the sync command yet.

Accepting either is what keeps the front end and the API agreed. The sidebar
gates on the production reports right, so if this class demanded only the
dedicated one, the menu entry would appear and every request behind it would
come back 403 -- a visible, broken board, which is worse than a hidden one.
"""

from rest_framework.permissions import BasePermission

PACKING_MATERIAL_VIEW_PERMISSIONS = (
    "packing_material.can_view_packing_material",
    "production_execution.can_view_reports",
)


class CanViewPackingMaterial(BasePermission):
    """Permission to view the Packing Material dashboard."""

    message = (
        "You need the Packing Material or the production reports permission to "
        "read this dashboard."
    )

    def has_permission(self, request, view):
        user = request.user
        return any(user.has_perm(perm) for perm in PACKING_MATERIAL_VIEW_PERMISSIONS)
