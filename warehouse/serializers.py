from rest_framework import serializers
from .models import BOMRequest, BOMRequestLine, FinishedGoodsReceipt


# ---------------------------------------------------------------------------
# BOM Request Lines
# ---------------------------------------------------------------------------

class BOMRequestLineSerializer(serializers.ModelSerializer):
    class Meta:
        model = BOMRequestLine
        fields = [
            'id', 'item_code', 'item_name', 'per_unit_qty',
            'required_qty', 'available_stock', 'approved_qty', 'issued_qty',
            'warehouse', 'uom', 'base_line', 'status', 'remarks',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['created_at', 'updated_at']


class BOMLineApprovalSerializer(serializers.Serializer):
    line_id = serializers.IntegerField()
    approved_qty = serializers.DecimalField(
        max_digits=12, decimal_places=3, required=False
    )
    status = serializers.ChoiceField(
        choices=['APPROVED', 'REJECTED'], default='APPROVED'
    )
    remarks = serializers.CharField(required=False, allow_blank=True, default='')


# ---------------------------------------------------------------------------
# BOM Request
# ---------------------------------------------------------------------------

class BOMRequestCreateSerializer(serializers.Serializer):
    production_run_id = serializers.IntegerField()
    required_qty = serializers.DecimalField(max_digits=12, decimal_places=2)
    remarks = serializers.CharField(required=False, allow_blank=True, default='')


class BOMRequestListSerializer(serializers.ModelSerializer):
    run_number = serializers.IntegerField(source='production_run.run_number', read_only=True)
    run_date = serializers.DateField(source='production_run.date', read_only=True)
    line_name = serializers.CharField(source='production_run.line.name', read_only=True)
    product = serializers.CharField(source='production_run.product', read_only=True)
    requested_by_name = serializers.CharField(
        source='requested_by.full_name', read_only=True, default=''
    )
    reviewed_by_name = serializers.CharField(
        source='reviewed_by.full_name', read_only=True, default=''
    )
    lines_count = serializers.SerializerMethodField()

    class Meta:
        model = BOMRequest
        fields = [
            'id', 'production_run', 'run_number', 'run_date',
            'line_name', 'product', 'sap_doc_entry',
            'required_qty', 'status', 'material_issue_status',
            'remarks', 'rejection_reason',
            'requested_by', 'requested_by_name',
            'reviewed_by', 'reviewed_by_name', 'reviewed_at',
            'lines_count', 'created_at', 'updated_at',
        ]

    def get_lines_count(self, obj):
        return obj.lines.count()


class BOMRequestDetailSerializer(serializers.ModelSerializer):
    run_number = serializers.IntegerField(source='production_run.run_number', read_only=True)
    run_date = serializers.DateField(source='production_run.date', read_only=True)
    line_name = serializers.CharField(source='production_run.line.name', read_only=True)
    product = serializers.CharField(source='production_run.product', read_only=True)
    requested_by_name = serializers.CharField(
        source='requested_by.full_name', read_only=True, default=''
    )
    reviewed_by_name = serializers.CharField(
        source='reviewed_by.full_name', read_only=True, default=''
    )
    lines = BOMRequestLineSerializer(many=True, read_only=True)

    class Meta:
        model = BOMRequest
        fields = [
            'id', 'production_run', 'run_number', 'run_date',
            'line_name', 'product', 'sap_doc_entry',
            'required_qty', 'status', 'material_issue_status',
            'sap_issue_doc_entries',
            'remarks', 'rejection_reason',
            'requested_by', 'requested_by_name',
            'reviewed_by', 'reviewed_by_name', 'reviewed_at',
            'lines', 'created_at', 'updated_at',
        ]


class BOMRequestApproveSerializer(serializers.Serializer):
    lines = BOMLineApprovalSerializer(many=True)


class BOMRequestRejectSerializer(serializers.Serializer):
    reason = serializers.CharField()


class MaterialIssueSerializer(serializers.Serializer):
    posting_date = serializers.DateField(required=False)
    lines = serializers.ListField(
        child=serializers.DictField(), required=False, default=list,
        help_text="[{line_id, quantity}] — defaults to all approved remaining"
    )


# ---------------------------------------------------------------------------
# Finished Goods Receipt
# ---------------------------------------------------------------------------

class FGReceiptCreateSerializer(serializers.Serializer):
    production_run_id = serializers.IntegerField()
    item_code = serializers.CharField(max_length=50, required=False, default='')
    item_name = serializers.CharField(max_length=255, required=False, default='')
    warehouse = serializers.CharField(max_length=20, required=False, default='')
    uom = serializers.CharField(max_length=20, required=False, default='')
    posting_date = serializers.DateField(required=False)


class FGReceiptListSerializer(serializers.ModelSerializer):
    run_number = serializers.IntegerField(
        source='production_run.run_number', read_only=True
    )
    run_date = serializers.DateField(source='production_run.date', read_only=True)
    received_by_name = serializers.CharField(
        source='received_by.full_name', read_only=True, default=''
    )

    class Meta:
        model = FinishedGoodsReceipt
        fields = [
            'id', 'production_run', 'run_number', 'run_date',
            'sap_doc_entry', 'item_code', 'item_name',
            'produced_qty', 'good_qty', 'rejected_qty',
            'warehouse', 'uom', 'posting_date',
            'status', 'sap_receipt_doc_entry', 'sap_error',
            'received_by', 'received_by_name', 'received_at',
            'created_at', 'updated_at',
        ]


class FGReceiptDetailSerializer(FGReceiptListSerializer):
    class Meta(FGReceiptListSerializer.Meta):
        pass


class StockCheckSerializer(serializers.Serializer):
    item_codes = serializers.ListField(
        child=serializers.CharField(max_length=50)
    )
