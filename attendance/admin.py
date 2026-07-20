from django.contrib import admin
from django.utils.html import format_html

from .models import Employee, AttendanceRecord


@admin.register(Employee)
class EmployeeAdmin(admin.ModelAdmin):
    list_display = ("employee_code", "name", "department", "is_active", "created_at")
    list_filter = ("is_active", "department")
    search_fields = ("employee_code", "name")
    list_select_related = ("department",)
    ordering = ("name",)


@admin.register(AttendanceRecord)
class AttendanceRecordAdmin(admin.ModelAdmin):
    list_display = ("employee", "date", "time", "photo_preview", "created_by", "created_at")
    list_filter = ("date", "employee__department")
    search_fields = ("employee__name", "employee__employee_code")
    autocomplete_fields = ("employee",)
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
