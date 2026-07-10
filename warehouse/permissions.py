"""
Permission classes for the Warehouse module (BOM requests + FG receipts).

Dedicated `warehouse.*` permissions so warehouse-store users can be granted
BOM/FG access without borrowing production_execution permissions (which would
also expose the whole production module).
"""

from rest_framework.permissions import BasePermission


class CanViewBOMRequest(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm("warehouse.can_view_bom_request")


class CanCreateBOMRequest(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm("warehouse.can_create_bom_request")


class CanApproveBOMRequest(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm("warehouse.can_approve_bom_request")


class CanIssueMaterials(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm("warehouse.can_issue_materials")


class CanViewFGReceipt(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm("warehouse.can_view_fg_receipt")


class CanCreateFGReceipt(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm("warehouse.can_create_fg_receipt")


class CanReceiveFG(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm("warehouse.can_receive_fg")


class CanPostFGToSAP(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm("warehouse.can_post_fg_to_sap")
