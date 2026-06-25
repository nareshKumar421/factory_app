from rest_framework import serializers
from gate_core.models import UnitChoice
from .models import FixedAssetGateEntry, FixedAssetItem, AssetCategory


class AssetCategorySerializer(serializers.ModelSerializer):
    """Serializer for AssetCategory lookup."""

    class Meta:
        model = AssetCategory
        fields = "__all__"


class FixedAssetItemSerializer(serializers.ModelSerializer):
    """A single asset line within a fixed asset gate entry."""

    asset_category = serializers.PrimaryKeyRelatedField(queryset=AssetCategory.objects.all())
    unit = serializers.PrimaryKeyRelatedField(queryset=UnitChoice.objects.all())

    class Meta:
        model = FixedAssetItem
        fields = ("id", "asset_category", "asset_name", "serial_number", "quantity", "unit")
        read_only_fields = ("id",)

    def validate_quantity(self, value):
        if value is None or value <= 0:
            raise serializers.ValidationError("Quantity must be a positive number")
        return value

    def validate_asset_name(self, value):
        if value and len(value.strip()) < 2:
            raise serializers.ValidationError("Asset name must be at least 2 characters")
        return value.strip() if value else value

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if instance.asset_category:
            data["asset_category"] = {
                "id": instance.asset_category.id,
                "category_name": instance.asset_category.category_name,
            }
        if instance.unit:
            data["unit"] = {"id": instance.unit.id, "name": instance.unit.name}
        return data


class FixedAssetGateEntrySerializer(serializers.ModelSerializer):
    """
    Serializer for FixedAssetGateEntry with nested asset line items.
    """

    items = FixedAssetItemSerializer(many=True, allow_empty=False)

    class Meta:
        model = FixedAssetGateEntry
        exclude = ("created_by", "vehicle_entry")
        read_only_fields = ("created_at", "updated_at", "inward_time", "work_order_number")

    def validate_supplier_name(self, value):
        if value and len(value.strip()) < 2:
            raise serializers.ValidationError("Supplier name must be at least 2 characters")
        return value.strip() if value else value

    def _replace_items(self, instance, items_data):
        instance.items.all().delete()
        for index, item in enumerate(items_data, start=1):
            FixedAssetItem.objects.create(
                fixed_asset_entry=instance,
                line_no=index,
                asset_category=item.get("asset_category"),
                asset_name=item["asset_name"],
                serial_number=item.get("serial_number"),
                quantity=item["quantity"],
                unit=item.get("unit"),
            )

    def create(self, validated_data):
        items_data = validated_data.pop("items", [])
        instance = super().create(validated_data)
        self._replace_items(instance, items_data)
        return instance

    def update(self, instance, validated_data):
        items_data = validated_data.pop("items", None)
        instance = super().update(instance, validated_data)
        if items_data is not None:
            self._replace_items(instance, items_data)
        return instance
