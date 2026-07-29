from rest_framework import serializers

from .models import WarehouseMovement, WarehouseMovementLine, WarehouseRole


class WarehouseRoleSerializer(serializers.ModelSerializer):
    company_code = serializers.CharField(source="company.code", read_only=True)

    class Meta:
        model = WarehouseRole
        fields = [
            "id",
            "company_code",
            "whs_code",
            "warehouse_name",
            "role",
            "family",
            "is_grpo_target",
            "is_bom_issue_point",
            "feeds_whs_code",
            "is_active",
            "needs_review",
            "notes",
            "updated_at",
        ]
        read_only_fields = ["id", "company_code", "updated_at"]


class StockBoardWarehouseSerializer(serializers.Serializer):
    whs_code = serializers.CharField()
    warehouse_name = serializers.CharField()
    role = serializers.CharField()
    family = serializers.CharField()
    is_grpo_target = serializers.BooleanField()
    is_bom_issue_point = serializers.BooleanField()
    feeds_whs_code = serializers.CharField(allow_blank=True)
    needs_review = serializers.BooleanField()
    notes = serializers.CharField(allow_blank=True)
    total_items = serializers.IntegerField()
    total_on_hand = serializers.FloatField()
    total_value = serializers.FloatField()
    in_sap = serializers.BooleanField()


class StockBoardResponseSerializer(serializers.Serializer):
    company_code = serializers.CharField()
    warehouses = StockBoardWarehouseSerializer(many=True)
    unmapped = serializers.ListField(child=serializers.DictField())


class WarehouseStockFilterSerializer(serializers.Serializer):
    pm_only = serializers.BooleanField(required=False, default=False)
    search = serializers.CharField(required=False, allow_blank=True)
    stock_filter = serializers.ChoiceField(
        choices=["with_stock", "zero_stock", "all"], required=False, default="with_stock"
    )
    page = serializers.IntegerField(required=False, default=1, min_value=1)
    page_size = serializers.IntegerField(required=False, default=50, min_value=1, max_value=500)


class TransferLineInputSerializer(serializers.Serializer):
    item_code = serializers.CharField()
    quantity = serializers.DecimalField(max_digits=19, decimal_places=6, min_value=0)
    item_name = serializers.CharField(required=False, allow_blank=True, default="")
    uom = serializers.CharField(required=False, allow_blank=True, default="")


class TransferRequestSerializer(serializers.Serializer):
    """POST body to move PM from a store into the company's BOM issue point."""

    from_whs = serializers.CharField()
    lines = TransferLineInputSerializer(many=True)
    posting_date = serializers.DateField(required=False)
    reference = serializers.CharField(required=False, allow_blank=True, default="")
    # Explicit override; when omitted the server uses the writes-enabled flag.
    dry_run = serializers.BooleanField(required=False, default=None, allow_null=True)

    def validate_lines(self, value):
        if not value:
            raise serializers.ValidationError("At least one line is required.")
        return value


class WarehouseMovementLineSerializer(serializers.ModelSerializer):
    class Meta:
        model = WarehouseMovementLine
        fields = ["item_code", "item_name", "quantity", "uom",
                  "from_whs_code", "to_whs_code", "base_line"]


class WarehouseMovementSerializer(serializers.ModelSerializer):
    company_code = serializers.CharField(source="company.code", read_only=True)
    lines = WarehouseMovementLineSerializer(many=True, read_only=True)

    class Meta:
        model = WarehouseMovement
        fields = [
            "id", "company_code", "movement_type", "status",
            "from_whs_code", "to_whs_code", "sap_object_type", "sap_doc_entry",
            "sap_doc_num", "itr_doc_entry", "posting_date", "error_message",
            "reference", "created_at", "lines",
        ]
