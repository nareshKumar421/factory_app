# quality_control/serializers.py

from rest_framework import serializers
from quality_control.models.material_type import MaterialType
from quality_control.models.material_type_sap_item import MaterialTypeSAPItem
from quality_control.models.qc_parameter_master import QCParameterMaster
from quality_control.models.qc_print_document import QCPrintDocument
from quality_control.models.material_arrival_slip import MaterialArrivalSlip
from quality_control.models.raw_material_inspection import RawMaterialInspection
from quality_control.models.inspection_parameter_result import InspectionParameterResult
from quality_control.models.inspection_attachment import InspectionAttachment
from quality_control.models.arrival_slip_attachment import ArrivalSlipAttachment
from quality_control.enums import InspectionDecision, InspectionStatus


DECISION_LABELS = {
    InspectionDecision.APPROVED: "Approved",
    InspectionDecision.HOLD: "Hold",
    InspectionDecision.REJECTED: "Rejected",
}


def _user_display(user):
    if not user:
        return None
    return getattr(user, "full_name", None) or getattr(user, "email", None) or str(user)


def _decision_payload(decision, user, decided_at, remarks):
    normalized_decision = decision or None
    return {
        "decision": normalized_decision,
        "label": DECISION_LABELS.get(normalized_decision, "Pending"),
        "by": _user_display(user),
        "decided_at": decided_at,
        "remarks": remarks or "",
    }


# ==================== Material Type Serializers ====================

class MaterialTypeSAPItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = MaterialTypeSAPItem
        fields = ["id", "item_code", "item_name", "is_active", "created_at", "updated_at"]
        read_only_fields = ["id", "is_active", "created_at", "updated_at"]


class MaterialTypeSAPItemInputSerializer(serializers.Serializer):
    item_code = serializers.CharField(max_length=50)
    item_name = serializers.CharField(max_length=200, required=False, allow_blank=True)


