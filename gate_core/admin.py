from django.contrib import admin
from .models import (
    BSTGateIn,
    BSTGateOut,
    BSTGateOutItem,
    BSTGateReturn,
    CashSaleGateOut,
    CashSaleGateOutBill,
    EmptyVehicleGateIn,
    EmptyVehicleGateOut,
    JobWorkGateIn,
    JobWorkGateInItem,
    RejectedQCReturnEntry,
    RejectedQCReturnItem,
    SalesDispatchAttachment,
    SalesDispatchGateOut,
    SalesDispatchGateOutDocument,
    SalesDispatchGateOutItem,
    SalesDispatchGatepassPrintLog,
    SalesDispatchGatepassSequence,
    SalesDispatchLock,
    UnitChoice,
)


# Register your models here.
admin.site.register(UnitChoice)


class BSTGateOutItemInline(admin.TabularInline):
    model = BSTGateOutItem
    extra = 0
    readonly_fields = (
        "line_num", "item_code", "item_name", "quantity", "uom",
        "from_warehouse", "to_warehouse",
    )


@admin.register(BSTGateOut)
class BSTGateOutAdmin(admin.ModelAdmin):
    list_display = (
        "entry_no", "company", "vehicle", "driver", "sap_doc_num",
        "sap_from_warehouse", "sap_to_warehouse", "gate_out_date",
        "status", "created_at",
    )
    list_filter = ("company", "status", "gate_out_date")
    search_fields = (
        "entry_no", "vehicle_entry__entry_no", "vehicle__vehicle_number",
        "driver__name", "sap_doc_num", "sap_from_warehouse", "sap_to_warehouse",
    )
    readonly_fields = ("entry_no", "created_at", "updated_at")
    inlines = [BSTGateOutItemInline]


class SalesDispatchGateOutItemInline(admin.TabularInline):
    model = SalesDispatchGateOutItem
    extra = 0
    readonly_fields = (
        "document", "line_num", "item_code", "item_name", "quantity", "uom",
        "warehouse_code", "from_warehouse", "to_warehouse",
    )


class SalesDispatchGateOutDocumentInline(admin.TabularInline):
    model = SalesDispatchGateOutDocument
    extra = 0
    readonly_fields = (
        "document_type", "sap_doc_entry", "sap_doc_num", "customer_name",
        "sap_branch_id", "sap_doc_total", "dispatch_plan",
    )


class SalesDispatchAttachmentInline(admin.TabularInline):
    model = SalesDispatchAttachment
    extra = 0
    readonly_fields = ("uploaded_at", "uploaded_by", "original_filename")


class SalesDispatchGatepassPrintLogInline(admin.TabularInline):
    model = SalesDispatchGatepassPrintLog
    extra = 0
    readonly_fields = (
        "gatepass_no",
        "entry_status",
        "copy_number",
        "print_type",
        "reprint_reason",
        "printed_by",
        "printed_at",
        "printer_name",
        "ip_address",
    )
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


class CashSaleGateOutBillInline(admin.TabularInline):
    model = CashSaleGateOutBill
    extra = 0
    readonly_fields = (
        "sap_doc_entry", "sap_doc_num", "doc_date", "customer_name",
        "customer_ref", "doc_total", "item_summary", "is_active",
    )
    exclude = ("company", "customer_code", "total_quantity")


@admin.register(CashSaleGateOut)
class CashSaleGateOutAdmin(admin.ModelAdmin):
    list_display = (
        "entry_no", "company", "taken_by_name", "vehicle_no", "out_at", "status",
    )
    list_filter = ("company", "status", "out_at")
    search_fields = (
        "entry_no", "taken_by_name", "taken_by_mobile", "vehicle_no",
        "bills__sap_doc_num",
    )
    readonly_fields = ("entry_no", "created_at", "updated_at")
    inlines = [CashSaleGateOutBillInline]


@admin.register(SalesDispatchGateOut)
class SalesDispatchGateOutAdmin(admin.ModelAdmin):
    list_display = (
        "entry_no", "company", "document_type", "sap_doc_num",
        "vehicle_no", "driver_name", "status", "gatepass_no", "created_at",
    )
    list_filter = ("company", "document_type", "status", "created_at")
    search_fields = (
        "entry_no", "vehicle_entry__entry_no", "sap_doc_num",
        "vehicle_no", "driver_name", "customer_name", "gatepass_no",
    )
    readonly_fields = ("entry_no", "gatepass_no", "created_at", "updated_at")
    inlines = [
        SalesDispatchGateOutDocumentInline,
        SalesDispatchGateOutItemInline,
        SalesDispatchAttachmentInline,
        SalesDispatchGatepassPrintLogInline,
    ]


@admin.register(SalesDispatchGateOutDocument)
class SalesDispatchGateOutDocumentAdmin(admin.ModelAdmin):
    list_display = (
        "sales_dispatch", "company", "document_type", "sap_doc_num",
        "customer_name", "sap_branch_id", "created_at",
    )
    list_filter = ("company", "document_type", "sap_branch_id")
    search_fields = (
        "sales_dispatch__entry_no", "sap_doc_num", "customer_name", "customer_code",
    )
    readonly_fields = ("created_at", "updated_at")


@admin.register(SalesDispatchGatepassSequence)
class SalesDispatchGatepassSequenceAdmin(admin.ModelAdmin):
    list_display = ("company", "financial_year", "last_number", "updated_at")
    list_filter = ("company", "financial_year")


