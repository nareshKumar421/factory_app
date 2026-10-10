"""Who may do what with production order entries.

Each SAP step is its own right, so the work can be split between people
without code changes. Today one person holds all of them for their kind of
order.

* ``can_view_production_orders`` — read entries. Every other right implies it.
* ``can_create_production_orders`` — the Plan step: save an entry, create its
  planned order in SAP.
* ``can_release_production_orders`` — release the order.
* ``can_issue_production_orders`` — issue the order's materials.
* ``can_receive_production_orders`` — receive the finished goods.
* ``can_close_production_orders`` — close the order.

Holding a right is not enough to post: the person also needs a SAP account
mapped to them (SAP Identities) with its password on the server, because SAP
records, and its own rules check, who posted.
"""

from rest_framework.permissions import BasePermission

from .models import Step

VIEW_PERMISSION = "production_orders.can_view_production_orders"
CREATE_PERMISSION = "production_orders.can_create_production_orders"
RELEASE_PERMISSION = "production_orders.can_release_production_orders"
ISSUE_PERMISSION = "production_orders.can_issue_production_orders"
RECEIVE_PERMISSION = "production_orders.can_receive_production_orders"
CLOSE_PERMISSION = "production_orders.can_close_production_orders"

STEP_PERMISSIONS = {
    Step.PLAN: CREATE_PERMISSION,
    Step.RELEASE: RELEASE_PERMISSION,
    Step.ISSUE: ISSUE_PERMISSION,
    Step.RECEIPT: RECEIVE_PERMISSION,
    Step.CLOSE: CLOSE_PERMISSION,
}
ALL_PERMISSIONS = (VIEW_PERMISSION, *STEP_PERMISSIONS.values())


def _authenticated(request):
    user = request.user
    return bool(user and user.is_authenticated)


def can_take(user, step) -> bool:
    return bool(user and user.is_authenticated and user.has_perm(STEP_PERMISSIONS[Step(step)]))


class CanViewProductionOrders(BasePermission):
    message = "You do not have permission to view production order entries."

    def has_permission(self, request, view):
        return _authenticated(request) and any(request.user.has_perm(p) for p in ALL_PERMISSIONS)


class CanCreateProductionOrders(BasePermission):
    message = "You do not have permission to enter production orders."

    def has_permission(self, request, view):
        return _authenticated(request) and request.user.has_perm(CREATE_PERMISSION)
