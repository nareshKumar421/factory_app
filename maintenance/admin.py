from django.contrib import admin

from .models import (
    Asset,
    AssetCategory,
    AssetDepartment,
    AssetDocument,
    AssetLocation,
    AssetPhoto,
    MaintenanceChecklistResult,
    MaintenanceChecklistTemplateItem,
    MaintenanceSpare,
    MaintenanceGateLink,
    MaintenanceSpareReceipt,
    MaintenanceVendorVisit,
    MaintenanceWorkOrder,
    MaintenanceWorkOrderPhoto,
    PreventiveMaintenanceExecution,
    PreventiveMaintenancePlan,
    SpareCategory,
    SpareMovement,
    SpareRequest,
)


@admin.register(AssetCategory)
class AssetCategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "company", "is_active", "created_at")
    list_filter = ("company", "is_active")
    search_fields = ("name", "description")


@admin.register(AssetLocation)
class AssetLocationAdmin(admin.ModelAdmin):
    list_display = ("name", "area", "line", "company", "is_active")
    list_filter = ("company", "is_active")
    search_fields = ("name", "area", "line", "description")


@admin.register(AssetDepartment)
class AssetDepartmentAdmin(admin.ModelAdmin):
    list_display = ("name", "department_code", "company", "is_active")
    list_filter = ("company", "is_active")
    search_fields = ("name", "department_code", "description")


@admin.register(SpareCategory)
class SpareCategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "company", "is_active", "created_at")
    list_filter = ("company", "is_active")
    search_fields = ("name", "description")


class AssetPhotoInline(admin.TabularInline):
    model = AssetPhoto
    extra = 0


class AssetDocumentInline(admin.TabularInline):
    model = AssetDocument
    extra = 0


@admin.register(Asset)
class AssetAdmin(admin.ModelAdmin):
    list_display = (
        "asset_code",
        "name",
        "status",
        "category",
        "department",
        "location",
        "production_machine",
        "is_active",
    )
    list_filter = ("company", "status", "category", "department", "location", "is_active")
    search_fields = ("asset_code", "name", "serial_number", "qr_code", "production_machine__name")
    inlines = [AssetPhotoInline, AssetDocumentInline]


@admin.register(MaintenanceSpare)
class MaintenanceSpareAdmin(admin.ModelAdmin):
    list_display = (
        "part_number",
        "name",
        "category",
        "sap_item_code",
        "current_stock",
        "reorder_level",
        "minimum_stock",
        "is_critical",
        "is_active",
    )
    list_filter = ("company", "category", "is_critical", "is_active")
    search_fields = ("part_number", "name", "sap_item_code", "storage_location")
    filter_horizontal = ("compatible_assets",)


@admin.register(AssetPhoto)
class AssetPhotoAdmin(admin.ModelAdmin):
    list_display = ("asset", "taken_on", "is_monthly_photo", "is_active")
    list_filter = ("is_monthly_photo", "is_active")
    search_fields = ("asset__asset_code", "asset__name", "caption")


@admin.register(AssetDocument)
class AssetDocumentAdmin(admin.ModelAdmin):
    list_display = ("asset", "title", "document_type", "document_date", "is_active")
    list_filter = ("document_type", "is_active")
    search_fields = ("asset__asset_code", "asset__name", "title", "notes")


class MaintenanceWorkOrderPhotoInline(admin.TabularInline):
    model = MaintenanceWorkOrderPhoto
    extra = 0


@admin.register(MaintenanceWorkOrder)
class MaintenanceWorkOrderAdmin(admin.ModelAdmin):
    list_display = (
        "work_order_no",
        "title",
        "work_type",
        "status",
        "priority",
        "asset",
        "production_run",
        "production_breakdown",
        "assigned_to",
        "target_date",
    )
    list_filter = ("company", "work_type", "status", "priority", "department", "is_active")
    search_fields = (
        "work_order_no",
        "title",
        "problem_statement",
        "asset__asset_code",
        "asset__name",
    )
    inlines = [MaintenanceWorkOrderPhotoInline]


class MaintenanceChecklistTemplateItemInline(admin.TabularInline):
    model = MaintenanceChecklistTemplateItem
    extra = 0


@admin.register(PreventiveMaintenancePlan)
class PreventiveMaintenancePlanAdmin(admin.ModelAdmin):
    list_display = ("plan_code", "title", "asset", "frequency", "next_due_date", "assigned_to", "is_active")
    list_filter = ("company", "frequency", "priority", "is_active", "next_due_date")
    search_fields = ("plan_code", "title", "asset__asset_code", "asset__name")
    raw_id_fields = ("asset", "assigned_to")
    inlines = [MaintenanceChecklistTemplateItemInline]


