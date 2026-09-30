"""The four rights, matching the people an audit involves.

``can_view_stock_audit``    -- open audits and see what has been counted.
``can_count_stock_audit``   -- enter physical counts (and take back their own).
``can_view_audit_sap_qty``  -- see SAP's quantity and the difference. Held back
                               from counters by default, so a count is of what
                               is on the floor, not a copy of SAP's figure.
``can_manage_stock_audit``  -- start, re-read from SAP; take back anybody's count.
``can_approve_stock_audit`` -- approve or reject a completed audit, and correct
                               its counts while it waits for them.
``can_post_stock_audit_to_sap`` -- post an approved audit's RM and PM
                               differences to SAP as an Inventory Posting: the
                               only right here that writes to SAP.

Every right implies viewing: nobody acts on an audit they cannot see.
"""
from rest_framework.permissions import BasePermission

VIEW = 'stock_audit.can_view_stock_audit'
COUNT = 'stock_audit.can_count_stock_audit'
SEE_SAP = 'stock_audit.can_view_audit_sap_qty'
MANAGE = 'stock_audit.can_manage_stock_audit'
APPROVE = 'stock_audit.can_approve_stock_audit'
POST_TO_SAP = 'stock_audit.can_post_stock_audit_to_sap'


def has(user, *codes) -> bool:
    return bool(user and user.is_authenticated) and any(user.has_perm(c) for c in codes)


def sees_sap(user) -> bool:
    # An approver approves against SAP's figure, so sees it.
    return has(user, SEE_SAP, MANAGE, APPROVE, POST_TO_SAP)


class CanViewStockAudit(BasePermission):
    message = 'You do not have access to stock audits.'

    def has_permission(self, request, view):
        return has(request.user, VIEW, COUNT, MANAGE, SEE_SAP, APPROVE, POST_TO_SAP)


class CanCountStockAudit(BasePermission):
    """Counting, or correcting counts as an approver (the service decides which applies)."""
    message = 'You are not allowed to enter counts in a stock audit.'

    def has_permission(self, request, view):
        return has(request.user, COUNT, MANAGE, APPROVE)


class CanManageStockAudit(BasePermission):
    message = 'Only a stock audit manager can do that.'

    def has_permission(self, request, view):
        return has(request.user, MANAGE)


class CanApproveStockAudit(BasePermission):
    message = 'Only a stock audit approver can do that.'

    def has_permission(self, request, view):
        return has(request.user, APPROVE)


class CanPostStockAuditToSap(BasePermission):
    message = 'You are not allowed to post a stock audit to SAP.'

    def has_permission(self, request, view):
        return has(request.user, POST_TO_SAP)

