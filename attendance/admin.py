from django.contrib import admin
from django.utils.html import format_html

from .models import AttendanceOverrideLog, AttendanceRecord, DailyAttendance


class OverrideLogInline(admin.TabularInline):
    """The trail, read-only. These rows are never edited or deleted."""

    model = AttendanceOverrideLog
    extra = 0
    can_delete = False
    fields = ("performed_at", "action", "from_status", "to_status", "reason_code", "reason", "performed_by")
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(DailyAttendance)
class DailyAttendanceAdmin(admin.ModelAdmin):
    list_display = (
        "employee", "date", "machine_status", "effective_status",
        "is_overridden", "machine_first_punch", "machine_last_punch", "synced_at",
    )
    list_filter = ("date", "effective_status", "machine_status", "is_overridden")
    search_fields = ("employee__full_name", "employee__employee_code")
    list_select_related = ("employee",)
    date_hierarchy = "date"
    inlines = [OverrideLogInline]
    # The machine's reading is the evidence a correction is judged against.
    # Editable here it would stop being evidence.
    readonly_fields = (
        "employee", "date", "machine_status", "machine_first_punch", "machine_last_punch",
        "machine_punch_count", "machine_worked_minutes", "devices", "synced_at",
        "overridden_by", "overridden_at",
    )


@admin.register(AttendanceOverrideLog)
class AttendanceOverrideLogAdmin(admin.ModelAdmin):
    list_display = ("daily_attendance", "action", "from_status", "to_status", "reason_code", "performed_by", "performed_at")
    list_filter = ("action", "reason_code", "performed_at")
    search_fields = ("daily_attendance__employee__full_name", "reason")
    list_select_related = ("daily_attendance", "daily_attendance__employee", "performed_by")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(AttendanceRecord)
class AttendanceRecordAdmin(admin.ModelAdmin):
    list_display = ("employee", "date", "time", "direction", "photo_preview", "created_by", "created_at")
    list_filter = ("date", "direction", "employee__department")
    search_fields = ("employee__full_name", "employee__employee_code")
    readonly_fields = ("created_by", "created_at", "updated_at", "photo_preview")
    date_hierarchy = "date"

    @admin.display(description="Photo")
    def photo_preview(self, obj):
        if obj.photo:
            return format_html(
                '<img src="{}" style="max-height:80px;border-radius:4px;" />',
                obj.photo.url,
            )
        return "—"