@admin.register(MaintenanceChecklistTemplateItem)
class MaintenanceChecklistTemplateItemAdmin(admin.ModelAdmin):
    list_display = ("pm_plan", "sort_order", "task", "input_type", "is_required", "safety_critical", "is_active")
    list_filter = ("company", "input_type", "is_required", "safety_critical", "is_active")
    search_fields = ("pm_plan__plan_code", "task")
    raw_id_fields = ("pm_plan",)


class MaintenanceChecklistResultInline(admin.TabularInline):
    model = MaintenanceChecklistResult
    extra = 0


@admin.register(PreventiveMaintenanceExecution)
class PreventiveMaintenanceExecutionAdmin(admin.ModelAdmin):
    list_display = ("pm_plan", "asset", "due_date", "status", "work_order", "completed_by")
    list_filter = ("company", "status", "due_date")
    search_fields = ("pm_plan__plan_code", "pm_plan__title", "asset__asset_code", "work_order__work_order_no")
    raw_id_fields = ("pm_plan", "asset", "work_order", "completed_by")
    inlines = [MaintenanceChecklistResultInline]


@admin.register(MaintenanceChecklistResult)
class MaintenanceChecklistResultAdmin(admin.ModelAdmin):
    list_display = ("execution", "template_item", "input_type", "is_ok")
    list_filter = ("company", "input_type", "is_ok")
    search_fields = ("execution__pm_plan__plan_code", "task_snapshot", "remarks")
    raw_id_fields = ("execution", "template_item")


@admin.register(SpareRequest)
class SpareRequestAdmin(admin.ModelAdmin):
    list_display = (
        "work_order",
        "spare",
        "status",
        "requested_qty",
        "issued_qty",
        "consumed_qty",
        "returned_qty",
        "required_by",
    )
    list_filter = ("company", "status", "spare__category", "required_by")
    search_fields = ("work_order__work_order_no", "work_order__title", "spare__part_number", "spare__name")


@admin.register(SpareMovement)
class SpareMovementAdmin(admin.ModelAdmin):
    list_display = ("movement_type", "work_order", "spare", "quantity", "unit_cost", "performed_by", "created_at")
    list_filter = ("company", "movement_type", "spare__category")
    search_fields = ("work_order__work_order_no", "spare__part_number", "spare__name", "remarks")


@admin.register(MaintenanceGateLink)
class MaintenanceGateLinkAdmin(admin.ModelAdmin):
    list_display = (
        "gate_entry",
        "asset",
        "work_order",
        "spare",
        "qc_required",
        "qc_status",
        "receipt_status",
        "received_quantity",
    )
    list_filter = ("company", "qc_required", "qc_status", "receipt_status")
    search_fields = (
        "gate_entry__work_order_number",
        "gate_entry__material_description",
        "asset__asset_code",
        "work_order__work_order_no",
        "spare__part_number",
        "grpo_reference",
        "grpo_doc_num",
    )
    raw_id_fields = ("gate_entry", "asset", "work_order", "spare", "received_by")


@admin.register(MaintenanceSpareReceipt)
class MaintenanceSpareReceiptAdmin(admin.ModelAdmin):
    list_display = ("gate_link", "spare", "quantity", "unit_cost", "qc_status", "received_by", "received_at")
    list_filter = ("company", "qc_status", "received_at")
    search_fields = (
        "gate_link__gate_entry__work_order_number",
        "spare__part_number",
        "spare__name",
        "grpo_reference",
        "grpo_doc_num",
        "invoice_number",
    )
    raw_id_fields = ("gate_link", "asset", "work_order", "spare", "received_by")


@admin.register(MaintenanceVendorVisit)
class MaintenanceVendorVisitAdmin(admin.ModelAdmin):
    list_display = ("work_order", "asset", "vendor_name", "status", "planned_start", "actual_start")
    list_filter = ("company", "status", "planned_start")
    search_fields = (
        "work_order__work_order_no",
        "asset__asset_code",
        "vendor_code",
        "vendor_name",
        "invoice_number",
    )
    raw_id_fields = ("work_order", "asset", "person_gate_entry", "material_gate_entry")


@admin.register(MaintenanceWorkOrderPhoto)
class MaintenanceWorkOrderPhotoAdmin(admin.ModelAdmin):
    list_display = ("work_order", "photo_type", "taken_on", "is_active")
    list_filter = ("photo_type", "is_active")
    search_fields = ("work_order__work_order_no", "work_order__title", "caption")
