from rest_framework.permissions import BasePermission


class CanViewShipments(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm("outbound_dispatch.view_shipmentorder")


class CanSyncShipments(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm("outbound_dispatch.can_sync_shipments")


class CanAssignDockBay(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm("outbound_dispatch.can_assign_dock_bay")


class CanExecutePickTask(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm("outbound_dispatch.can_execute_pick_task")


class CanInspectTrailer(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm("outbound_dispatch.can_inspect_trailer")


class CanLoadTruck(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm("outbound_dispatch.can_load_truck")


class CanConfirmLoad(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm("outbound_dispatch.can_confirm_load")


class CanDispatchShipment(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm("outbound_dispatch.can_dispatch_shipment")


class CanPostGoodsIssue(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm("outbound_dispatch.can_post_goods_issue")


class CanViewDashboard(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm("outbound_dispatch.can_view_outbound_dashboard")
