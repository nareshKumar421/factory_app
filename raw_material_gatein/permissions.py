# raw_material_gatein/permissions.py
"""
Permission-based access control for Raw Material Gate Entry module.
Uses Django's built-in permission system instead of role-based access.
Default Django permissions (add/view/change/delete) are used for standard CRUD.
"""

from rest_framework.permissions import BasePermission


# Raw Material Gate Entry Permissions (using Django defaults for CRUD)

class CanCreateRawMaterialEntry(BasePermission):
    """Permission to create new raw material gate entry."""

    def has_permission(self, request, view):
        return request.user.has_perm("raw_material_gatein.add_poreceipt")


class CanViewRawMaterialEntry(BasePermission):
    """Permission to view raw material gate entries."""

    def has_permission(self, request, view):
        return request.user.has_perm("raw_material_gatein.view_poreceipt")


class CanEditRawMaterialEntry(BasePermission):
    """Permission to edit raw material gate entry."""

    def has_permission(self, request, view):
        return request.user.has_perm("raw_material_gatein.change_poreceipt")


class CanDeleteRawMaterialEntry(BasePermission):
    """Permission to delete raw material gate entry."""

    def has_permission(self, request, view):
        return request.user.has_perm("raw_material_gatein.delete_poreceipt")


class CanCompleteRawMaterialEntry(BasePermission):
    """Permission to complete and lock a raw material gate entry."""

    def has_permission(self, request, view):
        return request.user.has_perm("raw_material_gatein.can_complete_raw_material_entry")


# PO Receipt Permissions (using Django defaults for CRUD)

class CanReceivePO(BasePermission):
    """Permission to receive PO items against a gate entry."""

    def has_permission(self, request, view):
        return request.user.has_perm("raw_material_gatein.can_receive_po")


class CanViewPOReceipt(BasePermission):
    """Permission to view PO receipts."""

    def has_permission(self, request, view):
        return request.user.has_perm("raw_material_gatein.view_poreceipt")


class CanEditPOReceipt(BasePermission):
    """Permission to edit PO receipts."""

    def has_permission(self, request, view):
        return request.user.has_perm("raw_material_gatein.change_poreceipt")


class CanDeletePOReceipt(BasePermission):
    """Permission to delete PO receipts."""

    def has_permission(self, request, view):
        return request.user.has_perm("raw_material_gatein.delete_poreceipt")


# Combined permission classes for common use cases

class CanManageRawMaterialEntry(BasePermission):
    """Permission to manage raw material gate entries."""

    def has_permission(self, request, view):
        if request.method in ["POST"]:
            return request.user.has_perm("raw_material_gatein.add_poreceipt")
        if request.method in ["PUT", "PATCH"]:
            return request.user.has_perm("raw_material_gatein.change_poreceipt")
        if request.method in ["DELETE"]:
            return request.user.has_perm("raw_material_gatein.delete_poreceipt")
        return request.user.has_perm("raw_material_gatein.view_poreceipt")


class CanManagePOReceipt(BasePermission):
    """Permission to manage PO receipts."""

    def has_permission(self, request, view):
        if request.method in ["POST"]:
            return request.user.has_perm("raw_material_gatein.can_receive_po")
        if request.method in ["PUT", "PATCH"]:
            return request.user.has_perm("raw_material_gatein.change_poreceipt")
        if request.method in ["DELETE"]:
            return request.user.has_perm("raw_material_gatein.delete_poreceipt")
        return request.user.has_perm("raw_material_gatein.view_poreceipt")


class CanRepointPOReceipt(BasePermission):
    """Permission to move a received PO onto a different open PO.

    A post-QC correction for a PO that ran out between gate-in and GRPO, so it
    is deliberately not part of ``can_receive_po``.
    """

    def has_permission(self, request, view):
        return request.user.has_perm("raw_material_gatein.can_repoint_po_receipt")
