from rest_framework import serializers
from .models import OutboundGateEntry, OutboundPurpose


class OutboundPurposeSerializer(serializers.ModelSerializer):
    class Meta:
        model = OutboundPurpose
        fields = "__all__"


class OutboundGateEntrySerializer(serializers.ModelSerializer):
    class Meta:
        model = OutboundGateEntry
        exclude = ("created_by", "vehicle_entry")
        read_only_fields = ("created_at", "updated_at", "arrival_time")

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if instance.purpose:
            data["purpose"] = {
                "id": instance.purpose.id,
                "name": instance.purpose.name,
            }
        return data


class OutboundGateEntryListSerializer(serializers.ModelSerializer):
    """Lightweight serializer for list views and dropdown."""
    vehicle_number = serializers.CharField(
        source="vehicle_entry.vehicle.vehicle_number", read_only=True
    )
    driver_name = serializers.CharField(
        source="vehicle_entry.driver.name", read_only=True
    )
    entry_no = serializers.CharField(
        source="vehicle_entry.entry_no", read_only=True
    )
    gate_status = serializers.CharField(
        source="vehicle_entry.status", read_only=True
    )
    vehicle_entry_id = serializers.IntegerField(
        source="vehicle_entry.id", read_only=True
    )

    class Meta:
        model = OutboundGateEntry
        fields = [
            "id",
            "vehicle_entry_id",
            "entry_no",
            "vehicle_number",
            "driver_name",
            "gate_status",
            "customer_name",
            "sales_order_ref",
            "assigned_zone",
            "assigned_bay",
            "vehicle_empty_confirmed",
            "arrival_time",
            "released_for_loading_at",
        ]
