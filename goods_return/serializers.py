from rest_framework import serializers

from .models import (
    GoodsReturn,
    GoodsReturnAttachment,
    GoodsReturnBasis,
    GoodsReturnInvoiceRef,
    GoodsReturnItem,
    GoodsReturnItemCondition,
)


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
class GoodsReturnAttachmentSerializer(serializers.ModelSerializer):
    file_url = serializers.FileField(source="file", read_only=True)

    class Meta:
        model = GoodsReturnAttachment
        fields = [
            "id",
            "attachment_type",
            "file_url",
            "original_filename",
            "notes",
            "uploaded_at",
        ]


class GoodsReturnInvoiceRefSerializer(serializers.ModelSerializer):
    """One source invoice, and the A/R Return posted for it (one per invoice)."""

    class Meta:
        model = GoodsReturnInvoiceRef
        fields = [
            "id",
            "sap_invoice_doc_entry",
            "sap_invoice_doc_num",
            "sap_gr_doc_entry",
            "sap_gr_doc_num",
            "sap_return_warehouse",
            "posted_at",
            "sap_post_error",
        ]


class GoodsReturnItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = GoodsReturnItem
        fields = [
            "id",
            "invoice_ref",
            "source_line_num",
            "item_code",
            "item_name",
            "uom",
            "invoice_quantity",
            "return_quantity",
            "reason",
            "condition",
            "remarks",
        ]


class GoodsReturnListSerializer(serializers.ModelSerializer):
    vehicle_no = serializers.CharField(source="vehicle.vehicle_number", default="", read_only=True)
    driver_name = serializers.CharField(source="driver.name", default="", read_only=True)
    company_code = serializers.CharField(source="company.code", read_only=True)
    company_name = serializers.CharField(source="company.name", read_only=True)
    line_count = serializers.SerializerMethodField()
    # The bills the return is booked against. On the row because that is what
    # people identify a return by -- and each of them posts its own A/R Return.
    invoice_doc_nums = serializers.SerializerMethodField()

    class Meta:
        model = GoodsReturn
        fields = [
            "id",
            "entry_no",
            "basis",
            "status",
            "customer_code",
            "customer_name",
            "customer_ref_no",
            "vehicle_no",
            "driver_name",
            "company_code",
            "company_name",
            "expected_arrival_at",
            "gated_in_at",
            "requires_approval",
            "approval_status",
            "line_count",
            "invoice_doc_nums",
            # Null while the clerk is still filling the return in -- the list uses
            # it to send them back into the wizard instead of the read-only view.
            "submitted_at",
            "created_at",
        ]

    def get_line_count(self, obj):
        return len([line for line in obj.lines.all() if line.is_active])

    def get_invoice_doc_nums(self, obj):
        return [
            ref.sap_invoice_doc_num or str(ref.sap_invoice_doc_entry)
            for ref in obj.active_invoice_refs
        ]


class GoodsReturnGateHistorySerializer(GoodsReturnListSerializer):
    """The queue row plus who let the truck in -- the gate's own record of it."""

    gated_in_by_name = serializers.CharField(
        source="gated_in_by.full_name", default="", read_only=True
    )

    class Meta(GoodsReturnListSerializer.Meta):
        fields = GoodsReturnListSerializer.Meta.fields + ["gated_in_by_name"]


class GoodsReturnDetailSerializer(serializers.ModelSerializer):
    vehicle_no = serializers.CharField(source="vehicle.vehicle_number", default="", read_only=True)
    driver_name = serializers.CharField(source="driver.name", default="", read_only=True)
    company_code = serializers.CharField(source="company.code", read_only=True)
    company_name = serializers.CharField(source="company.name", read_only=True)
    invoice_refs = serializers.SerializerMethodField()
    lines = serializers.SerializerMethodField()
    attachments = GoodsReturnAttachmentSerializer(many=True, read_only=True)
    # Every A/R Return this goods return posted -- one per invoice, so a return
    # booked against two bills has two. `sap_gr_doc_num` stays the first of them.
    sap_gr_doc_nums = serializers.SerializerMethodField()

    class Meta:
        model = GoodsReturn
        fields = [
            "id",
            "entry_no",
            "basis",
            "status",
            "customer_code",
            "customer_name",
            "customer_ref_no",
            "vehicle",
            "vehicle_no",
            "driver",
            "driver_name",
            "company_code",
            "company_name",
            "expected_arrival_at",
            "gated_in_at",
            "received_at",
            "requires_approval",
            "approval_status",
            "approval_remarks",
            "approved_at",
            "sap_gr_doc_num",
            "sap_gr_doc_nums",
            "sap_return_warehouse",
            "remarks",
            "submitted_at",
            "created_at",
            "invoice_refs",
            "lines",
            "attachments",
        ]

    def get_invoice_refs(self, obj):
        active = [ref for ref in obj.invoice_refs.all() if ref.is_active]
        return GoodsReturnInvoiceRefSerializer(active, many=True).data

    def get_sap_gr_doc_nums(self, obj):
        nums = [ref.sap_gr_doc_num for ref in obj.posted_invoice_refs if ref.sap_gr_doc_num]
        # A debit-note or letter-pad return has no invoice ref to hang its
        # document on, so the header's own number is the only one.
        if not nums and obj.sap_gr_doc_num:
            nums = [obj.sap_gr_doc_num]
        return nums

    def get_lines(self, obj):
        active = [line for line in obj.lines.all() if line.is_active]
        return GoodsReturnItemSerializer(active, many=True).data


