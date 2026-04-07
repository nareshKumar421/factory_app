from rest_framework import serializers
from .models import (
    ProductionLine, Machine, MachineChecklistTemplate,
    BreakdownCategory, LineSkuConfig,
    ProductionRun, ProductionSegment, MachineBreakdown,
    ProductionMaterialUsage, MachineRuntime, ProductionManpower,
    LineClearance, LineClearanceItem,
    MachineChecklistEntry, WasteLog,
    ResourceElectricity, ResourceWater, ResourceGas, ResourceCompressedAir,
    ResourceLabour, ResourceMachineCost, ResourceOverhead,
    ProductionRunCost, InProcessQCCheck, FinalQCCheck,
)


# ---------------------------------------------------------------------------
# Master Data
# ---------------------------------------------------------------------------

class ProductionLineSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductionLine
        fields = ['id', 'name', 'description', 'is_active', 'created_at', 'updated_at']
        read_only_fields = ['created_at', 'updated_at']


class ProductionLineCreateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=100)
    description = serializers.CharField(required=False, allow_blank=True, default='')


class MachineSerializer(serializers.ModelSerializer):
    line_name = serializers.CharField(source='line.name', read_only=True)

    class Meta:
        model = Machine
        fields = [
            'id', 'name', 'machine_type', 'line', 'line_name',
            'is_active', 'created_at', 'updated_at',
        ]
        read_only_fields = ['created_at', 'updated_at']


class MachineCreateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=200)
    machine_type = serializers.CharField(max_length=30)
    line_id = serializers.IntegerField()


class ChecklistTemplateSerializer(serializers.ModelSerializer):
    class Meta:
        model = MachineChecklistTemplate
        fields = [
            'id', 'machine_type', 'task', 'frequency',
            'sort_order', 'is_active', 'created_at', 'updated_at',
        ]
        read_only_fields = ['created_at', 'updated_at']


class ChecklistTemplateCreateSerializer(serializers.Serializer):
    machine_type = serializers.CharField(max_length=30)
    task = serializers.CharField(max_length=500)
    frequency = serializers.CharField(max_length=10)
    sort_order = serializers.IntegerField(required=False, default=0)


# ---------------------------------------------------------------------------
# Breakdown Categories
# ---------------------------------------------------------------------------

class BreakdownCategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = BreakdownCategory
        fields = ['id', 'name', 'is_active', 'created_at', 'updated_at']
        read_only_fields = ['created_at', 'updated_at']


class BreakdownCategoryCreateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=100)


# ---------------------------------------------------------------------------
# Production Runs
# ---------------------------------------------------------------------------

class ProductionRunCreateSerializer(serializers.Serializer):
    sap_doc_entry = serializers.IntegerField(required=False, allow_null=True)
    line_id = serializers.IntegerField()
    date = serializers.DateField()
    product = serializers.CharField(max_length=200, required=False, allow_blank=True, default='')
    required_qty = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=False, allow_null=True
    )
    rated_speed = serializers.DecimalField(
        max_digits=10, decimal_places=2, required=False, allow_null=True
    )
    machine_ids = serializers.ListField(
        child=serializers.IntegerField(), required=False, default=list
    )
    labour_count = serializers.IntegerField(min_value=0, required=False, default=0)
    other_manpower_count = serializers.IntegerField(min_value=0, required=False, default=0)
    supervisor = serializers.CharField(max_length=200, required=False, allow_blank=True, default='')
    operators = serializers.CharField(max_length=500, required=False, allow_blank=True, default='')
    materials = serializers.ListField(
        child=serializers.DictField(), required=False, default=list,
        help_text="List of {material_code, material_name, opening_qty, issued_qty, uom}"
    )


