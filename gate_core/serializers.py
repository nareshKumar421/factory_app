from rest_framework import serializers

from document_control import services as document_services
from document_control.serializers import ControlledDocumentSerializerMixin
from document_control.utils import count_pdf_pages

from .models import (
    BSTGateIn,
    BSTGateInItem,
    BSTGateOut,
    BSTGateOutItem,
    BSTGateReturn,
    EmptyVehicleGateIn,
    EmptyVehicleGateInItem,
    EmptyVehicleGateOut,
    GateAttachment,
    JobWorkGateIn,
    JobWorkGateInItem,
    RejectedQCReturnEntry,
    RejectedQCReturnItem,
    SalesDispatchGateOut,
    SalesDispatchGateOutItem,
    UnitChoice,
)

class UnitChoiceSerializer(serializers.ModelSerializer):
    class Meta:
        model = UnitChoice
        fields = ['id', 'name']


class GateAttachmentSerializer(
    ControlledDocumentSerializerMixin, serializers.ModelSerializer
):
    class Meta:
        model = GateAttachment
        fields = [
            'id', 'file', 'uploaded_at',
            *ControlledDocumentSerializerMixin.DOCUMENT_FIELDS,
        ]

    def create(self, validated_data):
        # Every gate PDF is assigned the next controlled-document code before it
        # is persisted (a save without a code is refused at the model layer).
        request = self.context.get('request')
        upload = validated_data.get('file')
        document = document_services.allocate_for_module(
            'GATE',
            total_pages=count_pdf_pages(upload) if upload else 1,
            source_reference=getattr(upload, 'name', '') or '',
            created_by=getattr(request, 'user', None) if request else None,
        )
        validated_data['document_code'] = document
        return super().create(validated_data)


class SAPStockTransferLineSerializer(serializers.Serializer):
    line_num = serializers.IntegerField()
    item_code = serializers.CharField()
    item_name = serializers.CharField()
    quantity = serializers.FloatField()
    uom = serializers.CharField()
    from_warehouse = serializers.CharField()
    to_warehouse = serializers.CharField()


class SAPStockTransferSerializer(serializers.Serializer):
    doc_entry = serializers.IntegerField()
    doc_num = serializers.CharField()
    doc_date = serializers.DateField(allow_null=True)
    tax_date = serializers.DateField(allow_null=True)
    doc_status = serializers.CharField()
    from_warehouse = serializers.CharField()
    to_warehouse = serializers.CharField()
    comments = serializers.CharField(allow_blank=True)
    reference = serializers.CharField(allow_blank=True)
    branch_id = serializers.IntegerField(allow_null=True)
    line_count = serializers.IntegerField()
    total_quantity = serializers.FloatField()
    lines = SAPStockTransferLineSerializer(many=True, required=False)


class SAPGRPOLineSerializer(serializers.Serializer):
    line_num = serializers.IntegerField()
    item_code = serializers.CharField()
    item_name = serializers.CharField()
    quantity = serializers.FloatField()
    uom = serializers.CharField()
    warehouse_code = serializers.CharField()
    base_type = serializers.IntegerField(allow_null=True)
    base_entry = serializers.IntegerField(allow_null=True)
    base_line = serializers.IntegerField(allow_null=True)


class SAPGRPOSerializer(serializers.Serializer):
    doc_entry = serializers.IntegerField()
    doc_num = serializers.CharField()
    doc_date = serializers.DateField(allow_null=True)
    doc_time = serializers.TimeField(allow_null=True)
    tax_date = serializers.DateField(allow_null=True)
    doc_status = serializers.CharField()
    supplier_code = serializers.CharField()
    supplier_name = serializers.CharField()
    reference = serializers.CharField(allow_blank=True)
    comments = serializers.CharField(allow_blank=True)
    branch_id = serializers.IntegerField(allow_null=True)
    line_count = serializers.IntegerField()
    total_quantity = serializers.FloatField()
    lines = SAPGRPOLineSerializer(many=True, required=False)


class SAPProductionOrderComponentSerializer(serializers.Serializer):
    line_num = serializers.IntegerField()
    item_code = serializers.CharField(allow_blank=True)
    item_name = serializers.CharField(allow_blank=True)
    planned_qty = serializers.FloatField()
    issued_qty = serializers.FloatField()
    warehouse = serializers.CharField(allow_blank=True, allow_null=True)
    uom = serializers.CharField(allow_blank=True, allow_null=True)


class SAPProductionOrderSerializer(serializers.Serializer):
    doc_entry = serializers.IntegerField()
    doc_num = serializers.CharField()
    item_code = serializers.CharField(allow_blank=True)
    item_name = serializers.CharField(allow_blank=True)
    planned_qty = serializers.FloatField()
    completed_qty = serializers.FloatField()
    rejected_qty = serializers.FloatField()
    remaining_qty = serializers.FloatField()
    start_date = serializers.DateField(allow_null=True)
    due_date = serializers.DateField(allow_null=True)
    warehouse = serializers.CharField(allow_blank=True, allow_null=True)
    status = serializers.CharField(allow_blank=True)
    components = SAPProductionOrderComponentSerializer(many=True, required=False)


class BSTGateOutItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = BSTGateOutItem
        fields = [
            "id", "line_num", "item_code", "item_name", "quantity",
            "actual_quantity", "uom", "from_warehouse", "to_warehouse",
        ]
        read_only_fields = fields


class BSTGateInItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = BSTGateInItem
        fields = [
            "id", "line_num", "item_code", "item_name", "quantity",
            "actual_quantity", "receiving_quantity", "uom",
            "from_warehouse", "to_warehouse",
        ]
        read_only_fields = fields


class BSTGateOutSerializer(serializers.ModelSerializer):
    source_type = serializers.SerializerMethodField()
    vehicle_entry_no = serializers.CharField(source="vehicle_entry.entry_no", read_only=True)
    vehicle_entry_status = serializers.CharField(source="vehicle_entry.status", read_only=True)
    empty_vehicle_gate_in_entry_no = serializers.CharField(
        source="empty_vehicle_gate_in.entry_no",
        read_only=True,
    )
    empty_vehicle_gate_in_date = serializers.DateField(
        source="empty_vehicle_gate_in.gate_in_date",
        read_only=True,
    )
    empty_vehicle_in_time = serializers.TimeField(
        source="empty_vehicle_gate_in.in_time",
        read_only=True,
    )
    vehicle_number = serializers.CharField(source="vehicle.vehicle_number", read_only=True)
    vehicle_type = serializers.CharField(source="vehicle.vehicle_type.name", read_only=True, allow_null=True)
    transporter_name = serializers.CharField(source="vehicle.transporter.name", read_only=True, allow_null=True)
    driver_name = serializers.CharField(source="driver.name", read_only=True)
    driver_mobile = serializers.CharField(source="driver.mobile_no", read_only=True)
    items = BSTGateOutItemSerializer(many=True, read_only=True)

    class Meta:
        model = BSTGateOut
        fields = [
            "id", "source_type", "entry_no", "company", "vehicle_entry", "vehicle_entry_no",
            "vehicle_entry_status", "empty_vehicle_gate_in",
            "empty_vehicle_gate_in_entry_no", "empty_vehicle_gate_in_date",
            "empty_vehicle_in_time", "vehicle", "vehicle_number", "vehicle_type",
            "transporter_name", "driver", "driver_name", "driver_mobile",
            "sap_doc_entry", "sap_doc_num", "sap_doc_date", "sap_from_warehouse",
            "sap_to_warehouse", "sap_reference", "sap_comments", "gate_out_date",
            "out_time", "security_name", "remarks", "status", "cancel_reason",
            "cancelled_at", "cancelled_by", "items", "created_at", "updated_at",
        ]
        read_only_fields = fields

    def get_source_type(self, obj):
        return "LEGACY_BST_OUT"


class SalesDispatchBSTEligibleItemSerializer(serializers.ModelSerializer):
    actual_quantity = serializers.DecimalField(
        source="quantity",
        max_digits=18,
        decimal_places=3,
        read_only=True,
    )

    class Meta:
        model = SalesDispatchGateOutItem
        fields = [
            "id", "line_num", "item_code", "item_name", "quantity",
            "actual_quantity", "uom", "from_warehouse", "to_warehouse",
        ]
        read_only_fields = fields


