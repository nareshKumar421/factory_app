# quality_control/admin.py
"""
Enhanced Quality Control Admin Configuration for Factory Jivo Wellness
"""

from django.contrib import admin
from django.utils.html import format_html
from django.urls import reverse
from .models import (
    MaterialType,
    MaterialTypeSAPItem,
    QCParameterSet,
    QCParameterMaster,
    QCPrintDocument,
    MaterialArrivalSlip,
    RawMaterialInspection,
    InspectionManagerDecisionLog,
    InspectionParameterResult,
    InspectionAttachment,
)
from .enums import InspectionStatus, InspectionWorkflowStatus


# ==================== Material Type Admin ====================

class MaterialTypeSAPItemInline(admin.TabularInline):
    model = MaterialTypeSAPItem
    extra = 1
    fields = ("item_code", "item_name", "is_active")


class QCParameterSetInline(admin.TabularInline):
    model = QCParameterSet
    extra = 0
    fields = ("vendor_code", "vendor_name", "notes", "is_active")
    verbose_name = "QC parameter set"
    verbose_name_plural = "QC parameter sets (blank vendor = default)"


@admin.register(MaterialType)
class MaterialTypeAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "company", "is_active", "created_at")
    list_filter = ("company", "is_active", "created_at")
    search_fields = ("code", "name", "description")
    ordering = ("code",)
    list_per_page = 25
    inlines = [MaterialTypeSAPItemInline, QCParameterSetInline]

    fieldsets = (
        (None, {
            "fields": ("code", "name", "description", "company"),
        }),
        ("Status", {
            "fields": ("is_active",),
        }),
        ("Audit", {
            "fields": ("created_by", "created_at", "updated_by", "updated_at"),
            "classes": ("collapse",),
        }),
    )
    readonly_fields = ("created_by", "created_at", "updated_by", "updated_at")

    def save_model(self, request, obj, form, change):
        if not change:
            obj.created_by = request.user
        obj.updated_by = request.user
        super().save_model(request, obj, form, change)


@admin.register(MaterialTypeSAPItem)
class MaterialTypeSAPItemAdmin(admin.ModelAdmin):
    list_display = ("item_code", "item_name", "material_type", "company", "is_active", "updated_at")
    list_filter = ("company", "material_type", "is_active")
    search_fields = ("item_code", "item_name", "material_type__code", "material_type__name")
    ordering = ("item_code",)


# ==================== QC Parameter Set / Master Admin ====================

class QCParameterMasterInline(admin.TabularInline):
    model = QCParameterMaster
    extra = 1
    fields = (
        "parameter_code", "parameter_name", "standard_value",
        "parameter_type", "min_value", "max_value", "uom",
        "sequence", "is_mandatory", "is_active"
    )


@admin.register(QCParameterSet)
class QCParameterSetAdmin(admin.ModelAdmin):
    list_display = (
        "material_type", "label", "vendor_code",
        "parameter_count", "is_active", "updated_at"
    )
    list_filter = ("material_type__company", "material_type", "is_active")
    search_fields = (
        "vendor_code", "vendor_name",
        "material_type__code", "material_type__name",
    )
    ordering = ("material_type", "vendor_code")
    list_per_page = 25
    inlines = [QCParameterMasterInline]

    fieldsets = (
        (None, {
            "fields": ("material_type", "vendor_code", "vendor_name", "notes"),
            "description": "Leave the vendor blank for the default set — "
                           "it applies to every vendor without a set of their own, "
                           "and to production QC.",
        }),
        ("Status", {
            "fields": ("is_active",),
        }),
        ("Audit", {
            "fields": ("created_by", "created_at", "updated_by", "updated_at"),
            "classes": ("collapse",),
        }),
    )
    readonly_fields = ("created_by", "created_at", "updated_by", "updated_at")

    @admin.display(description="Parameters")
    def parameter_count(self, obj):
        return obj.parameters.filter(is_active=True).count()

    def save_model(self, request, obj, form, change):
        if not change:
            obj.created_by = request.user
        obj.updated_by = request.user
        super().save_model(request, obj, form, change)


