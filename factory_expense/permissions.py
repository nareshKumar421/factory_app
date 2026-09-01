"""
factory_expense/permissions.py

Two rights, deliberately separate: everybody who can read the wall must not be
able to change what it counts. ``can_configure_factory_expense`` implies the
view right at read endpoints, so an admin does not need both assigned.
"""

from rest_framework.permissions import BasePermission

VIEW = "factory_expense.can_view_factory_expense"
CONFIGURE = "factory_expense.can_configure_factory_expense"


class CanViewFactoryExpense(BasePermission):
    """Read the board. Configuring implies it."""

    def has_permission(self, request, view):
        user = request.user
        return bool(user and (user.has_perm(VIEW) or user.has_perm(CONFIGURE)))


class CanConfigureFactoryExpense(BasePermission):
    """Change rates, salaries, budgets and board settings."""

    def has_permission(self, request, view):
        return bool(request.user and request.user.has_perm(CONFIGURE))


class CanReadOrConfigureFactoryExpense(BasePermission):
    """Read for anyone who can view; writes need the configure right.

    Used on the config endpoints so a viewer can *see* the current setup —
    which is what makes an unexplained zero on the wall diagnosable — without
    being able to edit it.
    """

    SAFE = {"GET", "HEAD", "OPTIONS"}

    def has_permission(self, request, view):
        user = request.user
        if not user:
            return False
        if request.method in self.SAFE:
            return bool(user.has_perm(VIEW) or user.has_perm(CONFIGURE))
        return bool(user.has_perm(CONFIGURE))
