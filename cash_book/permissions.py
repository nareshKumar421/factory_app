"""
Three rights, matching the three people a cash box involves.

``can_view_cash_book``    -- read the register and the bunches.
``can_manage_cash_book``  -- record, correct and cancel entries; send a bunch.
``can_approve_cash_bunch`` -- approve or reject a bunch somebody else sent.

Manage and approve both imply view at the read endpoints: nobody should be able
to act on a book they cannot look at. The custodian and the approver are
deliberately separate rights, because that separation is the only control the
module has -- but they are not mutually exclusive, so a small site can grant
both to one person knowingly.
"""

from rest_framework.permissions import BasePermission

#: HTTP verbs that change something.
WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

VIEW_PERMISSION = "cash_book.can_view_cash_book"
MANAGE_PERMISSION = "cash_book.can_manage_cash_book"
APPROVE_PERMISSION = "cash_book.can_approve_cash_bunch"


def _has(user, *codes) -> bool:
    if not user or not user.is_authenticated:
        return False
    return any(user.has_perm(code) for code in codes)


class CanViewCashBook(BasePermission):
    """Read-only access. A custodian or an approver passes this too."""

    message = "You do not have access to the cash book."

    def has_permission(self, request, view):
        return _has(request.user, VIEW_PERMISSION, MANAGE_PERMISSION, APPROVE_PERMISSION)


class CanManageCashBook(BasePermission):
    """Keep the book: record, correct, cancel, and send bunches for approval."""

    message = "You may read the cash book but not write in it."

    def has_permission(self, request, view):
        return _has(request.user, MANAGE_PERMISSION)


class CanApproveCashBunch(BasePermission):
    """Decide on a bunch of vouchers."""

    message = "You are not an approver for the cash book."

    def has_permission(self, request, view):
        return _has(request.user, APPROVE_PERMISSION)


class CashBookPermission(BasePermission):
    """One class for an endpoint that both reads and writes."""

    def has_permission(self, request, view):
        if request.method in WRITE_METHODS:
            return CanManageCashBook().has_permission(request, view)
        return CanViewCashBook().has_permission(request, view)