@admin.register(QCParameterMaster)
class QCParameterMasterAdmin(admin.ModelAdmin):
    list_display = (
        "parameter_code", "parameter_name", "parameter_set",
        "standard_value", "parameter_type", "sequence",
        "is_mandatory", "is_active"
    )
    list_filter = (
        "parameter_set__material_type", "parameter_type",
        "is_mandatory", "is_active",
    )
    search_fields = (
        "parameter_code", "parameter_name",
        "parameter_set__material_type__name",
        "parameter_set__vendor_code", "parameter_set__vendor_name",
    )
    ordering = ("parameter_set", "sequence")
    list_per_page = 25

    fieldsets = (
        ("Parameter Set", {
            "fields": ("parameter_set",),
        }),
        ("Parameter Definition", {
            "fields": ("parameter_code", "parameter_name", "standard_value", "parameter_type"),
        }),
        ("Validation", {
            "fields": ("min_value", "max_value", "uom"),
        }),
        ("Display", {
            "fields": ("sequence", "is_mandatory", "is_active"),
        }),
        ("Audit", {
            "fields": ("created_by", "created_at", "updated_by", "updated_at"),
            "classes": ("collapse",),
        }),
    )
    readonly_fields = ("created_by", "created_at", "updated_by", "updated_at")

    def save_model(self, request, obj, form, change):
        if not change:
            obj.created_by = request.user
        obj.updated_by = request.user
        super().save_model(request, obj, form, change)


# ==================== QC Print Document Admin ====================

@admin.register(QCPrintDocument)
class QCPrintDocumentAdmin(admin.ModelAdmin):
    list_display = ("document_key", "document_id", "company", "is_active", "updated_at")
    list_filter = ("company", "document_key", "is_active")
    search_fields = ("document_id", "notes")
    ordering = ("company", "document_key")
    readonly_fields = ("created_by", "created_at", "updated_by", "updated_at")

    fieldsets = (
        (None, {
            "fields": ("company", "document_key", "document_id", "notes"),
        }),
        ("Status", {
            "fields": ("is_active",),
        }),
        ("Audit", {
            "fields": ("created_by", "created_at", "updated_by", "updated_at"),
            "classes": ("collapse",),
        }),
    )

    def save_model(self, request, obj, form, change):
        if not change:
            obj.created_by = request.user
        obj.updated_by = request.user
        super().save_model(request, obj, form, change)


# ==================== Material Arrival Slip Admin ====================

