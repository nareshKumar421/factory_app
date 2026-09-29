"""What the oil lot endpoints accept and return. Kilograms and rates per kg, as
a lot is bought; the litre figures ride along, worked out."""

from decimal import Decimal

from rest_framework import serializers

from .models_lot import (
    ContractHistory,
    LotChange,
    LotShortage,
    LotStatus,
    OilLot,
    PaymentStatus,
)
from .models_tank import TankItem
from .services_lot import BULK_ACTIONS, INTO_STORE, Action


def _name(user):
    return (user.full_name or user.email) if user else None


class LotSerializer(serializers.ModelSerializer):
    item_code = serializers.CharField(source="item.code")
    item_name = serializers.CharField(source="item.name")
    item_color = serializers.CharField(source="item.color")
    status_label = serializers.CharField(source="get_status_display")
    created_by_name = serializers.SerializerMethodField()

    class Meta:
        model = OilLot
        fields = [
            "id", "item", "item_code", "item_name", "item_color", "status", "status_label",
            "vendor_code", "vendor_name", "rate", "quantity", "total", "rate_per_litre", "quantity_litres",
            "job_work", "vehicle_number", "transporter", "location", "eta", "arrival_date",
            "parent", "is_accumulator", "bilty_number", "grpo_number", "payment_status",
            "contract_start", "contract_end", "deleted", "created_at", "created_by_name", "updated_at",
        ]

    def get_created_by_name(self, obj):
        return _name(obj.created_by) or obj.created_by_label or None


class LotChangeSerializer(serializers.ModelSerializer):
    changed_by_name = serializers.SerializerMethodField()
    # Not "fields": that name is the serializer's own.
    changed_fields = serializers.SerializerMethodField()

    class Meta:
        model = LotChange
        fields = ["id", "lot", "action", "changed_by_name", "note", "timestamp", "changed_fields"]

    def get_changed_by_name(self, obj):
        return _name(obj.changed_by) or obj.changed_by_label or None

    def get_changed_fields(self, obj):
        return [
            {"field": f.field_name, "old": f.old_value, "new": f.new_value}
            for f in obj.field_changes.all()
        ]


class LotDetailSerializer(LotSerializer):
    parent_summary = serializers.SerializerMethodField()
    children = serializers.SerializerMethodField()
    history = serializers.SerializerMethodField()

    class Meta(LotSerializer.Meta):
        fields = LotSerializer.Meta.fields + ["parent_summary", "children", "history"]

    def _brief(self, lot):
        return {
            "id": lot.pk,
            "status": lot.status,
            "status_label": lot.get_status_display(),
            "quantity": str(lot.quantity),
            "vehicle_number": lot.vehicle_number,
            "deleted": lot.deleted,
        }

    def get_parent_summary(self, obj):
        return self._brief(obj.parent) if obj.parent_id else None

    def get_children(self, obj):
        return [self._brief(child) for child in obj.children.order_by("id")]

    def get_history(self, obj):
        changes = obj.changes.select_related("changed_by").prefetch_related("field_changes")
        return LotChangeSerializer(changes, many=True).data


def _kg(**kwargs):
    return serializers.DecimalField(max_digits=14, decimal_places=2, min_value=Decimal("0.01"), **kwargs)


def _rate(**kwargs):
    return serializers.DecimalField(max_digits=12, decimal_places=3, min_value=Decimal("0.001"), **kwargs)


class _LotFields(serializers.Serializer):
    rate = _rate(required=False)
    quantity = _kg(required=False)
    vehicle_number = serializers.CharField(max_length=50, allow_blank=True, required=False)
    transporter = serializers.CharField(max_length=255, allow_blank=True, required=False)
    location = serializers.CharField(max_length=255, allow_blank=True, required=False)
    eta = serializers.DateField(allow_null=True, required=False)
    arrival_date = serializers.DateField(allow_null=True, required=False)
    bilty_number = serializers.CharField(max_length=100, allow_blank=True, required=False)
    grpo_number = serializers.CharField(max_length=100, allow_blank=True, required=False)
    contract_start = serializers.DateField(allow_null=True, required=False)
    contract_end = serializers.DateField(allow_null=True, required=False)
    payment_status = serializers.ChoiceField(choices=PaymentStatus.choices, required=False)
    job_work = serializers.CharField(max_length=255, allow_blank=True, required=False)


