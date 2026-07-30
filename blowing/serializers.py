from rest_framework import serializers

from .models import (
    BlowingMachine, PreformSpec, BlowingRateConfig, BlowingRun, BlowingRunCost,
    BottleBuyPrice, BlowingSegment, BlowingBreakdown, BlowingBreakdownCategory,
    BlowingAuditLog, BlowingCostRate, BlowingRunCostLine,
)


class BlowingAuditLogSerializer(serializers.ModelSerializer):
    user_name = serializers.SerializerMethodField()

    class Meta:
        model = BlowingAuditLog
        fields = ['id', 'entity_type', 'entity_id', 'action', 'changes',
                  'user_name', 'created_at']

    def get_user_name(self, obj):
        u = obj.user
        if not u:
            return 'System'
        full = u.get_full_name() if hasattr(u, 'get_full_name') else ''
        return full or u.get_username()


def live_status(obj) -> str:
    """Derive RUNNING/BREAKDOWN/STOPPED from active children (not stored)."""
    if obj.status == 'COMPLETED':
        return 'COMPLETED'
    if obj.status == 'DRAFT':
        return 'DRAFT'
    if obj.breakdowns.filter(is_active=True).exists():
        return 'BREAKDOWN'
    if obj.segments.filter(is_active=True).exists():
        return 'RUNNING'
    return 'STOPPED'


# ---------------------------------------------------------------------------
# Machines
# ---------------------------------------------------------------------------
class BlowingMachineSerializer(serializers.ModelSerializer):
    company_code = serializers.CharField(source='company.code', read_only=True)

    class Meta:
        model = BlowingMachine
        fields = [
            'id', 'company_code', 'name', 'heads', 'sap_warehouse_code',
            'depreciation_per_day', 'is_active', 'created_at', 'updated_at',
        ]
        read_only_fields = ['created_at', 'updated_at']


class BlowingMachineCreateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=150)
    heads = serializers.IntegerField(required=False, allow_null=True, min_value=0)
    sap_warehouse_code = serializers.CharField(required=False, allow_blank=True, default='')
    depreciation_per_day = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=False, default=0)


# ---------------------------------------------------------------------------
# Preform specs
# ---------------------------------------------------------------------------
class PreformSpecSerializer(serializers.ModelSerializer):
    class Meta:
        model = PreformSpec
        fields = [
            'id', 'make', 'gram', 'preforms_per_box', 'preform_rate_per_bottle',
            'sap_item_code', 'sap_item_name', 'bottle_weight_g', 'bottles_per_kg',
            'mould_cost', 'mould_life_bottles',
            'std_make_cost_per_bottle', 'std_reject_pct', 'std_units_per_bottle',
            'is_active', 'created_at', 'updated_at',
        ]
        read_only_fields = ['created_at', 'updated_at']


class PreformSpecCreateSerializer(serializers.Serializer):
    make = serializers.CharField(max_length=100)
    gram = serializers.DecimalField(max_digits=6, decimal_places=2)
    preforms_per_box = serializers.IntegerField(min_value=1)
    preform_rate_per_bottle = serializers.DecimalField(
        max_digits=10, decimal_places=4, required=False, default=0)
    sap_item_code = serializers.CharField(required=False, allow_blank=True, default='')
    sap_item_name = serializers.CharField(required=False, allow_blank=True, default='')
    bottle_weight_g = serializers.DecimalField(
        max_digits=6, decimal_places=2, required=False, allow_null=True)
    bottles_per_kg = serializers.DecimalField(
        max_digits=8, decimal_places=2, required=False, allow_null=True)
    mould_cost = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=False, allow_null=True)
    mould_life_bottles = serializers.IntegerField(
        required=False, allow_null=True, min_value=0)
    std_make_cost_per_bottle = serializers.DecimalField(
        max_digits=12, decimal_places=4, required=False, allow_null=True)
    std_reject_pct = serializers.DecimalField(
        max_digits=6, decimal_places=3, required=False, allow_null=True)
    std_units_per_bottle = serializers.DecimalField(
        max_digits=10, decimal_places=6, required=False, allow_null=True)


