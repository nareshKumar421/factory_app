"""
stock_dashboard/serializers.py

DRF serializers for validating query parameters and shaping API responses.
All data is read-only (no database writes), so only plain Serializer classes are used.
"""

from rest_framework import serializers


# ---------------------------------------------------------------------------
# Query Parameter Serializers (Input Validation)
# ---------------------------------------------------------------------------


class StockDashboardFilterSerializer(serializers.Serializer):
    """Validates query parameters for the stock dashboard endpoint."""

    search = serializers.CharField(
        required=False,
        max_length=100,
        help_text="Search by item code, item name, or warehouse code",
    )
    warehouse = serializers.CharField(
        required=False,
        max_length=8,
        help_text="Filter by warehouse code (e.g. WH-01)",
    )
    status = serializers.ChoiceField(
        choices=[("all", "All"), ("healthy", "Healthy"), ("low", "Low"), ("critical", "Critical")],
        default="all",
        required=False,
        help_text="Filter by stock health status",
    )
    page = serializers.IntegerField(required=False, default=1, min_value=1)
    page_size = serializers.IntegerField(required=False, default=50, min_value=1, max_value=200)


# ---------------------------------------------------------------------------
# Response Serializers (Output Shape)
# ---------------------------------------------------------------------------


class StockItemSerializer(serializers.Serializer):
    """One row per item-warehouse combination."""

    item_code = serializers.CharField()
    item_name = serializers.CharField()
    warehouse = serializers.CharField()
    on_hand = serializers.FloatField()
    min_stock = serializers.FloatField()
    uom = serializers.CharField()
    stock_status = serializers.CharField()
    health_ratio = serializers.FloatField()


class StockDashboardMetaSerializer(serializers.Serializer):
    total_items = serializers.IntegerField()
    low_stock_count = serializers.IntegerField()
    critical_stock_count = serializers.IntegerField()
    fetched_at = serializers.CharField()
    page = serializers.IntegerField()
    page_size = serializers.IntegerField()
    total_pages = serializers.IntegerField()


class StockDashboardResponseSerializer(serializers.Serializer):
    data = StockItemSerializer(many=True)
    meta = StockDashboardMetaSerializer()
