from django.contrib import admin

from .models import FAItemReceipt, FAReceipt, FixedAssetGRPOClosure, FixedAssetGRPORecord


class FAItemReceiptInline(admin.TabularInline):
    model = FAItemReceipt
    fk_name = "po_receipt"
    extra = 0
    can_delete = False
    fields = (
        "sap_line_num", "po_item_code", "item_name", "ordered_qty", "received_qty",
        "accepted_qty", "uom", "unit_price", "tax_code", "warehouse_code", "gl_account",
    )
    readonly_fields = fields


@admin.register(FAReceipt)
class FAReceiptAdmin(admin.ModelAdmin):
    """Fixed-asset PO links, read-only: change them from the Fixed Asset GRPO screen,
    where the lock rules and the draft clean-up run."""

    list_display = ("po_number", "supplier_code", "supplier_name", "entry_no", "sap_doc_entry", "created_at")
    search_fields = ("po_number", "supplier_code", "supplier_name", "vehicle_entry__entry_no")
    list_select_related = ("vehicle_entry",)
    inlines = [FAItemReceiptInline]

    @admin.display(description="Gate entry")
    def entry_no(self, obj):
        return obj.vehicle_entry.entry_no

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(FixedAssetGRPOClosure)
class FixedAssetGRPOClosureAdmin(admin.ModelAdmin):
    list_display = ("vehicle_entry", "sap_doc_num", "closed_by", "closed_at")
    search_fields = ("vehicle_entry__entry_no", "sap_doc_num", "note")
    list_select_related = ("vehicle_entry", "closed_by")
    readonly_fields = ("vehicle_entry", "closed_by", "closed_at")


@admin.register(FixedAssetGRPORecord)
class FixedAssetGRPORecordAdmin(admin.ModelAdmin):
    list_display = ("grpo_posting", "simulated", "posted_by", "created_at")
    list_filter = ("simulated",)
    search_fields = ("grpo_posting__vehicle_entry__entry_no",)
    list_select_related = ("grpo_posting", "posted_by")
    readonly_fields = ("grpo_posting", "simulated", "sap_payload", "posted_by", "created_at")

    def has_add_permission(self, request):
        return False
