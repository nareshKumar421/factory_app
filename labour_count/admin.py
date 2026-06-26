from django.contrib import admin

from .models import (
    LabourCountSheet,
    LabourCountItem,
    LabourShiftWindow,
    LabourVerification,
)


@admin.register(LabourShiftWindow)
class LabourShiftWindowAdmin(admin.ModelAdmin):
    list_display = ("company", "shift", "submit_lock_time", "verify_deadline_time")
    list_filter = ("company", "shift")


class LabourCountItemInline(admin.TabularInline):
    model = LabourCountItem
    extra = 0
    raw_id_fields = ["contractor"]
    fields = ("contractor", "count")


@admin.register(LabourCountSheet)
class LabourCountSheetAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "department",
        "work_date",
        "shift",
        "status",
        "is_holiday",
        "total_count",
        "lock_at",
        "submitted_at",
    )
    list_display_links = ("id", "department")
    list_filter = ("status", "shift", "is_holiday", "work_date", "company")
    search_fields = ("department__name", "company__code")
    date_hierarchy = "work_date"
    ordering = ("-work_date", "department_id")
    list_per_page = 25

    inlines = [LabourCountItemInline]
    raw_id_fields = ["company", "department"]
    readonly_fields = (
        "created_at",
        "updated_at",
        "created_by",
        "updated_by",
        "submitted_at",
        "submitted_by",
        "verified_at",
        "verified_by",
        "lock_at",
    )

    @admin.display(description="Total")
    def total_count(self, obj):
        return obj.total_count

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("department", "company").prefetch_related("items")


@admin.register(LabourVerification)
class LabourVerificationAdmin(admin.ModelAdmin):
    list_display = (
        "work_date",
        "shift",
        "basis",
        "department",
        "contractor",
        "submitted_total",
        "gate_counted",
        "variance",
        "created_by",
        "created_at",
    )
    list_filter = ("shift", "basis", "work_date", "company")
    date_hierarchy = "work_date"
    ordering = ("-created_at",)
    readonly_fields = ("created_at", "updated_at", "created_by", "updated_by")
