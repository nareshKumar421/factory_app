from django.contrib import admin

from .models import LabourRequest, LabourRequestAudit


@admin.register(LabourRequest)
class LabourRequestAdmin(admin.ModelAdmin):
    list_display = (
        "work_date",
        "shift",
        "department",
        "requested_count",
        "status",
        "approved_count",
        "company",
        "is_active",
    )
    list_filter = ("company", "shift", "status", "is_active")
    search_fields = ("department__name", "note", "decision_note")
    date_hierarchy = "work_date"


@admin.register(LabourRequestAudit)
class LabourRequestAuditAdmin(admin.ModelAdmin):
    list_display = ("request", "action", "old_value", "new_value", "performed_by", "created_at")
    list_filter = ("action", "company")
    search_fields = ("detail",)
    readonly_fields = tuple(f.name for f in LabourRequestAudit._meta.fields)
