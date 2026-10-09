from django.contrib import admin

from .models import APInvoiceDraft


@admin.register(APInvoiceDraft)
class APInvoiceDraftAdmin(admin.ModelAdmin):
    list_display = (
        "entry_no", "company", "grpo_doc_num", "grpo_reference", "vendor_name",
        "sap_status", "sap_draft_entry", "created_at",
    )
    list_filter = ("company", "sap_status")
    search_fields = ("entry_no", "grpo_doc_num", "grpo_reference", "vendor_name", "vendor_code")
    raw_id_fields = ("created_by", "updated_by")
