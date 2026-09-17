"""
non_moving_rm/serializers.py

DRF serializers for validating query parameters and shaping API responses.
"""

from rest_framework import serializers


# ---------------------------------------------------------------------------
# Query Parameter Serializers (Input Validation)
# ---------------------------------------------------------------------------


class NonMovingRMFilterSerializer(serializers.Serializer):
    """Validates query parameters for the non-moving RM report endpoint."""

    age = serializers.IntegerField(
        required=True,
        min_value=0,
        help_text="Number of days since last movement; use 0 to include all stock",
    )
    item_group = serializers.IntegerField(
        required=False,
        default=0,
        min_value=0,
        help_text="Item group code from OITB, or 0/all omitted for all groups",
    )
    count_production = serializers.BooleanField(
        required=False,
        default=True,
        help_text=(
            "Whether a production entry counts as movement. Default true, the "
            "board's standing rule. False ages every row on its last Goods "
            "Receipt PO instead, so only a purchase resets the clock."
        ),
    )


# ---------------------------------------------------------------------------
# Response Serializers (Output Shape)
# ---------------------------------------------------------------------------


class NonMovingRMItemSerializer(serializers.Serializer):
    """One non-moving raw material item."""

    branch = serializers.CharField()
    item_code = serializers.CharField()
    item_name = serializers.CharField()
    item_group_name = serializers.CharField()
    sub_group = serializers.CharField()
    warehouse = serializers.CharField()
    warehouse_name = serializers.CharField(required=False, default="")
    quantity = serializers.FloatField()
    value = serializers.FloatField()
    last_movement_date = serializers.CharField(allow_null=True)
    days_since_last_movement = serializers.IntegerField()
    consumption_ratio = serializers.FloatField()

    # Which rule produced the age above. With the production rule on:
    # "production" for packing material, whose clock only a production order
    # resets, "any" for everything else. With it off: "grpo" where a Goods
    # Receipt PO dated the row, and "none" where the item has never been
    # bought in this company at all and the age fell back to its creation date.
    movement_basis = serializers.CharField(required=False, default="any")

    # The warehouse's own last movement of any kind, transfers included. On a
    # packing-material row this is what the age used to be, kept so a restack
    # between godowns stays visible next to an age that ignores it.
    last_warehouse_movement_date = serializers.CharField(
        required=False, allow_null=True, default=None
    )
    days_since_warehouse_movement = serializers.IntegerField(required=False, default=0)

    # The warehouse the movement behind `last_movement_date` happened in. On a
    # packing-material row that is rarely this row's own warehouse -- the age
    # is the item's last production, which happens on the floor the godown
    # feeds -- so without it the date cannot be looked up in SAP at all.
    last_movement_warehouse = serializers.CharField(required=False, default="")
    last_movement_warehouse_name = serializers.CharField(required=False, default="")


class BranchSummarySerializer(serializers.Serializer):
    """Summary per branch."""

    branch = serializers.CharField()
    item_count = serializers.IntegerField()
    total_value = serializers.FloatField()
    total_quantity = serializers.FloatField()


class ReportSummarySerializer(serializers.Serializer):
    """Aggregated summary of the non-moving RM report."""

    total_items = serializers.IntegerField()
    total_value = serializers.FloatField()
    total_quantity = serializers.FloatField()
    by_branch = BranchSummarySerializer(many=True)


class WarehouseItemSerializer(serializers.Serializer):
    """One item's pro-rated share of a warehouse."""

    item_code = serializers.CharField()
    quantity = serializers.FloatField()
    value = serializers.FloatField()


class WarehouseSummarySerializer(serializers.Serializer):
    """Summary per warehouse, with the items that make it up."""

    warehouse = serializers.CharField()
    warehouse_name = serializers.CharField()
    item_count = serializers.IntegerField()
    total_value = serializers.FloatField()
    total_quantity = serializers.FloatField()
    items = WarehouseItemSerializer(many=True, required=False)


class ReportMetaSerializer(serializers.Serializer):
    age_days = serializers.IntegerField()
    item_group = serializers.IntegerField()
    # Which clock the ages in `data` were measured on, echoed back so an
    # exported sheet can say which question it answers.
    count_production = serializers.BooleanField(required=False, default=True)
    fetched_at = serializers.CharField()


class NonMovingRMReportResponseSerializer(serializers.Serializer):
    data = NonMovingRMItemSerializer(many=True)
    summary = ReportSummarySerializer()
    warehouse_summary = WarehouseSummarySerializer(many=True)
    meta = ReportMetaSerializer()


# ---------------------------------------------------------------------------
# Item Group Dropdown Response
# ---------------------------------------------------------------------------


class ItemGroupSerializer(serializers.Serializer):
    """One item group for the dropdown."""

    item_group_code = serializers.IntegerField()
    item_group_name = serializers.CharField()


class ItemGroupMetaSerializer(serializers.Serializer):
    total_groups = serializers.IntegerField()
    fetched_at = serializers.CharField()


class ItemGroupResponseSerializer(serializers.Serializer):
    data = ItemGroupSerializer(many=True)
    meta = ItemGroupMetaSerializer()