@admin.register(MaterialArrivalSlip)
class MaterialArrivalSlipAdmin(admin.ModelAdmin):
    list_display = (
        "po_item_code_display", "item_name_display", "particulars_short",
        "party_name", "arrival_datetime", "status_badge", "is_submitted",
        "submitted_at", "created_at"
    )
    list_filter = ("status", "is_submitted", "weighing_required", "created_at")
    search_fields = (
        "po_item_receipt__po_item_code", "po_item_receipt__item_name",
        "particulars", "party_name",
        "commercial_invoice_no", "eway_bill_no", "bilty_no"
    )
    ordering = ("-created_at",)
    date_hierarchy = "created_at"
    list_per_page = 25

    fieldsets = (
        ("PO Item", {
            "fields": ("po_item_receipt",),
        }),
        ("Arrival Information", {
            "fields": ("particulars", "arrival_datetime", "weighing_required"),
        }),
        ("Party Information", {
            "fields": ("party_name", "billing_qty", "billing_uom"),
        }),
        ("Transport Details", {
            "fields": ("truck_no_as_per_bill",),
        }),
        ("Document References", {
            "fields": ("commercial_invoice_no", "eway_bill_no", "bilty_no"),
        }),
        ("Certificates", {
            "fields": ("has_certificate_of_analysis", "has_certificate_of_quantity"),
        }),
        ("Status", {
            "fields": ("status", "is_submitted", "submitted_at", "submitted_by", "in_time_to_qa"),
        }),
        ("Remarks", {
            "fields": ("remarks",),
        }),
        ("Audit", {
            "fields": ("created_by", "created_at", "updated_by", "updated_at"),
            "classes": ("collapse",),
        }),
    )
    readonly_fields = (
        "status", "is_submitted", "submitted_at", "submitted_by", "in_time_to_qa",
        "created_by", "created_at", "updated_by", "updated_at"
    )

    @admin.display(description="PO Item Code", ordering="po_item_receipt__po_item_code")
    def po_item_code_display(self, obj):
        return obj.po_item_receipt.po_item_code

    @admin.display(description="Item Name", ordering="po_item_receipt__item_name")
    def item_name_display(self, obj):
        name = obj.po_item_receipt.item_name
        if len(name) > 30:
            return name[:27] + "..."
        return name

    @admin.display(description="Particulars")
    def particulars_short(self, obj):
        if len(obj.particulars) > 40:
            return obj.particulars[:37] + "..."
        return obj.particulars

    @admin.display(description="Status")
    def status_badge(self, obj):
        colors = {
            "DRAFT": "#3498db",
            "SUBMITTED": "#27ae60",
            "REJECTED": "#e74c3c",
        }
        color = colors.get(obj.status, "#95a5a6")
        return format_html(
            '<span style="background-color: {}; color: white; padding: 3px 10px; '
            'border-radius: 4px; font-size: 11px;">{}</span>',
            color, obj.get_status_display()
        )

    def save_model(self, request, obj, form, change):
        if not change:
            obj.created_by = request.user
        obj.updated_by = request.user
        super().save_model(request, obj, form, change)


# ==================== Inspection Parameter Result Inline ====================

class InspectionParameterResultInline(admin.TabularInline):
    model = InspectionParameterResult
    extra = 0
    fields = (
        "parameter_name", "standard_value", "result_value",
        "result_numeric", "is_within_spec", "remarks"
    )
    readonly_fields = ("parameter_name", "standard_value")


# ==================== Inspection Attachment Inline ====================

class InspectionAttachmentInline(admin.TabularInline):
    model = InspectionAttachment
    extra = 0
    fields = ("file", "original_name", "uploaded_by", "uploaded_at")
    readonly_fields = ("uploaded_by", "uploaded_at")


# ==================== Manager Decision Log Inline ====================

class InspectionManagerDecisionLogInline(admin.TabularInline):
    """Read-only audit trail of every QA Manager decision, newest first."""
    model = InspectionManagerDecisionLog
    extra = 0
    can_delete = False
    fields = ("decision", "decided_by", "decided_at", "remarks")
    readonly_fields = ("decision", "decided_by", "decided_at", "remarks")
    ordering = ("-decided_at", "-id")

    def has_add_permission(self, request, obj=None):
        return False


# ==================== Raw Material Inspection Admin ====================

