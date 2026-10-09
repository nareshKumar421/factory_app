"""
production_dispatch/permissions.py

A right of its own, created by this app's migration. The page was asked for by
one reader and shows SKU-level sales volumes, which no production or stock
right has carried before, so it is granted on purpose rather than inherited.
"""

from rest_framework.permissions import BasePermission

VIEW_PERMISSION = "production_dispatch.can_view_production_dispatch"


class CanViewProductionDispatch(BasePermission):
    message = "You need the Production & Dispatch permission to read this report."

    def has_permission(self, request, view):
        return request.user.has_perm(VIEW_PERMISSION)
