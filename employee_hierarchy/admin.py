"""
Admin for the module -- a back door, kept deliberately narrow.

The admin bypasses :mod:`employee_hierarchy.services`, which is where the
history, the audit trail and the path maintenance live. So the structural
fields are read-only here: ``hierarchy_path``, ``hierarchy_level`` and the
cached salary figure cannot be typed in, and salary records cannot be added.
Somebody who needs to fix a manager assignment does it on the page, where it
gets recorded; if a bulk import has left the paths wrong, the repair is
``manage.py rebuild_employee_paths``.
"""

from django.contrib import admin

from .models import (
    Department,
    Designation,
    Employee,
    EmployeeAuditLog,
    EmployeeHistory,
    EmployeeSalary,
    SalaryRevision,
)


@admin.register(Department)
class DepartmentAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "company", "parent", "head", "status")
    list_filter = ("company", "status")
    search_fields = ("code", "name")
    list_select_related = ("company", "parent", "head")
    autocomplete_fields = ("parent", "head")


@admin.register(Designation)
class DesignationAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "level", "is_managerial", "company", "status")
    list_filter = ("company", "status", "is_managerial")
    search_fields = ("code", "name")
    ordering = ("company", "level", "name")


@admin.register(Employee)
class EmployeeAdmin(admin.ModelAdmin):
    list_display = (
        "employee_code",
        "full_name",
        "designation",
        "department",
        "reporting_manager",
        "hierarchy_level",
        "employment_status",
        "company",
    )
    list_filter = ("company", "employment_status", "department", "is_manager")
    search_fields = ("employee_code", "full_name", "email", "phone")
    list_select_related = ("company", "department", "designation", "reporting_manager")
    autocomplete_fields = ("reporting_manager", "department", "designation", "user")
    readonly_fields = (
        "full_name",
        "hierarchy_level",
        "hierarchy_path",
        "current_salary_amount",
        "current_salary_currency",
        "created_at",
        "updated_at",
    )
    ordering = ("company", "hierarchy_level", "full_name")


@admin.register(EmployeeSalary)
class EmployeeSalaryAdmin(admin.ModelAdmin):
    list_display = (
        "employee",
        "total_compensation",
        "currency",
        "effective_from",
        "status",
        "approved_by",
    )
    list_filter = ("status", "currency")
    search_fields = ("employee__employee_code", "employee__full_name")
    list_select_related = ("employee", "approved_by")
    readonly_fields = ("total_compensation",)

    def has_add_permission(self, request):
        """Salary is append-only through the API, which records the revision."""
        return False


@admin.register(SalaryRevision)
class SalaryRevisionAdmin(admin.ModelAdmin):
    list_display = (
        "employee",
        "revision_type",
        "previous_amount",
        "new_amount",
        "effective_date",
    )
    list_filter = ("revision_type",)
    search_fields = ("employee__employee_code", "employee__full_name")
    list_select_related = ("employee", "salary_record")

    def has_add_permission(self, request):
        return False


@admin.register(EmployeeHistory)
class EmployeeHistoryAdmin(admin.ModelAdmin):
    list_display = ("employee", "event", "occurred_on", "from_value", "to_value")
    list_filter = ("event",)
    search_fields = ("employee__employee_code", "employee__full_name")
    list_select_related = ("employee",)


@admin.register(EmployeeAuditLog)
class EmployeeAuditLogAdmin(admin.ModelAdmin):
    list_display = ("employee", "action", "field", "previous_value", "new_value", "performed_by", "performed_at")
    list_filter = ("action",)
    search_fields = ("employee__employee_code", "employee__full_name")
    list_select_related = ("employee", "performed_by")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        """An audit row that can be edited is not an audit row."""
        return False

    def has_delete_permission(self, request, obj=None):
        return False