class ProductionRunUpdateSerializer(serializers.Serializer):
    product = serializers.CharField(max_length=200, required=False, allow_blank=True)
    rated_speed = serializers.DecimalField(
        max_digits=10, decimal_places=2, required=False, allow_null=True
    )
    machine_ids = serializers.ListField(
        child=serializers.IntegerField(), required=False
    )
    labour_count = serializers.IntegerField(min_value=0, required=False)
    other_manpower_count = serializers.IntegerField(min_value=0, required=False)
    supervisor = serializers.CharField(max_length=200, required=False, allow_blank=True)
    operators = serializers.CharField(max_length=500, required=False, allow_blank=True)
    rejected_qty = serializers.DecimalField(
        max_digits=12, decimal_places=1, required=False, default=0
    )
    reworked_qty = serializers.DecimalField(
        max_digits=12, decimal_places=1, required=False, default=0
    )


class ProductionRunListSerializer(serializers.ModelSerializer):
    line_name = serializers.CharField(source='line.name', read_only=True)
    live_status = serializers.SerializerMethodField()

    class Meta:
        model = ProductionRun
        fields = [
            'id', 'sap_doc_entry', 'run_number', 'date',
            'line', 'line_name', 'product', 'required_qty', 'rated_speed',
            'total_production', 'total_running_minutes', 'total_breakdown_time',
            'rejected_qty', 'reworked_qty',
            'sap_receipt_doc_entry', 'sap_sync_status', 'sap_sync_error',
            'warehouse_approval_status',
            'status', 'live_status', 'created_by', 'created_at',
        ]

    def get_live_status(self, obj):
        if obj.status == 'COMPLETED':
            return 'COMPLETED'
        if obj.status == 'DRAFT':
            return 'DRAFT'
        # IN_PROGRESS — check for active segments/breakdowns
        if obj.breakdowns.filter(is_active=True).exists():
            return 'BREAKDOWN'
        if obj.segments.filter(is_active=True).exists():
            return 'RUNNING'
        return 'STOPPED'


class ProductionSegmentSerializer(serializers.ModelSerializer):
    duration_minutes = serializers.IntegerField(read_only=True)

    class Meta:
        model = ProductionSegment
        fields = [
            'id', 'start_time', 'end_time', 'produced_cases',
            'is_active', 'duration_minutes', 'remarks', 'created_at', 'updated_at',
        ]
        read_only_fields = ['created_at', 'updated_at']


class SegmentUpdateSerializer(serializers.Serializer):
    """Update a closed segment — only remarks and produced_cases editable."""
    remarks = serializers.CharField(required=False, allow_blank=True)
    produced_cases = serializers.DecimalField(
        max_digits=12, decimal_places=1, required=False
    )


class BreakdownUpdateSerializer(serializers.Serializer):
    """Update a breakdown — only remarks editable."""
    remarks = serializers.CharField(required=False, allow_blank=True)


class MachineBreakdownSerializer(serializers.ModelSerializer):
    machine_name = serializers.CharField(source='machine.name', read_only=True)
    breakdown_category_name = serializers.CharField(
        source='breakdown_category.name', read_only=True, default=''
    )

    class Meta:
        model = MachineBreakdown
        fields = [
            'id', 'machine', 'machine_name', 'start_time', 'end_time',
            'breakdown_minutes', 'breakdown_category', 'breakdown_category_name',
            'is_active', 'is_unrecovered',
            'reason', 'remarks', 'created_at', 'updated_at',
        ]
        read_only_fields = ['created_at', 'updated_at']


class ProductionRunDetailSerializer(serializers.ModelSerializer):
    line_name = serializers.CharField(source='line.name', read_only=True)
    segments = serializers.SerializerMethodField()
    breakdowns = serializers.SerializerMethodField()
    machine_ids = serializers.SerializerMethodField()

    class Meta:
        model = ProductionRun
        fields = [
            'id', 'sap_doc_entry', 'run_number', 'date',
            'line', 'line_name', 'product', 'required_qty', 'rated_speed',
            'labour_count', 'other_manpower_count', 'supervisor', 'operators',
            'total_production', 'total_running_minutes', 'total_breakdown_time',
            'rejected_qty', 'reworked_qty',
            'sap_receipt_doc_entry', 'sap_sync_status', 'sap_sync_error',
            'warehouse_approval_status',
            'status', 'created_by', 'created_at', 'updated_at',
            'segments', 'breakdowns', 'machine_ids',
        ]

    def get_machine_ids(self, obj):
        return list(obj.machines.values_list('id', flat=True))

    def get_segments(self, obj):
        return ProductionSegmentSerializer(obj.segments.all(), many=True).data

    def get_breakdowns(self, obj):
        return MachineBreakdownSerializer(
            obj.breakdowns.select_related('machine', 'breakdown_category').all(),
            many=True
        ).data