@admin.register(RawMaterialInspection)
class RawMaterialInspectionAdmin(admin.ModelAdmin):
    list_display = (
        "report_no", "internal_lot_no", "po_item_code_display",
        "description_short", "supplier_name", "inspection_date",
        "workflow_status_badge", "final_status_badge",
        "qa_chemist", "qam", "is_locked"
    )
    list_filter = (
        "workflow_status", "final_status", "is_locked",
        "inspection_date", "material_type", "created_at"
    )
    search_fields = (
        "report_no", "internal_lot_no", "internal_report_no", "description_of_material",
        "supplier_name", "manufacturer_name", "purchase_order_no",
        "invoice_bill_no", "sap_code",
        "arrival_slip__po_item_receipt__po_item_code"
    )
    ordering = ("-created_at",)
    date_hierarchy = "inspection_date"
    list_per_page = 25
    inlines = [
        InspectionParameterResultInline,
        InspectionAttachmentInline,
        InspectionManagerDecisionLogInline,
    ]

    fieldsets = (
        ("Identifiers", {
            "fields": ("report_no", "internal_lot_no", "internal_report_no", "arrival_slip"),
        }),
        ("Inspection Details", {
            "fields": ("inspection_date", "description_of_material", "sap_code", "material_type"),
        }),
        ("Supplier Information", {
            "fields": ("supplier_name", "manufacturer_name", "supplier_batch_lot_no"),
        }),
        ("Order Information", {
            "fields": ("unit_packing", "purchase_order_no", "invoice_bill_no", "vehicle_no"),
        }),
        ("Status", {
            "fields": ("workflow_status", "final_status", "is_locked"),
        }),
        ("QA Chemist Approval", {
            "fields": ("qa_chemist", "qa_chemist_approved_at", "qa_chemist_remarks"),
        }),
        ("QA Manager Approval", {
            "fields": ("qam", "qam_approved_at", "qam_remarks"),
        }),
        ("Remarks", {
            "fields": ("remarks",),
        }),
        ("Audit", {
            "fields": ("created_by", "created_at", "updated_by", "updated_at"),
            "classes": ("collapse",),
        }),
    )
    readonly_fields = (
        "report_no", "internal_lot_no", "workflow_status", "is_locked",
        "qa_chemist", "qa_chemist_approved_at", "qam", "qam_approved_at",
        "created_by", "created_at", "updated_by", "updated_at"
    )

    @admin.display(description="PO Item Code")
    def po_item_code_display(self, obj):
        try:
            return obj.arrival_slip.po_item_receipt.po_item_code
        except AttributeError:
            return "-"

    @admin.display(description="Description")
    def description_short(self, obj):
        if len(obj.description_of_material) > 40:
            return obj.description_of_material[:37] + "..."
        return obj.description_of_material

    @admin.display(description="Workflow")
    def workflow_status_badge(self, obj):
        colors = {
            InspectionWorkflowStatus.DRAFT: "#3498db",
            InspectionWorkflowStatus.SUBMITTED: "#f39c12",
            InspectionWorkflowStatus.QA_CHEMIST_APPROVED: "#9b59b6",
            InspectionWorkflowStatus.QAM_APPROVED: "#27ae60",
            InspectionWorkflowStatus.COMPLETED: "#2c3e50",
        }
        color = colors.get(obj.workflow_status, "#95a5a6")
        return format_html(
            '<span style="background-color: {}; color: white; padding: 3px 10px; '
            'border-radius: 4px; font-size: 11px;">{}</span>',
            color, obj.get_workflow_status_display()
        )

    @admin.display(description="Final Status")
    def final_status_badge(self, obj):
        colors = {
            InspectionStatus.PENDING: "#f39c12",
            InspectionStatus.ACCEPTED: "#27ae60",
            InspectionStatus.REJECTED: "#e74c3c",
            InspectionStatus.HOLD: "#9b59b6",
        }
        color = colors.get(obj.final_status, "#95a5a6")
        return format_html(
            '<span style="background-color: {}; color: white; padding: 3px 10px; '
            'border-radius: 4px; font-size: 11px;">{}</span>',
            color, obj.get_final_status_display()
        )

    def save_model(self, request, obj, form, change):
        if not change:
            obj.created_by = request.user
            if not obj.report_no:
                obj.report_no = RawMaterialInspection.generate_report_no()
            if not obj.internal_lot_no:
                obj.internal_lot_no = RawMaterialInspection.generate_lot_no()
        obj.updated_by = request.user
        super().save_model(request, obj, form, change)


# ==================== Inspection Attachment Admin ====================

@admin.register(InspectionAttachment)
class InspectionAttachmentAdmin(admin.ModelAdmin):
    list_display = ("original_name", "inspection", "uploaded_by", "uploaded_at")
    list_filter = ("uploaded_at",)
    search_fields = ("original_name", "inspection__report_no", "inspection__internal_lot_no")
    ordering = ("-uploaded_at",)
    readonly_fields = ("uploaded_at",)


