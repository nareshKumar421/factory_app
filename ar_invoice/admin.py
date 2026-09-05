from django.contrib import admin

from .models import ARInvoiceAttachment, ARInvoiceLine, ARInvoicePosting


class ARInvoiceLineInline(admin.TabularInline):
    model = ARInvoiceLine
    extra = 0


class ARInvoiceAttachmentInline(admin.TabularInline):
    model = ARInvoiceAttachment
    extra = 0


@admin.register(ARInvoicePosting)
class ARInvoicePostingAdmin(admin.ModelAdmin):
    list_display = (
        "id", "company", "customer_code", "customer_name", "status",
        "sap_draft_entry", "sap_doc_num", "created_at",
    )
    list_filter = ("company", "status")
    search_fields = ("customer_code", "customer_name", "customer_ref")
    inlines = [ARInvoiceLineInline, ARInvoiceAttachmentInline]
