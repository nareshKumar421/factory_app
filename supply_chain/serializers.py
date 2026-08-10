from rest_framework import serializers

from .models import (
    MachineCapacity,
    MaterialLeadTime,
    MaterialMachineMap,
    ReferenceImport,
    SupplyChainPolicy,
)


class MaterialLeadTimeSerializer(serializers.ModelSerializer):
    class Meta:
        model = MaterialLeadTime
        fields = [
            "id", "material_code", "material_name", "material_type", "category",
            "supplier_name", "lead_time_days", "moq", "unit", "remarks", "is_active",
        ]


class MachineCapacitySerializer(serializers.ModelSerializer):
    available_hours = serializers.DecimalField(
        max_digits=18, decimal_places=2, read_only=True
    )
    effective_capacity_units = serializers.SerializerMethodField()

    class Meta:
        model = MachineCapacity
        fields = [
            "id", "machine_id", "name", "location", "pack_type", "pack_size_range",
            "output_per_hour", "shift_hours", "shifts_per_day", "working_days_per_month",
            "changeover_minutes", "available_hours", "effective_capacity_units", "is_active",
        ]

    def get_effective_capacity_units(self, obj):
        """Capacity with no changeover — the template's own formula, for comparison."""
        return str(obj.effective_capacity_units(changeover_count=0))


class MaterialMachineMapSerializer(serializers.ModelSerializer):
    alternates = serializers.ListField(child=serializers.CharField(), read_only=True)

    class Meta:
        model = MaterialMachineMap
        fields = [
            "id", "sku_code", "sku_name", "brand", "pack_type", "pack_size",
            "primary_machine_id", "alternate_machine_ids", "alternates",
            "output_on_primary", "is_active",
        ]


class ReferenceImportSerializer(serializers.ModelSerializer):
    class Meta:
        model = ReferenceImport
        fields = [
            "id", "filename", "lead_times_loaded", "machines_loaded", "mappings_loaded",
            "examples_skipped", "warnings", "imported_by", "created_at",
        ]


class SupplyChainPolicySerializer(serializers.ModelSerializer):
    class Meta:
        model = SupplyChainPolicy
        fields = [
            "floor_percent", "floor_basis", "urgency_window_days", "use_net_of_open_po",
            "apply_moq_rounding", "include_changeover_in_capacity", "updated_at",
        ]


class DashboardQuerySerializer(serializers.Serializer):
    forecast_id = serializers.IntegerField(required=False, allow_null=True)
