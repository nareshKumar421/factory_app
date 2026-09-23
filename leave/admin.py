"""
Admin registrations.

Read-mostly on purpose. The trail and the day rows are shown but not editable:
every write in this module goes through :mod:`leave.services` so that it lands
in the audit trail, and an admin form that bypassed that would produce exactly
the records nobody can explain later.
"""

from django.contrib import admin

from .models import Holiday, LeaveApproval, LeaveRequest, LeaveRequestDay, LeaveType


@admin.register(LeaveType)
class LeaveTypeAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "company", "is_paid", "allow_half_day", "annual_quota", "status")
    list_filter = ("company", "status", "is_paid")
    search_fields = ("code", "name")


@admin.register(Holiday)
class HolidayAdmin(admin.ModelAdmin):
    list_display = ("date", "name", "company", "is_optional")
    list_filter = ("company", "is_optional")
    date_hierarchy = "date"


class LeaveRequestDayInline(admin.TabularInline):
    model = LeaveRequestDay
    extra = 0
    can_delete = False
    readonly_fields = ("date", "portion", "status", "is_projected", "projected_at")

    def has_add_permission(self, request, obj=None):
        return False


class LeaveApprovalInline(admin.TabularInline):
    model = LeaveApproval
    extra = 0
    can_delete = False
    readonly_fields = (
        "action", "from_status", "to_status", "comment", "authority",
        "performed_by", "performed_at",
    )

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(LeaveRequest)
class LeaveRequestAdmin(admin.ModelAdmin):
    list_display = (
        "id", "employee", "leave_type", "from_date", "to_date",
        "total_days", "status", "decided_by",
    )
    list_filter = ("company", "status", "leave_type")
    search_fields = ("employee__employee_code", "employee__full_name", "reason")
    date_hierarchy = "from_date"
    inlines = [LeaveRequestDayInline, LeaveApprovalInline]
    readonly_fields = ("applied_by", "applied_at", "decided_by", "decided_at", "total_days")


@admin.register(LeaveApproval)
class LeaveApprovalAdmin(admin.ModelAdmin):
    """Append-only: visible, never editable."""

    list_display = ("request", "action", "from_status", "to_status", "authority", "performed_by", "performed_at")
    list_filter = ("action", "authority")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
