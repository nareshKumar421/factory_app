"""Serializers for the marketplace app.

Split into input serializers (plain ``Serializer``) and output serializers
(``ModelSerializer``), following ``gate_core/serializers_sales_dispatch.py``.
Dispatch/return have a light *List* serializer and a *Detail* serializer that
computes resolved lines + scan progress.
"""
from rest_framework import serializers

from .models import (
    ComboComponent,
    ComboDefinition,
    MarketplaceDispatch,
    MarketplaceOrder,
    MarketplaceOrderBilling,
    MarketplaceOrderLine,
    MarketplaceReturn,
    MarketplaceReturnScan,
    MarketplaceScan,
    MarketplaceWarehouse,
    SkuMapping,
)
from .services import resolve_service, scan_service


# ── Masters ──────────────────────────────────────────────────────────────────
class MarketplaceWarehouseSerializer(serializers.ModelSerializer):
    class Meta:
        model = MarketplaceWarehouse
        fields = [
            "id", "channel", "name", "sap_warehouse_code", "sap_customer_card_code",
            "facility_code", "is_active", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]


class ComboComponentSerializer(serializers.ModelSerializer):
    id = serializers.IntegerField(required=False)

    class Meta:
        model = ComboComponent
        fields = ["id", "component_type", "item_code", "item_name", "quantity", "uom"]


class ComboDefinitionSerializer(serializers.ModelSerializer):
    components = ComboComponentSerializer(many=True)

    class Meta:
        model = ComboDefinition
        fields = ["id", "channel", "code", "name", "is_active", "components",
                  "created_at", "updated_at"]
        read_only_fields = ["id", "created_at", "updated_at"]

    def validate(self, attrs):
        components = attrs.get("components")
        if components is not None:
            if not components:
                raise serializers.ValidationError({"components": "Add at least one component."})
            for comp in components:
                if not str(comp.get("item_code", "")).strip():
                    raise serializers.ValidationError({"components": "Every component needs an item code."})
                if comp.get("quantity") is None or comp["quantity"] <= 0:
                    raise serializers.ValidationError({"components": "Component quantity must be greater than 0."})
        return attrs

    def create(self, validated_data):
        components = validated_data.pop("components", [])
        combo = ComboDefinition.objects.create(**validated_data)
        for comp in components:
            comp.pop("id", None)
            ComboComponent.objects.create(combo=combo, **comp)
        return combo

    def update(self, instance, validated_data):
        components = validated_data.pop("components", None)
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()
        if components is not None:
            instance.components.all().delete()
            for comp in components:
                comp.pop("id", None)
                ComboComponent.objects.create(combo=instance, **comp)
        return instance


