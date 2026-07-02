from django.contrib import admin

from .models import LabourGateAudit, LabourGateEntry, LabourGateOutBatch


class LabourOutBatchInline(admin.TabularInline):
    model = LabourGateOutBatch
    extra = 0
    fields = ("count", "created_at", "created_by")
    readonly_fields = ("created_at", "created_by")


@admin.register(LabourGateEntry)
class LabourGateEntryAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "contractor",
        "work_date",
        "count_in",
        "total_out",
        "remaining",
        "company",
    )
    list_display_links = ("id", "contractor")
    list_filter = ("work_date", "company")
    search_fields = ("contractor__contractor_name", "company__code")
    date_hierarchy = "work_date"
    ordering = ("-work_date", "contractor_id")
    list_per_page = 25

    inlines = [LabourOutBatchInline]
    raw_id_fields = ["company", "contractor"]
    readonly_fields = ("created_at", "updated_at", "created_by", "updated_by")

    @admin.display(description="Out")
    def total_out(self, obj):
        return obj.total_out

    @admin.display(description="Remaining")
    def remaining(self, obj):
        return obj.remaining

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .select_related("contractor", "company")
            .prefetch_related("out_batches")
        )


@admin.register(LabourGateAudit)
class LabourGateAuditAdmin(admin.ModelAdmin):
    list_display = ("id", "entry", "action", "old_value", "new_value", "performed_by", "created_at")
    list_filter = ("action", "created_at", "company")
    search_fields = ("entry__contractor__contractor_name", "detail")
    date_hierarchy = "created_at"
    ordering = ("-created_at", "-id")
    list_per_page = 50
    raw_id_fields = ("company", "entry", "performed_by")
    readonly_fields = ("company", "entry", "action", "detail", "old_value", "new_value", "performed_by", "created_at")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