# ---------------------------------------------------------------------------
# Rate config
# ---------------------------------------------------------------------------
class BlowingRateConfigSerializer(serializers.ModelSerializer):
    class Meta:
        model = BlowingRateConfig
        fields = [
            'id', 'effective_from', 'operator_rate_per_day', 'labour_rate_per_day',
            'electricity_rate_per_unit',
            'scrap_rate_per_bottle', 'packing_rate_per_bottle',
            'maintenance_per_day', 'factory_overhead_per_day', 'qa_cost_per_day',
            'is_active', 'created_at', 'updated_at',
        ]
        read_only_fields = ['created_at', 'updated_at']


class BlowingRateConfigCreateSerializer(serializers.Serializer):
    effective_from = serializers.DateField()
    operator_rate_per_day = serializers.DecimalField(max_digits=10, decimal_places=2)
    labour_rate_per_day = serializers.DecimalField(max_digits=10, decimal_places=2)
    electricity_rate_per_unit = serializers.DecimalField(max_digits=10, decimal_places=4)
    scrap_rate_per_bottle = serializers.DecimalField(max_digits=10, decimal_places=4)
    packing_rate_per_bottle = serializers.DecimalField(max_digits=10, decimal_places=4)
    maintenance_per_day = serializers.DecimalField(max_digits=12, decimal_places=2, required=False, default=0)
    factory_overhead_per_day = serializers.DecimalField(max_digits=12, decimal_places=2, required=False, default=0)
    qa_cost_per_day = serializers.DecimalField(max_digits=12, decimal_places=2, required=False, default=0)


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------
class BlowingRunCostSerializer(serializers.ModelSerializer):
    class Meta:
        model = BlowingRunCost
        exclude = ['id', 'run']


class BlowingRunCostLineSerializer(serializers.ModelSerializer):
    category_display = serializers.CharField(source='get_category_display', read_only=True)

    class Meta:
        model = BlowingRunCostLine
        fields = ['id', 'category', 'category_display', 'basis',
                  'quantity', 'rate', 'amount', 'is_credit', 'note']
        read_only_fields = fields


class BlowingCostRateSerializer(serializers.ModelSerializer):
    machine_name = serializers.CharField(source='machine.name', read_only=True, default=None)
    category_display = serializers.CharField(source='get_category_display', read_only=True)
    basis_display = serializers.CharField(source='get_basis_display', read_only=True)

    class Meta:
        model = BlowingCostRate
        fields = ['id', 'machine', 'machine_name', 'category', 'category_display',
                  'basis', 'basis_display', 'rate', 'is_credit', 'label',
                  'is_active', 'created_at', 'updated_at']
        read_only_fields = ['id', 'machine_name', 'category_display', 'basis_display',
                            'created_at', 'updated_at']


class BlowingCostRateCreateSerializer(serializers.Serializer):
    machine_id = serializers.IntegerField(required=False, allow_null=True)
    category = serializers.ChoiceField(choices=BlowingCostRate._meta.get_field('category').choices)
    basis = serializers.ChoiceField(choices=BlowingCostRate._meta.get_field('basis').choices)
    rate = serializers.DecimalField(max_digits=15, decimal_places=4)
    is_credit = serializers.BooleanField(required=False, default=False)
    label = serializers.CharField(max_length=200, required=False, allow_blank=True, default='')


class BlowingCostRateUpdateSerializer(serializers.Serializer):
    basis = serializers.ChoiceField(
        choices=BlowingCostRate._meta.get_field('basis').choices, required=False)
    rate = serializers.DecimalField(max_digits=15, decimal_places=4, required=False)
    is_credit = serializers.BooleanField(required=False)
    label = serializers.CharField(max_length=200, required=False, allow_blank=True)


