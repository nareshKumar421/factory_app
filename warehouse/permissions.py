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


# --- Warehouse transfer requests -------------------------------------------
# Approval is separated from raising deliberately: the point of the flow is that
# the receiving warehouse decides, so the two must be grantable independently.

class CanViewTransferRequest(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm("warehouse.can_view_transfer_request")


class CanCreateTransferRequest(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm("warehouse.can_create_transfer_request")


class CanApproveTransferRequest(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm("warehouse.can_approve_transfer_request")


class CanPostTransferToSAP(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm("warehouse.can_post_transfer_to_sap")


# --- Warehouse managers (per-user warehouse scoping) ------------------------
# Separate from the movement permissions on purpose: deciding WHO runs a
# warehouse is an administrator's job, not something a warehouse manager should
# be able to grant themselves.

class CanManageUserWarehouses(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm("warehouse.can_manage_user_warehouses")


# --- Raw-material stock register -------------------------------------------
# Viewing is separate from setting because the register is read by production
# planning and supervisors, while only the store keeper of a warehouse states
# what is on its floor. Holding `can_set_rm_stock` is necessary but not
# sufficient: the service also insists the user manages that warehouse (see
# services/warehouse_scope).

class CanViewRMStock(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm("warehouse.can_view_rm_stock")


class CanSetRMStock(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm("warehouse.can_set_rm_stock")


# --- Barcode receiving (godown gate) ---------------------------------------
# Deliberately a warehouse permission, not a barcode one: the person who stands
# at the godown gate is warehouse staff, and the real restriction is the
# UserWarehouse assignment enforced alongside it (see services/warehouse_scope).

class CanReceiveBarcodes(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm("warehouse.can_receive_barcodes")


# --- Godown outward-movement register --------------------------------------
# Same split, same reason as the raw-material register above: the dashboard
# this feeds is read widely, while only the keeper of a floor declares what is
# leaving it. `can_record_pf_movement` is necessary but not sufficient — the
# service also insists the user manages the source warehouse.

class CanViewPFMovement(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm("warehouse.can_view_pf_movement")


class CanRecordPFMovement(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm("warehouse.can_record_pf_movement")
