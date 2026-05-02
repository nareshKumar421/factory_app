# quality_control/permissions.py
"""
Permission-based access control for Quality Control module.
Uses Django's built-in permission system instead of role-based access.
Default Django permissions (add/view/change/delete) are used for standard CRUD.
"""

from rest_framework.permissions import BasePermission


class CanCreateArrivalSlip(BasePermission):
    """Permission to create material arrival slip."""

    def has_permission(self, request, view):
        return request.user.has_perm("quality_control.add_materialarrivalslip")


class CanEditArrivalSlip(BasePermission):
    """Permission to edit material arrival slip."""

    def has_permission(self, request, view):
        return request.user.has_perm("quality_control.change_materialarrivalslip")


class CanSubmitArrivalSlip(BasePermission):
    """Permission to submit arrival slip to QA."""

    def has_permission(self, request, view):
        return request.user.has_perm("quality_control.can_submit_arrival_slip")


class CanViewArrivalSlip(BasePermission):
    """Permission to view material arrival slip."""

    def has_permission(self, request, view):
        return request.user.has_perm("quality_control.view_materialarrivalslip")


class CanCreateInspection(BasePermission):
    """Permission to create raw material inspection."""

    def has_permission(self, request, view):
        return request.user.has_perm("quality_control.add_rawmaterialinspection")


class CanEditInspection(BasePermission):
    """Permission to edit raw material inspection."""

    def has_permission(self, request, view):
        return request.user.has_perm("quality_control.change_rawmaterialinspection")


class CanSubmitInspection(BasePermission):
    """Permission to submit inspection for approval."""

    def has_permission(self, request, view):
        return request.user.has_perm("quality_control.can_submit_inspection")


class CanViewInspection(BasePermission):
    """Permission to view raw material inspection."""

    def has_permission(self, request, view):
        return request.user.has_perm("quality_control.view_rawmaterialinspection")


class CanApproveAsChemist(BasePermission):
    """Permission to approve inspection as QA Chemist."""

    def has_permission(self, request, view):
        return request.user.has_perm("quality_control.can_approve_as_chemist")


class CanApproveAsQAM(BasePermission):
    """Permission to approve inspection as QA Manager."""

    def has_permission(self, request, view):
        return request.user.has_perm("quality_control.can_approve_as_qam")


class CanSendBackArrivalSlip(BasePermission):
    """Permission to send arrival slip back to gate for correction."""

    def has_permission(self, request, view):
        return request.user.has_perm("quality_control.can_send_back_arrival_slip")


class CanRejectInspection(BasePermission):
    """Permission to reject inspection."""

    def has_permission(self, request, view):
        return request.user.has_perm("quality_control.can_reject_inspection")


class CanFactoryHeadDecision(BasePermission):
    """Permission to record factory head decision after QA rejection."""

    def has_permission(self, request, view):
        return (
            request.user.has_perm("quality_control.can_factory_head_decision") or
            request.user.has_perm("quality_control.change_rawmaterialinspection")
        )


class CanManageMaterialTypes(BasePermission):
    """Permission to manage material types."""

    def has_permission(self, request, view):
        return request.user.has_perm("quality_control.can_manage_material_types")


class CanManageQCParameters(BasePermission):
    """Permission to manage QC parameters."""

    def has_permission(self, request, view):
        return request.user.has_perm("quality_control.can_manage_qc_parameters")


# Combined permission classes for common use cases

class CanManageArrivalSlip(BasePermission):
    """Permission to create or edit arrival slip."""

    def has_permission(self, request, view):
        if request.method in ["POST", "PUT", "PATCH"]:
            return (
                request.user.has_perm("quality_control.add_materialarrivalslip") or
                request.user.has_perm("quality_control.change_materialarrivalslip")
            )
        return request.user.has_perm("quality_control.view_materialarrivalslip")


class CanManageInspection(BasePermission):
    """Permission to create or edit inspection."""

    def has_permission(self, request, view):
        if request.method in ["POST", "PUT", "PATCH"]:
            return (
                request.user.has_perm("quality_control.add_rawmaterialinspection") or
                request.user.has_perm("quality_control.change_rawmaterialinspection")
            )
        return request.user.has_perm("quality_control.view_rawmaterialinspection")


# ==================== Production QC Permissions ====================

class CanViewProductionQC(BasePermission):
    """Permission to view production QC sessions."""

    def has_permission(self, request, view):
        return request.user.has_perm("quality_control.can_view_production_qc")


class CanCreateProductionQC(BasePermission):
    """Permission to create production QC sessions."""

    def has_permission(self, request, view):
        return request.user.has_perm("quality_control.can_create_production_qc")


class CanSubmitProductionQC(BasePermission):
    """Permission to submit production QC for approval."""

    def has_permission(self, request, view):
        return request.user.has_perm("quality_control.can_submit_production_qc")


class CanApproveProductionQC(BasePermission):
    """Permission to approve/reject production QC sessions."""

    def has_permission(self, request, view):
        return request.user.has_perm("quality_control.can_approve_production_qc")
