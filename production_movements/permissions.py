from rest_framework.permissions import BasePermission


class CanViewProductionStock(BasePermission):
    """View the role-tagged production warehouse stock board."""

    def has_permission(self, request, view):
        return request.user.has_perm("production_movements.can_view_production_stock")


class CanViewWarehouseRoles(BasePermission):
    """Read the warehouse-role configuration."""

    def has_permission(self, request, view):
        return request.user.has_perm("production_movements.can_view_warehouse_roles")


class CanManageWarehouseRoles(BasePermission):
    """Edit the warehouse-role configuration."""

    def has_permission(self, request, view):
        return request.user.has_perm("production_movements.can_manage_warehouse_roles")


class CanViewMovements(BasePermission):
    """View the warehouse-movement ledger."""

    def has_permission(self, request, view):
        return request.user.has_perm("production_movements.can_view_movements")


class CanCreateMovement(BasePermission):
    """Create warehouse movements (transfers)."""

    def has_permission(self, request, view):
        return request.user.has_perm("production_movements.can_create_movement")
