from rest_framework.permissions import BasePermission


# Master Data
class CanManageProductionLines(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm('production_execution.can_manage_production_lines')


# Line configuration (the Line Management page: a line's operating profile and
# its SKU presets). Reading it is open to anyone who already sees production —
# plus ``can_view_line_config``, which grants the page and nothing else, so a
# reviewer can be given the configuration without the rest of the module.
class CanViewLineConfig(BasePermission):
    def has_permission(self, request, view):
        return (
            request.user.has_perm('production_execution.can_view_line_config') or
            request.user.has_perm('production_execution.can_view_production_run') or
            request.user.has_perm('production_execution.can_manage_production_lines')
        )


# Editing it is deliberately separate from ``can_manage_production_lines``: a
# rated speed or a manpower count feeds run planning and costing, so the write
# is held by no group (see setup_production_groups) and only superusers pass.
class CanManageLineConfig(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm('production_execution.can_manage_line_config')


# Production settings (the RM/PM/FG warehouses). Anyone who sees production may
# read them — the run screen needs the FG warehouse — but only the holder of
# ``can_manage_production_settings`` (Production HOD) may change them.
class CanViewProductionSettings(BasePermission):
    def has_permission(self, request, view):
        return (
            request.user.has_perm('production_execution.can_view_production_run') or
            request.user.has_perm('production_execution.can_manage_production_settings')
        )


class CanManageProductionSettings(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm('production_execution.can_manage_production_settings')


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
        return (
            request.user.has_perm('production_execution.can_view_line_clearance') or
            request.user.has_perm('quality_control.can_view_line_clearance_qc') or
            request.user.has_perm('quality_control.can_approve_line_clearance_qc')
        )


class CanCreateLineClearance(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm('production_execution.can_create_line_clearance')


class CanApproveLineClearanceQA(BasePermission):
    def has_permission(self, request, view):
        return (
            request.user.has_perm('production_execution.can_approve_line_clearance_qa') or
            request.user.has_perm('quality_control.can_approve_line_clearance_qc')
        )


class CanManageLineClearance(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm('production_execution.can_manage_line_clearance')


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


class CanApproveWaste(BasePermission):
    def has_permission(self, request, view):
        return any([
            request.user.has_perm('production_execution.can_approve_waste_engineer'),
            request.user.has_perm('production_execution.can_approve_waste_am'),
            request.user.has_perm('production_execution.can_approve_waste_store'),
            request.user.has_perm('production_execution.can_approve_waste_hod'),
        ])


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


# Cost / costing — a dedicated, separately-granted permission. Held by no role
# by default (not listed in setup_production_groups), so run cost is hidden
# until it is explicitly granted.
class CanViewRunCost(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm('production_execution.can_view_run_cost')


#: The filling cost sheet is Beverages'. The sheet is a Beverages practice and
#: the figures on it are Beverages' own, so the page and its API exist for that
#: company alone — a company added to the deployment later has to be named here
#: on purpose before it can keep one.
FILLING_COST_COMPANY_CODES = frozenset({'JIVO_BEVERAGES'})


class InFillingCostCompany(BasePermission):
    """The company context is one that keeps a filling cost sheet.

    Sits alongside the view/manage permissions rather than inside them: holding
    the permission and switching the ``Company-Code`` header must not be a way
    to start a second company's sheet, so the check is on every read and write.
    """
    message = 'The filling cost sheet is kept by Jivo Beverages only.'

    def has_permission(self, request, view):
        # HasCompanyContext runs first and attaches the membership; be explicit
        # anyway, so a reordering of the classes fails closed.
        user_company = getattr(request, 'company', None)
        if user_company is None:
            return False
        return user_company.company.code in FILLING_COST_COMPANY_CODES


# Filling cost sheet — the month's filling cost, entered by hand. Cost figures,
# so it follows can_view_run_cost: granted to no group by default (see
# setup_production_groups) and held only by whoever is explicitly given it.
# A run-cost holder reads the sheet too; entering one is its own permission.
class CanViewFillingCost(BasePermission):
    def has_permission(self, request, view):
        return (
            request.user.has_perm('production_execution.can_view_filling_cost') or
            request.user.has_perm('production_execution.can_manage_filling_cost') or
            request.user.has_perm('production_execution.can_view_run_cost')
        )


class CanManageFillingCost(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm('production_execution.can_manage_filling_cost')
