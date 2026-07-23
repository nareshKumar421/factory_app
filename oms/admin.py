"""Read-only admin for the OMS invoice-approval audit trail."""
from django.contrib import admin

from .models import InvoiceApprovalAudit


@admin.register(InvoiceApprovalAudit)
class InvoiceApprovalAuditAdmin(admin.ModelAdmin):
    list_display = (
        "invoice_log_id",
        "so_number",
        "party_name",
        "decision",
        "company",
        "created_by",
        "created_at",
    )
    list_filter = ("decision", "company", "created_at")
    search_fields = ("invoice_log_id", "so_number", "party_name")
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    raw_id_fields = ("created_by", "updated_by", "company")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