class MaterialTypeSerializer(serializers.ModelSerializer):
    sap_items = serializers.SerializerMethodField()

    class Meta:
        model = MaterialType
        fields = [
            "id", "code", "name", "description",
            "company", "sap_items", "is_active", "created_at", "updated_at"
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def get_sap_items(self, obj):
        prefetched_items = getattr(obj, "_prefetched_objects_cache", {}).get("sap_items")
        items = prefetched_items if prefetched_items is not None else obj.sap_items.filter(
            is_active=True
        ).order_by("item_code")
        return MaterialTypeSAPItemSerializer(items, many=True).data


class MaterialTypeCreateSerializer(serializers.ModelSerializer):
    sap_items = MaterialTypeSAPItemInputSerializer(many=True, required=False)
    copy_parameters_from_material_type_id = serializers.IntegerField(
        required=False,
        allow_null=True,
        write_only=True,
    )

    class Meta:
        model = MaterialType
        fields = [
            "code",
            "name",
            "description",
            "sap_items",
            "copy_parameters_from_material_type_id",
        ]


class MaterialTypeSAPItemLinkSerializer(serializers.Serializer):
    material_type_id = serializers.IntegerField()
    item_code = serializers.CharField(max_length=50)
    item_name = serializers.CharField(max_length=200, required=False, allow_blank=True)


# ==================== QC Print Document Serializers ====================

class QCPrintDocumentSerializer(serializers.ModelSerializer):
    document_key_label = serializers.CharField(source="get_document_key_display", read_only=True)

    class Meta:
        model = QCPrintDocument
        fields = [
            "id", "document_key", "document_key_label", "document_id",
            "notes", "is_active", "created_at", "updated_at"
        ]
        read_only_fields = ["id", "document_key_label", "created_at", "updated_at"]


# ==================== QC Parameter Master Serializers ====================

class QCParameterMasterSerializer(serializers.ModelSerializer):
    class Meta:
        model = QCParameterMaster
        fields = [
            "id", "material_type", "parameter_name", "parameter_code",
            "standard_value", "parameter_type", "min_value", "max_value",
            "uom", "sequence", "is_mandatory", "is_active",
            "created_at", "updated_at"
        ]
        read_only_fields = ["id", "created_at", "updated_at"]


class QCParameterMasterCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = QCParameterMaster
        fields = [
            "parameter_name", "parameter_code", "standard_value",
            "parameter_type", "min_value", "max_value", "uom",
            "sequence", "is_mandatory"
        ]


# ==================== Arrival Slip Attachment Serializer ====================

class ArrivalSlipAttachmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = ArrivalSlipAttachment
        fields = ["id", "file", "attachment_type", "uploaded_at"]
        read_only_fields = ["id", "uploaded_at"]


# ==================== Inspection Attachment Serializer ====================

class InspectionAttachmentSerializer(serializers.ModelSerializer):
    uploaded_by_name = serializers.CharField(
        source="uploaded_by.full_name", read_only=True, allow_null=True, default=None
    )

    class Meta:
        model = InspectionAttachment
        fields = ["id", "file", "original_name", "uploaded_by", "uploaded_by_name", "uploaded_at"]
        read_only_fields = ["id", "uploaded_by", "uploaded_by_name", "uploaded_at"]


# ==================== Material Arrival Slip Serializers ====================

class MaterialArrivalSlipSerializer(serializers.ModelSerializer):
    submitted_by_name = serializers.CharField(
        source="submitted_by.full_name", read_only=True
    )
    po_item_code = serializers.CharField(
        source="po_item_receipt.po_item_code", read_only=True
    )
    item_name = serializers.CharField(
        source="po_item_receipt.item_name", read_only=True
    )
    po_receipt_id = serializers.IntegerField(
        source="po_item_receipt.po_receipt.id", read_only=True
    )
    vehicle_entry_id = serializers.IntegerField(
        source="po_item_receipt.po_receipt.vehicle_entry.id", read_only=True
    )
    entry_no = serializers.CharField(
        source="po_item_receipt.po_receipt.vehicle_entry.entry_no", read_only=True
    )
    attachments = ArrivalSlipAttachmentSerializer(many=True, read_only=True)

    class Meta:
        model = MaterialArrivalSlip
        fields = [
            "id", "po_item_receipt", "po_item_code", "item_name",
            "po_receipt_id", "vehicle_entry_id", "entry_no",
            "particulars", "arrival_datetime", "weighing_required",
            "party_name", "billing_qty", "billing_uom",
            "in_time_to_qa", "truck_no_as_per_bill", "commercial_invoice_no",
            "eway_bill_no", "bilty_no", "has_certificate_of_analysis",
            "has_certificate_of_quantity", "status", "is_submitted",
            "submitted_at", "submitted_by", "submitted_by_name", "remarks",
            "attachments", "created_at", "updated_at"
        ]
        read_only_fields = [
            "id", "po_item_code", "item_name", "po_receipt_id",
            "vehicle_entry_id", "entry_no", "status", "is_submitted",
            "submitted_at", "submitted_by", "submitted_by_name",
            "in_time_to_qa", "created_at", "updated_at"
        ]


class MaterialArrivalSlipCreateSerializer(serializers.Serializer):
    """For creation/update by security guard"""
    particulars = serializers.CharField()
    arrival_datetime = serializers.DateTimeField()
    weighing_required = serializers.BooleanField(default=False)
    party_name = serializers.CharField(max_length=200)
    billing_qty = serializers.DecimalField(max_digits=12, decimal_places=3)
    billing_uom = serializers.CharField(max_length=20, required=False, allow_blank=True)
    truck_no_as_per_bill = serializers.CharField(max_length=50)
    commercial_invoice_no = serializers.CharField(max_length=100, required=False, allow_blank=True)
    eway_bill_no = serializers.CharField(max_length=100, required=False, allow_blank=True)
    bilty_no = serializers.CharField(max_length=100, required=False, allow_blank=True)
    has_certificate_of_analysis = serializers.BooleanField(default=False)
    has_certificate_of_quantity = serializers.BooleanField(default=False)
    remarks = serializers.CharField(required=False, allow_blank=True)


# ==================== Inspection Parameter Result Serializers ====================

class InspectionParameterResultSerializer(serializers.ModelSerializer):
    parameter_code = serializers.CharField(
        source="parameter_master.parameter_code", read_only=True
    )
    parameter_type = serializers.CharField(
        source="parameter_master.parameter_type", read_only=True
    )
    min_value = serializers.DecimalField(
        source="parameter_master.min_value", read_only=True,
        max_digits=12, decimal_places=4
    )
    max_value = serializers.DecimalField(
        source="parameter_master.max_value", read_only=True,
        max_digits=12, decimal_places=4
    )
    uom = serializers.CharField(
        source="parameter_master.uom", read_only=True
    )

    class Meta:
        model = InspectionParameterResult
        fields = [
            "id", "parameter_master", "parameter_code", "parameter_name",
            "standard_value", "parameter_type", "min_value", "max_value",
            "uom", "result_value", "result_numeric", "is_within_spec", "remarks"
        ]
        read_only_fields = [
            "id", "parameter_code", "parameter_name", "standard_value",
            "parameter_type", "min_value", "max_value", "uom"
        ]


class InspectionParameterResultCreateSerializer(serializers.Serializer):
    """For bulk creating/updating parameter results"""
    parameter_master_id = serializers.IntegerField()
    result_value = serializers.CharField(max_length=200, required=False, allow_blank=True)
    result_numeric = serializers.DecimalField(
        max_digits=12, decimal_places=4, required=False, allow_null=True
    )
    is_within_spec = serializers.BooleanField(required=False, allow_null=True)
    remarks = serializers.CharField(required=False, allow_blank=True)


# ==================== Inspection List Item Serializer ====================

class InspectionListItemSerializer(serializers.ModelSerializer):
    """Light serializer for all list endpoints. Source: MaterialArrivalSlip."""
    arrival_slip_id = serializers.IntegerField(source="id")
    inspection_id = serializers.SerializerMethodField()
    entry_no = serializers.CharField(
        source="po_item_receipt.po_receipt.vehicle_entry.entry_no", read_only=True
    )
    report_no = serializers.SerializerMethodField()
    internal_lot_no = serializers.SerializerMethodField()
    item_name = serializers.CharField(
        source="po_item_receipt.item_name", read_only=True
    )
    po_item_code = serializers.CharField(
        source="po_item_receipt.po_item_code", read_only=True
    )
    workflow_status = serializers.SerializerMethodField()
    final_status = serializers.SerializerMethodField()
    effective_final_status = serializers.SerializerMethodField()
    chemist_decision = serializers.SerializerMethodField()
    manager_decision = serializers.SerializerMethodField()
    qc_stage = serializers.SerializerMethodField()
    qc_decision = serializers.SerializerMethodField()
    factory_head_decision = serializers.SerializerMethodField()
    factory_head_decided_at = serializers.SerializerMethodField()
    rejected_qc_return_entry_id = serializers.SerializerMethodField()
    rejected_qc_return_entry_no = serializers.SerializerMethodField()
    material_type_name = serializers.SerializerMethodField()

    class Meta:
        model = MaterialArrivalSlip
        fields = [
            "arrival_slip_id", "inspection_id",
            "entry_no", "report_no", "internal_lot_no",
            "po_item_code", "item_name", "party_name", "billing_qty", "billing_uom",
            "workflow_status", "final_status", "effective_final_status",
            "chemist_decision", "manager_decision", "qc_stage", "qc_decision",
            "factory_head_decision", "factory_head_decided_at", "material_type_name",
            "rejected_qc_return_entry_id", "rejected_qc_return_entry_no",
            "created_at", "submitted_at",
        ]

    def _get_inspection(self, obj):
        try:
            return obj.inspection
        except RawMaterialInspection.DoesNotExist:
            return None

    def get_inspection_id(self, obj):
        insp = self._get_inspection(obj)
        return insp.id if insp else None

    def get_report_no(self, obj):
        insp = self._get_inspection(obj)
        return insp.report_no if insp else None

    def get_internal_lot_no(self, obj):
        insp = self._get_inspection(obj)
        return insp.internal_lot_no if insp else None

    def get_workflow_status(self, obj):
        insp = self._get_inspection(obj)
        return insp.workflow_status if insp else "NOT_STARTED"

    def get_final_status(self, obj):
        insp = self._get_inspection(obj)
        return insp.final_status if insp else None

    def get_effective_final_status(self, obj):
        insp = self._get_inspection(obj)
        return insp.effective_final_status if insp else None

    def get_chemist_decision(self, obj):
        insp = self._get_inspection(obj)
        if not insp:
            return _decision_payload(None, None, None, "")
        return _decision_payload(
            insp.qa_chemist_decision,
            insp.qa_chemist,
            insp.qa_chemist_approved_at,
            insp.qa_chemist_remarks,
        )

    def get_manager_decision(self, obj):
        insp = self._get_inspection(obj)
        if not insp:
            return _decision_payload(None, None, None, "")
        return _decision_payload(
            insp.manager_decision,
            insp.qam,
            insp.qam_approved_at,
            insp.qam_remarks,
        )

    def get_qc_stage(self, obj):
        insp = self._get_inspection(obj)
        return insp.qc_stage if insp else "NOT_STARTED"

    def get_qc_decision(self, obj):
        insp = self._get_inspection(obj)
        if not insp:
            return None
        return insp.manager_decision or None

    def get_factory_head_decision(self, obj):
        insp = self._get_inspection(obj)
        return insp.factory_head_decision if insp else ""

    def get_factory_head_decided_at(self, obj):
        insp = self._get_inspection(obj)
        return insp.factory_head_decided_at if insp else None

    def get_rejected_qc_return_entry_id(self, obj):
        insp = self._get_inspection(obj)
        entry = insp.rejected_qc_return_entry if insp else None
        return entry.id if entry else None

    def get_rejected_qc_return_entry_no(self, obj):
        insp = self._get_inspection(obj)
        entry = insp.rejected_qc_return_entry if insp else None
        return entry.entry_no if entry else None

    def get_material_type_name(self, obj):
        insp = self._get_inspection(obj)
        if insp and insp.material_type:
            return insp.material_type.name
        return None


# ==================== Raw Material Inspection Serializers ====================

class RawMaterialInspectionSerializer(serializers.ModelSerializer):
    parameter_results = serializers.SerializerMethodField()
    print_document_id = serializers.SerializerMethodField()
    attachments = ArrivalSlipAttachmentSerializer(
        source="arrival_slip.attachments", many=True, read_only=True
    )
    qc_attachments = InspectionAttachmentSerializer(many=True, read_only=True)
    qa_chemist_name = serializers.CharField(source="qa_chemist.full_name", read_only=True, allow_null=True, default=None)
    qam_name = serializers.CharField(source="qam.full_name", read_only=True, allow_null=True, default=None)
    rejected_by_name = serializers.CharField(source="rejected_by.full_name", read_only=True, allow_null=True, default=None)
    factory_head_name = serializers.CharField(source="factory_head.full_name", read_only=True, allow_null=True, default=None)
    effective_final_status = serializers.CharField(read_only=True)
    chemist_decision = serializers.SerializerMethodField()
    manager_decision = serializers.SerializerMethodField()
    qc_stage = serializers.CharField(read_only=True)
    qc_decision = serializers.SerializerMethodField()
    rejected_qc_return_entry_id = serializers.SerializerMethodField()
    rejected_qc_return_entry_no = serializers.SerializerMethodField()
    material_type_name = serializers.CharField(source="material_type.name", read_only=True, allow_null=True, default=None)

    # Arrival slip info
    arrival_slip_id = serializers.IntegerField(source="arrival_slip.id", read_only=True)
    arrival_slip_status = serializers.CharField(source="arrival_slip.status", read_only=True)

    # PO item info via arrival slip
    po_item_receipt_id = serializers.IntegerField(
        source="arrival_slip.po_item_receipt.id", read_only=True
    )
    po_item_code = serializers.CharField(
        source="arrival_slip.po_item_receipt.po_item_code", read_only=True
    )
    item_name = serializers.CharField(
        source="arrival_slip.po_item_receipt.item_name", read_only=True
    )

    # Vehicle entry info via chain
    vehicle_entry_id = serializers.SerializerMethodField()
    entry_no = serializers.SerializerMethodField()

    class Meta:
        model = RawMaterialInspection
        fields = [
            "id", "arrival_slip", "arrival_slip_id", "arrival_slip_status",
            "po_item_receipt_id", "po_item_code", "item_name",
            "vehicle_entry_id", "entry_no",
            "report_no", "internal_lot_no", "internal_report_no", "inspection_date",
            "description_of_material", "sap_code",
            "supplier_name", "manufacturer_name", "supplier_batch_lot_no",
            "unit_packing", "purchase_order_no", "invoice_bill_no",
            "vehicle_no", "material_type", "material_type_name",
            "final_status", "qa_chemist", "qa_chemist_name",
            "qa_chemist_approved_at", "qa_chemist_decision", "qa_chemist_remarks",
            "qam", "qam_name", "qam_approved_at", "qam_remarks",
            "qam_decision", "chemist_decision", "manager_decision",
            "qc_stage", "qc_decision",
            "rejected_by", "rejected_by_name", "rejected_at",
            "factory_head", "factory_head_name", "factory_head_decision",
            "factory_head_remarks", "factory_head_decided_at",
            "effective_final_status", "rejected_qc_return_entry_id",
            "rejected_qc_return_entry_no",
            "workflow_status", "is_locked", "remarks",
            "parameter_results", "attachments", "qc_attachments", "print_document_id",
            "created_at", "updated_at"
        ]
        read_only_fields = [
            "id", "arrival_slip_id", "arrival_slip_status",
            "po_item_receipt_id", "po_item_code", "item_name",
            "vehicle_entry_id", "entry_no",
            "report_no", "internal_lot_no",
            "qa_chemist", "qa_chemist_name", "qa_chemist_approved_at",
            "qa_chemist_decision", "qam", "qam_name", "qam_approved_at",
            "qam_decision", "chemist_decision", "manager_decision",
            "qc_stage", "qc_decision",
            "rejected_by", "rejected_by_name", "rejected_at",
            "factory_head", "factory_head_name", "factory_head_decision",
            "factory_head_remarks", "factory_head_decided_at",
            "effective_final_status", "rejected_qc_return_entry_id",
            "rejected_qc_return_entry_no",
            "workflow_status", "is_locked", "qc_attachments", "created_at", "updated_at"
        ]

    def get_vehicle_entry_id(self, obj):
        try:
            return obj.arrival_slip.po_item_receipt.po_receipt.vehicle_entry.id
        except AttributeError:
            return None

    def get_entry_no(self, obj):
        try:
            return obj.arrival_slip.po_item_receipt.po_receipt.vehicle_entry.entry_no
        except AttributeError:
            return None

    def get_rejected_qc_return_entry_id(self, obj):
        entry = obj.rejected_qc_return_entry
        return entry.id if entry else None

    def get_rejected_qc_return_entry_no(self, obj):
        entry = obj.rejected_qc_return_entry
        return entry.entry_no if entry else None

    def get_chemist_decision(self, obj):
        return _decision_payload(
            obj.qa_chemist_decision,
            obj.qa_chemist,
            obj.qa_chemist_approved_at,
            obj.qa_chemist_remarks,
        )

    def get_manager_decision(self, obj):
        return _decision_payload(
            obj.manager_decision,
            obj.qam,
            obj.qam_approved_at,
            obj.qam_remarks,
        )

    def get_qc_decision(self, obj):
        return obj.manager_decision or None

    def get_parameter_results(self, obj):
        results = obj.parameter_results.filter(is_active=True)
        return InspectionParameterResultSerializer(results, many=True).data

    def get_print_document_id(self, obj):
        request = self.context.get("request")
        request_company = getattr(getattr(request, "company", None), "company", None)
        company = request_company or getattr(getattr(obj, "material_type", None), "company", None)
        if not company:
            return ""

        document = QCPrintDocument.objects.filter(
            company=company,
            document_key=QCPrintDocument.DocumentKey.RAW_MATERIAL_INSPECTION,
            is_active=True,
        ).only("document_id").first()
        return document.document_id if document else ""


class RawMaterialInspectionCreateSerializer(serializers.Serializer):
    """For creating inspection by QA"""
    inspection_date = serializers.DateField()
    description_of_material = serializers.CharField()
    sap_code = serializers.CharField(max_length=50, required=False, allow_blank=True)
    supplier_name = serializers.CharField(max_length=200)
    manufacturer_name = serializers.CharField(max_length=200, required=False, allow_blank=True)
    supplier_batch_lot_no = serializers.CharField(max_length=100, required=False, allow_blank=True)
    unit_packing = serializers.CharField(max_length=100, required=False, allow_blank=True)
    purchase_order_no = serializers.CharField(max_length=50, required=False, allow_blank=True)
    internal_report_no = serializers.CharField(max_length=100, required=False, allow_blank=True)
    # Report number and internal lot number are manually entered by QC (no auto-generation).
    report_no = serializers.CharField(max_length=50)
    internal_lot_no = serializers.CharField(max_length=50)
    invoice_bill_no = serializers.CharField(max_length=100, required=False, allow_blank=True)
    vehicle_no = serializers.CharField(max_length=50, required=False, allow_blank=True)
    material_type_id = serializers.IntegerField(required=False, allow_null=True)
    remarks = serializers.CharField(required=False, allow_blank=True)


class ApprovalSerializer(serializers.Serializer):
    """For approval actions"""
    remarks = serializers.CharField(required=False, allow_blank=True)
    decision = serializers.ChoiceField(
        choices=["APPROVED", "HOLD", "REJECTED"],
        required=False
    )
    final_status = serializers.ChoiceField(
        choices=["ACCEPTED", "REJECTED", "HOLD"],
        required=False
    )

    def validate(self, attrs):
        if not attrs.get("decision") and attrs.get("final_status"):
            final_to_decision = {
                InspectionStatus.ACCEPTED: InspectionDecision.APPROVED,
                InspectionStatus.REJECTED: InspectionDecision.REJECTED,
                InspectionStatus.HOLD: InspectionDecision.HOLD,
            }
            attrs["decision"] = final_to_decision.get(attrs["final_status"])
        return attrs


class FactoryHeadDecisionSerializer(serializers.Serializer):
    """For factory head decision after QA rejection."""
    decision = serializers.ChoiceField(
        choices=[
            "ACCEPT_QC_OVERRIDE",
            "RETURN_TO_VENDOR",
            "HOLD_FOR_REVIEW",
            "SEND_FOR_RECHECK",
            "SCRAP",
        ]
    )
    remarks = serializers.CharField(required=False, allow_blank=True)


class ParameterResultBulkUpdateSerializer(serializers.Serializer):
    """For bulk updating parameter results"""
    results = InspectionParameterResultCreateSerializer(many=True)


# ==================== Production QC Serializers ====================

from quality_control.models.production_qc_session import ProductionQCSession
from quality_control.models.production_qc_result import ProductionQCResult


class ProductionQCResultSerializer(serializers.ModelSerializer):
    """Read serializer for production QC parameter results."""
    parameter_code = serializers.CharField(
        source="parameter_master.parameter_code", read_only=True
    )
    parameter_type = serializers.CharField(
        source="parameter_master.parameter_type", read_only=True
    )
    min_value = serializers.DecimalField(
        source="parameter_master.min_value", read_only=True,
        max_digits=12, decimal_places=4
    )
    max_value = serializers.DecimalField(
        source="parameter_master.max_value", read_only=True,
        max_digits=12, decimal_places=4
    )
    uom = serializers.CharField(
        source="parameter_master.uom", read_only=True
    )
    is_mandatory = serializers.BooleanField(
        source="parameter_master.is_mandatory", read_only=True
    )

    class Meta:
        model = ProductionQCResult
        fields = [
            "id", "parameter_master", "parameter_code", "parameter_name",
            "standard_value", "parameter_type", "min_value", "max_value",
            "uom", "is_mandatory", "result_value", "result_numeric",
            "is_within_spec", "remarks"
        ]
        read_only_fields = [
            "id", "parameter_code", "parameter_name", "standard_value",
            "parameter_type", "min_value", "max_value", "uom", "is_mandatory"
        ]


class ProductionQCResultCreateSerializer(serializers.Serializer):
    """For bulk creating/updating parameter results in a session."""
    parameter_master_id = serializers.IntegerField()
    result_value = serializers.CharField(max_length=200, required=False, allow_blank=True)
    result_numeric = serializers.DecimalField(
        max_digits=12, decimal_places=4, required=False, allow_null=True
    )
    is_within_spec = serializers.BooleanField(required=False, allow_null=True)
    remarks = serializers.CharField(required=False, allow_blank=True)


class ProductionQCSessionSerializer(serializers.ModelSerializer):
    """Read serializer for production QC sessions."""
    results = ProductionQCResultSerializer(many=True, read_only=True)
    checked_by_name = serializers.CharField(
        source="checked_by.full_name", read_only=True,
        allow_null=True, default=None
    )
    submitted_by_name = serializers.CharField(
        source="submitted_by.full_name", read_only=True,
        allow_null=True, default=None
    )
    approved_by_name = serializers.CharField(
        source="approved_by.full_name", read_only=True,
        allow_null=True, default=None
    )
    rejected_by_name = serializers.CharField(
        source="rejected_by.full_name", read_only=True,
        allow_null=True, default=None
    )
    material_type_name = serializers.CharField(
        source="material_type.name", read_only=True,
        allow_null=True, default=None
    )
    material_type_code = serializers.CharField(
        source="material_type.code", read_only=True,
        allow_null=True, default=None
    )
    run_number = serializers.IntegerField(
        source="production_run.run_number", read_only=True
    )

    class Meta:
        model = ProductionQCSession
        fields = [
            "id", "production_run", "run_number",
            "material_type", "material_type_name", "material_type_code",
            "session_number", "session_type",
            "checked_at", "checked_by", "checked_by_name",
            "overall_result", "workflow_status",
            "submitted_by", "submitted_by_name", "submitted_at",
            "approved_by", "approved_by_name", "approved_at",
            "approval_remarks",
            "rejected_by", "rejected_by_name", "rejected_at",
            "rejection_remarks",
            "remarks", "results",
            "created_at", "updated_at",
        ]
        read_only_fields = [
            "id", "run_number", "material_type_name", "material_type_code",
            "session_number", "overall_result", "workflow_status",
            "checked_by", "checked_by_name",
            "submitted_by", "submitted_by_name", "submitted_at",
            "approved_by", "approved_by_name", "approved_at",
            "approval_remarks",
            "rejected_by", "rejected_by_name", "rejected_at",
            "rejection_remarks",
            "created_at", "updated_at",
        ]


class ProductionQCSessionListSerializer(serializers.ModelSerializer):
    """Lightweight serializer for list views."""
    checked_by_name = serializers.CharField(
        source="checked_by.full_name", read_only=True,
        allow_null=True, default=None
    )
    material_type_name = serializers.CharField(
        source="material_type.name", read_only=True,
        allow_null=True, default=None
    )
    run_number = serializers.IntegerField(
        source="production_run.run_number", read_only=True
    )
    run_date = serializers.DateField(
        source="production_run.date", read_only=True
    )
    product = serializers.CharField(
        source="production_run.product", read_only=True
    )
    line_name = serializers.CharField(
        source="production_run.line.name", read_only=True
    )
    pass_count = serializers.SerializerMethodField()
    fail_count = serializers.SerializerMethodField()
    total_params = serializers.SerializerMethodField()

    class Meta:
        model = ProductionQCSession
        fields = [
            "id", "production_run", "run_number", "run_date", "product", "line_name",
            "material_type", "material_type_name",
            "session_number", "session_type",
            "checked_at", "checked_by_name",
            "overall_result", "workflow_status",
            "pass_count", "fail_count", "total_params",
            "created_at",
        ]

    def get_pass_count(self, obj):
        return obj.results.filter(is_within_spec=True, is_active=True).count()

    def get_fail_count(self, obj):
        return obj.results.filter(is_within_spec=False, is_active=True).count()

    def get_total_params(self, obj):
        return obj.results.filter(is_active=True).count()


class ProductionQCSessionCreateSerializer(serializers.Serializer):
    """For creating a new QC session."""
    material_type_id = serializers.IntegerField()
    session_type = serializers.ChoiceField(
        choices=["IN_PROCESS", "FINAL"], default="IN_PROCESS"
    )
    checked_at = serializers.DateTimeField()
    remarks = serializers.CharField(required=False, allow_blank=True, default="")


class ProductionQCResultBulkUpdateSerializer(serializers.Serializer):
    """For bulk updating parameter results in a session."""
    results = ProductionQCResultCreateSerializer(many=True)


class ProductionQCSubmitSerializer(serializers.Serializer):
    """For submitting/finalizing a QC session with PASS/FAIL result."""
    overall_result = serializers.ChoiceField(choices=["PASS", "FAIL"])


class ProductionQCApprovalSerializer(serializers.Serializer):
    """For approving a submitted production QC session."""
    overall_result = serializers.ChoiceField(choices=["PASS", "FAIL"], required=False)
    remarks = serializers.CharField(required=False, allow_blank=True, default="")


class ProductionQCRejectSerializer(serializers.Serializer):
    """For rejecting a submitted production QC session."""
    remarks = serializers.CharField(required=False, allow_blank=True, default="")
