"""Serializers for the Branch Stock Transfer (BST) flow."""

from rest_framework import serializers

from company.models import Company
from driver_management.models import Driver
from vehicle_management.models import Vehicle

from .models_bst import (
    BSTBoxScan,
    BSTManualItemEntry,
    BSTPartialTransferApproval,
    BSTSourceType,
    BSTTransfer,
    BSTTransferDoc,
    BSTTransferItem,
)
from .services.bst_service import (
    partial_transfer_state,
    scan_status_payload,
    vehicle_editable,
)


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
    pcs_per_carton = serializers.FloatField(required=False, default=0)
    box_count = serializers.IntegerField(required=False, default=0)


class BSTSapDocumentSerializer(serializers.Serializer):
    """A SAP source document for a BST — a stock transfer OR an invoice.

    Invoice-only fields (`card_code`/`card_name`/`total_boxes`) default to empty
    for stock transfers; `to_warehouse` is empty for invoices (the destination is
    a company, not a warehouse). Both shapes are normalized in BSTService so the
    picker and the create path can treat them uniformly.
    """
    document_type = serializers.CharField(default="STOCK_TRANSFER")
    doc_entry = serializers.IntegerField()
    doc_num = serializers.CharField()
    doc_date = serializers.DateField(allow_null=True)
    from_warehouse = serializers.CharField(allow_blank=True, default="")
    to_warehouse = serializers.CharField(allow_blank=True, default="")
    warehouses = serializers.CharField(allow_blank=True, default="")
    comments = serializers.CharField(allow_blank=True, default="")
    reference = serializers.CharField(allow_blank=True, default="")
    card_code = serializers.CharField(allow_blank=True, default="")
    card_name = serializers.CharField(allow_blank=True, default="")
    line_count = serializers.IntegerField(default=0)
    total_quantity = serializers.FloatField(default=0)
    total_boxes = serializers.IntegerField(default=0)
    lines = SAPStockTransferLineSerializer(many=True, required=False)


# ---------------------------------------------------------------------------
# BST read serializers
# ---------------------------------------------------------------------------

class BSTTransferItemSerializer(serializers.ModelSerializer):
    # The SAP document this line belongs to (for grouping the bill by document).
    sap_doc_num = serializers.CharField(source="doc.sap_doc_num", read_only=True, default="")

    class Meta:
        model = BSTTransferItem
        fields = [
            "id", "doc", "sap_doc_num",
            "line_num", "item_code", "item_name", "quantity", "uom",
            "from_warehouse", "to_warehouse", "expected_boxes",
        ]


class BSTTransferDocSerializer(serializers.ModelSerializer):
    item_count = serializers.SerializerMethodField()
    expected_box_count = serializers.SerializerMethodField()

    class Meta:
        model = BSTTransferDoc
        fields = [
            "id", "sap_doc_entry", "sap_doc_num", "sap_doc_date",
            "sap_reference", "invoice_no", "item_count", "expected_box_count",
        ]

    def get_item_count(self, obj) -> int:
        return len(obj.items.all())

    def get_expected_box_count(self, obj) -> int:
        return sum(i.expected_boxes or 0 for i in obj.items.all())


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


class BSTManualItemEntrySerializer(serializers.ModelSerializer):
    """A hand-typed quantity for a scan-exempt (PM) line — one row per item code."""

    entered_by_name = serializers.SerializerMethodField()

    class Meta:
        model = BSTManualItemEntry
        fields = [
            "id", "item_code", "item_name", "quantity", "uom", "notes",
            "entered_by_name", "entered_at",
        ]

    def get_entered_by_name(self, obj) -> str:
        return _user_name(obj.entered_by)


