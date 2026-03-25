# gate_core/permissions.py
"""
Permission-based access control for Gate Core module.
Uses Django's built-in permission system instead of role-based access.
"""

from rest_framework.permissions import BasePermission


# Gate Entry Permissions

class CanViewGateEntry(BasePermission):
    """Permission to view gate entries."""

    def has_permission(self, request, view):
        return request.user.has_perm("gate_core.can_view_gate_entry")


# Full View Permissions (read-only combined gate entry views)

class CanViewRawMaterialFullEntry(BasePermission):
    """Permission to view full raw material gate entry details."""

    def has_permission(self, request, view):
        return request.user.has_perm("gate_core.can_view_raw_material_full_entry")


class CanViewDailyNeedFullEntry(BasePermission):
    """Permission to view full daily need gate entry details."""

    def has_permission(self, request, view):
        return request.user.has_perm("gate_core.can_view_daily_need_full_entry")


class CanViewMaintenanceFullEntry(BasePermission):
    """Permission to view full maintenance gate entry details."""

    def has_permission(self, request, view):
        return request.user.has_perm("gate_core.can_view_maintenance_full_entry")


class CanViewConstructionFullEntry(BasePermission):
    """Permission to view full construction gate entry details."""

    def has_permission(self, request, view):
        return request.user.has_perm("gate_core.can_view_construction_full_entry")


class CanViewOutboundFullEntry(BasePermission):
    """Permission to view full outbound gate entry details."""

    def has_permission(self, request, view):
        return request.user.has_perm("gate_core.can_view_outbound_full_entry")
