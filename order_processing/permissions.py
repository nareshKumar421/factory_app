from rest_framework.permissions import BasePermission


class _Perm(BasePermission):
    codename = ""

    def has_permission(self, request, view):
        return request.user.has_perm(f"order_processing.{self.codename}")


class CanViewOrders(_Perm):
    codename = "can_view_orders"


class CanSyncOrders(_Perm):
    codename = "can_sync_orders"


class CanAllocateStock(_Perm):
    codename = "can_allocate_stock"


class CanPlanProduction(_Perm):
    codename = "can_plan_production"


class CanPlanProcurement(_Perm):
    codename = "can_plan_procurement"
