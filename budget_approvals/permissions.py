"""
budget_approvals/permissions.py

Permission-based access control for the Budget Approvals Dashboard module.
"""

from rest_framework.permissions import BasePermission


class CanViewBudgetApprovals(BasePermission):
    """Permission to view the Budget Approvals Dashboard."""

    def has_permission(self, request, view):
        return request.user.has_perm("budget_approvals.can_view_budget_approvals")
