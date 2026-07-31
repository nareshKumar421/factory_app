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
    class Meta:
        model = GoodsReturnInvoiceRef
        fields = ["id", "sap_invoice_doc_entry", "sap_invoice_doc_num"]


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

    class Meta:
        model = GoodsReturn
        fields = [
            "id",
            "entry_no",
            "basis",
            "status",
            "customer_code",
            "customer_name",
            "vehicle_no",
            "driver_name",
            "company_code",
            "company_name",
            "expected_arrival_at",
            "gated_in_at",
            "line_count",
            "created_at",
        ]

    def get_line_count(self, obj):
        return len([line for line in obj.lines.all() if line.is_active])


class GoodsReturnDetailSerializer(serializers.ModelSerializer):
    vehicle_no = serializers.CharField(source="vehicle.vehicle_number", default="", read_only=True)
    driver_name = serializers.CharField(source="driver.name", default="", read_only=True)
    company_code = serializers.CharField(source="company.code", read_only=True)
    company_name = serializers.CharField(source="company.name", read_only=True)
    invoice_refs = serializers.SerializerMethodField()
    lines = serializers.SerializerMethodField()
    attachments = GoodsReturnAttachmentSerializer(many=True, read_only=True)

    class Meta:
        model = GoodsReturn
        fields = [
            "id",
            "entry_no",
            "basis",
            "status",
            "customer_code",
            "customer_name",
            "vehicle",
            "vehicle_no",
            "driver",
            "driver_name",
            "company_code",
            "company_name",
            "expected_arrival_at",
            "gated_in_at",
            "sap_gr_doc_num",
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

    def get_lines(self, obj):
        active = [line for line in obj.lines.all() if line.is_active]
        return GoodsReturnItemSerializer(active, many=True).data


# ---------------------------------------------------------------------------
# Input
# ---------------------------------------------------------------------------
class GoodsReturnCreateSerializer(serializers.Serializer):
    basis = serializers.ChoiceField(choices=GoodsReturnBasis.choices)
    invoice_numbers = serializers.ListField(
        child=serializers.CharField(), required=False, allow_empty=True
    )
    customer_code = serializers.CharField(required=False, allow_blank=True)
    customer_name = serializers.CharField(required=False, allow_blank=True)
    remarks = serializers.CharField(required=False, allow_blank=True)


class GoodsReturnHeaderPatchSerializer(serializers.Serializer):
    customer_code = serializers.CharField(required=False, allow_blank=True)
    customer_name = serializers.CharField(required=False, allow_blank=True)
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
    vehicle_id = serializers.IntegerField()
    driver_id = serializers.IntegerField()
    expected_arrival_at = serializers.DateField()


class GoodsReturnAttachmentUploadSerializer(serializers.Serializer):
    file = serializers.FileField()
    attachment_type = serializers.CharField(required=False, allow_blank=True)
    notes = serializers.CharField(required=False, allow_blank=True)


class GoodsReturnMarkInSerializer(serializers.Serializer):
    remarks = serializers.CharField(required=False, allow_blank=True)