class SalesDispatchBSTEligibleOutSerializer(serializers.ModelSerializer):
    source_type = serializers.SerializerMethodField()
    vehicle_entry_no = serializers.CharField(source="vehicle_entry.entry_no", read_only=True)
    vehicle_entry_status = serializers.CharField(source="vehicle_entry.status", read_only=True)
    empty_vehicle_gate_in = serializers.SerializerMethodField()
    empty_vehicle_gate_in_entry_no = serializers.SerializerMethodField()
    empty_vehicle_gate_in_date = serializers.SerializerMethodField()
    empty_vehicle_in_time = serializers.SerializerMethodField()
    vehicle_number = serializers.SerializerMethodField()
    vehicle_type = serializers.CharField(source="vehicle.vehicle_type.name", read_only=True, allow_null=True)
    transporter_name = serializers.SerializerMethodField()
    driver_name = serializers.SerializerMethodField()
    driver_mobile = serializers.SerializerMethodField()
    sap_from_warehouse = serializers.CharField(source="from_warehouse", read_only=True)
    sap_to_warehouse = serializers.CharField(source="to_warehouse", read_only=True)
    status = serializers.SerializerMethodField()
    cancel_reason = serializers.SerializerMethodField()
    cancelled_at = serializers.SerializerMethodField()
    cancelled_by = serializers.SerializerMethodField()
    items = SalesDispatchBSTEligibleItemSerializer(many=True, read_only=True)

    class Meta:
        model = SalesDispatchGateOut
        fields = [
            "id", "source_type", "entry_no", "company", "vehicle_entry", "vehicle_entry_no",
            "vehicle_entry_status", "empty_vehicle_gate_in",
            "empty_vehicle_gate_in_entry_no", "empty_vehicle_gate_in_date",
            "empty_vehicle_in_time", "vehicle", "vehicle_number", "vehicle_type",
            "transporter_name", "driver", "driver_name", "driver_mobile",
            "sap_doc_entry", "sap_doc_num", "sap_doc_date", "sap_from_warehouse",
            "sap_to_warehouse", "sap_reference", "sap_comments", "gate_out_date",
            "out_time", "security_name", "remarks", "status", "cancel_reason",
            "cancelled_at", "cancelled_by", "items", "created_at", "updated_at",
        ]
        read_only_fields = fields

    def get_source_type(self, obj):
        return "DOCKING_STOCK_TRANSFER"

    def get_empty_vehicle_gate_in(self, obj):
        return None

    def get_empty_vehicle_gate_in_entry_no(self, obj):
        return ""

    def get_empty_vehicle_gate_in_date(self, obj):
        return None

    def get_empty_vehicle_in_time(self, obj):
        return None

    def get_vehicle_number(self, obj):
        return obj.vehicle_no or getattr(obj.vehicle, "vehicle_number", "")

    def get_transporter_name(self, obj):
        return obj.transporter_name or getattr(obj.transporter, "name", "")

    def get_driver_name(self, obj):
        return obj.driver_name or getattr(obj.driver, "name", "")

    def get_driver_mobile(self, obj):
        return obj.driver_mobile_no or getattr(obj.driver, "mobile_no", "")

    def get_status(self, obj):
        return "COMPLETED"

    def get_cancel_reason(self, obj):
        return ""

    def get_cancelled_at(self, obj):
        return None

    def get_cancelled_by(self, obj):
        return None


class BSTGateOutCreateSerializer(serializers.Serializer):
    empty_vehicle_gate_in_id = serializers.IntegerField()
    sap_doc_entry = serializers.IntegerField()
    gate_out_date = serializers.DateField()
    out_time = serializers.TimeField()
    security_name = serializers.CharField(required=False, allow_blank=True, default="")
    remarks = serializers.CharField(required=False, allow_blank=True, default="")


class BSTGateOutCancelSerializer(serializers.Serializer):
    cancel_reason = serializers.CharField(required=True, allow_blank=False, trim_whitespace=True)


class JobWorkGateInItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = JobWorkGateInItem
        fields = [
            "id", "line_num", "item_code", "item_name", "quantity",
            "uom", "warehouse_code", "base_type", "base_entry", "base_line",
        ]
        read_only_fields = fields


class JobWorkGateInSerializer(serializers.ModelSerializer):
    vehicle_entry_no = serializers.CharField(source="vehicle_entry.entry_no", read_only=True)
    vehicle_entry_status = serializers.CharField(source="vehicle_entry.status", read_only=True)
    vehicle_number = serializers.CharField(source="vehicle.vehicle_number", read_only=True)
    vehicle_type = serializers.CharField(source="vehicle.vehicle_type.name", read_only=True, allow_null=True)
    transporter_name = serializers.CharField(source="vehicle.transporter.name", read_only=True, allow_null=True)
    driver_name = serializers.CharField(source="driver.name", read_only=True)
    driver_mobile = serializers.CharField(source="driver.mobile_no", read_only=True)
    items = JobWorkGateInItemSerializer(many=True, read_only=True)

    class Meta:
        model = JobWorkGateIn
        fields = [
            "id", "entry_no", "company", "vehicle_entry", "vehicle_entry_no",
            "vehicle_entry_status", "vehicle", "vehicle_number", "vehicle_type",
            "transporter_name", "driver", "driver_name", "driver_mobile",
            "sap_doc_entry", "sap_doc_num", "sap_doc_date", "sap_doc_time",
            "sap_supplier_code", "sap_supplier_name", "sap_reference",
            "sap_comments", "sap_branch_id", "production_order_doc_entry",
            "production_order_doc_num", "production_item_code",
            "production_item_name", "production_planned_qty",
            "production_completed_qty", "production_rejected_qty",
            "production_remaining_qty", "production_start_date",
            "production_due_date", "production_warehouse",
            "production_status", "gate_in_date", "in_time", "security_name",
            "remarks", "status", "items", "created_at", "updated_at",
        ]
        read_only_fields = fields


class JobWorkGateInCreateSerializer(serializers.Serializer):
    vehicle_id = serializers.IntegerField()
    driver_id = serializers.IntegerField()
    sap_doc_entry = serializers.IntegerField(required=False, allow_null=True)
    production_order_doc_entry = serializers.IntegerField(required=False, allow_null=True)
    gate_in_date = serializers.DateField()
    in_time = serializers.TimeField()
    security_name = serializers.CharField(required=False, allow_blank=True, default="")
    remarks = serializers.CharField(required=False, allow_blank=True, default="")


