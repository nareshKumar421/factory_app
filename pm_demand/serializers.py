"""
pm_demand/serializers.py

Query-parameter validation and response shape for the PM Demand dashboard.
"""

from rest_framework import serializers

from .constants import DEFAULT_TOP_N, MAX_RANGE_DAYS, MAX_TOP_N, SOURCE_SAP, SOURCES


# ---------------------------------------------------------------------------
# Input
# ---------------------------------------------------------------------------


class PmDemandFilterSerializer(serializers.Serializer):
    """Validates the report endpoint's query parameters."""

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
        help_text=f"How many items each top list returns (1-{MAX_TOP_N})",
    )
    source = serializers.ChoiceField(
        required=False,
        default=SOURCE_SAP,
        choices=SOURCES,
        help_text=(
            "Where the quantities come from: 'sap' (goods issues and invoices) "
            "or 'app' (FactoryFlow's own production runs, approved BOMs and "
            "gate-outs). The recipe and the stock always come from SAP."
        ),
    )
    include_intercompany = serializers.BooleanField(
        required=False,
        default=True,
        help_text=(
            "Count invoices to group companies as dispatch. True by default: "
            "the packing material physically left the factory. Set false for "
            "the third-party-only view."
        ),
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


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


class PmDemandItemSerializer(serializers.Serializer):
    """One packing-material item, across all three columns."""

    item_code = serializers.CharField()
    item_name = serializers.CharField()
    sub_group = serializers.CharField(allow_blank=True)
    uom = serializers.CharField(allow_blank=True)
    unit_price = serializers.FloatField()
    in_house = serializers.BooleanField()
    in_house_qty = serializers.FloatField()

    consumed_qty = serializers.FloatField()
    consumed_value = serializers.FloatField()
    bom_qty = serializers.FloatField()
    bom_value = serializers.FloatField()
    variance_qty = serializers.FloatField()
    variance_value = serializers.FloatField()
    variance_pct = serializers.FloatField(allow_null=True)
    wastage_qty = serializers.FloatField()
    wastage_value = serializers.FloatField()
    dispatched_qty = serializers.FloatField()
    dispatched_value = serializers.FloatField()
    retained_qty = serializers.FloatField()
    retained_value = serializers.FloatField()
    per_1000_fg = serializers.FloatField(allow_null=True)
    share_pct = serializers.FloatField(required=False, default=0.0)

    # Cover. days_cover and avg_daily_qty are null when nothing was consumed
    # in the period -- there is no burn rate to divide by, which is not the
    # same as unbounded cover.
    stock_qty = serializers.FloatField()
    stock_value = serializers.FloatField()
    avg_daily_qty = serializers.FloatField(allow_null=True)
    days_cover = serializers.FloatField(allow_null=True)
    # Cover once what is already bought arrives. This, not days_cover, is what
    # cover_status and the watch-list ordering are based on.
    days_cover_incl_po = serializers.FloatField(allow_null=True)
    open_po_qty = serializers.FloatField()
    open_po_lines = serializers.IntegerField()
    open_po_earliest_due = serializers.CharField(allow_null=True)
    open_po_overdue = serializers.BooleanField()
    cover_status = serializers.ChoiceField(
        choices=["critical", "low", "ok", "unknown"]
    )


class PmDemandUpstreamItemSerializer(serializers.Serializer):
    """One item consumed at the blowing line, reported apart from the totals."""

    item_code = serializers.CharField()
    item_name = serializers.CharField()
    sub_group = serializers.CharField(allow_blank=True)
    uom = serializers.CharField(allow_blank=True)
    unit_price = serializers.FloatField()
    consumed_qty = serializers.FloatField()
    consumed_value = serializers.FloatField()
    share_pct = serializers.FloatField()


class PmDemandFamilySerializer(serializers.Serializer):
    """One packaging family (``OITM.U_Sub_Group``)."""

    sub_group = serializers.CharField()
    item_count = serializers.IntegerField()
    consumed_value = serializers.FloatField()
    bom_value = serializers.FloatField()
    variance_value = serializers.FloatField()
    dispatched_value = serializers.FloatField()
    wastage_value = serializers.FloatField()
    consumed_share_pct = serializers.FloatField()


class PmDemandSummarySerializer(serializers.Serializer):
    fg_produced_qty = serializers.FloatField()
    fg_dispatched_qty = serializers.FloatField()
    fg_dispatched_all_qty = serializers.FloatField()
    fg_dispatched_intercompany_qty = serializers.FloatField()
    fg_dispatched_third_party_qty = serializers.FloatField()
    fg_returns_qty = serializers.FloatField()
    dispatch_ratio_pct = serializers.FloatField(allow_null=True)

    pm_items = serializers.IntegerField()
    pm_consumed_value = serializers.FloatField()
    pm_bom_value = serializers.FloatField()
    pm_variance_value = serializers.FloatField()
    pm_variance_pct = serializers.FloatField(allow_null=True)
    pm_dispatched_value = serializers.FloatField()
    pm_retained_value = serializers.FloatField()
    pm_wastage_value = serializers.FloatField()
    pm_stock_value = serializers.FloatField()
    pm_items_critical_cover = serializers.IntegerField()
    pm_items_low_cover = serializers.IntegerField()
    pm_items_overdue_po = serializers.IntegerField()


class PmDemandCoverageSerializer(serializers.Serializer):
    fg_items = serializers.IntegerField()
    fg_items_with_bom = serializers.IntegerField()
    fg_items_without_bom = serializers.ListField(child=serializers.CharField())
    qty_total = serializers.FloatField()
    qty_with_bom = serializers.FloatField()
    qty_covered_pct = serializers.FloatField()


class PmDemandMetaSerializer(serializers.Serializer):
    company_code = serializers.CharField()
    date_from = serializers.CharField()
    date_to = serializers.CharField()
    top_n = serializers.IntegerField()
    include_intercompany = serializers.BooleanField()
    source = serializers.CharField()
    # 'issued' on SAP, 'approved' on app -- a different fact, named.
    consumption_basis = serializers.CharField()
    source_notes = serializers.ListField(child=serializers.CharField())
    ranked_by = serializers.CharField()
    pm_item_group = serializers.IntegerField()
    pm_item_group_name = serializers.CharField(allow_blank=True)
    fg_warehouses = serializers.ListField(child=serializers.CharField())
    consumption_warehouses = serializers.ListField(child=serializers.CharField())
    wastage_warehouses = serializers.ListField(child=serializers.CharField())
    upstream_warehouses = serializers.ListField(child=serializers.CharField())
    stock_warehouses = serializers.ListField(child=serializers.CharField())
    period_working_days = serializers.IntegerField()
    cover_critical_days = serializers.IntegerField()
    cover_low_days = serializers.IntegerField()
    intercompany_card_codes = serializers.ListField(child=serializers.CharField())
    production_bom_coverage = PmDemandCoverageSerializer()
    dispatch_bom_coverage = PmDemandCoverageSerializer()
    fetched_at = serializers.CharField()


class PmDemandReportResponseSerializer(serializers.Serializer):
    summary = PmDemandSummarySerializer()
    production_top = PmDemandItemSerializer(many=True)
    dispatch_top = PmDemandItemSerializer(many=True)
    families = PmDemandFamilySerializer(many=True)
    cover_watch = PmDemandItemSerializer(many=True)
    upstream = PmDemandUpstreamItemSerializer(many=True)
    meta = PmDemandMetaSerializer()
