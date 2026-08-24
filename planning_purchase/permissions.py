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
