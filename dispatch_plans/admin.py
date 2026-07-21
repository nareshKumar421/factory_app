from django.contrib import admin

from .models import (
    DispatchPlan,
    TransporterAPInvoiceAttachment,
    TransporterAPInvoiceLine,
    TransporterAPInvoicePosting,
)


@admin.register(DispatchPlan)
class DispatchPlanAdmin(admin.ModelAdmin):
    list_display = (
        "company",
        "sap_invoice_doc_num",
        "booking_status",
        "dispatch_date",
        "transporter_name",
        "vehicle_no",
        "driver_name",
        "bilty_no",
        "updated_at",
    )
    list_filter = ("company", "booking_status", "dispatch_date")
    search_fields = (
        "sap_invoice_doc_num",
        "transporter__name",
        "vehicle__vehicle_number",
        "driver__name",
        "driver__mobile_no",
        "driver__license_no",
        "bilty_no",
        "transporter__mobile_no",
    )
    readonly_fields = ("created_at", "updated_at", "created_by", "updated_by")


class TransporterAPInvoiceLineInline(admin.TabularInline):
    model = TransporterAPInvoiceLine
    extra = 0
    readonly_fields = (
        "service_grpo_posting",
        "service_grpo_line",
        "dispatch_plan",
        "base_entry",
        "base_line",
        "base_doc_num",
        "bilty_no",
        "service_description",
        "line_total",
        "tax_code",
        "gl_account",
    )
    can_delete = False


class TransporterAPInvoiceAttachmentInline(admin.TabularInline):
    model = TransporterAPInvoiceAttachment
    extra = 0
    readonly_fields = (
        "file",
        "original_filename",
        "sap_attachment_status",
        "sap_absolute_entry",
        "sap_error_message",
        "uploaded_at",
        "uploaded_by",
    )
    can_delete = False


@admin.register(TransporterAPInvoicePosting)
class TransporterAPInvoicePostingAdmin(admin.ModelAdmin):
    list_display = (
        "company",
        "vendor_code",
        "invoice_number",
        "status",
        "invoice_amount",
        "sap_doc_num",
        "posted_at",
        "created_at",
    )
    list_filter = ("company", "status", "posted_at")
    search_fields = (
        "vendor_code",
        "vendor_name",
        "invoice_number",
        "sap_doc_entry",
        "sap_doc_num",
    )
    readonly_fields = (
        "created_at",
        "updated_at",
        "created_by",
        "updated_by",
        "posted_at",
    )
    inlines = [TransporterAPInvoiceLineInline, TransporterAPInvoiceAttachmentInline]
