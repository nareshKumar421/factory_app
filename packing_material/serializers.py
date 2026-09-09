"""
packing_material/serializers.py

Query-parameter validation and response shape for the packing-material board.
"""

from rest_framework import serializers

from .constants import DEFAULT_TOP_N, MAX_RANGE_DAYS, MAX_TOP_N, SOURCE_SAP, SOURCES


# ---------------------------------------------------------------------------
# Input
# ---------------------------------------------------------------------------


class PeriodFilterSerializer(serializers.Serializer):
    """The period and the length of the list, for both top sections."""

    date_from = serializers.DateField(
        required=True, help_text="First document date to count, inclusive"
    )
    date_to = serializers.DateField(
        required=True, help_text="Last document date to count, inclusive"
    )
    top = serializers.IntegerField(
        required=False,
        default=DEFAULT_TOP_N,
        min_value=1,
        max_value=MAX_TOP_N,
        help_text=f"How many items the list returns (1-{MAX_TOP_N})",
    )

    def validate(self, attrs):
        date_from = attrs["date_from"]
        date_to = attrs["date_to"]

        if date_from > date_to:
            raise serializers.ValidationError(
                {"date_from": "date_from cannot be after date_to."}
            )

        span_days = (date_to - date_from).days + 1
        if span_days > MAX_RANGE_DAYS:
            raise serializers.ValidationError(
                {
                    "date_to": (
                        f"Range is {span_days} days; the widest allowed is "
                        f"{MAX_RANGE_DAYS}."
                    )
                }
            )
        return attrs


class DispatchFilterSerializer(PeriodFilterSerializer):
    """The dispatch section also chooses which register it reads."""

    source = serializers.ChoiceField(
        required=False,
        default=SOURCE_SAP,
        choices=SOURCES,
        help_text=(
            "Which register counts as dispatch: 'sap' (A/R invoices net of "
            "credit notes) or 'app' (FactoryFlow bills that went out through "
            "docking). The recipe comes from SAP either way."
        ),
    )


# ---------------------------------------------------------------------------
# Output -- shared
# ---------------------------------------------------------------------------


class ItemGroupMetaSerializer(serializers.Serializer):
    """Which item group was counted, and whether SAP still agrees it is that."""

    pm_item_group = serializers.IntegerField()
    pm_item_group_name = serializers.CharField(allow_blank=True)
    pm_item_group_matches = serializers.BooleanField()


# ---------------------------------------------------------------------------
# Output -- stock
# ---------------------------------------------------------------------------


class StockItemSerializer(serializers.Serializer):
    """One packing-material item in one warehouse."""

    item_code = serializers.CharField()
    item_name = serializers.CharField(allow_blank=True)
    sub_group = serializers.CharField(allow_blank=True)
    uom = serializers.CharField(allow_blank=True)
    unit_price = serializers.FloatField()
    stock_qty = serializers.FloatField()
    stock_value = serializers.FloatField()


class StockWarehouseSerializer(serializers.Serializer):
    """One card: a warehouse, its totals, and everything in it."""

    code = serializers.CharField()
    name = serializers.CharField(allow_blank=True)
    # SAP has this warehouse flagged decommissioned. Reported rather than
    # filtered, so a frozen balance shows as frozen instead of as empty.
    inactive = serializers.BooleanField()
    # False when SAP has no such warehouse in this company at all.
    exists = serializers.BooleanField()
    item_count = serializers.IntegerField()
    total_qty = serializers.FloatField()
    total_value = serializers.FloatField()
    share_pct = serializers.FloatField()
    items = StockItemSerializer(many=True)


class StockTotalSerializer(serializers.Serializer):
    warehouse_count = serializers.IntegerField()
    # DISTINCT items across the warehouses, not the sum of their counts.
    item_count = serializers.IntegerField()
    total_qty = serializers.FloatField()
    total_value = serializers.FloatField()


class StockMetaSerializer(ItemGroupMetaSerializer):
    company_code = serializers.CharField()
    stock_warehouses = serializers.ListField(child=serializers.CharField())
    ranked_by = serializers.CharField()
    fetched_at = serializers.CharField()


