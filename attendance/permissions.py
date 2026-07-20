"""
Permission-based access control for the Attendance module.
Uses Django's built-in model permissions (add/view/change/delete).
"""

from rest_framework.permissions import BasePermission


class CanManageEmployee(BasePermission):
    """CRUD gate for the attendance Employee master."""

    def has_permission(self, request, view):
        if request.method == "POST":
            return request.user.has_perm("attendance.add_employee")
        if request.method in ["PUT", "PATCH"]:
            return request.user.has_perm("attendance.change_employee")
        if request.method == "DELETE":
            return request.user.has_perm("attendance.delete_employee")
        return request.user.has_perm("attendance.view_employee")


class CanManageAttendance(BasePermission):
    """CRUD gate for attendance records."""

    def has_permission(self, request, view):
        if request.method == "POST":
            return request.user.has_perm("attendance.add_attendancerecord")
        if request.method in ["PUT", "PATCH"]:
            return request.user.has_perm("attendance.change_attendancerecord")
        if request.method == "DELETE":
            return request.user.has_perm("attendance.delete_attendancerecord")
        return request.user.has_perm("attendance.view_attendancerecord")