# ==================== Inspection Parameter Result Admin ====================

@admin.register(InspectionParameterResult)
class InspectionParameterResultAdmin(admin.ModelAdmin):
    list_display = (
        "inspection", "parameter_name", "standard_value",
        "result_value", "result_numeric", "is_within_spec"
    )
    list_filter = ("is_within_spec", "parameter_master__parameter_set__material_type")
    search_fields = (
        "inspection__report_no", "parameter_name", "result_value"
    )
    ordering = ("inspection", "sequence")
    list_per_page = 25

    fieldsets = (
        ("Inspection", {
            "fields": ("inspection", "parameter_master"),
        }),
        ("Parameter (snapshot taken at inspection time)", {
            "fields": (
                "parameter_code", "parameter_name", "standard_value",
                "parameter_type", "min_value", "max_value", "uom",
                "sequence", "is_mandatory",
            ),
        }),
        ("Results", {
            "fields": ("result_value", "result_numeric", "is_within_spec"),
        }),
        ("Remarks", {
            "fields": ("remarks",),
        }),
        ("Audit", {
            "fields": ("created_by", "created_at", "updated_by", "updated_at"),
            "classes": ("collapse",),
        }),
    )
    # The snapshot is what the report reprints from, so it stays read-only.
    readonly_fields = (
        "parameter_code", "parameter_name", "standard_value", "parameter_type",
        "min_value", "max_value", "uom", "sequence", "is_mandatory",
        "created_by", "created_at", "updated_by", "updated_at",
    )

    def save_model(self, request, obj, form, change):
        if not change:
            obj.created_by = request.user
        obj.updated_by = request.user
        super().save_model(request, obj, form, change)


# ==================== Online Quality Monitoring ====================
from quality_control.models.online_monitoring import (  # noqa: E402
    OnlineQualityRecord,
    OnlineQualityReading,
    OnlineQualityTorque,
    OnlineQualitySpec,
)


class OnlineQualityTorqueInline(admin.TabularInline):
    model = OnlineQualityTorque
    extra = 0


class OnlineQualityReadingInline(admin.TabularInline):
    model = OnlineQualityReading
    extra = 0
    show_change_link = True


@admin.register(OnlineQualityRecord)
class OnlineQualityRecordAdmin(admin.ModelAdmin):
    list_display = ("id", "date", "production_line", "sku", "shift", "batch_no", "status")
    list_filter = ("status", "shift", "production_line")
    search_fields = ("sku", "product_name", "batch_no")
    date_hierarchy = "date"
    inlines = [OnlineQualityReadingInline]


@admin.register(OnlineQualityReading)
class OnlineQualityReadingAdmin(admin.ModelAdmin):
    list_display = ("id", "record", "reading_time", "filler_speed", "ph", "tds")
    inlines = [OnlineQualityTorqueInline]


@admin.register(OnlineQualitySpec)
class OnlineQualitySpecAdmin(admin.ModelAdmin):
    list_display = ("parameter_name", "parameter_key", "company", "specification_text",
                    "min_value", "max_value", "unit", "validation_type", "is_active")
    list_filter = ("validation_type", "company", "is_active")
    search_fields = ("parameter_name", "parameter_key")


# ==================== Testing Procedure (QC Documents) Admin ====================

from .models import (  # noqa: E402
    TestingProcedure,
    TestingProcedureSection,
    TestingProcedureLine,
)


class TestingProcedureSectionInline(admin.TabularInline):
    model = TestingProcedureSection
    extra = 0
    fields = ("sequence", "section_number", "section_key", "title")
    ordering = ("sequence",)
    show_change_link = True


class TestingProcedureLineInline(admin.TabularInline):
    model = TestingProcedureLine
    extra = 0
    fields = ("sequence", "kind", "marker", "text", "interpretation")
    ordering = ("sequence",)


