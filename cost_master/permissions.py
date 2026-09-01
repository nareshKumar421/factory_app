from rest_framework.permissions import BasePermission


class CanViewCostMaster(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm('cost_master.can_view_cost_master')


class CanManageCostMaster(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm('cost_master.can_manage_cost_master')


class CanViewOrManageCostMaster(BasePermission):
    def has_permission(self, request, view):
        return (request.user.has_perm('cost_master.can_view_cost_master')
                or request.user.has_perm('cost_master.can_manage_cost_master'))
