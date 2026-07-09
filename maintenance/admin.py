from django.contrib import admin

from .models import (
    Asset,
    AssetCategory,
    AssetDepartment,
    AssetDocument,
    AssetLocation,
    AssetPhoto,
    FireCategory,
    FireEquipmentIssue,
    FireEquipmentIssueItem,
    FireMovement,
    FireRequest,
    FireShiftReport,
    FireShiftReportAttachment,
    FireShiftReportItem,
    FireShiftReportPhoto,
    MaintenanceChecklistResult,
    MaintenanceChecklistTemplateItem,
    MaintenanceFire,
    MaintenanceSpare,
    MaintenanceGateLink,
    MaintenanceSpareReceipt,
    MaintenanceVendorVisit,
    MaintenanceWorkOrder,
    MaintenanceWorkOrderPhoto,
    PreventiveMaintenanceExecution,
    PreventiveMaintenancePlan,
    SafetyFine,
    SafetyFinePhoto,
    SafetyViolationType,
    SpareCategory,
    SpareMovement,
    SpareRequest,
    WorkPermit,
    WorkPermitApproval,
    WorkPermitAttachment,
    WorkPermitWorker,
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


@admin.register(FireCategory)
class FireCategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "company", "is_active", "created_at")
    list_filter = ("company", "is_active")
    search_fields = ("name", "description")


@admin.register(MaintenanceFire)
class MaintenanceFireAdmin(admin.ModelAdmin):
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


@admin.register(FireRequest)
class FireRequestAdmin(admin.ModelAdmin):
    list_display = (
        "work_order",
        "fire_item",
        "status",
        "requested_qty",
        "issued_qty",
        "consumed_qty",
        "returned_qty",
        "required_by",
    )
    list_filter = ("company", "status", "fire_item__category", "required_by")
    search_fields = (
        "work_order__work_order_no",
        "work_order__title",
        "fire_item__part_number",
        "fire_item__name",
    )


@admin.register(FireMovement)
class FireMovementAdmin(admin.ModelAdmin):
    list_display = ("movement_type", "work_order", "fire_item", "quantity", "unit_cost", "performed_by", "created_at")
    list_filter = ("company", "movement_type", "fire_item__category")
    search_fields = ("work_order__work_order_no", "fire_item__part_number", "fire_item__name", "remarks")


class FireShiftReportPhotoInline(admin.TabularInline):
    model = FireShiftReportPhoto
    extra = 0
    raw_id_fields = ("item",)


class FireShiftReportItemInline(admin.TabularInline):
    model = FireShiftReportItem
    extra = 0
    raw_id_fields = ("asset",)


class FireShiftReportAttachmentInline(admin.TabularInline):
    model = FireShiftReportAttachment
    extra = 0


@admin.register(FireShiftReport)
class FireShiftReportAdmin(admin.ModelAdmin):
    list_display = ("report_date", "shift", "area", "status", "submitted_by", "reviewed_by", "created_at")
    list_filter = ("company", "shift", "status", "report_date")
    search_fields = ("area", "summary_remarks", "items__equipment_name")
    raw_id_fields = ("submitted_by", "reviewed_by")
    inlines = [FireShiftReportItemInline, FireShiftReportAttachmentInline]


@admin.register(FireShiftReportAttachment)
class FireShiftReportAttachmentAdmin(admin.ModelAdmin):
    list_display = ("title", "report", "is_active", "created_at")
    list_filter = ("is_active",)
    search_fields = ("title", "report__area")
    raw_id_fields = ("report",)


@admin.register(SafetyViolationType)
class SafetyViolationTypeAdmin(admin.ModelAdmin):
    list_display = ("name", "default_fine_amount", "company", "is_active")
    list_filter = ("company", "is_active")
    search_fields = ("name", "description")


class SafetyFinePhotoInline(admin.TabularInline):
    model = SafetyFinePhoto
    extra = 0


@admin.register(SafetyFine)
class SafetyFineAdmin(admin.ModelAdmin):
    list_display = (
        "fine_no",
        "offender_name",
        "violation_type",
        "fine_amount",
        "status",
        "occurred_at",
    )
    list_filter = ("company", "status", "violation_type", "occurred_at")
    search_fields = ("fine_no", "offender_name", "employee_code", "location")
    raw_id_fields = ("issued_by", "settled_by", "department")
    inlines = [SafetyFinePhotoInline]


class WorkPermitWorkerInline(admin.TabularInline):
    model = WorkPermitWorker
    extra = 0


class WorkPermitAttachmentInline(admin.TabularInline):
    model = WorkPermitAttachment
    extra = 0


class WorkPermitApprovalInline(admin.TabularInline):
    model = WorkPermitApproval
    extra = 0
    raw_id_fields = ("approved_by",)


@admin.register(WorkPermit)
class WorkPermitAdmin(admin.ModelAdmin):
    list_display = ("serial_no", "valid_date", "status", "job_location", "issued_to_name", "created_at")
    list_filter = ("company", "status", "valid_date")
    search_fields = ("serial_no", "job_location", "job_description", "issued_to_name")
    raw_id_fields = ("accepted_by", "completed_by")
    inlines = [WorkPermitWorkerInline, WorkPermitApprovalInline, WorkPermitAttachmentInline]


@admin.register(WorkPermitAttachment)
class WorkPermitAttachmentAdmin(admin.ModelAdmin):
    list_display = ("title", "permit", "is_active", "created_at")
    list_filter = ("is_active",)
    search_fields = ("title", "permit__serial_no")
    raw_id_fields = ("permit",)


class FireEquipmentIssueItemInline(admin.TabularInline):
    model = FireEquipmentIssueItem
    extra = 0
    raw_id_fields = ("fire_item",)


@admin.register(FireEquipmentIssue)
class FireEquipmentIssueAdmin(admin.ModelAdmin):
    list_display = (
        "issued_to_name",
        "employee_code",
        "department",
        "status",
        "issued_at",
        "expected_return",
        "returned_at",
    )
    list_filter = ("company", "status", "issued_at")
    search_fields = ("issued_to_name", "employee_code", "department", "items__equipment_name")
    raw_id_fields = ("issued_by",)
    inlines = [FireEquipmentIssueItemInline]


@admin.register(FireEquipmentIssueItem)
class FireEquipmentIssueItemAdmin(admin.ModelAdmin):
    list_display = (
        "equipment_name",
        "issue",
        "fire_item",
        "quantity_issued",
        "quantity_returned",
        "return_condition",
    )
    list_filter = ("company", "return_condition")
    search_fields = ("equipment_name", "issue__issued_to_name", "fire_item__part_number")
    raw_id_fields = ("issue", "fire_item")


@admin.register(FireShiftReportItem)
class FireShiftReportItemAdmin(admin.ModelAdmin):
    list_display = ("equipment_name", "equipment_type", "status", "report", "asset", "created_at")
    list_filter = ("company", "equipment_type", "status")
    search_fields = ("equipment_name", "reading", "remarks", "asset__asset_code")
    raw_id_fields = ("report", "asset")
    inlines = [FireShiftReportPhotoInline]


@admin.register(FireShiftReportPhoto)
class FireShiftReportPhotoAdmin(admin.ModelAdmin):
    list_display = ("item", "taken_on", "caption", "is_active")
    list_filter = ("taken_on", "is_active")
    search_fields = ("item__equipment_name", "caption")
    raw_id_fields = ("item",)


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
