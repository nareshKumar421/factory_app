"""The four rights, matching the people an audit involves.

``can_view_stock_audit``    -- open audits and see what has been counted.
``can_count_stock_audit``   -- enter physical counts (and take back their own).
``can_view_audit_sap_qty``  -- see SAP's quantity and the difference. Held back
                               from counters by default, so a count is of what
                               is on the floor, not a copy of SAP's figure.
``can_manage_stock_audit``  -- start, re-read from SAP, close; take back anybody's count.

Counting and managing imply viewing: nobody acts on an audit they cannot see.
"""
from rest_framework.permissions import BasePermission

VIEW = 'stock_audit.can_view_stock_audit'
COUNT = 'stock_audit.can_count_stock_audit'
SEE_SAP = 'stock_audit.can_view_audit_sap_qty'
MANAGE = 'stock_audit.can_manage_stock_audit'


def has(user, *codes) -> bool:
    return bool(user and user.is_authenticated) and any(user.has_perm(c) for c in codes)


def sees_sap(user) -> bool:
    return has(user, SEE_SAP, MANAGE)


class CanViewStockAudit(BasePermission):
    message = 'You do not have access to stock audits.'

    def has_permission(self, request, view):
        return has(request.user, VIEW, COUNT, MANAGE, SEE_SAP)


class CanCountStockAudit(BasePermission):
    message = 'You are not allowed to enter counts in a stock audit.'

    def has_permission(self, request, view):
        return has(request.user, COUNT, MANAGE)


class CanManageStockAudit(BasePermission):
    message = 'Only a stock audit manager can do that.'

    def has_permission(self, request, view):
        return has(request.user, MANAGE)
