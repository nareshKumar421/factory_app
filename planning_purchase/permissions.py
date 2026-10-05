"""Permission classes for the Planning & Purchase module.

Create, approve and post are three separate permissions on purpose: raising a
purchase order and committing it to a supplier must not be the same person's
click.
"""

from rest_framework.permissions import BasePermission


class CanViewProductionPlan(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm("planning_purchase.can_view_production_plan")


class CanCreatePurchaseOrder(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm("planning_purchase.can_create_purchase_order")


class CanApprovePurchaseOrder(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm("planning_purchase.can_approve_purchase_order")


class CanPostPurchaseOrderToSAP(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm(
            "planning_purchase.can_post_purchase_order_to_sap"
        )


def _any(request, *rights) -> bool:
    user = request.user
    return bool(user and user.is_authenticated) and any(user.has_perm(r) for r in rights)


# EXIM's Open POs and monthly plan moved here; their users hold EXIM's own
# rights (under the ``exim`` label), which open the same screens here.

class CanViewOpenPOs(BasePermission):
    message = "You do not have permission to view open purchase orders."

    def has_permission(self, request, view):
        return _any(request, "planning_purchase.can_view_open_pos", "exim.view_open_pos")


class CanViewMonthlyPlan(BasePermission):
    message = "You do not have permission to view the monthly plan."

    def has_permission(self, request, view):
        return _any(
            request,
            "planning_purchase.can_view_production_plan",
            "planning_purchase.can_upload_monthly_plan",
            "exim.view_planningupload",
        )


class CanUploadMonthlyPlan(BasePermission):
    message = "You do not have permission to upload the monthly plan."

    def has_permission(self, request, view):
        return _any(request, "planning_purchase.can_upload_monthly_plan", "exim.add_planningupload")


class CanRemoveMonthlyPlan(BasePermission):
    message = "You do not have permission to remove a monthly plan."

    def has_permission(self, request, view):
        return _any(request, "planning_purchase.can_upload_monthly_plan", "exim.delete_planningupload")
