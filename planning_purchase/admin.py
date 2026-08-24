from django.contrib import admin

from .models import PurchaseOrder, PurchaseOrderLine


class PurchaseOrderLineInline(admin.TabularInline):
    model = PurchaseOrderLine
    extra = 0
    fields = (
        "item_code", "item_name", "material_type", "quantity", "uom",
        "unit_price", "warehouse_code", "required_date",
        "required_qty", "available_qty", "on_order_qty", "shortage_qty", "moq_applied",
    )
    readonly_fields = (
        "required_qty", "available_qty", "on_order_qty", "shortage_qty", "moq_applied",
    )


@admin.register(PurchaseOrder)
class PurchaseOrderAdmin(admin.ModelAdmin):
    list_display = (
        "id", "company_code", "vendor_name", "plan_code", "status",
        "total_value", "sap_doc_num", "simulated", "created_at",
    )
    list_filter = ("company_code", "status", "simulated")
    search_fields = (
        "vendor_code", "vendor_name", "plan_code", "sap_doc_num", "lines__item_code",
    )
    date_hierarchy = "created_at"
    inlines = [PurchaseOrderLineInline]
    # Posting is a commitment to a supplier and belongs on the reviewed screen,
    # not in a raw admin edit. Everything SAP wrote back is read-only here.
    readonly_fields = (
        "sap_doc_entry", "sap_doc_num", "sap_error_message", "posted_at", "posted_by",
        "simulated", "idempotency_key", "created_by", "approved_by", "approved_at",
        "total_value", "created_at", "updated_at",
    )
