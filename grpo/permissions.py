# grpo/permissions.py
"""
Permission-based access control for GRPO module.
Uses Django's built-in permission system instead of role-based access.
Default Django permissions (add/view/change/delete) are used for standard CRUD.
"""

from rest_framework.permissions import BasePermission


# GRPO Posting Permissions (using Django defaults for view/create)

class CanViewGRPOPosting(BasePermission):
    """Permission to view GRPO postings."""

    def has_permission(self, request, view):
        return request.user.has_perm("grpo.view_grpoposting")


class CanCreateGRPOPosting(BasePermission):
    """Permission to create/post GRPO to SAP."""

    def has_permission(self, request, view):
        return request.user.has_perm("grpo.add_grpoposting")


class CanViewPendingGRPO(BasePermission):
    """Permission to view pending GRPO entries."""

    def has_permission(self, request, view):
        return request.user.has_perm("grpo.can_view_pending_grpo")


class CanPreviewGRPO(BasePermission):
    """Permission to preview GRPO data before posting."""

    def has_permission(self, request, view):
        return request.user.has_perm("grpo.can_preview_grpo")


class CanViewGRPOHistory(BasePermission):
    """Permission to view GRPO posting history."""

    def has_permission(self, request, view):
        return request.user.has_perm("grpo.can_view_grpo_history")


class CanPrintPurchaseOrder(BasePermission):
    """Permission to print the SAP purchase order behind a gate entry.

    Any of the GRPO read permissions will do. The button sits on four screens
    guarded by three different permissions — the pending and all-entries lists,
    the posting preview and the posting history — and printing an order whose
    number the operator is already looking at is not a capability beyond seeing
    it listed. One permission of its own would mean a new group row before any
    of those buttons worked.
    """

    PERMISSIONS = (
        "grpo.can_view_pending_grpo",
        "grpo.can_preview_grpo",
        "grpo.can_view_grpo_history",
    )

    def has_permission(self, request, view):
        return any(request.user.has_perm(perm) for perm in self.PERMISSIONS)


class CanManageGRPOAttachments(BasePermission):
    """Permission to upload/manage GRPO attachments."""

    def has_permission(self, request, view):
        return request.user.has_perm("grpo.add_grpoattachment")


class CanManagePOPrintSettings(BasePermission):
    """Permission to change how the printed Purchase Order names its approver."""

    def has_permission(self, request, view):
        return request.user.has_perm("grpo.can_manage_po_print_settings")


class CanEditGRPOInSap(BasePermission):
    """Either right to change a posted GRPO in SAP: material or service.

    Opening the edit screen needs one of them; the save then checks the one for
    the GRPO's own type (``grpo.sap_edit.PERMISSION_BY_TYPE``).
    """

    PERMISSIONS = (
        "grpo.can_edit_grpo_in_sap",
        "grpo.can_edit_service_grpo_in_sap",
    )

    def has_permission(self, request, view):
        return any(request.user.has_perm(perm) for perm in self.PERMISSIONS)
