"""Serializers for the Branch Stock Transfer (BST) flow."""

from rest_framework import serializers

from company.models import Company
from driver_management.models import Driver
from vehicle_management.models import Vehicle

from .models_bst import BSTBoxScan, BSTTransfer, BSTTransferItem


def _user_name(user) -> str:
    if not user:
        return ""
    return getattr(user, "full_name", "") or getattr(user, "email", "") or str(user)


# ---------------------------------------------------------------------------
# SAP stock-transfer (read) — header + lines fetched from SAP HANA
# ---------------------------------------------------------------------------

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
    from_warehouse = serializers.CharField()
    to_warehouse = serializers.CharField()
    comments = serializers.CharField(allow_blank=True)
    reference = serializers.CharField(allow_blank=True)
    line_count = serializers.IntegerField()
    total_quantity = serializers.FloatField()
    lines = SAPStockTransferLineSerializer(many=True, required=False)


# ---------------------------------------------------------------------------
# BST read serializers
# ---------------------------------------------------------------------------

class BSTTransferItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = BSTTransferItem
        fields = [
            "id", "line_num", "item_code", "item_name", "quantity", "uom",
            "from_warehouse", "to_warehouse", "expected_boxes",
        ]


class BSTBoxScanSerializer(serializers.ModelSerializer):
    scanned_by_name = serializers.SerializerMethodField()
    received_by_name = serializers.SerializerMethodField()

    class Meta:
        model = BSTBoxScan
        fields = [
            "id", "box", "pallet", "box_barcode",
            "item_code", "item_name", "batch_number", "quantity", "uom",
            "warehouse_code", "pallet_code",
            "scanned_by_name", "scanned_at",
            "receive_status", "reject_reason", "is_unexpected",
            "received_by_name", "received_at",
        ]

    def get_scanned_by_name(self, obj) -> str:
        return _user_name(obj.scanned_by)

    def get_received_by_name(self, obj) -> str:
        return _user_name(obj.received_by)


class BSTTransferListSerializer(serializers.ModelSerializer):
    company_code = serializers.CharField(source="company.code", read_only=True)
    company_name = serializers.CharField(source="company.name", read_only=True)
    to_company_code = serializers.CharField(source="to_company.code", read_only=True)
    to_company_name = serializers.CharField(source="to_company.name", read_only=True)
    vehicle_number = serializers.CharField(source="vehicle.vehicle_number", read_only=True)
    driver_name = serializers.CharField(source="driver.name", read_only=True)
    scanned_box_count = serializers.SerializerMethodField()
    item_count = serializers.SerializerMethodField()

    class Meta:
        model = BSTTransfer
        fields = [
            "id", "entry_no", "status",
            "company_code", "company_name", "to_company_code", "to_company_name",
            "sap_doc_entry", "sap_doc_num", "sap_doc_date",
            "sap_from_warehouse", "sap_to_warehouse", "sap_reference",
            "invoice_no", "vehicle_number", "driver_name", "requires_gate",
            "scanned_box_count", "item_count",
            "dispatched_at", "received_at", "created_at",
        ]

    def get_scanned_box_count(self, obj) -> int:
        annotated = getattr(obj, "scanned_box_count", None)
        if annotated is not None:
            return annotated
        return obj.box_scans.count()

    def get_item_count(self, obj) -> int:
        annotated = getattr(obj, "item_count", None)
        if annotated is not None:
            return annotated
        return obj.items.count()


class BSTTransferDetailSerializer(BSTTransferListSerializer):
    items = BSTTransferItemSerializer(many=True, read_only=True)
    box_scans = BSTBoxScanSerializer(many=True, read_only=True)
    created_by_name = serializers.SerializerMethodField()
    dispatched_by_name = serializers.SerializerMethodField()
    received_by_name = serializers.SerializerMethodField()
    accepted_count = serializers.SerializerMethodField()
    rejected_count = serializers.SerializerMethodField()

    class Meta(BSTTransferListSerializer.Meta):
        fields = BSTTransferListSerializer.Meta.fields + [
            "remarks", "cancel_reason",
            "gated_out_at", "gated_in_at",
            "created_by_name", "dispatched_by_name", "received_by_name",
            "accepted_count", "rejected_count",
            "items", "box_scans", "updated_at",
        ]

    def get_created_by_name(self, obj) -> str:
        return _user_name(obj.created_by)

    def get_dispatched_by_name(self, obj) -> str:
        return _user_name(obj.dispatched_by)

    def get_received_by_name(self, obj) -> str:
        return _user_name(obj.received_by)

    def _scans(self, obj):
        return obj.box_scans.all()

    def get_accepted_count(self, obj) -> int:
        return sum(1 for s in self._scans(obj) if s.receive_status == "ACCEPTED")

    def get_rejected_count(self, obj) -> int:
        return sum(1 for s in self._scans(obj) if s.receive_status == "REJECTED")


# ---------------------------------------------------------------------------
# BST write serializers
# ---------------------------------------------------------------------------

class BSTTransferCreateSerializer(serializers.Serializer):
    sap_doc_entry = serializers.IntegerField()
    to_company = serializers.PrimaryKeyRelatedField(
        queryset=Company.objects.filter(is_active=True),
    )
    vehicle = serializers.PrimaryKeyRelatedField(queryset=Vehicle.objects.all())
    driver = serializers.PrimaryKeyRelatedField(queryset=Driver.objects.all())
    invoice_no = serializers.CharField(max_length=100, required=False, allow_blank=True, default="")
    requires_gate = serializers.BooleanField(required=False, default=False)
    remarks = serializers.CharField(required=False, allow_blank=True, default="")


class BSTTransferUpdateSerializer(serializers.Serializer):
    vehicle = serializers.PrimaryKeyRelatedField(queryset=Vehicle.objects.all(), required=False)
    driver = serializers.PrimaryKeyRelatedField(queryset=Driver.objects.all(), required=False)
    invoice_no = serializers.CharField(max_length=100, required=False, allow_blank=True)
    requires_gate = serializers.BooleanField(required=False)
    remarks = serializers.CharField(required=False, allow_blank=True)


class BSTBoxScanCreateSerializer(serializers.Serializer):
    barcode_raw = serializers.CharField(max_length=100)


class BSTBoxScanBatchSerializer(serializers.Serializer):
    barcodes = serializers.ListField(
        child=serializers.CharField(max_length=100),
        allow_empty=False,
    )


class BSTTransferCancelSerializer(serializers.Serializer):
    cancel_reason = serializers.CharField(allow_blank=True, required=False, default="")