class LotCreateSerializer(_LotFields):
    item = serializers.PrimaryKeyRelatedField(queryset=TankItem.objects.all())
    status = serializers.ChoiceField(choices=LotStatus.choices)
    vendor_code = serializers.CharField(max_length=50, trim_whitespace=True)
    vendor_name = serializers.CharField(max_length=255, allow_blank=True, required=False, default="")
    rate = _rate()
    quantity = _kg()


class LotUpdateSerializer(_LotFields):
    pass


def _action():
    return serializers.ChoiceField(choices=[Action.RETAIN, Action.TOLERATE], required=False, allow_null=True)


class MoveSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=LotStatus.choices)
    quantity = _kg()
    action = _action()
    arrival_date = serializers.DateField(allow_null=True, required=False)
    location = serializers.CharField(max_length=255, allow_blank=True, required=False, allow_null=True)
    payment_status = serializers.ChoiceField(choices=PaymentStatus.choices, required=False, allow_null=True)


class DispatchSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=LotStatus.choices)
    quantity = _kg()
    action = _action()
    vehicle_number = serializers.CharField(max_length=50, allow_blank=True, required=False, default="")
    transporter = serializers.CharField(max_length=255, allow_blank=True, required=False, default="")
    location = serializers.CharField(max_length=255, allow_blank=True, required=False, default="")
    eta = serializers.DateField(allow_null=True, required=False)
    payment_status = serializers.ChoiceField(choices=PaymentStatus.choices, required=False, allow_null=True)


class ArriveSerializer(serializers.Serializer):
    weighed_qty = _kg()
    status = serializers.ChoiceField(choices=LotStatus.choices, required=False, default=LotStatus.AT_REFINERY)
    action = _action()
    job_work = serializers.CharField(max_length=255, allow_blank=True, required=False, default="")


class IntoTankSerializer(serializers.Serializer):
    weighed_qty = _kg()
    status = serializers.ChoiceField(choices=[(s, LotStatus(s).label) for s in INTO_STORE],
                                     required=False, default=LotStatus.IN_TANK)
    bilty_number = serializers.CharField(max_length=100, allow_blank=True, required=False, allow_null=True)
    grpo_number = serializers.CharField(max_length=100, allow_blank=True, required=False, allow_null=True)


class BulkSerializer(serializers.Serializer):
    action = serializers.ChoiceField(choices=BULK_ACTIONS)
    lots = serializers.ListField(child=serializers.IntegerField(min_value=1), min_length=1, max_length=500)


class TemporaryVendorSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=255, trim_whitespace=True)


class DashboardOrderSerializer(serializers.Serializer):
    items = serializers.ListField(child=serializers.IntegerField(min_value=1), max_length=500)


class ShortageSerializer(serializers.ModelSerializer):
    created_by_name = serializers.SerializerMethodField()

    class Meta:
        model = LotShortage
        fields = [
            "id", "lot", "item_code", "item_name", "supplier_code", "supplier", "vehicle_number", "transporter",
            "bilty_number", "grpo_number", "rate", "load_qty_mt", "unload_qty_mt", "shortage_mt", "allowed_mt",
            "deducted_mt", "deduction_amount", "created_at", "created_by_name",
        ]

    def get_created_by_name(self, obj):
        return _name(obj.created_by) or obj.created_by_label or None


class ContractHistorySerializer(serializers.ModelSerializer):
    class Meta:
        model = ContractHistory
        fields = [
            "id", "item_code", "item_name", "vendor_code", "vendor_name", "rate",
            "contract_start", "contract_end", "created_at", "created_by_label",
        ]
