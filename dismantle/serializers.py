from rest_framework import serializers

from .models import Dismantle, DismantleComponent, DismantleSource


class DismantleComponentSerializer(serializers.ModelSerializer):
    class Meta:
        model = DismantleComponent
        fields = [
            "id",
            "item_code",
            "item_name",
            "uom",
            "qty_per_piece",
            "quantity",
            "warehouse_code",
            "is_batch_managed",
            "batch_number",
            "recovered",
            "sap_line_num",
        ]


class DismantleListSerializer(serializers.ModelSerializer):
    company_code = serializers.CharField(source="company.code", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    source_display = serializers.CharField(source="get_source_display", read_only=True)
    goods_return_entry_no = serializers.CharField(
        source="goods_return.entry_no", read_only=True, default=""
    )
    customer_name = serializers.CharField(
        source="goods_return.customer_name", read_only=True, default=""
    )

    class Meta:
        model = Dismantle
        fields = [
            "id",
            "entry_no",
            "company_code",
            "status",
            "status_display",
            "source",
            "source_display",
            "goods_return_entry_no",
            "customer_name",
            "warehouse_code",
            "item_code",
            "item_name",
            "uom",
            "batch_number",
            "quantity",
            "sap_order_doc_num",
            "sap_receipt_doc_num",
            "sap_issue_doc_num",
            "posting_date",
            "created_at",
        ]


class DismantleDetailSerializer(DismantleListSerializer):
    components = DismantleComponentSerializer(many=True, read_only=True)
    # True when the recipe's batch size disagrees with the item's box size, i.e.
    # every component quantity is out by their ratio. Surfaced so the screen can
    # say so; see ``guards.bom_inflation_warning``.
    bom_inflated = serializers.BooleanField(read_only=True)
    # Set by a posting run SAP stopped part-way through, and only by that -- it
    # is an attribute on the returned instance rather than a column, so it is
    # read defensively here and comes back blank on every other read.
    posting_error = serializers.SerializerMethodField()

    class Meta(DismantleListSerializer.Meta):
        fields = DismantleListSerializer.Meta.fields + [
            "pieces_per_box",
            "bom_batch_size",
            "bom_inflated",
            "variety_code",
            "remarks",
            "goods_return",
            "goods_return_item",
            "sap_order_doc_entry",
            "sap_receipt_doc_entry",
            "sap_issue_doc_entry",
            "order_closed",
            "sap_post_error",
            "posting_error",
            "posted_at",
            "components",
        ]

    def get_posting_error(self, obj) -> str:
        return getattr(obj, "posting_error", "") or ""


class DismantleCreateSerializer(serializers.Serializer):
    """Either a returned line or an item in a warehouse — never both."""

    source = serializers.ChoiceField(
        choices=DismantleSource.choices, default=DismantleSource.GOODS_RETURN
    )
    goods_return_item_id = serializers.IntegerField(required=False, allow_null=True)
    warehouse_code = serializers.CharField(required=False, allow_blank=True)
    item_code = serializers.CharField(required=False, allow_blank=True)
    batch_number = serializers.CharField(required=False, allow_blank=True)
    quantity = serializers.DecimalField(max_digits=18, decimal_places=3)
    remarks = serializers.CharField(required=False, allow_blank=True)

    def validate(self, attrs):
        if attrs.get("source", DismantleSource.GOODS_RETURN) == DismantleSource.GOODS_RETURN:
            if not attrs.get("goods_return_item_id"):
                raise serializers.ValidationError(
                    {"goods_return_item_id": "Pick the returned line being dismantled."}
                )
        else:
            missing = {
                field: "Required when dismantling warehouse stock."
                for field in ("warehouse_code", "item_code")
                if not attrs.get(field)
            }
            if missing:
                raise serializers.ValidationError(missing)
        return attrs


class DismantleBulkCreateSerializer(serializers.Serializer):
    """A basket of items to take apart — one dismantle record per entry.

    SAP has no multi-item disassembly (an order names one parent), so this is a
    convenience for the operator rather than a different kind of document.
    """

    items = DismantleCreateSerializer(many=True, allow_empty=False)


class DismantleHeaderPatchSerializer(serializers.Serializer):
    quantity = serializers.DecimalField(
        max_digits=18, decimal_places=3, required=False
    )
    batch_number = serializers.CharField(required=False, allow_blank=True)
    remarks = serializers.CharField(required=False, allow_blank=True)


class DismantleComponentSaveSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    quantity = serializers.DecimalField(
        max_digits=18, decimal_places=6, required=False
    )
    recovered = serializers.BooleanField(required=False)
    warehouse_code = serializers.CharField(required=False, allow_blank=True)


class DismantleComponentsSaveSerializer(serializers.Serializer):
    components = DismantleComponentSaveSerializer(many=True)
