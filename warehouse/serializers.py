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


# A BOM request comes from either a production run or a blowing run — these
# helpers present the originating run uniformly for both.
def _bom_run_number(obj):
    r = obj.production_run or obj.blowing_run
    return getattr(r, 'run_number', None) if r else None


def _bom_run_date(obj):
    r = obj.production_run or obj.blowing_run
    return getattr(r, 'date', None) if r else None


def _bom_line_name(obj):
    if obj.production_run_id:
        return getattr(getattr(obj.production_run, 'line', None), 'name', '')
    if obj.blowing_run_id:
        return getattr(getattr(obj.blowing_run, 'machine', None), 'name', '')
    return ''


def _bom_product(obj):
    if obj.production_run_id:
        return obj.production_run.product
    if obj.blowing_run_id:
        spec = getattr(obj.blowing_run, 'preform_spec', None)
        return f"{spec.make} {spec.gram:g}g preform" if spec else ''
    return ''


class BOMRunFieldsMixin(serializers.Serializer):
    run_number = serializers.SerializerMethodField()
    run_date = serializers.SerializerMethodField()
    line_name = serializers.SerializerMethodField()
    product = serializers.SerializerMethodField()
    source = serializers.CharField(read_only=True)

    def get_run_number(self, obj):
        return _bom_run_number(obj)

    def get_run_date(self, obj):
        return _bom_run_date(obj)

    def get_line_name(self, obj):
        return _bom_line_name(obj)

    def get_product(self, obj):
        return _bom_product(obj)


class BOMRequestListSerializer(BOMRunFieldsMixin, serializers.ModelSerializer):
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
            'id', 'production_run', 'blowing_run', 'source', 'run_number', 'run_date',
            'line_name', 'product', 'sap_doc_entry',
            'required_qty', 'status', 'material_issue_status',
            'remarks', 'rejection_reason',
            'requested_by', 'requested_by_name',
            'reviewed_by', 'reviewed_by_name', 'reviewed_at',
            'lines_count', 'created_at', 'updated_at',
        ]

    def get_lines_count(self, obj):
        return obj.lines.count()


class BOMRequestDetailSerializer(BOMRunFieldsMixin, serializers.ModelSerializer):
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
            'id', 'production_run', 'blowing_run', 'source', 'run_number', 'run_date',
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