class BSTGateInSerializer(serializers.ModelSerializer):
    source_type = serializers.SerializerMethodField()
    vehicle_entry_no = serializers.CharField(source="vehicle_entry.entry_no", read_only=True)
    vehicle_entry_status = serializers.CharField(source="vehicle_entry.status", read_only=True)
    bst_gate_out_entry_no = serializers.SerializerMethodField()
    bst_gate_out_vehicle_entry = serializers.SerializerMethodField()
    bst_gate_out_date = serializers.SerializerMethodField()
    bst_gate_out_time = serializers.SerializerMethodField()
    vehicle_number = serializers.CharField(source="vehicle.vehicle_number", read_only=True)
    vehicle_type = serializers.CharField(source="vehicle.vehicle_type.name", read_only=True, allow_null=True)
    transporter_name = serializers.CharField(source="vehicle.transporter.name", read_only=True, allow_null=True)
    driver_name = serializers.CharField(source="driver.name", read_only=True)
    driver_mobile = serializers.CharField(source="driver.mobile_no", read_only=True)
    sap_doc_entry = serializers.SerializerMethodField()
    sap_doc_num = serializers.SerializerMethodField()
    sap_doc_date = serializers.SerializerMethodField()
    sap_from_warehouse = serializers.SerializerMethodField()
    sap_to_warehouse = serializers.SerializerMethodField()
    sap_reference = serializers.SerializerMethodField()
    sap_comments = serializers.SerializerMethodField()
    items = BSTGateInItemSerializer(many=True, read_only=True)

    class Meta:
        model = BSTGateIn
        fields = [
            "id", "source_type", "entry_no", "company", "vehicle_entry", "vehicle_entry_no",
            "vehicle_entry_status", "bst_gate_out", "sales_dispatch_gate_out",
            "bst_gate_out_entry_no",
            "bst_gate_out_vehicle_entry", "bst_gate_out_date", "bst_gate_out_time",
            "vehicle", "vehicle_number", "vehicle_type", "transporter_name",
            "driver", "driver_name", "driver_mobile", "sap_doc_entry",
            "sap_doc_num", "sap_doc_date", "sap_from_warehouse",
            "sap_to_warehouse", "sap_reference", "sap_comments", "gate_in_date",
            "in_time", "sap_receipt_doc_num", "sap_receipt_doc_date",
            "sap_receipt_reference", "security_name", "remarks", "status",
            "items", "created_at", "updated_at",
        ]
        read_only_fields = fields

    def _source(self, obj):
        return obj.sales_dispatch_gate_out or obj.bst_gate_out

    def get_source_type(self, obj):
        return "DOCKING_STOCK_TRANSFER" if obj.sales_dispatch_gate_out_id else "LEGACY_BST_OUT"

    def get_bst_gate_out_entry_no(self, obj):
        source = self._source(obj)
        return getattr(source, "entry_no", "")

    def get_bst_gate_out_vehicle_entry(self, obj):
        source = self._source(obj)
        return getattr(source, "vehicle_entry_id", None)

    def get_bst_gate_out_date(self, obj):
        source = self._source(obj)
        return getattr(source, "gate_out_date", None)

    def get_bst_gate_out_time(self, obj):
        source = self._source(obj)
        return getattr(source, "out_time", None)

    def get_sap_doc_entry(self, obj):
        source = self._source(obj)
        return getattr(source, "sap_doc_entry", None)

    def get_sap_doc_num(self, obj):
        source = self._source(obj)
        return getattr(source, "sap_doc_num", "")

    def get_sap_doc_date(self, obj):
        source = self._source(obj)
        return getattr(source, "sap_doc_date", None)

    def get_sap_from_warehouse(self, obj):
        source = self._source(obj)
        return getattr(source, "from_warehouse", "") or getattr(source, "sap_from_warehouse", "")

    def get_sap_to_warehouse(self, obj):
        source = self._source(obj)
        return getattr(source, "to_warehouse", "") or getattr(source, "sap_to_warehouse", "")

    def get_sap_reference(self, obj):
        source = self._source(obj)
        return getattr(source, "sap_reference", "")

    def get_sap_comments(self, obj):
        source = self._source(obj)
        return getattr(source, "sap_comments", "")


class BSTGateInCreateSerializer(serializers.Serializer):
    bst_gate_out_id = serializers.IntegerField(required=False)
    sales_dispatch_gate_out_id = serializers.IntegerField(required=False)
    gate_in_date = serializers.DateField()
    in_time = serializers.TimeField()
    sap_receipt_doc_num = serializers.CharField(required=False, allow_blank=True, default="")
    sap_receipt_doc_date = serializers.DateField(required=False, allow_null=True)
    sap_receipt_reference = serializers.CharField(required=False, allow_blank=True, default="")
    security_name = serializers.CharField(required=False, allow_blank=True, default="")
    remarks = serializers.CharField(required=False, allow_blank=True, default="")
    items = serializers.ListField(
        child=serializers.DictField(),
        required=False,
        allow_empty=True,
        default=list,
    )

    def validate(self, attrs):
        has_legacy_bst_out = bool(attrs.get("bst_gate_out_id"))
        has_docking_stock_transfer = bool(attrs.get("sales_dispatch_gate_out_id"))
        if has_legacy_bst_out == has_docking_stock_transfer:
            raise serializers.ValidationError(
                "Select exactly one BST source: bst_gate_out_id or sales_dispatch_gate_out_id."
            )
        return attrs


