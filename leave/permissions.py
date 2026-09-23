"""
DRF permission classes for the module.

These are the coarse gate only: "are you the kind of person who applies / views
/ decides?". **Whose** leave you may act on is a different question and belongs
to :mod:`leave.routing`, which every view consults per object -- exactly the way
``employee_hierarchy.permissions`` gates the salary endpoints and then lets
``SalaryReach`` decide whose figures come back.

Keeping the two apart matters here more than usual. A manager holding
``can_decide_leave`` passes :class:`CanDecideLeave` for *every* request in the
system; it is ``routing.authority_of`` that then refuses the ones outside their
team. A class that tried to do both would have to load the object, and a
permission class that loads objects is one that gets skipped on list endpoints.
"""

from rest_framework.permissions import BasePermission

from employee_hierarchy.access import has_any

from .routing import (
    ANY_ACCESS,
    APPLY,
    APPLY_FOR_OTHERS,
    CANCEL_APPROVED,
    DECIDE_ANY,
    DECIDE_OWN_TEAM,
    MANAGE_TYPES,
    VIEW_TEAM,
)

WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


class CanAccessLeave(BasePermission):
    """Anything in the module at all. The sidebar keys off this."""

    def has_permission(self, request, view):
        return has_any(request.user, ANY_ACCESS)


class CanApplyLeave(BasePermission):
    """Raise an application -- for yourself, or for somebody else.

    Which of the two is settled by ``routing.can_apply_for`` once the view
    knows who the application names.
    """

    def has_permission(self, request, view):
        return has_any(request.user, (APPLY, APPLY_FOR_OTHERS))


class CanViewLeave(BasePermission):
    """Read the requests your reach covers.

    Everybody who can apply can also read -- their own, at least -- so this is
    deliberately as wide as the module itself. ``routing.visible_filter`` is
    what narrows the rows.
    """

    def has_permission(self, request, view):
        if request.method in WRITE_METHODS:
            return has_any(request.user, (APPLY, APPLY_FOR_OTHERS))
        return has_any(request.user, ANY_ACCESS)


class CanDecideLeave(BasePermission):
    """Approve or reject. Scope is ``routing.authority_of``'s business."""

    def has_permission(self, request, view):
        return has_any(request.user, (DECIDE_OWN_TEAM, DECIDE_ANY))


class CanCancelApprovedLeave(BasePermission):
    """Take back an approval -- separate, because it may unpick a projection."""

    def has_permission(self, request, view):
        return has_any(request.user, (CANCEL_APPROVED,))


class CanViewTeamLeave(BasePermission):
    """The team calendar and the pending queue."""

    def has_permission(self, request, view):
        return has_any(request.user, (VIEW_TEAM, DECIDE_OWN_TEAM, DECIDE_ANY))


class CanManageLeaveTypes(BasePermission):
    """The leave-type and holiday masters: read widely, edit narrowly.

    Everybody raising an application needs to read the types -- a form with an
    empty dropdown is not a form -- so only writing is held back.
    """

    def has_permission(self, request, view):
        if request.method in WRITE_METHODS:
            return has_any(request.user, (MANAGE_TYPES,))
        return has_any(request.user, ANY_ACCESS)