# ---------------------------------------------------------------------------
# Input
# ---------------------------------------------------------------------------
class GoodsReturnCreateSerializer(serializers.Serializer):
    basis = serializers.ChoiceField(choices=GoodsReturnBasis.choices)
    # The truck is captured first, before the paperwork: saving Step 1 puts the
    # return straight into the gate's arrival queue, so the vehicle and driver
    # have to be known by then. Both are enforced in the service (so the message
    # is the human one); the expected arrival date stays optional.
    vehicle_id = serializers.IntegerField(required=False, allow_null=True)
    driver_id = serializers.IntegerField(required=False, allow_null=True)
    expected_arrival_at = serializers.DateField(required=False, allow_null=True)
    invoice_numbers = serializers.ListField(
        child=serializers.CharField(), required=False, allow_empty=True
    )
    customer_code = serializers.CharField(required=False, allow_blank=True)
    customer_name = serializers.CharField(required=False, allow_blank=True)
    # The customer's own debit-note / letter-pad number. Optional: plenty of
    # letter pads carry no number, and the return is already on the road.
    customer_ref_no = serializers.CharField(
        required=False, allow_blank=True, max_length=100
    )
    remarks = serializers.CharField(required=False, allow_blank=True)
    requires_approval = serializers.BooleanField(required=False, default=False)


class GoodsReturnHeaderPatchSerializer(serializers.Serializer):
    customer_code = serializers.CharField(required=False, allow_blank=True)
    customer_name = serializers.CharField(required=False, allow_blank=True)
    customer_ref_no = serializers.CharField(
        required=False, allow_blank=True, max_length=100
    )
    remarks = serializers.CharField(required=False, allow_blank=True)
    requires_approval = serializers.BooleanField(required=False)


class GoodsReturnApprovalDecisionSerializer(serializers.Serializer):
    remarks = serializers.CharField(required=False, allow_blank=True)


class InvoiceRefAddSerializer(serializers.Serializer):
    invoice_number = serializers.CharField()


class GoodsReturnItemInputSerializer(serializers.Serializer):
    invoice_ref_id = serializers.IntegerField(required=False, allow_null=True)
    source_line_num = serializers.IntegerField(required=False, allow_null=True)
    item_code = serializers.CharField(required=False, allow_blank=True)
    item_name = serializers.CharField(required=False, allow_blank=True)
    uom = serializers.CharField(required=False, allow_blank=True)
    invoice_quantity = serializers.DecimalField(
        max_digits=18, decimal_places=3, required=False, default=0
    )
    return_quantity = serializers.DecimalField(max_digits=18, decimal_places=3)
    reason = serializers.CharField(required=False, allow_blank=True)
    condition = serializers.ChoiceField(
        choices=GoodsReturnItemCondition.choices, required=False, default="DAMAGED"
    )
    remarks = serializers.CharField(required=False, allow_blank=True)


class GoodsReturnItemsSaveSerializer(serializers.Serializer):
    lines = GoodsReturnItemInputSerializer(many=True)


class GoodsReturnVehicleSerializer(serializers.Serializer):
    """Corrects the truck on a return that is already in the gate's queue (the
    vehicle is captured at creation, not here). A key left out is not touched;
    the vehicle and driver cannot be cleared back to nothing, because the gate is
    already waiting on them -- ``expected_arrival_at: null`` still clears."""

    vehicle_id = serializers.IntegerField(required=False, allow_null=True)
    driver_id = serializers.IntegerField(required=False, allow_null=True)
    expected_arrival_at = serializers.DateField(required=False, allow_null=True)


class GoodsReturnAttachmentUploadSerializer(serializers.Serializer):
    file = serializers.FileField()
    attachment_type = serializers.CharField(required=False, allow_blank=True)
    notes = serializers.CharField(required=False, allow_blank=True)


class GoodsReturnMarkInSerializer(serializers.Serializer):
    remarks = serializers.CharField(required=False, allow_blank=True)
    # The returns clerk may have left the vehicle step blank; the gate then
    # supplies the truck it is actually looking at. Ignored when the return
    # already carries a vehicle/driver.
    vehicle_id = serializers.IntegerField(required=False, allow_null=True)
    driver_id = serializers.IntegerField(required=False, allow_null=True)


class GoodsReturnReceiveSerializer(serializers.Serializer):
    # Required for invoice-basis returns (destination goods-return warehouse); the
    # service enforces that. Optional at the serializer level for DN/LP.
    warehouse_code = serializers.CharField(required=False, allow_blank=True)


class ReturnWarehouseSerializer(serializers.Serializer):
    warehouse_code = serializers.CharField()
    warehouse_name = serializers.CharField()
