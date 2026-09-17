from rest_framework import serializers

from .models import ShortDispatch, ShortDispatchItem, ShortDispatchReason


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
class ShortDispatchItemSerializer(serializers.ModelSerializer):
    reason_display = serializers.CharField(source="get_reason_display", read_only=True)

    class Meta:
        model = ShortDispatchItem
        fields = [
            "id",
            "source_line_num",
            "item_code",
            "item_name",
            "uom",
            "invoice_quantity",
            "short_quantity",
            "unit_price",
            "tax_code",
            "source_warehouse_code",
            "original_batch_number",
            "reason",
            "reason_display",
            "remarks",
        ]


class ShortDispatchListSerializer(serializers.ModelSerializer):
    company_code = serializers.CharField(source="company.code", read_only=True)
    company_name = serializers.CharField(source="company.name", read_only=True)
    posted_by_name = serializers.CharField(
        source="posted_by.full_name", default="", read_only=True
    )
    line_count = serializers.SerializerMethodField()
    total_short_quantity = serializers.SerializerMethodField()

    class Meta:
        model = ShortDispatch
        fields = [
            "id",
            "entry_no",
            "company_code",
            "company_name",
            "sap_invoice_doc_entry",
            "sap_invoice_doc_num",
            "customer_code",
            "customer_name",
            "warehouse_code",
            "sap_return_doc_entry",
            "sap_return_doc_num",
            "posted_at",
            "posted_by_name",
            "line_count",
            "total_short_quantity",
            "created_at",
        ]

    def get_line_count(self, obj):
        return len(obj.active_lines)

    def get_total_short_quantity(self, obj):
        return sum(line.short_quantity for line in obj.active_lines)


class ShortDispatchDetailSerializer(ShortDispatchListSerializer):
    lines = serializers.SerializerMethodField()

    class Meta(ShortDispatchListSerializer.Meta):
        fields = ShortDispatchListSerializer.Meta.fields + ["remarks", "lines"]

    def get_lines(self, obj):
        return ShortDispatchItemSerializer(obj.active_lines, many=True).data


class WarehouseOptionSerializer(serializers.Serializer):
    warehouse_code = serializers.CharField()
    warehouse_name = serializers.CharField()


# ---------------------------------------------------------------------------
# Input
# ---------------------------------------------------------------------------
class ShortDispatchLineInputSerializer(serializers.Serializer):
    """One short line. Only the line number, the quantity and the reason are
    accepted -- everything else about the line is read off the invoice, so a stale
    form cannot put an item on a return that was never sold."""

    source_line_num = serializers.IntegerField()
    short_quantity = serializers.DecimalField(max_digits=18, decimal_places=3)
    reason = serializers.ChoiceField(
        choices=ShortDispatchReason.choices,
        required=False,
        default=ShortDispatchReason.SHORT,
    )
    remarks = serializers.CharField(required=False, allow_blank=True, max_length=255)


class ShortDispatchCreateSerializer(serializers.Serializer):
    """The single form. Submitting it posts the SAP Return -- there is no draft."""

    invoice_number = serializers.CharField()
    warehouse_code = serializers.CharField()
    lines = ShortDispatchLineInputSerializer(many=True)
    remarks = serializers.CharField(required=False, allow_blank=True)
