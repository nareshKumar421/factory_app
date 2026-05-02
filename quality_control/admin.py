# quality_control/admin.py
"""
Enhanced Quality Control Admin Configuration for Factory Jivo Wellness
"""

from django.contrib import admin
from django.utils.html import format_html
from django.urls import reverse
from .models import (
    MaterialType,
    QCParameterMaster,
    MaterialArrivalSlip,
    RawMaterialInspection,
    InspectionParameterResult,
)
from .enums import InspectionStatus, InspectionWorkflowStatus


# ==================== Material Type Admin ====================

@admin.register(MaterialType)
class MaterialTypeAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "company", "is_active", "created_at")
    list_filter = ("company", "is_active", "created_at")
    search_fields = ("code", "name", "description")
    ordering = ("code",)
    list_per_page = 25

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


# ==================== QC Parameter Master Admin ====================

class QCParameterMasterInline(admin.TabularInline):
    model = QCParameterMaster
    extra = 1
    fields = (
        "parameter_code", "parameter_name", "standard_value",
        "parameter_type", "min_value", "max_value", "uom",
        "sequence", "is_mandatory", "is_active"
    )


@admin.register(QCParameterMaster)
class QCParameterMasterAdmin(admin.ModelAdmin):
    list_display = (
        "parameter_code", "parameter_name", "material_type",
        "standard_value", "parameter_type", "sequence",
        "is_mandatory", "is_active"
    )
    list_filter = ("material_type", "parameter_type", "is_mandatory", "is_active")
    search_fields = ("parameter_code", "parameter_name", "material_type__name")
    ordering = ("material_type", "sequence")
    list_per_page = 25

    fieldsets = (
        ("Material Type", {
            "fields": ("material_type",),
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
    inlines = [InspectionParameterResultInline]

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
        ("Factory Head Decision", {
            "fields": (
                "factory_head", "factory_head_decision",
                "factory_head_decided_at", "factory_head_remarks",
            ),
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
        "factory_head", "factory_head_decided_at",
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


# ==================== Inspection Parameter Result Admin ====================

@admin.register(InspectionParameterResult)
class InspectionParameterResultAdmin(admin.ModelAdmin):
    list_display = (
        "inspection", "parameter_name", "standard_value",
        "result_value", "result_numeric", "is_within_spec"
    )
    list_filter = ("is_within_spec", "parameter_master__material_type")
    search_fields = (
        "inspection__report_no", "parameter_name", "result_value"
    )
    ordering = ("inspection", "parameter_master__sequence")
    list_per_page = 25

    fieldsets = (
        ("Inspection", {
            "fields": ("inspection", "parameter_master"),
        }),
        ("Parameter", {
            "fields": ("parameter_name", "standard_value"),
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
    readonly_fields = ("parameter_name", "standard_value", "created_by", "created_at", "updated_by", "updated_at")

    def save_model(self, request, obj, form, change):
        if not change:
            obj.created_by = request.user
        obj.updated_by = request.user
        super().save_model(request, obj, form, change)