class BSTTransferListSerializer(serializers.ModelSerializer):
    company_code = serializers.CharField(source="company.code", read_only=True)
    company_name = serializers.CharField(source="company.name", read_only=True)
    destination_company_code = serializers.CharField(
        source="destination_company.code", read_only=True, default="",
    )
    destination_company_name = serializers.CharField(
        source="destination_company.name", read_only=True, default="",
    )
    vehicle_number = serializers.CharField(source="vehicle.vehicle_number", read_only=True)
    driver_name = serializers.CharField(source="driver.name", read_only=True)
    scanned_box_count = serializers.SerializerMethodField()
    item_count = serializers.SerializerMethodField()
    doc_count = serializers.SerializerMethodField()

    class Meta:
        model = BSTTransfer
        fields = [
            "id", "entry_no", "status",
            "source_type",
            "company_code", "company_name",
            "destination_company", "destination_company_code", "destination_company_name",
            "customer_code", "customer_name",
            "sap_doc_entry", "sap_doc_num", "sap_doc_date",
            "sap_from_warehouse", "sap_to_warehouse", "sap_reference",
            "invoice_no", "vehicle", "vehicle_number", "driver", "driver_name",
            "requires_gate",
            "scanned_box_count", "item_count", "doc_count",
            "scan_approved_at", "dispatched_at", "received_at", "created_at",
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

    def get_doc_count(self, obj) -> int:
        annotated = getattr(obj, "doc_count", None)
        if annotated is not None:
            return annotated
        return obj.docs.count()


class BSTTransferDetailSerializer(BSTTransferListSerializer):
    docs = BSTTransferDocSerializer(many=True, read_only=True)
    items = BSTTransferItemSerializer(many=True, read_only=True)
    box_scans = BSTBoxScanSerializer(many=True, read_only=True)
    # Hand-typed quantities for the scan-exempt (PM) lines, which have no box scans.
    manual_entries = BSTManualItemEntrySerializer(many=True, read_only=True)
    created_by_name = serializers.SerializerMethodField()
    scan_approved_by_name = serializers.SerializerMethodField()
    dispatched_by_name = serializers.SerializerMethodField()
    received_by_name = serializers.SerializerMethodField()
    accepted_count = serializers.SerializerMethodField()
    rejected_count = serializers.SerializerMethodField()
    # Scanned-vs-expected QUANTITY completeness (the sender's seal gate). Drives the
    # frontend Partial/Complete display and the "scan all boxes" lock; the same rule
    # blocks approve() on the backend, so the two can't disagree.
    scan_status = serializers.SerializerMethodField()
    # Latest admin partial-transfer approval request (or null) — lets the sender's
    # lock show "pending approval" / unlock once approved.
    partial_transfer = serializers.SerializerMethodField()
    # Whether the vehicle + driver can still be corrected (open until gate-out) —
    # the same rule update_transfer enforces, so the screen can't offer an edit
    # the backend will refuse.
    can_edit_vehicle = serializers.SerializerMethodField()

    class Meta(BSTTransferListSerializer.Meta):
        fields = BSTTransferListSerializer.Meta.fields + [
            "remarks", "cancel_reason",
            "gated_out_at", "gated_in_at",
            "created_by_name", "scan_approved_by_name", "dispatched_by_name", "received_by_name",
            "accepted_count", "rejected_count",
            "scan_status", "partial_transfer", "can_edit_vehicle",
            "docs", "items", "box_scans", "manual_entries", "updated_at",
        ]

    def get_created_by_name(self, obj) -> str:
        return _user_name(obj.created_by)

    def get_scan_approved_by_name(self, obj) -> str:
        return _user_name(obj.scan_approved_by)

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

    def get_scan_status(self, obj) -> dict:
        return scan_status_payload(obj)

    def get_partial_transfer(self, obj):
        return partial_transfer_state(obj)

    def get_can_edit_vehicle(self, obj) -> bool:
        return vehicle_editable(obj)


# ---------------------------------------------------------------------------
# BST write serializers
# ---------------------------------------------------------------------------

class BSTTransferCreateSerializer(serializers.Serializer):
    # One or more SAP source documents combined into a single entry. For a
    # STOCK_TRANSFER they must share the same source + destination warehouse; for
    # an INVOICE the same source warehouse and customer (checked in the service
    # against the live SAP documents).
    document_type = serializers.ChoiceField(
        choices=[BSTSourceType.STOCK_TRANSFER, BSTSourceType.INVOICE],
        required=False, default=BSTSourceType.STOCK_TRANSFER,
    )
    sap_doc_entries = serializers.ListField(
        child=serializers.IntegerField(), allow_empty=False,
    )
    # Required for (and only used by) an INVOICE transfer: the receiving company
    # of the cross-company sale.
    destination_company = serializers.PrimaryKeyRelatedField(
        queryset=Company.objects.all(), required=False, allow_null=True,
    )
    # Vehicle + driver are only required when the transfer needs a gate movement.
    vehicle = serializers.PrimaryKeyRelatedField(
        queryset=Vehicle.objects.all(), required=False, allow_null=True,
    )
    driver = serializers.PrimaryKeyRelatedField(
        queryset=Driver.objects.all(), required=False, allow_null=True,
    )
    invoice_no = serializers.CharField(max_length=100, required=False, allow_blank=True, default="")
    requires_gate = serializers.BooleanField(required=False, default=False)
    remarks = serializers.CharField(required=False, allow_blank=True, default="")

    def validate(self, attrs):
        if attrs.get("requires_gate") and (not attrs.get("vehicle") or not attrs.get("driver")):
            raise serializers.ValidationError(
                "Vehicle and driver are required for a gate movement.",
            )
        if attrs.get("document_type") == BSTSourceType.INVOICE and not attrs.get("destination_company"):
            raise serializers.ValidationError(
                "Destination company is required for an invoice transfer.",
            )
        return attrs


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


class BSTBoxScanBulkDeleteSerializer(serializers.Serializer):
    """The scans an operator ticked off for removal."""

    scan_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        allow_empty=False,
    )


