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
        min_value=1,
        help_text="Number of days since last movement (e.g. 45, 90, 180)",
    )
    item_group = serializers.IntegerField(
        required=True,
        help_text="Item group code from OITB (e.g. 105 for Packaging Material, 106 for Raw Material)",
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
    quantity = serializers.FloatField()
    value = serializers.FloatField()
    last_movement_date = serializers.CharField(allow_null=True)
    days_since_last_movement = serializers.IntegerField()
    consumption_ratio = serializers.FloatField()


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


class ReportMetaSerializer(serializers.Serializer):
    age_days = serializers.IntegerField()
    item_group = serializers.IntegerField()
    fetched_at = serializers.CharField()


class NonMovingRMReportResponseSerializer(serializers.Serializer):
    data = NonMovingRMItemSerializer(many=True)
    summary = ReportSummarySerializer()
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
