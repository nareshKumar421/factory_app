from rest_framework import serializers
from .models import ProductionPlan, PlanMaterialRequirement, WeeklyPlan, DailyProductionEntry


# ---------------------------------------------------------------------------
# Dropdown serializers (SAP HANA data)
# ---------------------------------------------------------------------------

class ItemDropdownSerializer(serializers.Serializer):
    item_code = serializers.CharField()
    item_name = serializers.CharField()
    uom = serializers.CharField()
    item_group = serializers.CharField()
    make_item = serializers.BooleanField()
    purchase_item = serializers.BooleanField()


class UoMDropdownSerializer(serializers.Serializer):
    uom_code = serializers.CharField()
    uom_name = serializers.CharField()


class WarehouseDropdownSerializer(serializers.Serializer):
    warehouse_code = serializers.CharField()
    warehouse_name = serializers.CharField()


class BOMComponentSerializer(serializers.Serializer):
    component_code  = serializers.CharField()
    component_name  = serializers.CharField()
    uom             = serializers.CharField()
    qty_per_unit    = serializers.FloatField()
    required_qty    = serializers.FloatField()
    available_stock = serializers.FloatField()
    shortage_qty    = serializers.FloatField()
    has_shortage    = serializers.BooleanField()


class BOMResponseSerializer(serializers.Serializer):
    item_code    = serializers.CharField()
    item_name    = serializers.CharField()
    planned_qty  = serializers.FloatField()
    bom_found    = serializers.BooleanField()
    has_shortage = serializers.BooleanField()
    components   = BOMComponentSerializer(many=True)


# ---------------------------------------------------------------------------
# Material requirement
# ---------------------------------------------------------------------------

class PlanMaterialSerializer(serializers.ModelSerializer):
    class Meta:
        model = PlanMaterialRequirement
        fields = ['id', 'component_code', 'component_name', 'required_qty', 'uom', 'warehouse_code']


class PlanMaterialCreateSerializer(serializers.Serializer):
    component_code = serializers.CharField(max_length=50)
    component_name = serializers.CharField(max_length=255)
    required_qty = serializers.DecimalField(max_digits=12, decimal_places=3, min_value=0.001)
    uom = serializers.CharField(max_length=20, required=False, allow_blank=True, default='')
    warehouse_code = serializers.CharField(max_length=20, required=False, allow_blank=True, default='')


# ---------------------------------------------------------------------------
# Production plan create / update
# ---------------------------------------------------------------------------

class ProductionPlanCreateSerializer(serializers.Serializer):
    """Used when creating a new production plan in Sampooran."""
    item_code = serializers.CharField(max_length=50)
    item_name = serializers.CharField(max_length=255)
    uom = serializers.CharField(max_length=20, required=False, allow_blank=True, default='')
    warehouse_code = serializers.CharField(max_length=20, required=False, allow_blank=True, default='')
    planned_qty = serializers.DecimalField(max_digits=12, decimal_places=3, min_value=0.001)
    target_start_date = serializers.DateField()
    due_date = serializers.DateField()
    branch_id = serializers.IntegerField(required=False, allow_null=True)
    remarks = serializers.CharField(required=False, allow_blank=True, default='')
    materials = PlanMaterialCreateSerializer(many=True, required=False, default=list)

    def validate(self, attrs):
        if attrs['due_date'] < attrs['target_start_date']:
            raise serializers.ValidationError("due_date must be on or after target_start_date.")
        return attrs


class ProductionPlanUpdateSerializer(serializers.Serializer):
    """Partial update — only allowed for DRAFT plans."""
    item_code = serializers.CharField(max_length=50, required=False)
    item_name = serializers.CharField(max_length=255, required=False)
    uom = serializers.CharField(max_length=20, required=False, allow_blank=True)
    warehouse_code = serializers.CharField(max_length=20, required=False, allow_blank=True)
    planned_qty = serializers.DecimalField(max_digits=12, decimal_places=3, min_value=0.001, required=False)
    target_start_date = serializers.DateField(required=False)
    due_date = serializers.DateField(required=False)
    branch_id = serializers.IntegerField(required=False, allow_null=True)
    remarks = serializers.CharField(required=False, allow_blank=True)


# ---------------------------------------------------------------------------
# Plan list / detail
# ---------------------------------------------------------------------------

class ProductionPlanListSerializer(serializers.ModelSerializer):
    progress_percent = serializers.ReadOnlyField()

    class Meta:
        model = ProductionPlan
        fields = [
            'id', 'item_code', 'item_name', 'uom', 'warehouse_code',
            'planned_qty', 'completed_qty', 'progress_percent',
            'target_start_date', 'due_date', 'status',
            'sap_posting_status', 'sap_doc_num', 'sap_error_message',
            'created_by', 'created_at',
        ]


class ProductionPlanDetailSerializer(serializers.ModelSerializer):
    progress_percent = serializers.ReadOnlyField()
    weekly_plans = serializers.SerializerMethodField()
    materials = PlanMaterialSerializer(many=True, read_only=True)

    class Meta:
        model = ProductionPlan
        fields = [
            'id', 'item_code', 'item_name', 'uom', 'warehouse_code',
            'planned_qty', 'completed_qty', 'progress_percent',
            'target_start_date', 'due_date', 'status',
            'sap_posting_status', 'sap_doc_entry', 'sap_doc_num',
            'sap_status', 'sap_error_message',
            'branch_id', 'remarks',
            'created_by', 'created_at', 'closed_by', 'closed_at',
            'materials', 'weekly_plans',
        ]

    def get_weekly_plans(self, obj):
        return WeeklyPlanSerializer(
            obj.weekly_plans.all(), many=True
        ).data


