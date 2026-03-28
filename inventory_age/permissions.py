from rest_framework.permissions import BasePermission


class CanViewInventoryAge(BasePermission):
    """Permission to view the Inventory Age & Value dashboard."""

    def has_permission(self, request, view):
        return request.user.has_perm("inventory_age.can_view_inventory_age")
