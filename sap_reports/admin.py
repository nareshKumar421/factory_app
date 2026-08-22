"""
sap_reports/admin.py

Admin for the report catalogue. The mirrored SAP fields are read-only -- editing
them here would only be overwritten by the next sync -- so what is left editable
is exactly what this app owns: the friendly name, the description, visibility,
and each parameter's description.
"""

from django.contrib import admin

from .models import SapReport, SapReportParameter, SapReportRun


class SapReportParameterInline(admin.TabularInline):
    model = SapReportParameter
    extra = 0
    fields = [
        "position",
        "label",
        "kind",
        "is_required",
        "default_value",
        "blank_value",
        "help_text",
        "is_quoted",
        "occurrences",
        "is_customised",
    ]
    readonly_fields = ["position", "is_quoted", "occurrences"]
    ordering = ["position"]


@admin.register(SapReport)
class SapReportAdmin(admin.ModelAdmin):
    list_display = [
        "sap_name",
        "company",
        "sap_category_name",
        "statement_kind",
        "parameter_count",
        "is_enabled",
        "is_runnable",
        "is_missing_in_sap",
        "last_run_at",
        "last_synced_at",
    ]
    list_filter = [
        "company",
        "sap_category_name",
        "is_enabled",
        "is_runnable",
        "is_missing_in_sap",
        "statement_kind",
    ]
    search_fields = ["sap_name", "display_name", "description", "slug"]
    ordering = ["company__code", "sort_order", "sap_name"]
    inlines = [SapReportParameterInline]

    fieldsets = (
        (
            "Shown to users",
            {"fields": ("display_name", "description", "is_enabled", "sort_order", "row_limit")},
        ),
        (
            "From SAP (read-only)",
            {
                "fields": (
                    "company",
                    "slug",
                    "sap_name",
                    "sap_internal_key",
                    "sap_category_id",
                    "sap_category_name",
                    "statement_kind",
                    "is_runnable",
                    "not_runnable_reason",
                    "is_missing_in_sap",
                    "sql_text",
                    "sql_hash",
                    "sap_changed_at",
                    "last_synced_at",
                    "last_run_at",
                )
            },
        ),
    )
    readonly_fields = [
        "company",
        "slug",
        "sap_name",
        "sap_internal_key",
        "sap_category_id",
        "sap_category_name",
        "statement_kind",
        "is_runnable",
        "not_runnable_reason",
        "is_missing_in_sap",
        "sql_text",
        "sql_hash",
        "sap_changed_at",
        "last_synced_at",
        "last_run_at",
    ]

    @admin.display(description="Filters")
    def parameter_count(self, report):
        return report.parameters.count()

    def has_add_permission(self, request):
        # Reports only ever arrive through a sync from SAP.
        return False


@admin.register(SapReportRun)
class SapReportRunAdmin(admin.ModelAdmin):
    list_display = [
        "created_at",
        "report",
        "company",
        "run_by",
        "status",
        "row_count",
        "was_truncated",
        "duration_ms",
        "export_format",
    ]
    list_filter = ["company", "status", "was_truncated", "export_format"]
    search_fields = ["report__sap_name", "run_by__username", "error_message"]
    date_hierarchy = "created_at"
    readonly_fields = [field.name for field in SapReportRun._meta.fields]

    def has_add_permission(self, request):
        return False
