"""
Access control for SAP Finance.

One class per right. SAP Portal gated journal entries, the general ledger and
the chart of accounts behind one module (``journal-entries``) and the budget
screen behind another (``budget``); the rights follow the same split. Managing
budgets implies viewing them: nobody should write what they cannot read back.
"""

from rest_framework.permissions import BasePermission

VIEW_LEDGERS_PERMISSION = "sap_finance.can_view_sap_ledgers"
VIEW_BUDGETS_PERMISSION = "sap_finance.can_view_sap_budgets"
MANAGE_BUDGETS_PERMISSION = "sap_finance.can_manage_sap_budgets"


def _authenticated(request):
    user = request.user
    return bool(user and user.is_authenticated)


class CanViewSapLedgers(BasePermission):
    """Journal entries, general ledger, chart of accounts."""

    message = "You do not have permission to view SAP ledgers."

    def has_permission(self, request, view):
        return _authenticated(request) and request.user.has_perm(VIEW_LEDGERS_PERMISSION)


class CanViewSapBudgets(BasePermission):
    """Read budgets and their change log. Managing implies it."""

    message = "You do not have permission to view SAP budgets."

    def has_permission(self, request, view):
        user = request.user
        return _authenticated(request) and (
            user.has_perm(VIEW_BUDGETS_PERMISSION) or user.has_perm(MANAGE_BUDGETS_PERMISSION)
        )


class CanManageSapBudgets(BasePermission):
    """Create, edit and delete budgets in SAP."""

    message = "You do not have permission to change SAP budgets."

    def has_permission(self, request, view):
        return _authenticated(request) and request.user.has_perm(MANAGE_BUDGETS_PERMISSION)
