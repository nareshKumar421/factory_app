from django.contrib import admin

from .models import DockingScanSkipRequest


@admin.register(DockingScanSkipRequest)
class DockingScanSkipRequestAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "sales_dispatch",
        "status",
        "requested_by",
        "requested_at",
        "reviewed_by",
        "reviewed_at",
    )
    list_filter = ("status", "company")
    search_fields = ("sales_dispatch__entry_no", "reason", "review_notes")
    raw_id_fields = ("company", "sales_dispatch", "requested_by", "reviewed_by")
    readonly_fields = ("created_at", "updated_at", "requested_at", "reviewed_at")