class BlowingRunListSerializer(serializers.ModelSerializer):
    machine_name = serializers.CharField(source='machine.name', read_only=True)
    preform_make = serializers.CharField(source='preform_spec.make', read_only=True)
    preform_gram = serializers.DecimalField(
        source='preform_spec.gram', max_digits=6, decimal_places=2, read_only=True)
    net_cost = serializers.DecimalField(
        source='cost_summary.net_cost', max_digits=14, decimal_places=2,
        read_only=True, default=None)
    per_bottle_cost = serializers.DecimalField(
        source='cost_summary.per_bottle_cost', max_digits=12, decimal_places=6,
        read_only=True, default=None)
    make_cost_per_bottle = serializers.DecimalField(
        source='cost_summary.make_cost_per_bottle', max_digits=12, decimal_places=6,
        read_only=True, default=None)
    live_status = serializers.SerializerMethodField()

    class Meta:
        model = BlowingRun
        fields = [
            'id', 'run_number', 'date', 'machine', 'machine_name',
            'preform_spec', 'preform_make', 'preform_gram',
            'preform_boxes_used', 'preform_used_g', 'total_units',
            'total_counter_production', 'rejection_pcs', 'rejection_pct',
            'total_manpower', 'status', 'live_status', 'warehouse_approval_status',
            'net_cost', 'per_bottle_cost', 'make_cost_per_bottle', 'created_at',
        ]

    def get_live_status(self, obj):
        return live_status(obj)


class BlowingSegmentSerializer(serializers.ModelSerializer):
    duration_minutes = serializers.IntegerField(read_only=True)

    class Meta:
        model = BlowingSegment
        fields = [
            'id', 'start_time', 'end_time', 'produced_pcs', 'is_active',
            'duration_minutes', 'remarks', 'created_at', 'updated_at',
        ]


class BlowingBreakdownSerializer(serializers.ModelSerializer):
    machine_name = serializers.CharField(source='machine.name', read_only=True, default=None)
    breakdown_category_name = serializers.CharField(
        source='breakdown_category.name', read_only=True, default='')

    class Meta:
        model = BlowingBreakdown
        fields = [
            'id', 'machine', 'machine_name', 'breakdown_category', 'breakdown_category_name',
            'start_time', 'end_time', 'breakdown_minutes', 'is_active', 'is_unrecovered',
            'reason', 'remarks', 'created_at', 'updated_at',
        ]


class BlowingBreakdownCategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = BlowingBreakdownCategory
        fields = ['id', 'name', 'is_active', 'created_at', 'updated_at']
        read_only_fields = ['created_at', 'updated_at']


class BlowingBreakdownCategoryCreateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=100)


class BlowingRunDetailSerializer(serializers.ModelSerializer):
    machine_name = serializers.CharField(source='machine.name', read_only=True)
    preform_make = serializers.CharField(source='preform_spec.make', read_only=True)
    preform_gram = serializers.DecimalField(
        source='preform_spec.gram', max_digits=6, decimal_places=2, read_only=True)
    cost = BlowingRunCostSerializer(source='cost_summary', read_only=True)
    cost_lines = BlowingRunCostLineSerializer(many=True, read_only=True)
    live_status = serializers.SerializerMethodField()
    segments = BlowingSegmentSerializer(many=True, read_only=True)
    breakdowns = BlowingBreakdownSerializer(many=True, read_only=True)

    class Meta:
        model = BlowingRun
        fields = [
            'id', 'run_number', 'date', 'machine', 'machine_name',
            'preform_spec', 'preform_make', 'preform_gram',
            'preform_boxes_used', 'preform_used_g',
            'machine_start_reading', 'machine_stop_reading', 'machine_units',
            'utility_units', 'utility_cost', 'total_units',
            'total_counter_production', 'rejection_pcs', 'rejection_pct',
            'operator_count', 'contract_labour_count', 'own_labour_count',
            'total_manpower', 'scrap_carton_value', 'remarks',
            'status', 'live_status', 'warehouse_approval_status',
            'total_running_minutes', 'total_breakdown_time',
            'rate_config',
            'operator_rate_per_day', 'labour_rate_per_day',
            'electricity_rate_per_unit', 'preform_rate_per_bottle',
            'scrap_rate_per_bottle', 'packing_rate_per_bottle',
            'sap_preform_item_code', 'sap_bottle_item_code',
            'segments', 'breakdowns',
            'cost', 'cost_lines', 'created_at', 'updated_at',
        ]

    def get_live_status(self, obj):
        return live_status(obj)


