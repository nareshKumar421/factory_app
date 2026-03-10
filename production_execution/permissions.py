from rest_framework.permissions import BasePermission


# Master Data
class CanManageProductionLines(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm('production_execution.can_manage_production_lines')


class CanManageMachines(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm('production_execution.can_manage_machines')


class CanManageChecklistTemplates(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm('production_execution.can_manage_checklist_templates')


# Production Runs
class CanViewProductionRun(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm('production_execution.can_view_production_run')


class CanCreateProductionRun(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm('production_execution.can_create_production_run')


class CanEditProductionRun(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm('production_execution.can_edit_production_run')


class CanCompleteProductionRun(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm('production_execution.can_complete_production_run')


# Hourly Logs
class CanViewProductionLog(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm('production_execution.can_view_production_log')


class CanEditProductionLog(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm('production_execution.can_edit_production_log')


# Breakdowns
class CanViewBreakdown(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm('production_execution.can_view_breakdown')


class CanCreateBreakdown(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm('production_execution.can_create_breakdown')


class CanEditBreakdown(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm('production_execution.can_edit_breakdown')


# Material Usage
class CanViewMaterialUsage(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm('production_execution.can_view_material_usage')


class CanCreateMaterialUsage(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm('production_execution.can_create_material_usage')


class CanEditMaterialUsage(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm('production_execution.can_edit_material_usage')


# Machine Runtime
class CanViewMachineRuntime(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm('production_execution.can_view_machine_runtime')


class CanCreateMachineRuntime(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm('production_execution.can_create_machine_runtime')


# Manpower
class CanViewManpower(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm('production_execution.can_view_manpower')


class CanCreateManpower(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm('production_execution.can_create_manpower')


# Line Clearance
class CanViewLineClearance(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm('production_execution.can_view_line_clearance')


class CanCreateLineClearance(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm('production_execution.can_create_line_clearance')


class CanApproveLineClearanceQA(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm('production_execution.can_approve_line_clearance_qa')


# Machine Checklists
class CanViewMachineChecklist(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm('production_execution.can_view_machine_checklist')


class CanCreateMachineChecklist(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm('production_execution.can_create_machine_checklist')


# Waste Management
class CanViewWasteLog(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm('production_execution.can_view_waste_log')


class CanCreateWasteLog(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm('production_execution.can_create_waste_log')


class CanApproveWasteEngineer(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm('production_execution.can_approve_waste_engineer')


class CanApproveWasteAM(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm('production_execution.can_approve_waste_am')


class CanApproveWasteStore(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm('production_execution.can_approve_waste_store')


class CanApproveWasteHOD(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm('production_execution.can_approve_waste_hod')


# Reports
class CanViewReports(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm('production_execution.can_view_reports')
