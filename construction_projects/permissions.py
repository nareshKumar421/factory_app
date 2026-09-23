"""DRF permission classes for the construction module.

Same shape as ``maintenance/permissions.py``: one tiny class per permission,
named after what it lets somebody do.
"""

from rest_framework.permissions import BasePermission


class DjangoPermission(BasePermission):
    permission = ""

    def has_permission(self, request, view):
        return bool(request.user and request.user.has_perm(self.permission))


class CanViewProject(DjangoPermission):
    permission = "construction_projects.can_view_project"


class CanCreateProject(DjangoPermission):
    permission = "construction_projects.can_create_project"


class CanEditProject(DjangoPermission):
    permission = "construction_projects.can_edit_project"


class CanApproveProject(DjangoPermission):
    permission = "construction_projects.can_approve_project"


class CanLogDailyWork(DjangoPermission):
    permission = "construction_projects.can_log_daily_work"


class CanRecordExpense(DjangoPermission):
    permission = "construction_projects.can_record_expense"


class CanApproveExpense(DjangoPermission):
    permission = "construction_projects.can_approve_expense"


class CanReviewAnything(BasePermission):
    """Opens the approvals queue for either kind of approver.

    Sanctioning a two-crore budget and checking a day's cement bill are
    different jobs held by different people, so the queue opens for either and
    each section inside it is gated on its own right.
    """

    def has_permission(self, request, view):
        return bool(
            request.user
            and (
                request.user.has_perm("construction_projects.can_approve_project")
                or request.user.has_perm("construction_projects.can_approve_expense")
            )
        )


class CanCloseProject(DjangoPermission):
    permission = "construction_projects.can_close_project"