class BlowingRunCreateSerializer(serializers.Serializer):
    date = serializers.DateField()
    machine_id = serializers.IntegerField()
    preform_spec_id = serializers.IntegerField()
    preform_boxes_used = serializers.DecimalField(
        max_digits=10, decimal_places=2, required=False, default=0)
    machine_start_reading = serializers.DecimalField(
        max_digits=14, decimal_places=4, required=False, allow_null=True)
    machine_stop_reading = serializers.DecimalField(
        max_digits=14, decimal_places=4, required=False, allow_null=True)
    utility_cost = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=False, default=0)
    total_counter_production = serializers.IntegerField(required=False, default=0, min_value=0)
    rejection_pcs = serializers.IntegerField(required=False, default=0, min_value=0)
    operator_count = serializers.IntegerField(required=False, default=0, min_value=0)
    contract_labour_count = serializers.IntegerField(required=False, default=0, min_value=0)
    own_labour_count = serializers.IntegerField(required=False, default=0, min_value=0)
    scrap_carton_value = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=False, default=0)
    remarks = serializers.CharField(required=False, allow_blank=True, default='')
    status = serializers.ChoiceField(
        choices=['DRAFT', 'IN_PROGRESS', 'COMPLETED'], required=False, default='DRAFT')


class BottleBuyPriceSerializer(serializers.ModelSerializer):
    preform_make = serializers.CharField(source='preform_spec.make', read_only=True)
    preform_gram = serializers.DecimalField(
        source='preform_spec.gram', max_digits=6, decimal_places=2, read_only=True)
    landed_cost_per_bottle = serializers.DecimalField(
        max_digits=12, decimal_places=4, read_only=True)

    class Meta:
        model = BottleBuyPrice
        fields = [
            'id', 'preform_spec', 'preform_make', 'preform_gram', 'supplier_name',
            'effective_from', 'buy_price', 'freight_per_bottle', 'duties_per_bottle',
            'carrying_pct_annual', 'inventory_days', 'qa_allowance_pct',
            'risk_premium_per_bottle', 'landed_cost_per_bottle',
            'is_active', 'created_at', 'updated_at',
        ]
        read_only_fields = ['created_at', 'updated_at']


class BottleBuyPriceCreateSerializer(serializers.Serializer):
    preform_spec_id = serializers.IntegerField()
    supplier_name = serializers.CharField(required=False, allow_blank=True, default='')
    effective_from = serializers.DateField(required=False)
    buy_price = serializers.DecimalField(max_digits=12, decimal_places=4)
    freight_per_bottle = serializers.DecimalField(max_digits=12, decimal_places=4, required=False, default=0)
    duties_per_bottle = serializers.DecimalField(max_digits=12, decimal_places=4, required=False, default=0)
    carrying_pct_annual = serializers.DecimalField(max_digits=6, decimal_places=2, required=False, default=0)
    inventory_days = serializers.IntegerField(required=False, default=30, min_value=0)
    qa_allowance_pct = serializers.DecimalField(max_digits=6, decimal_places=2, required=False, default=0)
    risk_premium_per_bottle = serializers.DecimalField(max_digits=12, decimal_places=4, required=False, default=0)


class BlowingRunUpdateSerializer(serializers.Serializer):
    machine_id = serializers.IntegerField(required=False)
    preform_spec_id = serializers.IntegerField(required=False)
    preform_boxes_used = serializers.DecimalField(
        max_digits=10, decimal_places=2, required=False)
    machine_start_reading = serializers.DecimalField(
        max_digits=14, decimal_places=4, required=False, allow_null=True)
    machine_stop_reading = serializers.DecimalField(
        max_digits=14, decimal_places=4, required=False, allow_null=True)
    utility_cost = serializers.DecimalField(max_digits=12, decimal_places=2, required=False)
    total_counter_production = serializers.IntegerField(required=False, min_value=0)
    rejection_pcs = serializers.IntegerField(required=False, min_value=0)
    operator_count = serializers.IntegerField(required=False, min_value=0)
    contract_labour_count = serializers.IntegerField(required=False, min_value=0)
    own_labour_count = serializers.IntegerField(required=False, min_value=0)
    scrap_carton_value = serializers.DecimalField(max_digits=12, decimal_places=2, required=False)
    remarks = serializers.CharField(required=False, allow_blank=True)
    status = serializers.ChoiceField(
        choices=['DRAFT', 'IN_PROGRESS', 'COMPLETED'], required=False)


