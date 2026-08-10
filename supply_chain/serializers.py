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


# ── The daily operating loop ──────────────────────────────────────────────────

from .models import (  # noqa: E402
    DailyRun,
    DailyRunRow,
    DataQualityIssue,
    MonitoredSku,
    OperatingParameters,
    RowVerdict,
    SkuComponent,
)


class RowVerdictSerializer(serializers.ModelSerializer):
    class Meta:
        model = RowVerdict
        fields = ["outcome", "note", "supplier_promised_date", "recorded_by", "recorded_at"]


class DailyRunRowSerializer(serializers.ModelSerializer):
    """Every intermediate number travels with the answer, so the row can be
    checked on paper rather than believed."""

    row_verdict = RowVerdictSerializer(read_only=True)

    class Meta:
        model = DailyRunRow
        fields = [
            "id", "sku_code", "material_code", "material_name", "material_type",
            "supplier_name", "unit",
            "units_per_day", "quantity_per_unit", "consumption_per_day",
            "on_hand", "committed", "free_stock",
            "days_of_cover", "cover_calendar_days",
            "lead_time_days", "lead_time_source", "lead_time_samples",
            "stockout_date", "order_by_date", "days_late",
            "verdict", "owner", "row_verdict",
        ]


class DataQualityIssueSerializer(serializers.ModelSerializer):
    class Meta:
        model = DataQualityIssue
        fields = ["id", "code", "sku_code", "item_code", "message", "blocking"]


class DailyRunSerializer(serializers.ModelSerializer):
    is_credible = serializers.BooleanField(read_only=True)

    class Meta:
        model = DailyRun
        fields = [
            "id", "company_code", "run_date", "status",
            "red_count", "amber_count", "green_count", "unknown_count", "issue_count",
            "comment", "parameters_snapshot", "is_credible",
            "generated_at", "reviewed_by", "reviewed_at",
            "published_by", "published_at", "recipients",
        ]


class SkuComponentSerializer(serializers.ModelSerializer):
    class Meta:
        model = SkuComponent
        fields = ["id", "material_code", "material_name", "material_type",
                  "quantity_per_unit", "unit"]


class MonitoredSkuSerializer(serializers.ModelSerializer):
    components = SkuComponentSerializer(many=True, read_only=True)
    units_per_day = serializers.DecimalField(max_digits=24, decimal_places=6, read_only=True)

    class Meta:
        model = MonitoredSku
        fields = ["id", "sku_code", "sku_name", "plan_quantity", "working_days_left",
                  "units_per_day", "is_active", "components"]


class OperatingParametersSerializer(serializers.ModelSerializer):
    class Meta:
        model = OperatingParameters
        fields = [
            "lead_time_percentile", "min_delivery_samples", "amber_multiplier",
            "working_days_per_month", "calendar_days_per_month",
            "max_red_before_block", "last_changed_reason", "updated_by", "updated_at",
        ]
        read_only_fields = ["updated_by", "updated_at"]

    def validate_last_changed_reason(self, value):
        """A parameter change with no reason is indistinguishable from someone
        making the alarms quieter because they were annoying."""
        if not (value or "").strip():
            raise serializers.ValidationError(
                "Say why you are changing this — the weekly review depends on it."
            )
        return value


class VerdictInputSerializer(serializers.Serializer):
    outcome = serializers.ChoiceField(choices=["REAL", "WRONG_DATA", "ALREADY_HANDLED"])
    note = serializers.CharField(required=False, allow_blank=True)
    supplier_promised_date = serializers.DateField(required=False, allow_null=True)
