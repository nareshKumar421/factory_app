"""What the tank farm endpoints accept and return. Litres throughout, except a
tank log, which carries the lot's kilograms."""

from decimal import Decimal

from rest_framework import serializers

from .models_tank import OilCategory, Tank, TankItem, TankKind, TankLog


def _name(user):
    return (user.full_name or user.email) if user else None


class OilSerializer(serializers.ModelSerializer):
    category_label = serializers.SerializerMethodField()
    tank_count = serializers.IntegerField(read_only=True, default=0)
    lot_count = serializers.IntegerField(read_only=True, default=0)

    class Meta:
        model = TankItem
        fields = [
            "id", "code", "name", "category", "category_label", "color", "is_active",
            "tank_count", "lot_count", "created_at", "updated_at",
        ]

    def get_category_label(self, obj):
        return obj.get_category_display() if obj.category else ""


class OilWriteSerializer(serializers.Serializer):
    #: One of SAP's raw-material oils; the name is read from SAP, not sent.
    code = serializers.CharField(max_length=50, trim_whitespace=True)
    category = serializers.ChoiceField(choices=OilCategory.choices, allow_blank=True, required=False, default="")
    color = serializers.RegexField(r"^#[0-9a-fA-F]{6}$", required=False, allow_blank=True, default="")
    is_active = serializers.BooleanField(required=False, default=True)


class TankSerializer(serializers.ModelSerializer):
    item_code = serializers.CharField(source="item.code", default=None)
    item_name = serializers.CharField(source="item.name", default=None)
    item_color = serializers.CharField(source="item.color", default=None)
    used_pct = serializers.SerializerMethodField()
    updated_by_name = serializers.SerializerMethodField()

    class Meta:
        model = Tank
        fields = [
            "id", "code", "kind", "item", "item_code", "item_name", "item_color",
            "capacity_l", "level_l", "used_pct", "is_active", "updated_at", "updated_by_name",
        ]

    def get_used_pct(self, obj):
        if not obj.capacity_l:
            return None
        return round(float(obj.level_l or 0) / float(obj.capacity_l) * 100, 1)

    def get_updated_by_name(self, obj):
        return _name(obj.updated_by)


class TankCreateSerializer(serializers.Serializer):
    kind = serializers.ChoiceField(choices=TankKind.choices, default=TankKind.TANK)
    capacity_l = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal("0.01"))
    item = serializers.PrimaryKeyRelatedField(queryset=TankItem.objects.all(), allow_null=True, required=False)
    level_l = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal("0"), required=False,
                                       default=Decimal("0"))
    is_active = serializers.BooleanField(required=False, default=True)


class TankUpdateSerializer(serializers.Serializer):
    capacity_l = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal("0.01"), required=False)
    item = serializers.PrimaryKeyRelatedField(queryset=TankItem.objects.all(), allow_null=True, required=False)
    level_l = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal("0"), required=False,
                                       allow_null=True)
    is_active = serializers.BooleanField(required=False)


class TankLogSerializer(serializers.ModelSerializer):
    created_by_name = serializers.SerializerMethodField()

    class Meta:
        model = TankLog
        fields = [
            "id", "kind", "lot", "quantity_kg", "rate", "vehicle_number", "party",
            "item_code", "item_name", "arrival", "created_at", "created_by_name",
        ]

    def get_created_by_name(self, obj):
        return _name(obj.created_by) or obj.created_by_label or None


class OpeningStockSerializer(serializers.Serializer):
    item = serializers.PrimaryKeyRelatedField(queryset=TankItem.objects.all())
    rate_per_litre = serializers.DecimalField(max_digits=12, decimal_places=3, min_value=Decimal("0.001"))
    quantity_litres = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=Decimal("0.01"))
