"""Serializers for warehouse transfer requests."""

from rest_framework import serializers

from .models_transfer import (
    WarehouseTransferRequest,
    WarehouseTransferRequestLine,
)


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

class TransferRequestLineSerializer(serializers.ModelSerializer):
    outstanding_qty = serializers.DecimalField(
        max_digits=15, decimal_places=3, read_only=True
    )
    source_warehouse = serializers.CharField(read_only=True)
    destination_warehouse = serializers.CharField(read_only=True)

    class Meta:
        model = WarehouseTransferRequestLine
        fields = [
            'id', 'line_num', 'item_code', 'item_name', 'uom',
            'from_warehouse', 'to_warehouse',
            'source_warehouse', 'destination_warehouse',
            'requested_qty', 'approved_qty', 'transferred_qty', 'outstanding_qty',
            'is_batch_managed', 'batch_allocation',
            'status', 'notes',
        ]


def _person(user) -> str:
    """Display name for an audit stamp.

    `accounts.User` subclasses `AbstractBaseUser`, so it has `full_name` and no
    `get_full_name()` — reaching for the Django default returns nothing at best
    and raises at worst. Same helper the BST serializers use.
    """
    if not user:
        return ''
    return getattr(user, 'full_name', '') or getattr(user, 'email', '') or str(user)


class TransferRequestListSerializer(serializers.ModelSerializer):
    requested_by_name = serializers.SerializerMethodField()
    line_count = serializers.IntegerField(source='lines.count', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    posting_status_display = serializers.CharField(
        source='get_posting_status_display', read_only=True
    )

    def get_requested_by_name(self, obj) -> str:
        return _person(obj.requested_by)

    class Meta:
        model = WarehouseTransferRequest
        fields = [
            'id', 'entry_no', 'from_warehouse', 'to_warehouse',
            'route_type', 'intransit_warehouse',
            'status', 'status_display',
            'posting_status', 'posting_status_display',
            'sap_request_doc_num', 'sap_transfer_doc_num', 'sap_leg2_doc_num',
            'requested_by_name', 'line_count', 'created_at',
        ]


class TransferRequestDetailSerializer(serializers.ModelSerializer):
    lines = TransferRequestLineSerializer(many=True, read_only=True)
    requested_by_name = serializers.SerializerMethodField()
    reviewed_by_name = serializers.SerializerMethodField()
    posted_by_name = serializers.SerializerMethodField()
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    posting_status_display = serializers.CharField(
        source='get_posting_status_display', read_only=True
    )
    is_cross_branch = serializers.BooleanField(read_only=True)
    awaits_second_leg = serializers.BooleanField(read_only=True)
    leg1_destination = serializers.CharField(read_only=True)
    bst_entry_no = serializers.CharField(
        source='bst_transfer.entry_no', read_only=True, default=''
    )

    def get_requested_by_name(self, obj) -> str:
        return _person(obj.requested_by)

    def get_reviewed_by_name(self, obj) -> str:
        return _person(obj.reviewed_by)

    def get_posted_by_name(self, obj) -> str:
        return _person(obj.posted_by)

    class Meta:
        model = WarehouseTransferRequest
        fields = [
            'id', 'entry_no', 'company',
            'from_warehouse', 'to_warehouse',
            'route_type', 'is_cross_branch', 'intransit_warehouse',
            'leg1_destination', 'awaits_second_leg',
            'from_branch_id', 'to_branch_id',
            'status', 'status_display', 'remarks', 'rejection_reason',
            'posting_status', 'posting_status_display', 'posting_error',
            'sap_request_doc_entry', 'sap_request_doc_num', 'sap_request_closed_at',
            'sap_transfer_doc_entry', 'sap_transfer_doc_num',
            'sap_leg2_doc_entry', 'sap_leg2_doc_num',
            'bst_transfer', 'bst_entry_no',
            'requested_by_name', 'reviewed_by_name', 'posted_by_name',
            'reviewed_at', 'posted_at', 'created_at', 'updated_at',
            'lines',
        ]


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------

class TransferRequestLineCreateSerializer(serializers.Serializer):
    item_code = serializers.CharField(max_length=50)
    item_name = serializers.CharField(max_length=255, required=False, allow_blank=True)
    uom = serializers.CharField(max_length=20, required=False, allow_blank=True)
    quantity = serializers.DecimalField(max_digits=15, decimal_places=3, min_value=0)
    from_warehouse = serializers.CharField(max_length=20, required=False, allow_blank=True)
    to_warehouse = serializers.CharField(max_length=20, required=False, allow_blank=True)


class TransferRequestCreateSerializer(serializers.Serializer):
    from_warehouse = serializers.CharField(max_length=20)
    to_warehouse = serializers.CharField(max_length=20)
    remarks = serializers.CharField(required=False, allow_blank=True, default='')
    lines = TransferRequestLineCreateSerializer(many=True)

    def validate_lines(self, value):
        if not value:
            raise serializers.ValidationError("Add at least one item to the request.")
        seen = set()
        for line in value:
            key = (line['item_code'], line.get('from_warehouse', ''))
            if key in seen:
                raise serializers.ValidationError(
                    f"{line['item_code']} appears twice from the same warehouse. "
                    f"Combine the quantities into one line."
                )
            seen.add(key)
        return value


class TransferApprovalLineSerializer(serializers.Serializer):
    line_num = serializers.IntegerField(min_value=0)
    approved_qty = serializers.DecimalField(max_digits=15, decimal_places=3, min_value=0)


class TransferApproveSerializer(serializers.Serializer):
    """Lines left out are approved at the requested quantity."""
    lines = TransferApprovalLineSerializer(many=True, required=False)
    reason = serializers.CharField(required=False, allow_blank=True, default='')


class TransferRejectSerializer(serializers.Serializer):
    reason = serializers.CharField()

    def validate_reason(self, value):
        if not value.strip():
            raise serializers.ValidationError("A rejection needs a reason.")
        return value.strip()


class TransferBatchChoiceSerializer(serializers.Serializer):
    batch_number = serializers.CharField(max_length=40)
    quantity = serializers.DecimalField(max_digits=15, decimal_places=3, min_value=0)


class TransferPostLineSerializer(serializers.Serializer):
    line_num = serializers.IntegerField(min_value=0)
    batches = TransferBatchChoiceSerializer(many=True)


class TransferPostSerializer(serializers.Serializer):
    """Optional hand-picked batch splits; anything omitted falls back to FIFO."""
    lines = TransferPostLineSerializer(many=True, required=False)


class TransferSecondLegLineSerializer(serializers.Serializer):
    line_num = serializers.IntegerField(min_value=0)
    received_qty = serializers.DecimalField(max_digits=15, decimal_places=3, min_value=0)


class TransferSecondLegSerializer(serializers.Serializer):
    """Lines left out use whatever leg 1 moved."""
    lines = TransferSecondLegLineSerializer(many=True, required=False)
