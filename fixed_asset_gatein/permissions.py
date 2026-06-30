# fixed_asset_gatein/permissions.py
"""
Permission-based access control for the Fixed Asset Gate Entry module.
Uses Django's built-in permission system (mirrors construction_gatein).
"""

from rest_framework.permissions import BasePermission


class CanCreateFixedAssetEntry(BasePermission):
    """Permission to create a fixed asset gate entry."""

    def has_permission(self, request, view):
        return request.user.has_perm("fixed_asset_gatein.add_fixedassetgateentry")


class CanViewFixedAssetEntry(BasePermission):
    """Permission to view fixed asset gate entries."""

    def has_permission(self, request, view):
        return request.user.has_perm("fixed_asset_gatein.view_fixedassetgateentry")


class CanEditFixedAssetEntry(BasePermission):
    """Permission to edit a fixed asset gate entry."""

    def has_permission(self, request, view):
        return request.user.has_perm("fixed_asset_gatein.change_fixedassetgateentry")


class CanDeleteFixedAssetEntry(BasePermission):
    """Permission to delete a fixed asset gate entry."""

    def has_permission(self, request, view):
        return request.user.has_perm("fixed_asset_gatein.delete_fixedassetgateentry")


class CanCompleteFixedAssetEntry(BasePermission):
    """Permission to complete and lock a fixed asset gate entry."""

    def has_permission(self, request, view):
        return request.user.has_perm("fixed_asset_gatein.can_complete_fixed_asset_entry")


class CanViewAssetCategory(BasePermission):
    """Permission to view asset categories."""

    def has_permission(self, request, view):
        return request.user.has_perm("fixed_asset_gatein.view_assetcategory")


class CanManageAssetCategory(BasePermission):
    """Permission to manage asset categories."""

    def has_permission(self, request, view):
        return request.user.has_perm("fixed_asset_gatein.can_manage_asset_category")
