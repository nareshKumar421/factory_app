"""
inventory_age/serializers.py

DRF serializers for validating query parameters and shaping API responses.
"""

from rest_framework import serializers


# ---------------------------------------------------------------------------
# Query Parameter Serializers (Input)
# ---------------------------------------------------------------------------


class InventoryAgeFilterSerializer(serializers.Serializer):
    """Validates query parameters for the inventory age endpoint."""

    search = serializers.CharField(
        required=False,
        max_length=100,
        help_text="Search by item code or item name",
    )
    warehouse = serializers.CharField(
        required=False,
        max_length=20,
        help_text="Filter by warehouse code",
    )
    item_group = serializers.CharField(
        required=False,
        max_length=100,
        help_text="Filter by item group name (required for report)",
    )
    sub_group = serializers.CharField(
        required=False,
        max_length=100,
        help_text="Filter by sub-group name",
    )
    variety = serializers.CharField(
        required=False,
        max_length=100,
        help_text="Filter by variety",
    )
    min_age = serializers.IntegerField(
        required=False,
        min_value=0,
        help_text="Minimum age in days (e.g. 30, 60, 90, 180, 365)",
    )


# ---------------------------------------------------------------------------
# Response Serializers (Output)
# ---------------------------------------------------------------------------


class InventoryAgeItemSerializer(serializers.Serializer):
    item_code = serializers.CharField()
    item_name = serializers.CharField()
    is_litre = serializers.BooleanField()
    item_group = serializers.CharField()
    unit = serializers.CharField()
    variety = serializers.CharField()
    sku = serializers.CharField()
    sub_group = serializers.CharField()
    warehouse = serializers.CharField()
    on_hand = serializers.FloatField()
    litres = serializers.FloatField()
    in_stock_value = serializers.FloatField()
    calc_price = serializers.FloatField()
    effective_date = serializers.CharField(allow_null=True)
    days_age = serializers.IntegerField()


class InventoryAgeMetaSerializer(serializers.Serializer):
    total_items = serializers.IntegerField()
    total_value = serializers.FloatField()
    total_quantity = serializers.FloatField()
    total_litres = serializers.FloatField()
    warehouse_count = serializers.IntegerField()
    fetched_at = serializers.CharField()


class WarehouseSummarySerializer(serializers.Serializer):
    warehouse = serializers.CharField()
    item_count = serializers.IntegerField()
    total_value = serializers.FloatField()
    total_quantity = serializers.FloatField()
    total_litres = serializers.FloatField()


class ItemGroupOptionSerializer(serializers.Serializer):
    item_group_code = serializers.IntegerField()
    item_group_name = serializers.CharField()


class FilterOptionsSerializer(serializers.Serializer):
    item_groups = ItemGroupOptionSerializer(many=True)
    sub_groups = serializers.ListField(child=serializers.CharField())
    warehouses = serializers.ListField(child=serializers.CharField())
    varieties = serializers.ListField(child=serializers.CharField())


class InventoryAgeResponseSerializer(serializers.Serializer):
    data = InventoryAgeItemSerializer(many=True)
    meta = InventoryAgeMetaSerializer()
    warehouse_summary = WarehouseSummarySerializer(many=True)