class SkuMappingSerializer(serializers.ModelSerializer):
    combo_code = serializers.CharField(source="combo.code", read_only=True, default="")

    class Meta:
        model = SkuMapping
        fields = [
            "id", "channel", "marketplace_sku", "sku_name", "sku_type",
            "fg_item_code", "fg_item_name", "combo", "combo_code", "default_uom",
            "is_active", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def validate(self, attrs):
        sku_type = attrs.get("sku_type", getattr(self.instance, "sku_type", None))
        combo = attrs.get("combo", getattr(self.instance, "combo", None))
        fg = attrs.get("fg_item_code", getattr(self.instance, "fg_item_code", None))
        if sku_type == "COMBO" and not combo:
            raise serializers.ValidationError({"combo": "Required for combo SKUs."})
        if sku_type == "RAW" and not fg:
            raise serializers.ValidationError({"fg_item_code": "Required for raw SKUs."})
        return attrs


class SkuMappingImportSerializer(serializers.Serializer):
    rows = SkuMappingSerializer(many=True)


# ── Orders ───────────────────────────────────────────────────────────────────
class MarketplaceOrderLineSerializer(serializers.ModelSerializer):
    class Meta:
        model = MarketplaceOrderLine
        fields = ["id", "marketplace_sku", "sku_name", "ordered_quantity"]


class MarketplaceOrderSerializer(serializers.ModelSerializer):
    lines = MarketplaceOrderLineSerializer(many=True, read_only=True)
    # Annotated by OrderListView; true once warehouse materials were issued.
    dispatch_ready = serializers.BooleanField(read_only=True, default=False)

    class Meta:
        model = MarketplaceOrder
        fields = [
            "id", "channel", "order_id", "order_date", "buyer_name",
            "sap_warehouse_code", "status", "lines", "created_at", "dispatch_ready",
        ]


class ResolvedLineSerializer(serializers.Serializer):
    item_code = serializers.CharField()
    item_name = serializers.CharField()
    component_type = serializers.CharField()
    required_quantity = serializers.DecimalField(max_digits=18, decimal_places=3)
    uom = serializers.CharField()
    warehouse_code = serializers.CharField()
    source_skus = serializers.ListField(child=serializers.CharField())


class ResolvedOrderSerializer(serializers.Serializer):
    order = MarketplaceOrderSerializer()
    resolved_lines = ResolvedLineSerializer(many=True)
    unmapped_skus = serializers.ListField(child=serializers.CharField())


# ── Scans + dispatch ─────────────────────────────────────────────────────────
class MarketplaceScanSerializer(serializers.ModelSerializer):
    scanned_by_name = serializers.CharField(source="scanned_by.full_name", read_only=True, default="")

    class Meta:
        model = MarketplaceScan
        fields = [
            "id", "dispatch", "barcode_raw", "item_code", "item_name",
            "component_type", "source_sku", "quantity", "uom", "warehouse_code",
            "scanned_by", "scanned_by_name", "scanned_at",
        ]
        read_only_fields = fields


class MarketplaceDispatchListSerializer(serializers.ModelSerializer):
    order_id = serializers.CharField(source="order.order_id", read_only=True)
    buyer_name = serializers.CharField(source="order.buyer_name", read_only=True, default="")
    internal_billing_num = serializers.CharField(
        source="internal_billing.invoice_number", read_only=True, default=""
    )

    class Meta:
        model = MarketplaceDispatch
        fields = [
            "id", "channel", "order", "order_id", "buyer_name", "sap_warehouse_code",
            "status", "sap_delivery_note_num", "internal_billing_num",
            "confirmed_at", "created_at", "updated_at",
        ]


class MarketplaceDispatchDetailSerializer(MarketplaceDispatchListSerializer):
    scans = MarketplaceScanSerializer(many=True, read_only=True)
    resolved_lines = serializers.SerializerMethodField()
    progress = serializers.SerializerMethodField()
    unmapped_skus = serializers.SerializerMethodField()

    class Meta(MarketplaceDispatchListSerializer.Meta):
        fields = MarketplaceDispatchListSerializer.Meta.fields + [
            "scans", "resolved_lines", "progress", "unmapped_skus",
        ]

    def _resolved(self, obj):
        if not hasattr(self, "_resolved_cache"):
            self._resolved_cache = resolve_service.resolve_order(obj.order)
        return self._resolved_cache

    def get_resolved_lines(self, obj):
        return ResolvedLineSerializer(self._resolved(obj)["resolved_lines"], many=True).data

    def get_unmapped_skus(self, obj):
        return self._resolved(obj)["unmapped_skus"]

    def get_progress(self, obj):
        flines = resolve_service.fg_lines(self._resolved(obj)["resolved_lines"])
        scanned = {
            (ic or "").strip().upper(): q
            for ic, q in obj.scans.filter(is_active=True).values_list("item_code", "quantity")
        }
        from decimal import Decimal
        scanned = {k: Decimal(v) for k, v in scanned.items()}
        return scan_service.build_progress(flines, scanned)


# ── Returns ──────────────────────────────────────────────────────────────────
class MarketplaceReturnScanSerializer(serializers.ModelSerializer):
    scanned_by_name = serializers.CharField(source="scanned_by.full_name", read_only=True, default="")

    class Meta:
        model = MarketplaceReturnScan
        fields = [
            "id", "mp_return", "barcode_raw", "item_code", "item_name",
            "component_type", "source_sku", "quantity", "uom",
            "scanned_by", "scanned_by_name", "scanned_at",
        ]
        read_only_fields = fields


class MarketplaceReturnListSerializer(serializers.ModelSerializer):
    order_id = serializers.CharField(source="order.order_id", read_only=True)

    class Meta:
        model = MarketplaceReturn
        fields = [
            "id", "channel", "order", "order_id", "status",
            "internal_credit_doc_num", "submitted_at", "created_at", "updated_at",
        ]


class MarketplaceReturnDetailSerializer(MarketplaceReturnListSerializer):
    scans = MarketplaceReturnScanSerializer(many=True, read_only=True)
    progress = serializers.SerializerMethodField()

    class Meta(MarketplaceReturnListSerializer.Meta):
        fields = MarketplaceReturnListSerializer.Meta.fields + ["scans", "progress"]

    def get_progress(self, obj):
        return scan_service.return_progress(obj)


class MarketplaceOrderBillingSerializer(serializers.ModelSerializer):
    class Meta:
        model = MarketplaceOrderBilling
        fields = [
            "id", "channel", "order_id", "invoice_number", "buyer_name",
            "sap_delivery_note_num", "total_amount", "status", "created_at",
        ]


# ── Input serializers ────────────────────────────────────────────────────────
class ChannelField(serializers.ChoiceField):
    def __init__(self, **kwargs):
        from .models import MarketplaceChannel
        super().__init__(choices=MarketplaceChannel.choices, **kwargs)


class DispatchCreateSerializer(serializers.Serializer):
    channel = ChannelField()
    order_id = serializers.CharField(max_length=120)


class ScanCreateSerializer(serializers.Serializer):
    barcode_raw = serializers.CharField(max_length=500, trim_whitespace=True)
    item_code = serializers.CharField(max_length=100, required=False, allow_blank=True)
    quantity = serializers.DecimalField(
        max_digits=18, decimal_places=3, required=False, min_value=0
    )


class ConfirmSerializer(serializers.Serializer):
    override_deviation = serializers.BooleanField(required=False, default=False)
    remarks = serializers.CharField(required=False, allow_blank=True)


class CancelSerializer(serializers.Serializer):
    reason = serializers.CharField(required=False, allow_blank=True)


class ReturnCreateSerializer(serializers.Serializer):
    channel = ChannelField()
    order_id = serializers.CharField(max_length=120)


class ReturnSubmitSerializer(serializers.Serializer):
    remarks = serializers.CharField(required=False, allow_blank=True)
