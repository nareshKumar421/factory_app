from django.contrib import admin

from .models import APInvoiceDraft, APInvoiceDraftCheck


class APInvoiceDraftCheckInline(admin.TabularInline):
    model = APInvoiceDraftCheck
    extra = 0
    fields = ("position", "key", "status", "detail", "review_decision", "review_remark", "reviewed_by")
    readonly_fields = fields


@admin.register(APInvoiceDraft)
class APInvoiceDraftAdmin(admin.ModelAdmin):
    list_display = (
        "entry_no", "company", "grpo_doc_num", "grpo_reference", "vendor_name",
        "sap_status", "sap_draft_entry", "invoice_read_status", "created_at",
    )
    list_filter = ("company", "sap_status", "invoice_read_status")
    search_fields = ("entry_no", "grpo_doc_num", "grpo_reference", "vendor_name", "vendor_code")
    raw_id_fields = ("grpo_posting", "created_by", "updated_by")
    inlines = [APInvoiceDraftCheckInline]
