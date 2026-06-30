from django.contrib import admin

from .models import LabourGateEntry, LabourGateOutBatch


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