@admin.register(SalesDispatchGatepassPrintLog)
class SalesDispatchGatepassPrintLogAdmin(admin.ModelAdmin):
    list_display = (
        "gatepass_no",
        "company",
        "sales_dispatch",
        "print_type",
        "copy_number",
        "printed_by",
        "printed_at",
        "printer_name",
    )
    list_filter = ("company", "print_type", "printed_at")
    search_fields = (
        "gatepass_no",
        "sales_dispatch__entry_no",
        "sales_dispatch__vehicle_no",
        "printed_by__email",
        "printed_by__full_name",
        "reprint_reason",
    )
    readonly_fields = (
        "company",
        "sales_dispatch",
        "gatepass_no",
        "entry_status",
        "copy_number",
        "print_type",
        "reprint_reason",
        "printed_by",
        "printed_at",
        "printer_name",
        "ip_address",
        "user_agent",
        "created_at",
        "updated_at",
    )

    def has_add_permission(self, request):
        return False


@admin.register(SalesDispatchLock)
class SalesDispatchLockAdmin(admin.ModelAdmin):
    list_display = ("company", "is_locked", "changed_by", "changed_at", "updated_at")
    list_filter = ("company", "is_locked")
    search_fields = ("company__name", "company__code", "reason")
    readonly_fields = ("created_at", "updated_at", "changed_at")


class JobWorkGateInItemInline(admin.TabularInline):
    model = JobWorkGateInItem
    extra = 0
    readonly_fields = (
        "line_num", "item_code", "item_name", "quantity", "uom",
        "warehouse_code", "base_type", "base_entry", "base_line",
    )


@admin.register(JobWorkGateIn)
class JobWorkGateInAdmin(admin.ModelAdmin):
    list_display = (
        "entry_no", "company", "vehicle", "driver", "production_order_doc_num",
        "production_item_name", "gate_in_date", "in_time", "status", "created_at",
    )
    list_filter = ("company", "status", "gate_in_date")
    search_fields = (
        "entry_no", "vehicle_entry__entry_no", "vehicle__vehicle_number",
        "driver__name", "production_order_doc_num", "production_item_code",
        "production_item_name", "sap_doc_num",
    )
    readonly_fields = ("entry_no", "created_at", "updated_at")
    inlines = [JobWorkGateInItemInline]


@admin.register(BSTGateIn)
class BSTGateInAdmin(admin.ModelAdmin):
    list_display = (
        "entry_no", "company", "bst_gate_out", "vehicle", "driver",
        "gate_in_date", "in_time", "status", "created_at",
    )
    list_filter = ("company", "status", "gate_in_date")
    search_fields = (
        "entry_no", "vehicle_entry__entry_no", "bst_gate_out__entry_no",
        "vehicle__vehicle_number", "driver__name", "bst_gate_out__sap_doc_num",
    )
    readonly_fields = ("entry_no", "created_at", "updated_at")


@admin.register(BSTGateReturn)
class BSTGateReturnAdmin(admin.ModelAdmin):
    list_display = (
        "entry_no", "company", "bst_gate_out", "vehicle", "driver",
        "gate_in_date", "in_time", "status", "created_at",
    )
    list_filter = ("company", "status", "gate_in_date")
    search_fields = (
        "entry_no", "vehicle_entry__entry_no", "bst_gate_out__entry_no",
        "vehicle__vehicle_number", "driver__name", "bst_gate_out__sap_doc_num",
    )
    readonly_fields = ("entry_no", "created_at", "updated_at")


class RejectedQCReturnItemInline(admin.TabularInline):
    model = RejectedQCReturnItem
    extra = 0
    readonly_fields = (
        "inspection", "gate_entry_no", "report_no", "internal_lot_no",
        "item_name", "supplier_name", "quantity", "uom",
    )


@admin.register(RejectedQCReturnEntry)
class RejectedQCReturnEntryAdmin(admin.ModelAdmin):
    list_display = (
        "entry_no", "company", "vehicle", "driver", "gate_out_date",
        "status", "created_at",
    )
    list_filter = ("company", "status", "gate_out_date")
    search_fields = (
        "entry_no", "vehicle__vehicle_number", "driver__name",
        "challan_no", "eway_bill_no",
    )
    readonly_fields = ("entry_no", "created_at", "updated_at")
    inlines = [RejectedQCReturnItemInline]


@admin.register(EmptyVehicleGateOut)
class EmptyVehicleGateOutAdmin(admin.ModelAdmin):
    list_display = (
        "entry_no", "company", "vehicle_entry", "vehicle", "driver",
        "gate_out_date", "out_time", "status", "created_at",
    )
    list_filter = ("company", "status", "gate_out_date")
    search_fields = (
        "entry_no", "vehicle_entry__entry_no", "vehicle__vehicle_number",
        "driver__name", "security_name",
    )
    readonly_fields = ("entry_no", "created_at", "updated_at")


@admin.register(EmptyVehicleGateIn)
class EmptyVehicleGateInAdmin(admin.ModelAdmin):
    list_display = (
        "entry_no", "company", "vehicle_entry", "vehicle", "driver",
        "reason", "sap_doc_num", "gate_in_date", "in_time", "created_at",
    )
    list_filter = ("company", "reason", "gate_in_date")
    search_fields = (
        "entry_no", "vehicle_entry__entry_no", "vehicle__vehicle_number",
        "driver__name", "security_name", "sap_doc_num", "document_reference",
    )
    readonly_fields = ("entry_no", "created_at", "updated_at")
