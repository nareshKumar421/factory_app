"""
The rights, matching the people a cash box involves.

``can_view_cash_book``    -- read the register and the bunches.
``can_manage_cash_book``  -- record, correct and cancel entries; send a bunch.
``can_approve_cash_entries`` -- approve or reject payments somebody else
                             recorded.
``can_manage_cash_branches`` -- configure the branch list every entry is
                             filed under. Declared beside its own class below.
``can_approve_salary_advances`` -- HR's: agree that an advance comes back off
                             a wage. Also declared below, with the screen it
                             guards.

Manage and approve both imply view at the read endpoints: nobody should be able
to act on a book they cannot look at. The custodian and the approver are
deliberately separate rights, because that separation is the only control the
module has -- but they are not mutually exclusive, so a small site can grant
both to one person knowingly.

The salary advance right is the one that does NOT imply view, and is the only
right here that opens a screen without opening the register. Agreeing to dock
somebody's pay is a payroll decision; it does not need the factory's petty
cash. See :class:`SalaryAdvancePermission`.
"""

from rest_framework.permissions import BasePermission

#: HTTP verbs that change something.
WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

VIEW_PERMISSION = "cash_book.can_view_cash_book"
MANAGE_PERMISSION = "cash_book.can_manage_cash_book"
APPROVE_PERMISSION = "cash_book.can_approve_cash_entries"


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


class CanApproveCashEntries(BasePermission):
    """Decide on payments somebody else recorded.

    A bunch is not decided on -- it is paperwork over vouchers already agreed
    to, one at a time -- so this guards the entry queue, not the batch.
    """

    message = "You are not an approver for the cash book."

    def has_permission(self, request, view):
        return _has(request.user, APPROVE_PERMISSION)


class CashBookPermission(BasePermission):
    """One class for an endpoint that both reads and writes."""

    def has_permission(self, request, view):
        if request.method in WRITE_METHODS:
            return CanManageCashBook().has_permission(request, view)
        return CanViewCashBook().has_permission(request, view)


BRANCHES_PERMISSION = "cash_book.can_manage_cash_branches"


class CanManageCashBranches(BasePermission):
    """Configure the branches a payment can be spent for.

    A separate right from keeping the book. The branch list is what every
    entry is filed under and what the reports group by, so renaming or retiring
    one reaches back through the whole register -- that is a settings decision,
    not a day's cash handling.
    """

    message = "You are not allowed to configure the cash book's branches."

    def has_permission(self, request, view):
        return _has(request.user, BRANCHES_PERMISSION)


class CashBranchPermission(BasePermission):
    """Anyone who can read the book may read the branch list; writes are tighter."""

    def has_permission(self, request, view):
        if request.method in WRITE_METHODS:
            return CanManageCashBranches().has_permission(request, view)
        return CanViewCashBook().has_permission(request, view)


SALARY_ADVANCE_PERMISSION = "cash_book.can_approve_salary_advances"


class CanApproveSalaryAdvances(BasePermission):
    """HR's right: agree that an advance comes back off a wage.

    Kept apart from ``can_approve_cash_entries`` on purpose. The cash approver
    agrees that money should have left the box; this agrees to dock somebody's
    pay, which is the payroll's decision and nobody else's. A site that wants
    one person doing both grants both.
    """

    message = "You are not allowed to decide on advances against salary."

    def has_permission(self, request, view):
        return _has(request.user, SALARY_ADVANCE_PERMISSION)


class SalaryAdvancePermission(BasePermission):
    """Who may open the salary advance screen at all.

    HR are let in on their own right, without the cash book's view permission.
    They are not book-keepers and have no business reading the register -- but
    the page they decide on is one of its screens, so the read has to admit
    them by name.
    """

    message = "You do not have access to advances against salary."

    def has_permission(self, request, view):
        if request.method in WRITE_METHODS:
            # Recording one is book-keeping; deciding it is HR's. The views
            # that decide check the HR right themselves, so a writer here is
            # either of them and the narrower check comes after.
            return _has(request.user, MANAGE_PERMISSION, SALARY_ADVANCE_PERMISSION)
        return _has(
            request.user,
            VIEW_PERMISSION,
            MANAGE_PERMISSION,
            APPROVE_PERMISSION,
            SALARY_ADVANCE_PERMISSION,
        )


class SalaryAdvanceWritePermission(BasePermission):
    """The one advance: readable by anyone on the screen, written by accounts.

    Correcting or withdrawing an advance is book-keeping, not a verdict, so it
    stays with whoever keeps the book. HR may still read the row -- refusing
    them the row whose list they are already reading would be a distinction
    without a reason.
    """

    message = "You may read advances against salary but not change one."

    def has_permission(self, request, view):
        if request.method in WRITE_METHODS:
            return CanManageCashBook().has_permission(request, view)
        return SalaryAdvancePermission().has_permission(request, view)
