"""
warehouse/serializers.py

DRF serializers for validating query parameters and shaping API responses.
Phase 1: Inventory visibility (read-only from SAP HANA).
"""

from rest_framework import serializers


# ---------------------------------------------------------------------------
# Input Serializers (Query Parameters)
# ---------------------------------------------------------------------------


class InventoryFilterSerializer(serializers.Serializer):
    """Validates query parameters for the inventory list endpoint."""

    search = serializers.CharField(
        required=False,
        max_length=100,
        help_text="Search by item code or name",
    )
    warehouse = serializers.CharField(
        required=False,
        max_length=20,
        help_text="Filter by warehouse code",
    )
    item_group = serializers.CharField(
        required=False,
        max_length=20,
        help_text="Filter by item group code",
    )


class MovementHistoryFilterSerializer(serializers.Serializer):
    """Validates query parameters for movement history."""

    warehouse = serializers.CharField(
        required=False,
        max_length=20,
        help_text="Filter by warehouse code",
    )
    from_date = serializers.DateField(
        required=False,
        help_text="Start date (YYYY-MM-DD)",
    )
    to_date = serializers.DateField(
        required=False,
        help_text="End date (YYYY-MM-DD)",
    )


class DashboardFilterSerializer(serializers.Serializer):
    """Validates query parameters for the dashboard."""

    warehouse_code = serializers.CharField(
        required=False,
        max_length=20,
        help_text="Filter by warehouse code",
    )


class BatchExpiryFilterSerializer(serializers.Serializer):
    """Validates query parameters for batch expiry report."""

    warehouse = serializers.CharField(
        required=False,
        max_length=20,
        help_text="Filter by warehouse code",
    )


# ---------------------------------------------------------------------------
# Output Serializers (Response)
# ---------------------------------------------------------------------------


class FilterOptionWarehouseSerializer(serializers.Serializer):
    code = serializers.CharField()
    name = serializers.CharField()


class FilterOptionItemGroupSerializer(serializers.Serializer):
    code = serializers.IntegerField()
    name = serializers.CharField()


class FilterOptionsResponseSerializer(serializers.Serializer):
    warehouses = FilterOptionWarehouseSerializer(many=True)
    item_groups = FilterOptionItemGroupSerializer(many=True)


class InventoryItemSerializer(serializers.Serializer):
    item_code = serializers.CharField()
    item_name = serializers.CharField()
    item_group = serializers.CharField()
    warehouse = serializers.CharField()
    on_hand = serializers.FloatField()
    committed = serializers.FloatField()
    on_order = serializers.FloatField()
    available = serializers.FloatField()
    uom = serializers.CharField()
    is_batch_managed = serializers.BooleanField()
    min_stock = serializers.FloatField()
    is_below_min = serializers.BooleanField()


class InventoryMetaSerializer(serializers.Serializer):
    total_items = serializers.IntegerField()
    total_on_hand = serializers.FloatField()
    warehouse_count = serializers.IntegerField()
    below_min_count = serializers.IntegerField()
    fetched_at = serializers.CharField()


class InventoryResponseSerializer(serializers.Serializer):
    data = InventoryItemSerializer(many=True)
    meta = InventoryMetaSerializer()


class WarehouseStockSerializer(serializers.Serializer):
    warehouse_code = serializers.CharField()
    warehouse_name = serializers.CharField()
    on_hand = serializers.FloatField()
    committed = serializers.FloatField()
    on_order = serializers.FloatField()
    available = serializers.FloatField()
    min_stock = serializers.FloatField()


class BatchSerializer(serializers.Serializer):
    item_code = serializers.CharField()
    batch_number = serializers.CharField()
    warehouse_code = serializers.CharField()
    quantity = serializers.FloatField()
    admission_date = serializers.CharField(allow_null=True)
    expiry_date = serializers.CharField(allow_null=True)


class ItemDetailSerializer(serializers.Serializer):
    item_code = serializers.CharField()
    item_name = serializers.CharField()
    item_group = serializers.CharField()
    uom = serializers.CharField()
    is_batch_managed = serializers.BooleanField()
    variety = serializers.CharField()
    sub_group = serializers.CharField()
    total_on_hand = serializers.FloatField()
    total_committed = serializers.FloatField()
    total_on_order = serializers.FloatField()
    total_available = serializers.FloatField()
    warehouse_stock = WarehouseStockSerializer(many=True)
    batches = BatchSerializer(many=True)


class MovementSerializer(serializers.Serializer):
    item_code = serializers.CharField()
    warehouse = serializers.CharField()
    in_qty = serializers.FloatField()
    out_qty = serializers.FloatField()
    trans_type = serializers.IntegerField()
    create_date = serializers.CharField(allow_null=True)
    doc_num = serializers.IntegerField()
    balance = serializers.FloatField()


class MovementMetaSerializer(serializers.Serializer):
    total_movements = serializers.IntegerField()
    fetched_at = serializers.CharField()


class MovementHistoryResponseSerializer(serializers.Serializer):
    item_code = serializers.CharField()
    movements = MovementSerializer(many=True)
    meta = MovementMetaSerializer()


class LowStockAlertSerializer(serializers.Serializer):
    item_code = serializers.CharField()
    item_name = serializers.CharField()
    warehouse = serializers.CharField()
    on_hand = serializers.FloatField()
    min_stock = serializers.FloatField()
    uom = serializers.CharField()
    shortage = serializers.FloatField()


class DashboardMetaSerializer(serializers.Serializer):
    fetched_at = serializers.CharField()


class DashboardSummaryResponseSerializer(serializers.Serializer):
    low_stock_alerts = LowStockAlertSerializer(many=True)
    low_stock_count = serializers.IntegerField()
    meta = DashboardMetaSerializer()


class BatchExpiryItemSerializer(serializers.Serializer):
    item_code = serializers.CharField()
    item_name = serializers.CharField()
    item_group = serializers.CharField()
    batch_number = serializers.CharField()
    warehouse = serializers.CharField()
    quantity = serializers.FloatField()
    admission_date = serializers.CharField(allow_null=True)
    expiry_date = serializers.CharField(allow_null=True)
    days_age = serializers.IntegerField()
    uom = serializers.CharField()
    variety = serializers.CharField()


class BatchExpiryMetaSerializer(serializers.Serializer):
    total_batches = serializers.IntegerField()
    fetched_at = serializers.CharField()


class BatchExpiryResponseSerializer(serializers.Serializer):
    data = BatchExpiryItemSerializer(many=True)
    meta = BatchExpiryMetaSerializer()
