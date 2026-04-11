"""
warehouse/permissions.py

DRF permission classes for the warehouse module.
Phase 1 only needs inventory and dashboard permissions.
"""

from rest_framework.permissions import BasePermission


class CanViewWarehouseDashboard(BasePermission):
    """Permission to view the warehouse dashboard."""

    def has_permission(self, request, view):
        return request.user.has_perm("warehouse.can_view_warehouse_dashboard")


class CanViewInventory(BasePermission):
    """Permission to view inventory data."""

    def has_permission(self, request, view):
        return request.user.has_perm("warehouse.can_view_inventory")


class CanViewNonMoving(BasePermission):
    """Permission to view non-moving / expiry reports."""

    def has_permission(self, request, view):
        return request.user.has_perm("warehouse.can_view_non_moving")
