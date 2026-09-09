"""
packing_material/serializers.py

Query-parameter validation and response shape for the packing-material board.
"""

from rest_framework import serializers

from .constants import (
    DEFAULT_TOP_N,
    MAX_PLAN_LIST_LIMIT,
    MAX_RANGE_DAYS,
    MAX_TOP_N,
    PLAN_LIST_LIMIT,
    SOURCE_SAP,
    SOURCES,
)


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


# ---------------------------------------------------------------------------
# Input -- the requirement board
# ---------------------------------------------------------------------------


class RequirementFilterSerializer(serializers.Serializer):
    """Which plan to explode.

    Optional: with no plan named the service picks the one whose period
    contains today, which is what somebody opening the board wants nine times
    in ten. There is no date range to validate -- the period `Issue (PC)`
    counts is the plan's own, not something a caller may set, because a window
    that did not line up with the plan would net movements against a
    requirement they were never drawn for.
    """

    abs_id = serializers.IntegerField(
        required=False,
        allow_null=True,
        min_value=1,
        help_text=(
            "SAP OFCT AbsID of the production plan. Omit for the plan covering "
            "today."
        ),
    )


class PlanListFilterSerializer(serializers.Serializer):
    limit = serializers.IntegerField(
        required=False,
        default=PLAN_LIST_LIMIT,
        min_value=1,
        max_value=MAX_PLAN_LIST_LIMIT,
        help_text=f"How many plan headers to list (1-{MAX_PLAN_LIST_LIMIT})",
    )


# ---------------------------------------------------------------------------
# Output -- the requirement board
# ---------------------------------------------------------------------------


class PlanHeaderSerializer(serializers.Serializer):
    """One production plan as SAP holds it."""

    abs_id = serializers.IntegerField()
    code = serializers.CharField(allow_blank=True)
    name = serializers.CharField(allow_blank=True)
    start_date = serializers.CharField(allow_null=True)
    end_date = serializers.CharField(allow_null=True)
    # 'M' monthly, 'W' weekly, as SAP records it on OFCT.
    form_view = serializers.CharField(allow_blank=True)
    item_count = serializers.IntegerField()
    planned_qty = serializers.FloatField()


class PlanListMetaSerializer(serializers.Serializer):
    company_code = serializers.CharField()
    default_abs_id = serializers.IntegerField(allow_null=True)
    as_of = serializers.CharField()
    fetched_at = serializers.CharField()


class PlanListResponseSerializer(serializers.Serializer):
    plans = PlanHeaderSerializer(many=True)
    meta = PlanListMetaSerializer()


class RequirementDriverSerializer(serializers.Serializer):
    """One finished good driving a component's requirement."""

    parent_code = serializers.CharField(allow_blank=True)
    parent_name = serializers.CharField(allow_blank=True)
    plan_qty = serializers.FloatField()
    qty_per_unit = serializers.FloatField()
    required_qty = serializers.FloatField()


class RequirementRowSerializer(serializers.Serializer):
    """One packing-material component the plan needs."""

    item_code = serializers.CharField()
    item_name = serializers.CharField(allow_blank=True)
    sub_group = serializers.CharField(allow_blank=True)
    uom = serializers.CharField(allow_blank=True)
    unit_price = serializers.FloatField()

    # The seven columns, in reading order.
    planning_qty = serializers.FloatField()
    issued_pc_qty = serializers.FloatField()
    rest_planning_qty = serializers.FloatField()
    on_hand_qty = serializers.FloatField()
    req_qty = serializers.FloatField()
    open_po_qty = serializers.FloatField()
    req_after_po_qty = serializers.FloatField()

    # How the issue figure was made up: transferred up from the stores, or
    # made in-house straight onto the floor. Both count as plan fulfilled.
    issued_transfer_qty = serializers.FloatField()
    issued_produced_qty = serializers.FloatField()
    issued_other_qty = serializers.FloatField()

    # The shortfall as a positive magnitude, and what it costs to close.
    short_qty = serializers.FloatField()
    short_value = serializers.FloatField()

    sku_count = serializers.IntegerField()
    po_lines = serializers.IntegerField()
    po_earliest_due = serializers.CharField(allow_null=True)
    po_latest_due = serializers.CharField(allow_null=True)

    over_issued = serializers.BooleanField()
    po_covers_shortage = serializers.BooleanField()
    po_due_after_plan = serializers.BooleanField()
    po_overdue = serializers.BooleanField()

    drivers = RequirementDriverSerializer(many=True)
    driver_count = serializers.IntegerField()


