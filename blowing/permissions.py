from rest_framework.permissions import BasePermission


class CanViewBlowingRun(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm('blowing.can_view_blowing_run')


class CanCreateBlowingRun(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm('blowing.can_create_blowing_run')


class CanEditBlowingRun(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm('blowing.can_edit_blowing_run')


class CanCompleteBlowingRun(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm('blowing.can_complete_blowing_run')


class CanManageBlowingMachines(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm('blowing.can_manage_blowing_machines')


class CanManagePreformSpecs(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm('blowing.can_manage_preform_specs')


class CanManageBlowingRateConfig(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm('blowing.can_manage_blowing_rate_config')


class CanViewBlowingReports(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm('blowing.can_view_blowing_reports')


class CanCreateBlowingBreakdown(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm('blowing.can_create_blowing_breakdown')


class CanManageBlowingBreakdownCategories(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm('blowing.can_manage_blowing_breakdown_categories')


class CanRequestPreform(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm('blowing.can_request_preform')


class CanApprovePreformRequest(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm('blowing.can_approve_preform_request')
