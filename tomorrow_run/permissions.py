"""Who may see Tomorrow's run, pick on it, and put a planning sheet in.

Three rights, declared on ``TomorrowPlan``:

``tomorrow_run.can_view_tomorrow_run``
    See the page.
``tomorrow_run.can_pick_tomorrow_run``
    Pick what runs first on a machine — Gurvinder veerji's call each night.
``tomorrow_run.can_manage_tomorrow_run``
    Put in a planning sheet and read the plan again by hand.

Either of the last two also opens the page: a right to change it without the
right to see it would be a broken screen. ``manage.py setup_tomorrow_run_groups``
makes a group holding all three.
"""

from rest_framework.permissions import BasePermission

VIEW = "tomorrow_run.can_view_tomorrow_run"
PICK = "tomorrow_run.can_pick_tomorrow_run"
MANAGE = "tomorrow_run.can_manage_tomorrow_run"
VIEW_ANY = (VIEW, PICK, MANAGE)


class CanViewTomorrowRun(BasePermission):
    message = "You need the Tomorrow's run permission to see this page."

    def has_permission(self, request, view):
        return any(request.user.has_perm(p) for p in VIEW_ANY)


class CanPickTomorrowRun(BasePermission):
    message = "You need the right to pick on Tomorrow's run."

    def has_permission(self, request, view):
        return request.user.has_perm(PICK)


class CanManageTomorrowRun(BasePermission):
    message = "You need the right to put in a planning sheet."

    def has_permission(self, request, view):
        return request.user.has_perm(MANAGE)