class StockResponseSerializer(serializers.Serializer):
    warehouses = StockWarehouseSerializer(many=True)
    total = StockTotalSerializer()
    meta = StockMetaSerializer()


# ---------------------------------------------------------------------------
# Output -- the two top lists
# ---------------------------------------------------------------------------


class TopItemSerializer(serializers.Serializer):
    """One ranked packing-material item."""

    rank = serializers.IntegerField()
    item_code = serializers.CharField()
    item_name = serializers.CharField(allow_blank=True)
    sub_group = serializers.CharField(allow_blank=True)
    uom = serializers.CharField(allow_blank=True)
    unit_price = serializers.FloatField()
    qty = serializers.FloatField()
    value = serializers.FloatField()
    # Share of the whole period, not of the rows shown.
    share_pct = serializers.FloatField()


class TopTotalsSerializer(serializers.Serializer):
    item_count = serializers.IntegerField()
    total_qty = serializers.FloatField()
    total_value = serializers.FloatField()
    shown_qty = serializers.FloatField()
    shown_value = serializers.FloatField()
    shown_share_pct = serializers.FloatField()


class PeriodMetaSerializer(ItemGroupMetaSerializer):
    company_code = serializers.CharField()
    date_from = serializers.CharField()
    date_to = serializers.CharField()
    top_n = serializers.IntegerField()
    ranked_by = serializers.CharField()
    # 'issued' for production; 'invoiced' or 'gated-out' for dispatch.
    basis = serializers.CharField()
    fetched_at = serializers.CharField()


class ProductionMetaSerializer(PeriodMetaSerializer):
    consumption_warehouses = serializers.ListField(child=serializers.CharField())


class ProductionResponseSerializer(serializers.Serializer):
    items = TopItemSerializer(many=True)
    totals = TopTotalsSerializer()
    meta = ProductionMetaSerializer()


class DispatchCoverageSerializer(serializers.Serializer):
    """How much of what was dispatched could actually be exploded."""

    fg_items = serializers.IntegerField()
    fg_items_with_bom = serializers.IntegerField()
    fg_items_without_bom = serializers.ListField(child=serializers.CharField())
    fg_items_without_bom_count = serializers.IntegerField()
    # Absolute volume, not the net figure -- a net-negative item with no recipe
    # would otherwise shrink the denominator and report over 100% coverage.
    # The net figure is summary.fg_dispatched_qty.
    qty_total = serializers.FloatField()
    qty_with_bom = serializers.FloatField()
    qty_covered_pct = serializers.FloatField()
    # Packaging invoiced as itself rather than inside a finished good. Counted
    # here, never added to the ranked list.
    direct_pm_items = serializers.IntegerField()
    direct_pm_qty = serializers.FloatField()
    # Lines that are neither finished goods nor packaging.
    other_item_count = serializers.IntegerField()
    other_qty = serializers.FloatField()


class DispatchSummarySerializer(serializers.Serializer):
    fg_dispatched_qty = serializers.FloatField()
    fg_intercompany_qty = serializers.FloatField()
    fg_third_party_qty = serializers.FloatField()
    fg_returns_qty = serializers.FloatField()
    fg_item_count = serializers.IntegerField()
    # Bills. On SAP the invoices carrying finished goods; on FactoryFlow the
    # bill documents on the trucks that left.
    document_count = serializers.IntegerField()
    # Trucks. Null on SAP, which has no such thing.
    gate_out_count = serializers.IntegerField(allow_null=True)


class DispatchMetaSerializer(PeriodMetaSerializer):
    source = serializers.CharField()
    include_intercompany = serializers.BooleanField()
    # False on the FactoryFlow source, which cannot tell a group-company truck
    # from any other -- so its intercompany split is zero, not measured.
    intercompany_known = serializers.BooleanField()


class DispatchResponseSerializer(serializers.Serializer):
    items = TopItemSerializer(many=True)
    totals = TopTotalsSerializer()
    coverage = DispatchCoverageSerializer()
    summary = DispatchSummarySerializer()
    meta = DispatchMetaSerializer()
