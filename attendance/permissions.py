"""
Who may read the sheet, and who may argue with the machine.

OR-semantics over a set of codenames, the pattern the rest of this repo uses:
an endpoint asks "does this user hold *any* of these?" rather than naming one
required permission. A single required codename is what caused the service-GRPO
403 outage -- a set survives a group being renamed or a role being split.

The important line here is between **viewing** and **overriding**. Reading the
daily sheet is for anybody who supervises people: gate staff, HOD, production.
Changing a status is an assertion that contradicts a machine, on a day that has
already passed, and it is what payroll is later run from -- so it is a separate,
narrower grant that no amount of view access implies.
"""

from rest_framework.permissions import SAFE_METHODS, BasePermission

#: Anyone who may see who turned up.
VIEW_ATTENDANCE = (
    "attendance.can_view_daily_attendance",
    "attendance.can_view_attendance_dashboard",
    "attendance.view_attendancerecord",
    "employee_hierarchy.can_view_employees",
)

#: Deliberately short. Correcting attendance is an HR act.
OVERRIDE_ATTENDANCE = ("attendance.can_override_attendance_status",)

#: Triggering a pull from the punch machines.
SYNC_ATTENDANCE = (
    "attendance.can_sync_attendance",
    "attendance.can_override_attendance_status",
)

#: Marking somebody present by hand, when the machine is down.
MARK_ATTENDANCE = ("attendance.add_attendancerecord",)


def has_any(user, codenames):
    """True if the user holds at least one of ``codenames``.

    Superusers short-circuit, as everywhere else in the repo.
    """
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    return any(user.has_perm(codename) for codename in codenames)


class CanViewAttendance(BasePermission):
    """Read the daily sheet."""

    def has_permission(self, request, view):
        return has_any(request.user, VIEW_ATTENDANCE)


class CanOverrideAttendance(BasePermission):
    """Read for viewers; write only for the narrow override grant.

    The split is inside one class, by method, because the same viewset serves
    both -- that is the module pattern here, and it keeps the two rules next to
    each other where a reviewer sees them together.
    """

    def has_permission(self, request, view):
        if request.method in SAFE_METHODS:
            return has_any(request.user, VIEW_ATTENDANCE)
        return has_any(request.user, OVERRIDE_ATTENDANCE)


class CanSyncAttendance(BasePermission):
    def has_permission(self, request, view):
        if request.method in SAFE_METHODS:
            return has_any(request.user, VIEW_ATTENDANCE)
        return has_any(request.user, SYNC_ATTENDANCE)


class CanManageAttendance(BasePermission):
    """CRUD gate for the manual, photographed gate marks."""

    def has_permission(self, request, view):
        if request.method == "POST":
            return has_any(request.user, MARK_ATTENDANCE)
        if request.method in ("PUT", "PATCH"):
            return request.user.has_perm("attendance.change_attendancerecord")
        if request.method == "DELETE":
            return request.user.has_perm("attendance.delete_attendancerecord")
        return has_any(request.user, VIEW_ATTENDANCE)