class BSTGateReturnSerializer(serializers.ModelSerializer):
    vehicle_entry_no = serializers.CharField(source="vehicle_entry.entry_no", read_only=True)
    vehicle_entry_status = serializers.CharField(source="vehicle_entry.status", read_only=True)
    bst_gate_out_entry_no = serializers.CharField(source="bst_gate_out.entry_no", read_only=True)
    bst_gate_out_vehicle_entry = serializers.IntegerField(
        source="bst_gate_out.vehicle_entry_id",
        read_only=True,
    )
    bst_gate_out_date = serializers.DateField(source="bst_gate_out.gate_out_date", read_only=True)
    bst_gate_out_time = serializers.TimeField(source="bst_gate_out.out_time", read_only=True)
    vehicle_number = serializers.CharField(source="vehicle.vehicle_number", read_only=True)
    vehicle_type = serializers.CharField(source="vehicle.vehicle_type.name", read_only=True, allow_null=True)
    transporter_name = serializers.CharField(source="vehicle.transporter.name", read_only=True, allow_null=True)
    driver_name = serializers.CharField(source="driver.name", read_only=True)
    driver_mobile = serializers.CharField(source="driver.mobile_no", read_only=True)
    sap_doc_entry = serializers.IntegerField(source="bst_gate_out.sap_doc_entry", read_only=True)
    sap_doc_num = serializers.CharField(source="bst_gate_out.sap_doc_num", read_only=True)
    sap_doc_date = serializers.DateField(source="bst_gate_out.sap_doc_date", read_only=True)
    sap_from_warehouse = serializers.CharField(source="bst_gate_out.sap_from_warehouse", read_only=True)
    sap_to_warehouse = serializers.CharField(source="bst_gate_out.sap_to_warehouse", read_only=True)
    sap_reference = serializers.CharField(source="bst_gate_out.sap_reference", read_only=True)
    sap_comments = serializers.CharField(source="bst_gate_out.sap_comments", read_only=True)
    items = BSTGateOutItemSerializer(source="bst_gate_out.items", many=True, read_only=True)

    class Meta:
        model = BSTGateReturn
        fields = [
            "id", "entry_no", "company", "vehicle_entry", "vehicle_entry_no",
            "vehicle_entry_status", "bst_gate_out", "bst_gate_out_entry_no",
            "bst_gate_out_vehicle_entry", "bst_gate_out_date", "bst_gate_out_time",
            "vehicle", "vehicle_number", "vehicle_type", "transporter_name",
            "driver", "driver_name", "driver_mobile", "sap_doc_entry",
            "sap_doc_num", "sap_doc_date", "sap_from_warehouse",
            "sap_to_warehouse", "sap_reference", "sap_comments", "gate_in_date",
            "in_time", "sap_return_doc_num", "sap_return_doc_date",
            "sap_return_reference", "security_name", "remarks", "status",
            "items", "created_at", "updated_at",
        ]
        read_only_fields = fields


class BSTGateReturnCreateSerializer(serializers.Serializer):
    bst_gate_out_id = serializers.IntegerField()
    gate_in_date = serializers.DateField()
    in_time = serializers.TimeField()
    sap_return_doc_num = serializers.CharField(required=False, allow_blank=True, default="")
    sap_return_doc_date = serializers.DateField(required=False, allow_null=True)
    sap_return_reference = serializers.CharField(required=False, allow_blank=True, default="")
    security_name = serializers.CharField(required=False, allow_blank=True, default="")
    remarks = serializers.CharField(required=False, allow_blank=True, default="")


class EmptyVehicleGateInReasonSerializer(serializers.Serializer):
    value = serializers.CharField()
    label = serializers.CharField()


class EmptyVehicleGateInItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = EmptyVehicleGateInItem
        fields = [
            "id", "line_num", "item_code", "item_name", "sap_quantity",
            "actual_quantity", "uom", "from_warehouse", "to_warehouse",
        ]
        read_only_fields = fields


_UNSET = object()  # sentinel for per-row memoisation (distinguish "not computed" from None)