class BSTManualItemEntrySaveSerializer(serializers.Serializer):
    """Upsert one scan-exempt (PM) line's hand-typed quantity. `null` clears it."""

    item_code = serializers.CharField(max_length=100)
    quantity = serializers.DecimalField(
        max_digits=18, decimal_places=3, allow_null=True,
    )
    notes = serializers.CharField(allow_blank=True, required=False, default="")


class BSTTransferCancelSerializer(serializers.Serializer):
    cancel_reason = serializers.CharField(allow_blank=True, required=False, default="")


class BSTReceiveScanSerializer(serializers.Serializer):
    barcode_raw = serializers.CharField(max_length=100)
    decision = serializers.ChoiceField(
        choices=["ACCEPTED", "REJECTED"], required=False, default="ACCEPTED",
    )
    reject_reason = serializers.CharField(allow_blank=True, required=False, default="")


# ---------------------------------------------------------------------------
# Partial-transfer approval (seal a short scan with admin sign-off)
# ---------------------------------------------------------------------------

class BSTPartialTransferApprovalSerializer(serializers.ModelSerializer):
    transfer_entry_no = serializers.CharField(source="transfer.entry_no", read_only=True)
    requested_by_name = serializers.SerializerMethodField()
    reviewed_by_name = serializers.SerializerMethodField()

    class Meta:
        model = BSTPartialTransferApproval
        fields = [
            "id", "transfer", "transfer_entry_no",
            "scanned_qty", "expected_qty", "reason", "status",
            "requested_by_name", "requested_at",
            "reviewed_by_name", "reviewed_at", "review_notes",
        ]

    def get_requested_by_name(self, obj) -> str:
        return _user_name(obj.requested_by)

    def get_reviewed_by_name(self, obj) -> str:
        return _user_name(obj.reviewed_by)


class BSTPartialTransferRequestCreateSerializer(serializers.Serializer):
    reason = serializers.CharField()


class BSTPartialTransferReviewSerializer(serializers.Serializer):
    review_notes = serializers.CharField(allow_blank=True, required=False, default="")
