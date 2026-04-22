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
        default="",
        help_text="Comma-separated warehouse codes to filter by (e.g. 'WH-01,BH-PM')",
    )

    def validate_warehouse(self, value):
        if not value:
            return []
        return [w.strip() for w in value.split(",") if w.strip()]
    status = serializers.CharField(
        required=False,
        default="",
        help_text="Comma-separated stock health statuses to filter by (e.g. 'low,critical')",
    )

    def validate_status(self, value):
        if not value:
            return []
        allowed = {"healthy", "low", "critical", "unset"}
        statuses = [s.strip() for s in value.split(",") if s.strip()]
        invalid = set(statuses) - allowed
        if invalid:
            raise serializers.ValidationError(
                f"Invalid status values: {', '.join(invalid)}. Allowed: {', '.join(sorted(allowed))}"
            )
        return statuses
    page = serializers.IntegerField(required=False, default=1, min_value=1)
    page_size = serializers.IntegerField(required=False, default=50, min_value=1, max_value=200)


# ---------------------------------------------------------------------------
# Response Serializers (Output Shape)
# ---------------------------------------------------------------------------


class StockItemSerializer(serializers.Serializer):
    """One row per item-warehouse (or grouped item when multi-warehouse)."""

    item_code = serializers.CharField()
    item_name = serializers.CharField()
    warehouse = serializers.CharField(default="")
    on_hand = serializers.FloatField()
    min_stock = serializers.FloatField()
    uom = serializers.CharField()
    stock_status = serializers.CharField()
    health_ratio = serializers.FloatField()
    # Grouped-only fields
    warehouse_count = serializers.IntegerField(default=1)
    has_warning = serializers.BooleanField(default=False)


class StockDashboardMetaSerializer(serializers.Serializer):
    total_items = serializers.IntegerField()
    healthy_count = serializers.IntegerField()
    low_stock_count = serializers.IntegerField()
    critical_stock_count = serializers.IntegerField()
    warehouses = serializers.ListField(child=serializers.CharField())
    fetched_at = serializers.CharField()
    page = serializers.IntegerField()
    page_size = serializers.IntegerField()
    total_pages = serializers.IntegerField()


class StockDashboardResponseSerializer(serializers.Serializer):
    data = StockItemSerializer(many=True)
    meta = StockDashboardMetaSerializer()


# ---------------------------------------------------------------------------
# Item Detail (expand) Serializers
# ---------------------------------------------------------------------------


class ItemDetailFilterSerializer(serializers.Serializer):
    warehouse = serializers.CharField(
        required=True,
        help_text="Comma-separated warehouse codes",
    )

    def validate_warehouse(self, value):
        return [w.strip() for w in value.split(",") if w.strip()]


class ItemDetailResponseSerializer(serializers.Serializer):
    data = StockItemSerializer(many=True)
