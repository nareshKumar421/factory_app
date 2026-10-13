"""
Who may see and do what on the cash book -> SAP screens.

``cash_sap.can_post_cash_to_sap``          -- freeze a bunch and send it to SAP,
                                             change how a voucher posts, add
                                             posting bills, and use the escape
                                             hatches (stop, re-check, leave out,
                                             re-date, close the PO, reset).
``cash_sap.can_manage_cash_sap_settings``  -- edit the branch routing and the
                                             G/L item map.

Reading the plan is open to anyone who can read the cash book (any of its
rights) or either right above: the plan is the cash book seen from SAP's side.
Writing the voucher's bill facts (bill date, G.no., bill no.) is open to the
custodian (``cash_book.can_manage_cash_book``) as well as the poster -- they
are the voucher's own facts, written down by whoever holds the paper.
"""

from rest_framework.permissions import SAFE_METHODS, BasePermission

from cash_book.permissions import (
    APPROVE_PERMISSION,
    BRANCHES_PERMISSION,
    MANAGE_PERMISSION,
    VIEW_PERMISSION,
)

POST_PERMISSION = "cash_sap.can_post_cash_to_sap"
SETTINGS_PERMISSION = "cash_sap.can_manage_cash_sap_settings"

#: Any of these opens the read-only screens.
CASH_READ = (
    VIEW_PERMISSION,
    MANAGE_PERMISSION,
    APPROVE_PERMISSION,
    BRANCHES_PERMISSION,
    POST_PERMISSION,
    SETTINGS_PERMISSION,
)


def _has(user, *codes) -> bool:
    if not user or not user.is_authenticated:
        return False
    return any(user.has_perm(code) for code in codes)


class CanPostCashToSap(BasePermission):
    message = "You are not allowed to post the cash book to SAP."

    def has_permission(self, request, view):
        return _has(request.user, POST_PERMISSION)


class CashSapReadOrPost(BasePermission):
    """Reads: anyone on the cash book. Writes: the poster."""

    message = "You do not have access to the cash book's SAP posting."

    def has_permission(self, request, view):
        if request.method in SAFE_METHODS:
            return _has(request.user, *CASH_READ)
        return _has(request.user, POST_PERMISSION)


class CashSapSettingsPermission(BasePermission):
    """Reads: anyone on the cash book. Writes: the settings right."""

    message = "You are not allowed to change the cash book's SAP settings."

    def has_permission(self, request, view):
        if request.method in SAFE_METHODS:
            return _has(request.user, *CASH_READ)
        return _has(request.user, SETTINGS_PERMISSION)


class CanSearchSapItems(BasePermission):
    """The item picker, for whoever maps heads or overrides a voucher's item."""

    message = "You are not allowed to search SAP items here."

    def has_permission(self, request, view):
        return _has(request.user, POST_PERMISSION, SETTINGS_PERMISSION)


class VoucherInfoPermission(BasePermission):
    """The voucher's bill facts: read by anyone on the book, written by custodian or poster."""

    message = "You may read this voucher's bill details but not change them."

    def has_permission(self, request, view):
        if request.method in SAFE_METHODS:
            return _has(request.user, *CASH_READ)
        return _has(request.user, MANAGE_PERMISSION, POST_PERMISSION)
