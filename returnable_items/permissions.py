"""DRF permission classes for the Returnable Items module.

Same shape as ``maintenance/permissions.py`` — thin wrappers over
``user.has_perm``. Company scoping is handled separately by
``company.permissions.HasCompanyContext``.
"""

from rest_framework.permissions import BasePermission


class DjangoPermission(BasePermission):
    permission = ""

    def has_permission(self, request, view):
        return bool(request.user and request.user.has_perm(self.permission))


class AnyDjangoPermission(BasePermission):
    permissions = []

    def has_permission(self, request, view):
        return bool(
            request.user
            and any(request.user.has_perm(permission) for permission in self.permissions)
        )


class CanViewReturnableModule(DjangoPermission):
    permission = "returnable_items.can_view_returnable_module"


class CanViewReturnable(DjangoPermission):
    permission = "returnable_items.can_view_returnable_gatepass"


class CanManageReturnable(DjangoPermission):
    permission = "returnable_items.can_manage_returnable_gatepass"


class CanEditReturnable(AnyDjangoPermission):
    """Gate for editing an existing pass — held by two different people.

    The department edits its own draft; the approver edits a pass sitting in
    their queue, so a wrong party or the wrong pass type can be corrected on the
    spot instead of being bounced back. Which of the two applies is decided from
    the pass's status in ``ReturnableGatePassSerializer.update``; this class only
    keeps users who can do neither out of the endpoint.
    """

    permissions = [
        "returnable_items.can_manage_returnable_gatepass",
        "returnable_items.can_approve_returnable_gatepass",
    ]


class CanSubmitReturnable(DjangoPermission):
    permission = "returnable_items.can_submit_returnable_gatepass"


class CanApproveReturnable(DjangoPermission):
    permission = "returnable_items.can_approve_returnable_gatepass"


class CanGateOutReturnable(DjangoPermission):
    permission = "returnable_items.can_gate_out_returnable"


class CanGateInReturnable(DjangoPermission):
    permission = "returnable_items.can_gate_in_returnable"


class CanRejectReturnableAtGate(DjangoPermission):
    permission = "returnable_items.can_reject_returnable_at_gate"


class CanAcknowledgeReturnable(DjangoPermission):
    permission = "returnable_items.can_acknowledge_returnable"


class CanCloseReturnable(DjangoPermission):
    permission = "returnable_items.can_close_returnable"


class CanShortCloseReturnable(DjangoPermission):
    permission = "returnable_items.can_short_close_returnable"


class CanCancelReturnable(DjangoPermission):
    permission = "returnable_items.can_cancel_returnable"


class CanViewReturnableReports(DjangoPermission):
    permission = "returnable_items.can_view_returnable_reports"


class CanViewReturnableAtGate(AnyDjangoPermission):
    """Gate operators can read passes without holding the department view perm."""

    permissions = [
        "returnable_items.can_view_returnable_gatepass",
        "returnable_items.can_approve_returnable_gatepass",
        "returnable_items.can_gate_out_returnable",
        "returnable_items.can_gate_in_returnable",
    ]
