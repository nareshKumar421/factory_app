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
VIEW_OUTSTANDING_PERMISSION = "sap_finance.can_view_sap_outstanding"

#: The outstanding reports came across from EXIM, whose users hold its own
#: rights (under the ``exim`` label, one per screen). Each report also opens to
#: those, so nobody who could see it in EXIM loses it here.
EXIM_OUTSTANDING_RIGHTS = {
    "vendor_outstanding": ("exim.view_vendor_outstanding", "exim.sync_balance_sheet"),
    "customer_outstanding": ("exim.view_customer_outstanding", "exim.view_customer_balance_sheet"),
    "open_ap": ("exim.view_open_aps",),
    "open_ar": ("exim.view_open_ars",),
    "open_grpos": ("exim.sync_open_grpos",),
    "customer_aging": ("exim.view_customer_aging",),
}


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


def can_view_outstanding(user, report: str) -> bool:
    return bool(user and user.is_authenticated) and (
        user.has_perm(VIEW_OUTSTANDING_PERMISSION)
        or any(user.has_perm(right) for right in EXIM_OUTSTANDING_RIGHTS[report])
    )


def CanViewOutstanding(report: str):
    """One outstanding report: the right to them all, or EXIM's for this one."""

    class _CanViewOutstanding(BasePermission):
        message = "You do not have permission to view this SAP outstanding report."

        def has_permission(self, request, view):
            return can_view_outstanding(request.user, report)

    _CanViewOutstanding.__name__ = f"CanViewOutstanding({report})"
    return _CanViewOutstanding
