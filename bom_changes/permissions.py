"""
Access control for BOM Changes.

SAP Portal gated BOM work on a role (``backend_v1/server.js`` ``LEVEL_ROLES`` and
``canApproveAtStatus``, lines 26 and 41-49); here each role is a right:

* ``manager`` → ``can_approve_bom_level_1``
* ``sr_manager`` → ``can_approve_bom_level_2``
* ``sap_adder`` → ``can_push_bom_to_sap`` (the last sign-off, which writes SAP)
* ``admin`` → ``can_push_bom_directly`` (create or change a BOM in SAP at once)

Which right a status needs depends on ``settings.BOM_CHANGE_APPROVAL_LEVELS``;
that mapping is ``workflow.right_for``. The classes here gate the endpoints; the
status-specific check is made on the locked row in ``services``.

Every right implies viewing: nobody should be asked to approve, push or ask for
what they cannot read. In the portal any login could raise a request; here it
is ``can_request_bom_changes``.
"""

from rest_framework.permissions import BasePermission

VIEW_PERMISSION = "bom_changes.can_view_bom_changes"
REQUEST_PERMISSION = "bom_changes.can_request_bom_changes"
LEVEL_1_PERMISSION = "bom_changes.can_approve_bom_level_1"
LEVEL_2_PERMISSION = "bom_changes.can_approve_bom_level_2"
PUSH_PERMISSION = "bom_changes.can_push_bom_to_sap"
DIRECT_PERMISSION = "bom_changes.can_push_bom_directly"

APPROVER_PERMISSIONS = (LEVEL_1_PERMISSION, LEVEL_2_PERMISSION, PUSH_PERMISSION)
ALL_PERMISSIONS = (VIEW_PERMISSION, REQUEST_PERMISSION, *APPROVER_PERMISSIONS, DIRECT_PERMISSION)


def _authenticated(request):
    user = request.user
    return bool(user and user.is_authenticated)


def holds_any(user, permissions) -> bool:
    return any(user.has_perm(permission) for permission in permissions)


class CanViewBomChanges(BasePermission):
    """Requests, their history and the SAP BOM viewer. Any BOM right implies it."""

    message = "You do not have permission to view BOM changes."

    def has_permission(self, request, view):
        return _authenticated(request) and holds_any(request.user, ALL_PERMISSIONS)


class CanRequestBomChanges(BasePermission):
    """Raise a request for a new BOM or a change to one."""

    message = "You do not have permission to request BOM changes."

    def has_permission(self, request, view):
        return _authenticated(request) and request.user.has_perm(REQUEST_PERMISSION)


class CanDecideBomChanges(BasePermission):
    """Reach the approve / reject endpoints. Whether the caller may act at the
    request's current status is decided on the row (``services``)."""

    message = "You do not have permission to approve BOM changes."

    def has_permission(self, request, view):
        return _authenticated(request) and holds_any(request.user, APPROVER_PERMISSIONS)


class CanPushBomDirectly(BasePermission):
    """Write a new or changed BOM to SAP at once, skipping the approval levels."""

    message = "You do not have permission to write BOMs to SAP without approval."

    def has_permission(self, request, view):
        return _authenticated(request) and request.user.has_perm(DIRECT_PERMISSION)