# ---------------------------------------------------------------------------
# Timeline Action Serializers
# ---------------------------------------------------------------------------

class AddBreakdownSerializer(serializers.Serializer):
    """Used when operator clicks 'Add Breakdown' on the timeline."""
    breakdown_category_id = serializers.IntegerField()
    machine_id = serializers.IntegerField()
    reason = serializers.CharField(max_length=500)
    produced_cases = serializers.DecimalField(
        max_digits=12, decimal_places=1, required=False, default=0,
        help_text="Cases produced in the running segment being closed"
    )
    remarks = serializers.CharField(required=False, allow_blank=True, default='')


class ResolveBreakdownSerializer(serializers.Serializer):
    """Used when operator resolves a breakdown."""
    ACTION_CHOICES = [
        ('start_production', 'Fixed, Start Production'),
        ('stop_production', 'Fixed, Stop Production'),
        ('stop_unrecovered', 'Not Fixed, Stop Production'),
    ]
    action = serializers.ChoiceField(choices=ACTION_CHOICES)


class CompleteRunSerializer(serializers.Serializer):
    """Used when operator completes the run."""
    total_production = serializers.DecimalField(max_digits=12, decimal_places=1)


class StopProductionSerializer(serializers.Serializer):
    """Used when operator stops the running segment."""
    produced_cases = serializers.DecimalField(
        max_digits=12, decimal_places=1, default=0,
        help_text="Cases produced during this running period"
    )
    remarks = serializers.CharField(required=False, allow_blank=True, default='')


# ---------------------------------------------------------------------------
# Material Usage
# ---------------------------------------------------------------------------

class MaterialUsageSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductionMaterialUsage
        fields = [
            'id', 'material_code', 'material_name',
            'opening_qty', 'issued_qty', 'closing_qty', 'wastage_qty',
            'uom', 'created_at', 'updated_at',
        ]
        read_only_fields = ['wastage_qty', 'created_at', 'updated_at']


class MaterialUsageCreateSerializer(serializers.Serializer):
    material_code = serializers.CharField(max_length=50, required=False, allow_blank=True, default='')
    material_name = serializers.CharField(max_length=255)
    opening_qty = serializers.DecimalField(max_digits=12, decimal_places=3, min_value=0, default=0)
    issued_qty = serializers.DecimalField(max_digits=12, decimal_places=3, min_value=0, default=0)
    closing_qty = serializers.DecimalField(max_digits=12, decimal_places=3, min_value=0, required=False, allow_null=True)
    uom = serializers.CharField(max_length=20, required=False, allow_blank=True, default='')


# ---------------------------------------------------------------------------
# Machine Runtime
# ---------------------------------------------------------------------------

class MachineRuntimeSerializer(serializers.ModelSerializer):
    class Meta:
        model = MachineRuntime
        fields = [
            'id', 'machine', 'machine_type',
            'runtime_minutes', 'downtime_minutes',
            'remarks', 'created_at', 'updated_at',
        ]
        read_only_fields = ['created_at', 'updated_at']


class MachineRuntimeCreateSerializer(serializers.Serializer):
    machine_id = serializers.IntegerField(required=False, allow_null=True)
    machine_type = serializers.CharField(max_length=30)
    runtime_minutes = serializers.IntegerField(min_value=0, default=0)
    downtime_minutes = serializers.IntegerField(min_value=0, default=0)
    remarks = serializers.CharField(required=False, allow_blank=True, default='')


# ---------------------------------------------------------------------------
# Manpower
# ---------------------------------------------------------------------------

class ManpowerSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductionManpower
        fields = [
            'id', 'shift', 'worker_count',
            'supervisor', 'engineer', 'remarks',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['created_at', 'updated_at']


class ManpowerCreateSerializer(serializers.Serializer):
    shift = serializers.CharField(max_length=20)
    worker_count = serializers.IntegerField(min_value=0, default=0)
    supervisor = serializers.CharField(max_length=200, required=False, allow_blank=True, default='')
    engineer = serializers.CharField(max_length=200, required=False, allow_blank=True, default='')
    remarks = serializers.CharField(required=False, allow_blank=True, default='')


# ---------------------------------------------------------------------------
# Line Clearance
# ---------------------------------------------------------------------------

class LineClearanceItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = LineClearanceItem
        fields = ['id', 'checkpoint', 'sort_order', 'result', 'remarks']


class LineClearanceItemUpdateSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    result = serializers.CharField(max_length=5)
    remarks = serializers.CharField(required=False, allow_blank=True, default='')


class LineClearanceCreateSerializer(serializers.Serializer):
    production_run_id = serializers.IntegerField(required=False, allow_null=True)
    date = serializers.DateField()
    line_id = serializers.IntegerField()
    document_id = serializers.CharField(max_length=50, required=False, allow_blank=True, default='')


class LineClearanceDetailSerializer(serializers.ModelSerializer):
    items = LineClearanceItemSerializer(many=True, read_only=True)
    line_name = serializers.CharField(source='line.name', read_only=True)
    run_number = serializers.IntegerField(source='production_run.run_number', read_only=True, default=None)

    class Meta:
        model = LineClearance
        fields = [
            'id', 'production_run', 'run_number', 'date', 'line', 'line_name',
            'document_id', 'verified_by',
            'qa_approved', 'qa_approved_by', 'qa_approved_at',
            'production_supervisor_sign', 'production_incharge_sign',
            'status', 'created_by', 'created_at', 'updated_at',
            'items',
        ]


class LineClearanceListSerializer(serializers.ModelSerializer):
    line_name = serializers.CharField(source='line.name', read_only=True)
    run_number = serializers.IntegerField(source='production_run.run_number', read_only=True, default=None)

    class Meta:
        model = LineClearance
        fields = [
            'id', 'production_run', 'run_number', 'date', 'line', 'line_name',
            'document_id', 'status', 'qa_approved',
            'created_by', 'created_at',
        ]


class LineClearanceUpdateSerializer(serializers.Serializer):
    items = LineClearanceItemUpdateSerializer(many=True, required=False)
    production_supervisor_sign = serializers.CharField(
        max_length=200, required=False, allow_blank=True
    )
    production_incharge_sign = serializers.CharField(
        max_length=200, required=False, allow_blank=True
    )


# ---------------------------------------------------------------------------
# Machine Checklists
# ---------------------------------------------------------------------------

class MachineChecklistEntrySerializer(serializers.ModelSerializer):
    machine_name = serializers.CharField(source='machine.name', read_only=True)

    class Meta:
        model = MachineChecklistEntry
        fields = [
            'id', 'machine', 'machine_name', 'machine_type',
            'date', 'month', 'year', 'template',
            'task_description', 'frequency', 'status',
            'operator', 'shift_incharge', 'remarks',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['created_at', 'updated_at']


class MachineChecklistCreateSerializer(serializers.Serializer):
    machine_id = serializers.IntegerField()
    template_id = serializers.IntegerField()
    date = serializers.DateField()
    status = serializers.CharField(max_length=10, default='NA')
    operator = serializers.CharField(max_length=200, required=False, allow_blank=True, default='')
    shift_incharge = serializers.CharField(max_length=200, required=False, allow_blank=True, default='')
    remarks = serializers.CharField(required=False, allow_blank=True, default='')


# ---------------------------------------------------------------------------
# Waste Management
# ---------------------------------------------------------------------------

class WasteLogSerializer(serializers.ModelSerializer):
    run_number = serializers.IntegerField(source='production_run.run_number', read_only=True, default=None)
    run_date = serializers.DateField(source='production_run.date', read_only=True, default=None)
    run_product = serializers.CharField(source='production_run.product', read_only=True, default='')

    class Meta:
        model = WasteLog
        fields = [
            'id', 'production_run', 'run_number', 'run_date', 'run_product', 'material_code', 'material_name',
            'wastage_qty', 'uom', 'reason',
            'engineer_sign', 'engineer_signed_by', 'engineer_signed_at',
            'am_sign', 'am_signed_by', 'am_signed_at',
            'store_sign', 'store_signed_by', 'store_signed_at',
            'hod_sign', 'hod_signed_by', 'hod_signed_at',
            'wastage_approval_status',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['created_at', 'updated_at']


class WasteLogCreateSerializer(serializers.Serializer):
    production_run_id = serializers.IntegerField()
    material_code = serializers.CharField(max_length=50, required=False, allow_blank=True, default='')
    material_name = serializers.CharField(max_length=255)
    wastage_qty = serializers.DecimalField(max_digits=12, decimal_places=3)
    uom = serializers.CharField(max_length=20, required=False, allow_blank=True, default='')
    reason = serializers.CharField(required=False, allow_blank=True, default='')


class WasteApprovalSerializer(serializers.Serializer):
    sign = serializers.CharField(max_length=200)
    remarks = serializers.CharField(required=False, allow_blank=True, default='')


# ---------------------------------------------------------------------------
# Resource Tracking Serializers
# ---------------------------------------------------------------------------

class ResourceElectricitySerializer(serializers.ModelSerializer):
    class Meta:
        model = ResourceElectricity
        fields = ['id', 'description', 'units_consumed', 'rate_per_unit', 'total_cost', 'created_by', 'created_at', 'updated_at']
        read_only_fields = ['id', 'total_cost', 'created_at', 'updated_at']


class ResourceElectricityCreateSerializer(serializers.Serializer):
    description = serializers.CharField(max_length=200, required=False, default='')
    units_consumed = serializers.DecimalField(max_digits=12, decimal_places=3)
    rate_per_unit = serializers.DecimalField(max_digits=12, decimal_places=4)


class ResourceWaterSerializer(serializers.ModelSerializer):
    class Meta:
        model = ResourceWater
        fields = ['id', 'description', 'volume_consumed', 'rate_per_unit', 'total_cost', 'created_by', 'created_at', 'updated_at']
        read_only_fields = ['id', 'total_cost', 'created_at', 'updated_at']


class ResourceWaterCreateSerializer(serializers.Serializer):
    description = serializers.CharField(max_length=200, required=False, default='')
    volume_consumed = serializers.DecimalField(max_digits=12, decimal_places=3)
    rate_per_unit = serializers.DecimalField(max_digits=12, decimal_places=4)


class ResourceGasSerializer(serializers.ModelSerializer):
    class Meta:
        model = ResourceGas
        fields = ['id', 'description', 'qty_consumed', 'rate_per_unit', 'total_cost', 'created_by', 'created_at', 'updated_at']
        read_only_fields = ['id', 'total_cost', 'created_at', 'updated_at']


class ResourceGasCreateSerializer(serializers.Serializer):
    description = serializers.CharField(max_length=200, required=False, default='')
    qty_consumed = serializers.DecimalField(max_digits=12, decimal_places=3)
    rate_per_unit = serializers.DecimalField(max_digits=12, decimal_places=4)


class ResourceCompressedAirSerializer(serializers.ModelSerializer):
    class Meta:
        model = ResourceCompressedAir
        fields = ['id', 'description', 'units_consumed', 'rate_per_unit', 'total_cost', 'created_by', 'created_at', 'updated_at']
        read_only_fields = ['id', 'total_cost', 'created_at', 'updated_at']


class ResourceCompressedAirCreateSerializer(serializers.Serializer):
    description = serializers.CharField(max_length=200, required=False, default='')
    units_consumed = serializers.DecimalField(max_digits=12, decimal_places=3)
    rate_per_unit = serializers.DecimalField(max_digits=12, decimal_places=4)


class ResourceLabourSerializer(serializers.ModelSerializer):
    class Meta:
        model = ResourceLabour
        fields = ['id', 'description', 'worker_count', 'hours_worked', 'rate_per_hour', 'total_cost', 'created_by', 'created_at', 'updated_at']
        read_only_fields = ['id', 'total_cost', 'created_at', 'updated_at']


class ResourceLabourCreateSerializer(serializers.Serializer):
    description = serializers.CharField(max_length=200, required=False, allow_blank=True, default='')
    worker_count = serializers.IntegerField(min_value=1, default=1)
    hours_worked = serializers.DecimalField(max_digits=8, decimal_places=2)
    rate_per_hour = serializers.DecimalField(max_digits=12, decimal_places=4)


class ResourceMachineCostSerializer(serializers.ModelSerializer):
    class Meta:
        model = ResourceMachineCost
        fields = ['id', 'machine_name', 'hours_used', 'rate_per_hour', 'total_cost', 'created_by', 'created_at', 'updated_at']
        read_only_fields = ['id', 'total_cost', 'created_at', 'updated_at']


class ResourceMachineCostCreateSerializer(serializers.Serializer):
    machine_name = serializers.CharField(max_length=200)
    hours_used = serializers.DecimalField(max_digits=8, decimal_places=2)
    rate_per_hour = serializers.DecimalField(max_digits=12, decimal_places=4)


class ResourceOverheadSerializer(serializers.ModelSerializer):
    class Meta:
        model = ResourceOverhead
        fields = ['id', 'expense_name', 'amount', 'created_by', 'created_at', 'updated_at']
        read_only_fields = ['id', 'created_at', 'updated_at']


class ResourceOverheadCreateSerializer(serializers.Serializer):
    expense_name = serializers.CharField(max_length=200)
    amount = serializers.DecimalField(max_digits=15, decimal_places=2)


# ---------------------------------------------------------------------------
# Cost Summary Serializer
# ---------------------------------------------------------------------------

class ProductionRunCostSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductionRunCost
        fields = [
            'id', 'raw_material_cost', 'labour_cost', 'machine_cost',
            'electricity_cost', 'water_cost', 'gas_cost', 'compressed_air_cost',
            'overhead_cost', 'total_cost', 'produced_qty', 'per_unit_cost', 'calculated_at'
        ]
        read_only_fields = [
            'id', 'raw_material_cost', 'labour_cost', 'machine_cost',
            'electricity_cost', 'water_cost', 'gas_cost', 'compressed_air_cost',
            'overhead_cost', 'total_cost', 'produced_qty', 'per_unit_cost', 'calculated_at'
        ]


# ---------------------------------------------------------------------------
# QC Check Serializers
# ---------------------------------------------------------------------------

class InProcessQCCheckSerializer(serializers.ModelSerializer):
    class Meta:
        model = InProcessQCCheck
        fields = [
            'id', 'checked_at', 'parameter', 'acceptable_min', 'acceptable_max',
            'actual_value', 'result', 'remarks', 'checked_by', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class InProcessQCCheckCreateSerializer(serializers.Serializer):
    checked_at = serializers.DateTimeField()
    parameter = serializers.CharField(max_length=200)
    acceptable_min = serializers.DecimalField(max_digits=10, decimal_places=3, required=False, allow_null=True)
    acceptable_max = serializers.DecimalField(max_digits=10, decimal_places=3, required=False, allow_null=True)
    actual_value = serializers.DecimalField(max_digits=10, decimal_places=3, required=False, allow_null=True)
    result = serializers.ChoiceField(choices=['PASS', 'FAIL', 'NA'], default='NA')
    remarks = serializers.CharField(required=False, default='', allow_blank=True)


class FinalQCCheckSerializer(serializers.ModelSerializer):
    class Meta:
        model = FinalQCCheck
        fields = [
            'id', 'checked_at', 'overall_result', 'parameters',
            'remarks', 'checked_by', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class FinalQCCheckCreateSerializer(serializers.Serializer):
    checked_at = serializers.DateTimeField()
    overall_result = serializers.ChoiceField(choices=['PASS', 'FAIL', 'CONDITIONAL'])
    parameters = serializers.ListField(child=serializers.DictField(), default=list)
    remarks = serializers.CharField(required=False, default='', allow_blank=True)


# ---------------------------------------------------------------------------
# SAP Order Serializers (plain, not model-based)
# ---------------------------------------------------------------------------

class SAPProductionOrderSerializer(serializers.Serializer):
    DocEntry = serializers.IntegerField()
    DocNum = serializers.IntegerField()
    ItemCode = serializers.CharField()
    ProdName = serializers.CharField()
    PlannedQty = serializers.FloatField()
    CmpltQty = serializers.FloatField()
    RjctQty = serializers.FloatField()
    RemainingQty = serializers.FloatField()
    StartDate = serializers.DateField(allow_null=True)
    DueDate = serializers.DateField(allow_null=True)
    Warehouse = serializers.CharField(allow_null=True, allow_blank=True)
    Status = serializers.CharField()


class SAPOrderComponentSerializer(serializers.Serializer):
    ItemCode = serializers.CharField()
    ItemName = serializers.CharField()
    PlannedQty = serializers.FloatField()
    IssuedQty = serializers.FloatField()
    Warehouse = serializers.CharField(allow_null=True, allow_blank=True)
    UomCode = serializers.CharField(allow_null=True, allow_blank=True)


class SAPBOMComponentSerializer(serializers.Serializer):
    """BOM component from SAP — used for item BOM preview endpoint."""
    ItemCode = serializers.CharField()
    ItemName = serializers.CharField()
    PlannedQty = serializers.FloatField()
    IssuedQty = serializers.FloatField(required=False, default=0)
    UomCode = serializers.CharField(allow_null=True, allow_blank=True)


# ---------------------------------------------------------------------------
# Line SKU Config
# ---------------------------------------------------------------------------

class LineSkuConfigSerializer(serializers.ModelSerializer):
    line_name = serializers.CharField(source='line.name', read_only=True)

    class Meta:
        model = LineSkuConfig
        fields = [
            'id', 'line', 'line_name', 'config_name',
            'sku_code', 'sku_name',
            'rated_speed', 'labour_count', 'other_manpower_count',
            'supervisor', 'operators', 'is_active',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'line_name', 'created_at', 'updated_at']


class LineSkuConfigCreateSerializer(serializers.Serializer):
    line_id = serializers.IntegerField()
    config_name = serializers.CharField(max_length=300)
    sku_code = serializers.CharField(max_length=100, required=False, allow_blank=True, default='')
    sku_name = serializers.CharField(max_length=300, required=False, allow_blank=True, default='')
    rated_speed = serializers.DecimalField(max_digits=10, decimal_places=2, required=False, allow_null=True)
    labour_count = serializers.IntegerField(min_value=0, required=False, default=0)
    other_manpower_count = serializers.IntegerField(min_value=0, required=False, default=0)
    supervisor = serializers.CharField(max_length=200, required=False, allow_blank=True, default='')
    operators = serializers.CharField(max_length=500, required=False, allow_blank=True, default='')


class LineSkuConfigUpdateSerializer(serializers.Serializer):
    config_name = serializers.CharField(max_length=300, required=False)
    sku_code = serializers.CharField(max_length=100, required=False, allow_blank=True)
    sku_name = serializers.CharField(max_length=300, required=False, allow_blank=True)
    rated_speed = serializers.DecimalField(max_digits=10, decimal_places=2, required=False, allow_null=True)
    labour_count = serializers.IntegerField(min_value=0, required=False)
    other_manpower_count = serializers.IntegerField(min_value=0, required=False)
    supervisor = serializers.CharField(max_length=200, required=False, allow_blank=True)
    operators = serializers.CharField(max_length=500, required=False, allow_blank=True)