class EmptyVehicleGateInSerializer(serializers.ModelSerializer):
    vehicle_entry_no = serializers.CharField(source="vehicle_entry.entry_no", read_only=True)
    vehicle_entry_status = serializers.CharField(source="vehicle_entry.status", read_only=True)
    vehicle_entry_time = serializers.DateTimeField(source="vehicle_entry.entry_time", read_only=True)
    company_code = serializers.CharField(source="company.code", read_only=True)
    company_name = serializers.CharField(source="company.name", read_only=True)
    vehicle_number = serializers.CharField(source="vehicle.vehicle_number", read_only=True)
    vehicle_type = serializers.CharField(source="vehicle.vehicle_type.name", read_only=True, allow_null=True)
    transporter_name = serializers.CharField(source="vehicle.transporter.name", read_only=True, allow_null=True)
    driver_name = serializers.CharField(source="driver.name", read_only=True)
    driver_mobile = serializers.CharField(source="driver.mobile_no", read_only=True)
    reason_display = serializers.CharField(source="get_reason_display", read_only=True)
    # Cross-company trip this gate-in belongs to; the vehicle-grouping key on the
    # Empty Vehicle Entries board (null for legacy single-company gate-ins).
    arrival = serializers.IntegerField(source="arrival_id", read_only=True, allow_null=True)
    arrival_no = serializers.CharField(
        source="arrival.arrival_no", read_only=True, allow_null=True
    )
    bst_gate_out_id = serializers.SerializerMethodField()
    bst_gate_out_entry_no = serializers.SerializerMethodField()
    bst_gate_out_status = serializers.SerializerMethodField()
    is_bst_document_locked = serializers.SerializerMethodField()
    # DISPATCH gate-ins derive these on read from their covers (the bills they
    # carry) instead of storing a redundant copy of the bill text.
    document_reference = serializers.SerializerMethodField()
    document_notes = serializers.SerializerMethodField()
    # Aggregate "X at Y" status of the bills this gate-in carries (null for
    # non-dispatch gate-ins with no covers).
    pipeline_status = serializers.SerializerMethodField()
    items = EmptyVehicleGateInItemSerializer(many=True, read_only=True)

    class Meta:
        model = EmptyVehicleGateIn
        fields = [
            "id", "entry_no", "company", "company_code", "company_name",
            "arrival", "arrival_no",
            "vehicle_entry", "vehicle_entry_no",
            "vehicle_entry_status", "vehicle_entry_time", "vehicle", "vehicle_number",
            "vehicle_type", "transporter_name", "driver", "driver_name",
            "driver_mobile", "reason", "reason_display", "gate_in_date",
            "in_time", "sap_doc_entry", "sap_doc_num", "sap_doc_date",
            "sap_from_warehouse", "sap_to_warehouse", "sap_reference",
            "sap_comments", "sap_line_count", "sap_total_quantity",
            "document_reference", "document_notes", "pipeline_status",
            "bst_gate_out_id", "bst_gate_out_entry_no", "bst_gate_out_status",
            "is_bst_document_locked", "items", "security_name", "remarks",
            "created_at", "updated_at",
        ]
        read_only_fields = fields

    def _active_bst_gate_out(self, obj):
        # Read the prefetched ``bst_gate_outs`` via ``.all()`` + a Python filter, and
        # memoise the result on the row. A ``.filter(...)`` here ignores the prefetch
        # cache and re-queries, and this method backs FOUR serializer fields -- so a
        # month-long list fired ~4 queries per row (≈1,870 extra). Computing it once
        # from cache drops that to zero.
        cached = getattr(obj, "_active_bst_gate_out_cache", _UNSET)
        if cached is not _UNSET:
            return cached
        candidates = [
            b
            for b in obj.bst_gate_outs.all()
            if b.is_active and b.status in ("IN_PROGRESS", "COMPLETED")
        ]
        candidates.sort(key=lambda b: b.created_at, reverse=True)
        active = candidates[0] if candidates else None
        obj._active_bst_gate_out_cache = active
        return active

    def get_bst_gate_out_id(self, obj):
        bst_out = self._active_bst_gate_out(obj)
        return bst_out.id if bst_out else None

    def get_bst_gate_out_entry_no(self, obj):
        bst_out = self._active_bst_gate_out(obj)
        return bst_out.entry_no if bst_out else ""

    def get_bst_gate_out_status(self, obj):
        bst_out = self._active_bst_gate_out(obj)
        return bst_out.status if bst_out else ""

    def get_is_bst_document_locked(self, obj):
        return bool(self._active_bst_gate_out(obj))

    def _dispatch_covers(self, obj):
        return [c for c in obj.covers.all() if c.is_active]

    def get_pipeline_status(self, obj):
        """Aggregate "X at Y" status of the bills (covers) this gate-in carries.

        A vehicle's bills move in parallel, so this is their shared stage; null for
        non-dispatch gate-ins (BST/repair/job-work) that carry no covers.
        """
        from dispatch_plans.services import aggregate_pipeline_status

        plans = [c.dispatch_plan for c in self._dispatch_covers(obj) if c.dispatch_plan_id]
        return aggregate_pipeline_status(plans)

    def get_document_reference(self, obj):
        # Non-dispatch reasons keep their stored reference (e.g. BST SAP doc).
        if obj.reason != "DISPATCH":
            return obj.document_reference
        nums = []
        for cover in self._dispatch_covers(obj):
            num = cover.sap_doc_num or str(cover.sap_doc_entry)
            if num not in nums:
                nums.append(num)
        return f"Dispatch {', '.join(nums)}" if nums else obj.document_reference

    def get_document_notes(self, obj):
        if obj.reason != "DISPATCH":
            return obj.document_notes
        from decimal import Decimal

        customers, weight = [], Decimal("0")
        for cover in self._dispatch_covers(obj):
            plan = cover.dispatch_plan
            if not plan:
                continue
            if plan.customer_name and plan.customer_name not in customers:
                customers.append(plan.customer_name)
            if plan.invoice_weight:
                weight += plan.invoice_weight
        parts = []
        if customers:
            parts.append(f"Customers: {', '.join(customers)}")
        if weight > 0:
            parts.append(f"Weight: {weight:.3f} kg")
        return "\n".join(parts) if parts else obj.document_notes


