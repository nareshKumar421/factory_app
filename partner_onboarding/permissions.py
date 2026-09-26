"""
Access control for Partner Onboarding.

Three rights per kind of registration — view, verify (which includes editing
and rejecting) and approve (which creates the partner in SAP) — so customer
onboarding and vendor onboarding can go to different people, as they did in
SAP Portal. Each stronger right implies view: nobody should act on a record
they cannot read back.

The views serve both kinds; each names its kind in ``view.family`` and the
classes below check that kind's right. One class per action.

Fixed from the portal: there, any login could verify or edit a registration
(server.js:797 and :847 checked only the token) and verification never looked
at a role. Here verifying and editing need the verify right, and creating in
SAP the approve right.
"""

from rest_framework.permissions import BasePermission

from .families import FAMILIES


def _family(view):
    return FAMILIES[view.family]


def _holds(request, *permissions) -> bool:
    user = request.user
    return bool(user and user.is_authenticated) and any(user.has_perm(p) for p in permissions)


def can_view(user, family) -> bool:
    return any(user.has_perm(p) for p in family.permissions)


def can_verify(user, family) -> bool:
    return user.has_perm(family.verify_permission)


def can_approve(user, family) -> bool:
    return user.has_perm(family.approve_permission)


def can_reject(user, family) -> bool:
    """The verifier rejects at verification, the approver at approval (as in the portal)."""
    return can_verify(user, family) or can_approve(user, family)


class CanViewRegistrations(BasePermission):
    """List, open and download. Verifying or approving implies it."""

    message = "You do not have permission to view these registrations."

    def has_permission(self, request, view):
        return _holds(request, *_family(view).permissions)


class CanVerifyRegistrations(BasePermission):
    """Verify a pending registration, or edit an open one."""

    message = "You do not have permission to verify or edit these registrations."

    def has_permission(self, request, view):
        return _holds(request, _family(view).verify_permission)


class CanRejectRegistrations(BasePermission):
    """Reject an open registration: either the verify or the approve right."""

    message = "You do not have permission to reject these registrations."

    def has_permission(self, request, view):
        family = _family(view)
        return _holds(request, family.verify_permission, family.approve_permission)


class CanApproveRegistrations(BasePermission):
    """Set the SAP master data and create the partner in SAP."""

    message = "You do not have permission to create these partners in SAP."

    def has_permission(self, request, view):
        return _holds(request, _family(view).approve_permission)
