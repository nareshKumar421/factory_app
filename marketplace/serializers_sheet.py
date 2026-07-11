"""Serializers for the sheet-import + warehouse-issue flow."""
from rest_framework import serializers

from .models import (
    MarketplaceIssueLine,
    MarketplaceIssueRequest,
    OrderImportBatch,
)


class OrderImportBatchSerializer(serializers.ModelSerializer):
    uploaded_by_name = serializers.CharField(source="created_by.full_name", read_only=True, default="")

    class Meta:
        model = OrderImportBatch
        fields = [
            "id", "channel", "filename", "status", "row_count", "order_count",
            "line_count", "summary", "uploaded_by_name", "created_at",
        ]


class StockListLineSerializer(serializers.Serializer):
    item_code = serializers.CharField()
    item_name = serializers.CharField()
    component_type = serializers.CharField()
    uom = serializers.CharField()
    required_quantity = serializers.DecimalField(max_digits=18, decimal_places=3)
    source_skus = serializers.ListField(child=serializers.CharField())


class StockListSerializer(serializers.Serializer):
    lines = StockListLineSerializer(many=True)
    unmapped_skus = serializers.ListField(child=serializers.CharField())
    orders = serializers.IntegerField()


class MarketplaceIssueLineSerializer(serializers.ModelSerializer):
    class Meta:
        model = MarketplaceIssueLine
        fields = [
            "id", "item_code", "item_name", "component_type", "uom",
            "required_qty", "available_stock", "approved_qty", "issued_qty",
            "received_qty", "status", "reject_reason", "source_skus",
        ]


class MarketplaceIssueRequestListSerializer(serializers.ModelSerializer):
    batch_filename = serializers.CharField(source="batch.filename", read_only=True, default="")

    class Meta:
        model = MarketplaceIssueRequest
        fields = [
            "id", "channel", "batch", "batch_filename", "sap_warehouse_code",
            "status", "reject_reason", "reviewed_at", "created_at",
        ]


class MarketplaceIssueRequestDetailSerializer(MarketplaceIssueRequestListSerializer):
    lines = MarketplaceIssueLineSerializer(many=True, read_only=True)

    class Meta(MarketplaceIssueRequestListSerializer.Meta):
        fields = MarketplaceIssueRequestListSerializer.Meta.fields + ["lines", "sap_issue_doc_entries"]


# ── input ────────────────────────────────────────────────────────────────────
class SendIssueRequestSerializer(serializers.Serializer):
    batch_id = serializers.IntegerField()
    warehouse_code = serializers.CharField(required=False, allow_blank=True, default="")


class ReviewLineSerializer(serializers.Serializer):
    line_id = serializers.IntegerField()
    approved_qty = serializers.DecimalField(max_digits=18, decimal_places=3, required=False)
    status = serializers.CharField(required=False, allow_blank=True)
    reason = serializers.CharField(required=False, allow_blank=True)


class ReviewSerializer(serializers.Serializer):
    lines = ReviewLineSerializer(many=True)


class RejectSerializer(serializers.Serializer):
    reason = serializers.CharField(required=False, allow_blank=True, default="")


class ReceiveLineSerializer(serializers.Serializer):
    line_id = serializers.IntegerField()
    received_qty = serializers.DecimalField(max_digits=18, decimal_places=3)


class ReceiveSerializer(serializers.Serializer):
    lines = ReceiveLineSerializer(many=True, required=False)
