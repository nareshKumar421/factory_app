from django.contrib import admin

from .models import DockingApprovalAttachment, DockingScanSkipRequest


class DockingApprovalAttachmentInline(admin.TabularInline):
    model = DockingApprovalAttachment
    fk_name = "scan_skip_request"
    extra = 0
    fields = ("file", "original_filename", "file_size", "uploaded_by", "uploaded_at")
    readonly_fields = ("uploaded_at",)
    raw_id_fields = ("uploaded_by",)


@admin.register(DockingScanSkipRequest)
class DockingScanSkipRequestAdmin(admin.ModelAdmin):
    inlines = [DockingApprovalAttachmentInline]
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