class RequirementTotalsSerializer(serializers.Serializer):
    item_count = serializers.IntegerField()
    planning_qty = serializers.FloatField()
    issued_pc_qty = serializers.FloatField()
    issued_transfer_qty = serializers.FloatField()
    issued_produced_qty = serializers.FloatField()
    rest_planning_qty = serializers.FloatField()
    on_hand_qty = serializers.FloatField()
    open_po_qty = serializers.FloatField()
    # Shortage summed as a positive magnitude, so a surplus on one item can
    # never cancel a shortage on another.
    short_before_po_count = serializers.IntegerField()
    short_before_po_qty = serializers.FloatField()
    short_after_po_count = serializers.IntegerField()
    short_after_po_qty = serializers.FloatField()
    short_after_po_value = serializers.FloatField()
    covered_by_po_count = serializers.IntegerField()
    po_due_after_plan_count = serializers.IntegerField()
    po_overdue_count = serializers.IntegerField()
    over_issued_count = serializers.IntegerField()
    surplus_count = serializers.IntegerField()


class PlanCoverageItemSerializer(serializers.Serializer):
    item_code = serializers.CharField(allow_blank=True)
    item_name = serializers.CharField(allow_blank=True)
    plan_qty = serializers.FloatField()


class PlanCoverageSerializer(serializers.Serializer):
    """How much of the plan the requirement actually accounts for."""

    plan_item_count = serializers.IntegerField()
    plan_qty = serializers.FloatField()
    items_without_bom = serializers.IntegerField()
    items_without_bom_qty = serializers.FloatField()
    items_without_bom_list = PlanCoverageItemSerializer(many=True)
    items_with_bom_without_pm = serializers.IntegerField()
    # Share of PLANNED QUANTITY, not of item count: three missing recipes out
    # of 84 matters or does not entirely depending on how big those three are.
    qty_covered_pct = serializers.FloatField()


class UnplannedIssueItemSerializer(serializers.Serializer):
    item_code = serializers.CharField()
    item_name = serializers.CharField(allow_blank=True)
    sub_group = serializers.CharField(allow_blank=True)
    uom = serializers.CharField(allow_blank=True)
    unit_price = serializers.FloatField()
    qty = serializers.FloatField()


class UnplannedIssueSerializer(serializers.Serializer):
    """Packing material that reached the floor without being in the plan."""

    item_count = serializers.IntegerField()
    qty = serializers.FloatField()
    items = UnplannedIssueItemSerializer(many=True)


class RequirementMetaSerializer(ItemGroupMetaSerializer):
    company_code = serializers.CharField()
    date_from = serializers.CharField()
    date_to = serializers.CharField()
    as_of = serializers.CharField()
    issue_warehouses = serializers.ListField(child=serializers.CharField())
    supply_warehouses = serializers.ListField(child=serializers.CharField())
    basis = serializers.CharField()
    issue_basis = serializers.CharField()
    nets_committed = serializers.BooleanField()
    fetched_at = serializers.CharField()


class RequirementResponseSerializer(serializers.Serializer):
    data = RequirementRowSerializer(many=True)
    totals = RequirementTotalsSerializer()
    coverage = PlanCoverageSerializer()
    unplanned = UnplannedIssueSerializer()
    plan = PlanHeaderSerializer()
    meta = RequirementMetaSerializer()