# ---------------------------------------------------------------------------
# Lifecycle actions
# ---------------------------------------------------------------------------
class StopProductionSerializer(serializers.Serializer):
    produced_pcs = serializers.DecimalField(max_digits=12, decimal_places=1, min_value=0)
    remarks = serializers.CharField(required=False, allow_blank=True, default='')


class AddBreakdownSerializer(serializers.Serializer):
    breakdown_category_id = serializers.IntegerField(required=False, allow_null=True)
    machine_id = serializers.IntegerField(required=False, allow_null=True)
    reason = serializers.CharField(max_length=500)
    produced_pcs = serializers.DecimalField(
        max_digits=12, decimal_places=1, required=False, default=0)
    remarks = serializers.CharField(required=False, allow_blank=True, default='')


class ResolveBreakdownSerializer(serializers.Serializer):
    action = serializers.ChoiceField(
        choices=['start_production', 'stop_production', 'stop_unrecovered'])


class AddManualSegmentSerializer(serializers.Serializer):
    """Backfill a completed running segment with explicit start/end times."""
    start_time = serializers.DateTimeField()
    end_time = serializers.DateTimeField()
    produced_pcs = serializers.DecimalField(
        max_digits=12, decimal_places=1, required=False, default=0)
    remarks = serializers.CharField(required=False, allow_blank=True, default='')

    def validate(self, attrs):
        if attrs['end_time'] <= attrs['start_time']:
            raise serializers.ValidationError("End time must be after start time.")
        return attrs


class AddManualBreakdownSerializer(serializers.Serializer):
    """Backfill a completed breakdown with explicit start/end times."""
    start_time = serializers.DateTimeField()
    end_time = serializers.DateTimeField()
    breakdown_category_id = serializers.IntegerField(required=False, allow_null=True)
    machine_id = serializers.IntegerField(required=False, allow_null=True)
    reason = serializers.CharField(max_length=500)
    remarks = serializers.CharField(required=False, allow_blank=True, default='')

    def validate(self, attrs):
        if attrs['end_time'] <= attrs['start_time']:
            raise serializers.ValidationError("End time must be after start time.")
        return attrs


class UpdateSegmentSerializer(serializers.Serializer):
    produced_pcs = serializers.DecimalField(
        max_digits=12, decimal_places=1, required=False)
    remarks = serializers.CharField(required=False, allow_blank=True)


class CompleteRunSerializer(serializers.Serializer):
    total_counter_production = serializers.IntegerField(min_value=0)
    rejection_pcs = serializers.IntegerField(required=False, default=0, min_value=0)
    operator_count = serializers.IntegerField(required=False, default=0, min_value=0)
    contract_labour_count = serializers.IntegerField(required=False, default=0, min_value=0)
    own_labour_count = serializers.IntegerField(required=False, default=0, min_value=0)
    machine_start_reading = serializers.DecimalField(max_digits=14, decimal_places=4)
    machine_stop_reading = serializers.DecimalField(max_digits=14, decimal_places=4)
    utility_cost = serializers.DecimalField(max_digits=12, decimal_places=2)
    scrap_carton_value = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=False, default=0)

    def validate(self, data):
        start, stop = data.get('machine_start_reading'), data.get('machine_stop_reading')
        if start is not None and stop is not None and stop < start:
            raise serializers.ValidationError(
                {'machine_stop_reading': 'Stop reading must be ≥ start reading.'})
        return data


class SubmitPreformRequestSerializer(serializers.Serializer):
    remarks = serializers.CharField(required=False, allow_blank=True, default='')