class EmptyVehicleGateInCreateSerializer(serializers.Serializer):
    vehicle_id = serializers.IntegerField()
    driver_id = serializers.IntegerField()
    reason = serializers.ChoiceField(choices=EmptyVehicleGateIn._meta.get_field("reason").choices)
    gate_in_date = serializers.DateField()
    in_time = serializers.TimeField()
    sap_doc_entry = serializers.IntegerField(required=False, allow_null=True)
    items = serializers.ListField(
        child=serializers.DictField(),
        required=False,
        allow_empty=True,
        default=list,
    )
    document_reference = serializers.CharField(required=False, allow_blank=True, default="")
    document_notes = serializers.CharField(required=False, allow_blank=True, default="")
    security_name = serializers.CharField(required=False, allow_blank=True, default="")
    remarks = serializers.CharField(required=False, allow_blank=True, default="")

    def validate(self, attrs):
        if attrs["reason"] == "BST" and not attrs.get("sap_doc_entry"):
            raise serializers.ValidationError({
                "sap_doc_entry": "Select the SAP BST document for this empty vehicle entry."
            })
        if attrs["reason"] != "BST" and attrs.get("sap_doc_entry"):
            raise serializers.ValidationError({
                "sap_doc_entry": "SAP BST document can only be linked when the reason is BST."
            })
        return attrs


class EmptyVehicleGateInUpdateSerializer(serializers.Serializer):
    sap_doc_entry = serializers.IntegerField(required=False, allow_null=True)
    items = serializers.ListField(
        child=serializers.DictField(),
        required=False,
        allow_empty=True,
    )
    document_reference = serializers.CharField(required=False, allow_blank=True)
    document_notes = serializers.CharField(required=False, allow_blank=True)
    security_name = serializers.CharField(required=False, allow_blank=True)
    remarks = serializers.CharField(required=False, allow_blank=True)


class EmptyVehicleEligibleEntrySerializer(serializers.Serializer):
    id = serializers.IntegerField()
    entry_no = serializers.CharField()
    entry_type = serializers.CharField()
    status = serializers.CharField()
    entry_time = serializers.DateTimeField()
    vehicle_id = serializers.IntegerField(source="vehicle.id")
    vehicle_number = serializers.CharField(source="vehicle.vehicle_number")
    vehicle_type = serializers.CharField(source="vehicle.vehicle_type.name", allow_null=True)
    driver_id = serializers.IntegerField(source="driver.id")
    driver_name = serializers.CharField(source="driver.name")
    driver_mobile = serializers.CharField(source="driver.mobile_no")
    remarks = serializers.CharField()
    # Cross-company context: the factory is one physical place for all companies,
    # so the empty-out board can aggregate across companies and group a single
    # physical truck by its shared arrival, mirroring the empty-in board.
    company_id = serializers.IntegerField(source="company.id", read_only=True)
    company_code = serializers.CharField(source="company.code", read_only=True, allow_null=True)
    company_name = serializers.CharField(source="company.name", read_only=True, allow_null=True)
    arrival = serializers.SerializerMethodField()
    arrival_no = serializers.SerializerMethodField()
    # Side effects of marking this vehicle out empty (computed by the view).
    release_invoice_count = serializers.SerializerMethodField()
    release_cancels_docking = serializers.SerializerMethodField()

    def get_arrival(self, obj):
        # Reverse OneToOne: Django makes the missing-relation error subclass
        # AttributeError, so getattr(..., None) is safe for non-dispatch entries.
        gate_in = getattr(obj, "empty_vehicle_gate_in", None)
        return gate_in.arrival_id if gate_in else None

    def get_arrival_no(self, obj):
        gate_in = getattr(obj, "empty_vehicle_gate_in", None)
        if gate_in and gate_in.arrival_id:
            return gate_in.arrival.arrival_no
        return None

    def get_release_invoice_count(self, obj) -> int:
        return getattr(obj, "release_invoice_count", 0)

    def get_release_cancels_docking(self, obj) -> bool:
        return getattr(obj, "release_cancels_docking", False)