@admin.register(TestingProcedure)
class TestingProcedureAdmin(admin.ModelAdmin):
    list_display = (
        "document_code", "title", "procedure_type", "revision_label",
        "status", "company", "is_active",
    )
    list_filter = ("procedure_type", "status", "company", "is_active")
    search_fields = ("document_code", "title")
    inlines = [TestingProcedureSectionInline]
    readonly_fields = ("created_at", "updated_at")


@admin.register(TestingProcedureSection)
class TestingProcedureSectionAdmin(admin.ModelAdmin):
    list_display = ("procedure", "sequence", "section_number", "section_key", "title")
    list_filter = ("section_key",)
    search_fields = ("title", "procedure__document_code")
    inlines = [TestingProcedureLineInline]


# ==================== QC Record Forms (Documents) Admin ====================

from .models import (  # noqa: E402
    RecordTemplate,
    RecordTemplateSection,
    RecordTemplateParameter,
    QCRecord,
    RecordTimeSlot,
    RecordValue,
)


class RecordTemplateSectionInline(admin.TabularInline):
    model = RecordTemplateSection
    extra = 0
    fields = ("sequence", "title")
    ordering = ("sequence",)
    show_change_link = True


class RecordTemplateParameterInline(admin.TabularInline):
    """Add the rows of a printed form here -- no code change needed."""
    model = RecordTemplateParameter
    extra = 1
    fields = (
        "sequence", "sr_no", "name", "frequency", "specification",
        "unit", "value_type", "min_value", "max_value",
        "allowed_values", "conforming_values",
    )
    ordering = ("sequence",)


@admin.register(RecordTemplate)
class RecordTemplateAdmin(admin.ModelAdmin):
    list_display = ("document_code", "title", "revision_label", "company", "is_active")
    list_filter = ("company", "is_active")
    search_fields = ("document_code", "title")
    inlines = [RecordTemplateSectionInline]


@admin.register(RecordTemplateSection)
class RecordTemplateSectionAdmin(admin.ModelAdmin):
    list_display = ("template", "sequence", "title")
    search_fields = ("title", "template__document_code")
    inlines = [RecordTemplateParameterInline]


class RecordTimeSlotInline(admin.TabularInline):
    model = RecordTimeSlot
    extra = 0
    fields = ("sequence", "slot_time")
    ordering = ("sequence",)


@admin.register(QCRecord)
class QCRecordAdmin(admin.ModelAdmin):
    list_display = ("template", "record_date", "shift", "status", "company", "is_active")
    list_filter = ("status", "template", "company", "is_active")
    date_hierarchy = "record_date"
    inlines = [RecordTimeSlotInline]
    readonly_fields = ("submitted_at", "approved_at", "created_at", "updated_at")


@admin.register(RecordValue)
class RecordValueAdmin(admin.ModelAdmin):
    list_display = ("record", "time_slot", "parameter", "value")
    list_filter = ("parameter__section__template",)
    search_fields = ("value", "parameter__name")


# ==================== QC PDF Document Library Admin ====================

from .models import QCDocumentFile  # noqa: E402


@admin.register(QCDocumentFile)
class QCDocumentFileAdmin(admin.ModelAdmin):
    list_display = (
        "document_code", "title", "revision", "procedure_type", "company", "is_active",
    )
    list_filter = ("procedure_type", "company", "is_active")
    search_fields = ("document_code", "title")
    readonly_fields = ("original_name", "content_type", "file_size", "created_at", "updated_at")


# ==================== QA Procedures Audit Log Admin ====================

from .models import QCDocumentFileAuditLog  # noqa: E402


@admin.register(QCDocumentFileAuditLog)
class QCDocumentFileAuditLogAdmin(admin.ModelAdmin):
    """Read-only on purpose: an audit trail nobody can edit is the point."""

    list_display = (
        "created_at", "user", "action", "document_code", "title", "company",
    )
    list_filter = ("action", "company", "created_at")
    search_fields = ("document_code", "title", "user__full_name", "user__email")
    date_hierarchy = "created_at"
    readonly_fields = tuple(
        field.name for field in QCDocumentFileAuditLog._meta.fields
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