class PostToSAPResponseSerializer(serializers.Serializer):
    success = serializers.BooleanField()
    plan_id = serializers.IntegerField()
    sap_doc_entry = serializers.IntegerField(allow_null=True)
    sap_doc_num = serializers.IntegerField(allow_null=True)
    sap_status = serializers.CharField(allow_null=True)
    message = serializers.CharField()


class PlanCloseResponseSerializer(serializers.Serializer):
    success = serializers.BooleanField()
    plan_id = serializers.IntegerField()
    status = serializers.CharField()
    total_produced = serializers.DecimalField(max_digits=12, decimal_places=3)
    planned_qty = serializers.DecimalField(max_digits=12, decimal_places=3)
    message = serializers.CharField()


# ---------------------------------------------------------------------------
# Weekly plan
# ---------------------------------------------------------------------------

class WeeklyPlanSerializer(serializers.ModelSerializer):
    progress_percent = serializers.ReadOnlyField()

    class Meta:
        model = WeeklyPlan
        fields = [
            'id', 'week_number', 'week_label', 'start_date', 'end_date',
            'target_qty', 'produced_qty', 'progress_percent', 'status',
            'created_by', 'created_at',
        ]
        read_only_fields = ['produced_qty', 'status', 'created_by', 'created_at']


class WeeklyPlanCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = WeeklyPlan
        fields = ['week_number', 'week_label', 'start_date', 'end_date', 'target_qty']

    def validate(self, attrs):
        plan = self.context['production_plan']

        if attrs['start_date'] > attrs['end_date']:
            raise serializers.ValidationError("end_date must be >= start_date.")

        if attrs['start_date'] < plan.target_start_date or attrs['end_date'] > plan.due_date:
            raise serializers.ValidationError(
                f"Week dates must be within plan range: "
                f"{plan.target_start_date} to {plan.due_date}."
            )

        existing_total = sum(wp.target_qty for wp in plan.weekly_plans.all())
        if existing_total + attrs['target_qty'] > plan.planned_qty:
            available = plan.planned_qty - existing_total
            raise serializers.ValidationError(
                f"Total weekly target would exceed plan quantity. Available: {available}."
            )

        if plan.weekly_plans.filter(week_number=attrs['week_number']).exists():
            raise serializers.ValidationError(
                f"Week {attrs['week_number']} already exists for this plan."
            )

        return attrs


class WeeklyPlanUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        model = WeeklyPlan
        fields = ['week_label', 'target_qty']

    def validate_target_qty(self, value):
        plan = self.context['production_plan']
        instance = self.instance
        existing_total = sum(
            wp.target_qty for wp in plan.weekly_plans.exclude(pk=instance.pk)
        )
        if existing_total + value > plan.planned_qty:
            available = plan.planned_qty - existing_total
            raise serializers.ValidationError(
                f"Weekly target exceeds plan quantity. Available: {available}."
            )
        return value


# ---------------------------------------------------------------------------
# Daily entry
# ---------------------------------------------------------------------------

class DailyProductionEntrySerializer(serializers.ModelSerializer):
    class Meta:
        model = DailyProductionEntry
        fields = [
            'id', 'production_date', 'produced_qty', 'shift',
            'remarks', 'recorded_by', 'created_at',
        ]
        read_only_fields = ['recorded_by', 'created_at']


class DailyProductionEntryCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = DailyProductionEntry
        fields = ['production_date', 'produced_qty', 'shift', 'remarks']

    def validate(self, attrs):
        from django.utils import timezone

        weekly_plan = self.context['weekly_plan']

        if not (weekly_plan.start_date <= attrs['production_date'] <= weekly_plan.end_date):
            raise serializers.ValidationError(
                f"production_date must be within week range: "
                f"{weekly_plan.start_date} to {weekly_plan.end_date}."
            )

        if attrs['production_date'] > timezone.localdate():
            raise serializers.ValidationError("production_date cannot be in the future.")

        plan = weekly_plan.production_plan
        if plan.status in ('CLOSED', 'CANCELLED'):
            raise serializers.ValidationError(
                f"Cannot add entries to a plan with status '{plan.status}'."
            )

        qs = DailyProductionEntry.objects.filter(
            weekly_plan=weekly_plan,
            production_date=attrs['production_date'],
            shift=attrs.get('shift')
        )
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            shift_info = f" ({attrs['shift']} shift)" if attrs.get('shift') else ''
            raise serializers.ValidationError(
                f"Entry already exists for {attrs['production_date']}{shift_info}."
            )

        return attrs


class DailyProductionEntryResponseSerializer(serializers.ModelSerializer):
    weekly_plan_progress = serializers.SerializerMethodField()
    plan_progress = serializers.SerializerMethodField()

    class Meta:
        model = DailyProductionEntry
        fields = [
            'id', 'production_date', 'produced_qty', 'shift',
            'remarks', 'recorded_by', 'created_at',
            'weekly_plan_progress', 'plan_progress',
        ]
        read_only_fields = ['recorded_by', 'created_at']

    def get_weekly_plan_progress(self, obj):
        wp = obj.weekly_plan
        wp.refresh_from_db()
        return {
            'week_target': str(wp.target_qty),
            'produced_so_far': str(wp.produced_qty),
            'remaining': str(wp.target_qty - wp.produced_qty),
            'progress_percent': wp.progress_percent,
        }

    def get_plan_progress(self, obj):
        plan = obj.weekly_plan.production_plan
        plan.refresh_from_db()
        return {
            'plan_target': str(plan.planned_qty),
            'produced_so_far': str(plan.completed_qty),
            'progress_percent': plan.progress_percent,
        }