class EmptyVehicleGateOutSerializer(serializers.ModelSerializer):
    vehicle_entry_no = serializers.CharField(source="vehicle_entry.entry_no", read_only=True)
    vehicle_entry_type = serializers.CharField(source="vehicle_entry.entry_type", read_only=True)
    vehicle_entry_time = serializers.DateTimeField(source="vehicle_entry.entry_time", read_only=True)
    vehicle_number = serializers.CharField(source="vehicle.vehicle_number", read_only=True)
    driver_name = serializers.CharField(source="driver.name", read_only=True)
    driver_mobile = serializers.CharField(source="driver.mobile_no", read_only=True)

    class Meta:
        model = EmptyVehicleGateOut
        fields = [
            "id", "entry_no", "company", "vehicle_entry", "vehicle_entry_no",
            "vehicle_entry_type", "vehicle_entry_time", "vehicle", "vehicle_number",
            "driver", "driver_name", "driver_mobile", "gate_out_date", "out_time",
            "security_name", "remarks", "status", "cancel_reason", "cancelled_at",
            "cancelled_by", "created_at", "updated_at",
        ]
        read_only_fields = fields


class EmptyVehicleGateOutCreateSerializer(serializers.Serializer):
    vehicle_entry_id = serializers.IntegerField()
    gate_out_date = serializers.DateField()
    out_time = serializers.TimeField()
    security_name = serializers.CharField(required=False, allow_blank=True, default="")
    remarks = serializers.CharField(required=False, allow_blank=True, default="")


class EmptyVehicleGateOutCancelSerializer(serializers.Serializer):
    cancel_reason = serializers.CharField(required=True, allow_blank=False, trim_whitespace=True)


class RejectedQCReturnItemSerializer(serializers.ModelSerializer):
    inspection_id = serializers.IntegerField(source="inspection.id", read_only=True)

    class Meta:
        model = RejectedQCReturnItem
        fields = [
            "id", "inspection_id", "gate_entry_no", "report_no",
            "internal_lot_no", "item_name", "supplier_name",
            "quantity", "uom",
        ]
        read_only_fields = fields


class RejectedQCReturnEntrySerializer(serializers.ModelSerializer):
    vehicle_number = serializers.CharField(source="vehicle.vehicle_number", read_only=True)
    driver_name = serializers.CharField(source="driver.name", read_only=True)
    driver_mobile = serializers.CharField(source="driver.mobile_no", read_only=True)
    items = RejectedQCReturnItemSerializer(many=True, read_only=True)

    class Meta:
        model = RejectedQCReturnEntry
        fields = [
            "id", "entry_no", "company", "vehicle", "vehicle_number",
            "driver", "driver_name", "driver_mobile", "gate_out_date",
            "out_time", "challan_no", "eway_bill_no", "manual_sap_reference",
            "security_name", "gross_weight", "tare_weight", "net_weight",
            "weighbridge_slip_no", "first_weighment_time",
            "second_weighment_time", "gatepass_documents", "remarks", "status", "items",
            "created_at", "updated_at",
        ]
        read_only_fields = [
            "id", "entry_no", "company", "vehicle_number", "driver_name",
            "driver_mobile", "net_weight", "gatepass_documents", "status", "items",
            "created_at", "updated_at",
        ]


class RejectedQCReturnCreateSerializer(serializers.Serializer):
    vehicle_id = serializers.IntegerField()
    driver_id = serializers.IntegerField()
    gate_out_date = serializers.DateField()
    out_time = serializers.TimeField(required=False, allow_null=True)
    challan_no = serializers.CharField(required=False, allow_blank=True, default="")
    eway_bill_no = serializers.CharField(required=False, allow_blank=True, default="")
    manual_sap_reference = serializers.CharField(required=False, allow_blank=True, default="")
    security_name = serializers.CharField(required=False, allow_blank=True, default="")
    gross_weight = serializers.DecimalField(max_digits=12, decimal_places=3)
    tare_weight = serializers.DecimalField(max_digits=12, decimal_places=3)
    weighbridge_slip_no = serializers.CharField(required=False, allow_blank=True, default="")
    first_weighment_time = serializers.DateTimeField(required=False, allow_null=True)
    second_weighment_time = serializers.DateTimeField(required=False, allow_null=True)
    gatepass_documents = serializers.ListField(
        child=serializers.CharField(allow_blank=False, trim_whitespace=True),
        min_length=1,
    )
    remarks = serializers.CharField(required=False, allow_blank=True, default="")
    inspection_ids = serializers.ListField(
        child=serializers.IntegerField(),
        min_length=1,
    )

    def validate(self, attrs):
        gross_weight = attrs.get("gross_weight")
        tare_weight = attrs.get("tare_weight")

        if gross_weight is None or gross_weight <= 0:
            raise serializers.ValidationError({"gross_weight": "Gross weight is required."})
        if tare_weight is None or tare_weight < 0:
            raise serializers.ValidationError({"tare_weight": "Tare weight is required."})
        if tare_weight > gross_weight:
            raise serializers.ValidationError(
                {"tare_weight": "Tare weight cannot be greater than gross weight."}
            )

        return attrs
