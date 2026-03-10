from rest_framework.permissions import BasePermission


class CanCreateProductionPlan(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm('production_planning.can_create_production_plan')


class CanEditProductionPlan(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm('production_planning.can_edit_production_plan')


class CanDeleteProductionPlan(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm('production_planning.can_delete_production_plan')


class CanPostPlanToSAP(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm('production_planning.can_post_plan_to_sap')


class CanViewProductionPlan(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm('production_planning.can_view_production_plan')


class CanManageWeeklyPlan(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm('production_planning.can_manage_weekly_plan')


class CanAddDailyProduction(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm('production_planning.can_add_daily_production')


class CanViewDailyProduction(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm('production_planning.can_view_daily_production')


class CanCloseProductionPlan(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm('production_planning.can_close_production_plan')
