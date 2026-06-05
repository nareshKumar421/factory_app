from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db.models import Sum
from rest_framework import serializers

from company.models import UserCompany
from production_execution.models import Machine

from .constants import (
    AssetDocumentType,
    AssetHierarchyLevel,
    AssetStatus,
    ChecklistInputType,
    GateQCStatus,
    GateReceiptStatus,
    MaintenancePriority,
    PMExecutionStatus,
    PMFrequency,
    SpareMovementType,
    SpareRequestStatus,
    VendorVisitStatus,
    WorkImpact,
    WorkOrderPhotoType,
    WorkOrderStatus,
    WorkType,
    choices_payload,
)
from .models import (
    Asset,
    AssetCategory,
    AssetDepartment,
    AssetDocument,
    AssetLocation,
    AssetPhoto,
    MaintenanceChecklistResult,
    MaintenanceChecklistTemplateItem,
    MaintenanceSpare,
    MaintenanceGateLink,
    MaintenanceSpareReceipt,
    MaintenanceVendorVisit,
    MaintenanceWorkOrder,
    MaintenanceWorkOrderPhoto,
    PreventiveMaintenanceExecution,
    PreventiveMaintenancePlan,
    SpareCategory,
    SpareMovement,
    SpareRequest,
)

User = get_user_model()


class CompanyScopedModelSerializer(serializers.ModelSerializer):
    company = serializers.IntegerField(source="company_id", read_only=True)
    created_by_name = serializers.CharField(source="created_by.full_name", read_only=True, default="")
    updated_by_name = serializers.CharField(source="updated_by.full_name", read_only=True, default="")

    def _company(self):
        request = self.context.get("request")
        return request.company.company if request and hasattr(request, "company") else None


class MaintenanceUserOptionSerializer(serializers.ModelSerializer):
    label = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ["id", "email", "full_name", "employee_code", "label"]

    def get_label(self, obj):
        if obj.employee_code:
            return f"{obj.full_name} ({obj.employee_code})"
        return obj.full_name or obj.email


class ProductionMachineOptionSerializer(serializers.ModelSerializer):
    line_name = serializers.CharField(source="line.name", read_only=True)

    class Meta:
        model = Machine
        fields = ["id", "name", "machine_type", "line", "line_name"]


class CompanyUserPrimaryKeyRelatedField(serializers.PrimaryKeyRelatedField):
    def get_queryset(self):
        request = self.context.get("request")
        if not request or not hasattr(request, "company"):
            return User.objects.none()
        user_ids = UserCompany.objects.filter(
            company=request.company.company,
            is_active=True,
            user__is_active=True,
        ).values("user_id")
        return User.objects.filter(id__in=user_ids)


class AssetCategorySerializer(CompanyScopedModelSerializer):
    assets_count = serializers.IntegerField(read_only=True, default=0)

    class Meta:
        model = AssetCategory
        fields = [
            "id",
            "company",
            "name",
            "description",
            "assets_count",
            "is_active",
            "created_by",
            "created_by_name",
            "updated_by",
            "updated_by_name",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["created_by", "updated_by", "created_at", "updated_at"]

    def validate_name(self, value):
        company = self._company()
        qs = AssetCategory.objects.filter(company=company, name__iexact=value.strip())
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if company and qs.exists():
            raise serializers.ValidationError("Asset category already exists for this company.")
        return value.strip()


class AssetLocationSerializer(CompanyScopedModelSerializer):
    assets_count = serializers.IntegerField(read_only=True, default=0)

    class Meta:
        model = AssetLocation
        fields = [
            "id",
            "company",
            "name",
            "area",
            "line",
            "description",
            "assets_count",
            "is_active",
            "created_by",
            "created_by_name",
            "updated_by",
            "updated_by_name",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["created_by", "updated_by", "created_at", "updated_at"]


class AssetDepartmentSerializer(CompanyScopedModelSerializer):
    assets_count = serializers.IntegerField(read_only=True, default=0)

    class Meta:
        model = AssetDepartment
        fields = [
            "id",
            "company",
            "name",
            "department_code",
            "description",
            "assets_count",
            "is_active",
            "created_by",
            "created_by_name",
            "updated_by",
            "updated_by_name",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["created_by", "updated_by", "created_at", "updated_at"]


class SpareCategorySerializer(CompanyScopedModelSerializer):
    spares_count = serializers.IntegerField(read_only=True, default=0)

    class Meta:
        model = SpareCategory
        fields = [
            "id",
            "company",
            "name",
            "description",
            "spares_count",
            "is_active",
            "created_by",
            "created_by_name",
            "updated_by",
            "updated_by_name",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["created_by", "updated_by", "created_at", "updated_at"]

    def validate_name(self, value):
        company = self._company()
        qs = SpareCategory.objects.filter(company=company, name__iexact=value.strip())
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if company and qs.exists():
            raise serializers.ValidationError("Spare category already exists for this company.")
        return value.strip()


class AssetSerializer(CompanyScopedModelSerializer):
    category_name = serializers.CharField(source="category.name", read_only=True)
    location_name = serializers.CharField(source="location.name", read_only=True)
    department_name = serializers.CharField(source="department.name", read_only=True)
    parent_asset_code = serializers.CharField(source="parent_asset.asset_code", read_only=True, default="")
    parent_asset_name = serializers.CharField(source="parent_asset.name", read_only=True, default="")
    production_machine_name = serializers.CharField(
        source="production_machine.name",
        read_only=True,
        default="",
    )
    production_machine_type = serializers.CharField(
        source="production_machine.machine_type",
        read_only=True,
        default="",
    )
    production_line_name = serializers.CharField(
        source="production_machine.line.name",
        read_only=True,
        default="",
    )
    responsible_person_name = serializers.CharField(
        source="responsible_person.full_name",
        read_only=True,
        default="",
    )
    photos_count = serializers.IntegerField(read_only=True, default=0)
    documents_count = serializers.IntegerField(read_only=True, default=0)

    class Meta:
        model = Asset
        fields = [
            "id",
            "company",
            "asset_code",
            "name",
            "category",
            "category_name",
            "location",
            "location_name",
            "department",
            "department_name",
            "parent_asset",
            "parent_asset_code",
            "parent_asset_name",
            "production_machine",
            "production_machine_name",
            "production_machine_type",
            "production_line_name",
            "hierarchy_level",
            "area",
            "line",
            "status",
            "make",
            "model",
            "serial_number",
            "purchase_date",
            "warranty_start_date",
            "warranty_end_date",
            "amc_vendor",
            "amc_start_date",
            "amc_end_date",
            "responsible_person",
            "responsible_person_name",
            "qr_code",
            "description",
            "photos_count",
            "documents_count",
            "is_active",
            "deactivated_at",
            "created_by",
            "created_by_name",
            "updated_by",
            "updated_by_name",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "created_by",
            "updated_by",
            "deactivated_at",
            "created_at",
            "updated_at",
        ]

    def validate(self, attrs):
        company = self._company()
        asset_code = attrs.get("asset_code", getattr(self.instance, "asset_code", "")).strip()
        if asset_code:
            qs = Asset.objects.filter(company=company, asset_code__iexact=asset_code)
            if self.instance:
                qs = qs.exclude(pk=self.instance.pk)
            if company and qs.exists():
                raise serializers.ValidationError({"asset_code": "Asset code must be unique."})
            attrs["asset_code"] = asset_code.upper()

        for field in ("category", "location", "department", "parent_asset"):
            value = attrs.get(field)
            if value and company and value.company_id != company.id:
                raise serializers.ValidationError({field: "Selection must belong to current company."})

        production_machine = attrs.get("production_machine")
        if production_machine and company and production_machine.company_id != company.id:
            raise serializers.ValidationError(
                {"production_machine": "Production machine must belong to current company."}
            )

        parent_asset = attrs.get("parent_asset")
        if self.instance and parent_asset and parent_asset.pk == self.instance.pk:
            raise serializers.ValidationError({"parent_asset": "Asset cannot be its own parent."})

        warranty_start = attrs.get("warranty_start_date", getattr(self.instance, "warranty_start_date", None))
        warranty_end = attrs.get("warranty_end_date", getattr(self.instance, "warranty_end_date", None))
        if warranty_start and warranty_end and warranty_start > warranty_end:
            raise serializers.ValidationError({"warranty_end_date": "Warranty end date must be after start date."})

        amc_start = attrs.get("amc_start_date", getattr(self.instance, "amc_start_date", None))
        amc_end = attrs.get("amc_end_date", getattr(self.instance, "amc_end_date", None))
        if amc_start and amc_end and amc_start > amc_end:
            raise serializers.ValidationError({"amc_end_date": "AMC end date must be after start date."})

        return attrs


class MaintenanceSpareSerializer(CompanyScopedModelSerializer):
    category_name = serializers.CharField(source="category.name", read_only=True)
    compatible_asset_codes = serializers.SerializerMethodField()
    compatible_asset_names = serializers.SerializerMethodField()
    is_low_stock = serializers.BooleanField(read_only=True)
    is_below_minimum = serializers.BooleanField(read_only=True)
    reorder_shortage_qty = serializers.DecimalField(max_digits=14, decimal_places=3, read_only=True)

    class Meta:
        model = MaintenanceSpare
        fields = [
            "id",
            "company",
            "category",
            "category_name",
            "name",
            "part_number",
            "sap_item_code",
            "uom",
            "compatible_assets",
            "compatible_asset_codes",
            "compatible_asset_names",
            "is_critical",
            "minimum_stock",
            "reorder_level",
            "current_stock",
            "unit_cost",
            "storage_location",
            "description",
            "is_low_stock",
            "is_below_minimum",
            "reorder_shortage_qty",
            "is_active",
            "created_by",
            "created_by_name",
            "updated_by",
            "updated_by_name",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["created_by", "updated_by", "created_at", "updated_at"]

    def get_compatible_asset_codes(self, obj):
        return [asset.asset_code for asset in obj.compatible_assets.all()]

    def get_compatible_asset_names(self, obj):
        return [asset.name for asset in obj.compatible_assets.all()]

    def validate(self, attrs):
        company = self._company()
        category = attrs.get("category", getattr(self.instance, "category", None))
        if category and company and category.company_id != company.id:
            raise serializers.ValidationError({"category": "Category must belong to current company."})

        part_number = attrs.get("part_number", getattr(self.instance, "part_number", "")).strip()
        if part_number:
            qs = MaintenanceSpare.objects.filter(company=company, part_number__iexact=part_number)
            if self.instance:
                qs = qs.exclude(pk=self.instance.pk)
            if company and qs.exists():
                raise serializers.ValidationError({"part_number": "Part number must be unique."})
            attrs["part_number"] = part_number.upper()

        compatible_assets = attrs.get("compatible_assets")
        if compatible_assets is not None and company:
            invalid = [asset.id for asset in compatible_assets if asset.company_id != company.id]
            if invalid:
                raise serializers.ValidationError(
                    {"compatible_assets": "All compatible assets must belong to current company."}
                )

        minimum_stock = attrs.get("minimum_stock", getattr(self.instance, "minimum_stock", 0))
        reorder_level = attrs.get("reorder_level", getattr(self.instance, "reorder_level", 0))
        if reorder_level < minimum_stock:
            raise serializers.ValidationError(
                {"reorder_level": "Reorder level must be greater than or equal to minimum stock."}
            )
        return attrs


class AssetPhotoSerializer(serializers.ModelSerializer):
    asset_code = serializers.CharField(source="asset.asset_code", read_only=True)

    class Meta:
        model = AssetPhoto
        fields = [
            "id",
            "asset",
            "asset_code",
            "photo",
            "caption",
            "taken_on",
            "is_monthly_photo",
            "is_active",
            "created_by",
            "updated_by",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["created_by", "updated_by", "created_at", "updated_at"]

    def validate_asset(self, value):
        request = self.context.get("request")
        company = request.company.company if request and hasattr(request, "company") else None
        if company and value.company_id != company.id:
            raise serializers.ValidationError("Asset must belong to current company.")
        return value


class AssetDocumentSerializer(serializers.ModelSerializer):
    asset_code = serializers.CharField(source="asset.asset_code", read_only=True)

    class Meta:
        model = AssetDocument
        fields = [
            "id",
            "asset",
            "asset_code",
            "document_type",
            "title",
            "document",
            "document_date",
            "notes",
            "is_active",
            "created_by",
            "updated_by",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["created_by", "updated_by", "created_at", "updated_at"]

    def validate_asset(self, value):
        request = self.context.get("request")
        company = request.company.company if request and hasattr(request, "company") else None
        if company and value.company_id != company.id:
            raise serializers.ValidationError("Asset must belong to current company.")
        return value


class MaintenanceWorkOrderSerializer(CompanyScopedModelSerializer):
    asset_code = serializers.CharField(source="asset.asset_code", read_only=True)
    asset_name = serializers.CharField(source="asset.name", read_only=True)
    department_name = serializers.CharField(source="department.name", read_only=True)
    reported_by_name = serializers.CharField(source="reported_by.full_name", read_only=True, default="")
    assigned_to_name = serializers.CharField(source="assigned_to.full_name", read_only=True, default="")
    approved_by_name = serializers.CharField(source="approved_by.full_name", read_only=True, default="")
    closed_by_name = serializers.CharField(source="closed_by.full_name", read_only=True, default="")
    production_run_number = serializers.IntegerField(
        source="production_run.run_number",
        read_only=True,
        default=None,
    )
    production_run_date = serializers.DateField(
        source="production_run.date",
        read_only=True,
        default=None,
    )
    production_line_name = serializers.CharField(
        source="production_run.line.name",
        read_only=True,
        default="",
    )
    production_product = serializers.CharField(
        source="production_run.product",
        read_only=True,
        default="",
    )
    production_breakdown_reason = serializers.CharField(
        source="production_breakdown.reason",
        read_only=True,
        default="",
    )
    photos_count = serializers.IntegerField(read_only=True, default=0)
    spare_requests_count = serializers.IntegerField(read_only=True, default=0)
    spare_consumed_qty = serializers.SerializerMethodField()
    spare_consumed_cost = serializers.SerializerMethodField()
    response_time_minutes = serializers.IntegerField(read_only=True)
    repair_time_minutes = serializers.IntegerField(read_only=True)
    downtime_minutes = serializers.IntegerField(read_only=True)

    class Meta:
        model = MaintenanceWorkOrder
        fields = [
            "id",
            "company",
            "work_order_no",
            "work_type",
            "status",
            "priority",
            "asset",
            "asset_code",
            "asset_name",
            "department",
            "department_name",
            "area",
            "line",
            "title",
            "problem_statement",
            "impact",
            "impact_notes",
            "downtime_reason",
            "production_run",
            "production_run_number",
            "production_run_date",
            "production_line_name",
            "production_product",
            "production_breakdown",
            "production_breakdown_reason",
            "reported_by",
            "reported_by_name",
            "assigned_to",
            "assigned_to_name",
            "target_date",
            "start_time",
            "end_time",
            "technician_remarks",
            "completion_remarks",
            "root_cause",
            "corrective_action",
            "preventive_action",
            "closure_remarks",
            "completed_at",
            "approved_at",
            "closed_at",
            "approved_by",
            "approved_by_name",
            "closed_by",
            "closed_by_name",
            "photos_count",
            "spare_requests_count",
            "spare_consumed_qty",
            "spare_consumed_cost",
            "response_time_minutes",
            "repair_time_minutes",
            "downtime_minutes",
            "is_active",
            "created_by",
            "created_by_name",
            "updated_by",
            "updated_by_name",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "work_order_no",
            "status",
            "reported_by",
            "start_time",
            "end_time",
            "completed_at",
            "approved_at",
            "closed_at",
            "approved_by",
            "closed_by",
            "created_by",
            "updated_by",
            "created_at",
            "updated_at",
        ]

    def validate(self, attrs):
        company = self._company()
        asset = attrs.get("asset", getattr(self.instance, "asset", None))
        department = attrs.get("department", getattr(self.instance, "department", None))
        assigned_to = attrs.get("assigned_to", getattr(self.instance, "assigned_to", None))
        production_run = attrs.get("production_run", getattr(self.instance, "production_run", None))
        production_breakdown = attrs.get(
            "production_breakdown",
            getattr(self.instance, "production_breakdown", None),
        )

        if asset and company and asset.company_id != company.id:
            raise serializers.ValidationError({"asset": "Asset must belong to current company."})
        if department and company and department.company_id != company.id:
            raise serializers.ValidationError(
                {"department": "Department must belong to current company."}
            )
        if assigned_to and company:
            exists = UserCompany.objects.filter(
                user=assigned_to,
                company=company,
                is_active=True,
                user__is_active=True,
            ).exists()
            if not exists:
                raise serializers.ValidationError(
                    {"assigned_to": "Assignee must belong to current company."}
                )
        if production_run and company and production_run.company_id != company.id:
            raise serializers.ValidationError(
                {"production_run": "Production run must belong to current company."}
            )
        if production_breakdown and company and production_breakdown.production_run.company_id != company.id:
            raise serializers.ValidationError(
                {"production_breakdown": "Production breakdown must belong to current company."}
            )
        if production_breakdown and production_run and production_breakdown.production_run_id != production_run.id:
            raise serializers.ValidationError(
                {"production_breakdown": "Production breakdown must belong to selected production run."}
            )
        if asset and department and asset.company_id != department.company_id:
            raise serializers.ValidationError(
                {"department": "Department must belong to the same company as asset."}
            )

        title = attrs.get("title")
        if title is not None:
            attrs["title"] = title.strip()

        return attrs

    def get_spare_consumed_qty(self, obj):
        return obj.spare_requests.aggregate(total=Sum("consumed_qty"))["total"] or 0

    def get_spare_consumed_cost(self, obj):
        total = 0
        for movement in obj.spare_movements.filter(movement_type=SpareMovementType.CONSUME):
            total += movement.line_total
        return total

    def create(self, validated_data):
        asset = validated_data.get("asset")
        if asset:
            validated_data.setdefault("department", asset.department)
            validated_data.setdefault("area", asset.area)
            validated_data.setdefault("line", asset.line)
        return super().create(validated_data)


class PreventiveMaintenancePlanSerializer(CompanyScopedModelSerializer):
    asset_code = serializers.CharField(source="asset.asset_code", read_only=True)
    asset_name = serializers.CharField(source="asset.name", read_only=True)
    department = serializers.IntegerField(source="asset.department_id", read_only=True)
    department_name = serializers.CharField(source="asset.department.name", read_only=True)
    line = serializers.CharField(source="asset.line", read_only=True)
    assigned_to_name = serializers.CharField(source="assigned_to.full_name", read_only=True, default="")
    checklist_count = serializers.IntegerField(read_only=True, default=0)
    execution_count = serializers.IntegerField(read_only=True, default=0)
    open_execution_count = serializers.IntegerField(read_only=True, default=0)
    is_due = serializers.BooleanField(read_only=True)

    class Meta:
        model = PreventiveMaintenancePlan
        fields = [
            "id",
            "company",
            "plan_code",
            "title",
            "asset",
            "asset_code",
            "asset_name",
            "department",
            "department_name",
            "line",
            "frequency",
            "work_type",
            "priority",
            "assigned_to",
            "assigned_to_name",
            "start_date",
            "next_due_date",
            "last_generated_date",
            "advance_days",
            "auto_create_work_order",
            "checklist_required",
            "description",
            "checklist_count",
            "execution_count",
            "open_execution_count",
            "is_due",
            "is_active",
            "created_by",
            "created_by_name",
            "updated_by",
            "updated_by_name",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "plan_code",
            "last_generated_date",
            "created_by",
            "updated_by",
            "created_at",
            "updated_at",
        ]

    def validate(self, attrs):
        company = self._company()
        asset = attrs.get("asset", getattr(self.instance, "asset", None))
        assigned_to = attrs.get("assigned_to", getattr(self.instance, "assigned_to", None))
        if asset and company and asset.company_id != company.id:
            raise serializers.ValidationError({"asset": "Asset must belong to current company."})
        if assigned_to and company:
            allowed = UserCompany.objects.filter(
                company=company,
                user=assigned_to,
                is_active=True,
                user__is_active=True,
            ).exists()
            if not allowed:
                raise serializers.ValidationError(
                    {"assigned_to": "Assigned user must belong to current company."}
                )
        work_type = attrs.get("work_type", getattr(self.instance, "work_type", WorkType.PREVENTIVE))
        if work_type not in [WorkType.PREVENTIVE, WorkType.INSPECTION, WorkType.CALIBRATION]:
            raise serializers.ValidationError(
                {"work_type": "PM plans can only create preventive, inspection, or calibration work."}
            )
        start_date = attrs.get("start_date", getattr(self.instance, "start_date", None))
        next_due_date = attrs.get("next_due_date", getattr(self.instance, "next_due_date", None))
        if start_date and next_due_date and next_due_date < start_date:
            raise serializers.ValidationError({"next_due_date": "Next due date cannot be before start date."})
        title = attrs.get("title")
        if title is not None:
            attrs["title"] = title.strip()
        return attrs


class MaintenanceChecklistTemplateItemSerializer(CompanyScopedModelSerializer):
    pm_plan_code = serializers.CharField(source="pm_plan.plan_code", read_only=True)
    pm_plan_title = serializers.CharField(source="pm_plan.title", read_only=True)

    class Meta:
        model = MaintenanceChecklistTemplateItem
        fields = [
            "id",
            "company",
            "pm_plan",
            "pm_plan_code",
            "pm_plan_title",
            "task",
            "input_type",
            "is_required",
            "expected_text",
            "min_value",
            "max_value",
            "uom",
            "safety_critical",
            "sort_order",
            "is_active",
            "created_by",
            "created_by_name",
            "updated_by",
            "updated_by_name",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["created_by", "updated_by", "created_at", "updated_at"]

    def validate(self, attrs):
        company = self._company()
        plan = attrs.get("pm_plan", getattr(self.instance, "pm_plan", None))
        if plan and company and plan.company_id != company.id:
            raise serializers.ValidationError({"pm_plan": "PM plan must belong to current company."})
        min_value = attrs.get("min_value", getattr(self.instance, "min_value", None))
        max_value = attrs.get("max_value", getattr(self.instance, "max_value", None))
        if min_value is not None and max_value is not None and min_value > max_value:
            raise serializers.ValidationError({"max_value": "Maximum value must be greater than minimum value."})
        task = attrs.get("task")
        if task is not None:
            attrs["task"] = task.strip()
        return attrs


class MaintenanceChecklistResultSerializer(CompanyScopedModelSerializer):
    template_task = serializers.CharField(source="template_item.task", read_only=True)
    sort_order = serializers.IntegerField(source="template_item.sort_order", read_only=True)
    uom = serializers.CharField(source="template_item.uom", read_only=True)
    safety_critical = serializers.BooleanField(source="template_item.safety_critical", read_only=True)

    class Meta:
        model = MaintenanceChecklistResult
        fields = [
            "id",
            "company",
            "execution",
            "template_item",
            "template_task",
            "sort_order",
            "task_snapshot",
            "input_type",
            "value_text",
            "value_number",
            "is_ok",
            "remarks",
            "uom",
            "safety_critical",
            "is_active",
            "created_by",
            "created_by_name",
            "updated_by",
            "updated_by_name",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "company",
            "execution",
            "template_item",
            "task_snapshot",
            "input_type",
            "created_by",
            "updated_by",
            "created_at",
            "updated_at",
        ]


class PreventiveMaintenanceExecutionSerializer(CompanyScopedModelSerializer):
    pm_plan_code = serializers.CharField(source="pm_plan.plan_code", read_only=True)
    pm_plan_title = serializers.CharField(source="pm_plan.title", read_only=True)
    frequency = serializers.CharField(source="pm_plan.frequency", read_only=True)
    asset_code = serializers.CharField(source="asset.asset_code", read_only=True)
    asset_name = serializers.CharField(source="asset.name", read_only=True)
    department_name = serializers.CharField(source="asset.department.name", read_only=True)
    line = serializers.CharField(source="asset.line", read_only=True)
    work_order_no = serializers.CharField(source="work_order.work_order_no", read_only=True, default="")
    work_order_status = serializers.CharField(source="work_order.status", read_only=True, default="")
    completed_by_name = serializers.CharField(source="completed_by.full_name", read_only=True, default="")
    is_overdue = serializers.BooleanField(read_only=True)
    effective_status = serializers.CharField(read_only=True)
    results = MaintenanceChecklistResultSerializer(many=True, read_only=True)

    class Meta:
        model = PreventiveMaintenanceExecution
        fields = [
            "id",
            "company",
            "pm_plan",
            "pm_plan_code",
            "pm_plan_title",
            "frequency",
            "work_order",
            "work_order_no",
            "work_order_status",
            "asset",
            "asset_code",
            "asset_name",
            "department_name",
            "line",
            "due_date",
            "status",
            "effective_status",
            "is_overdue",
            "started_at",
            "completed_at",
            "skipped_at",
            "skip_reason",
            "completed_by",
            "completed_by_name",
            "remarks",
            "results",
            "is_active",
            "created_by",
            "created_by_name",
            "updated_by",
            "updated_by_name",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "work_order",
            "asset",
            "status",
            "started_at",
            "completed_at",
            "skipped_at",
            "completed_by",
            "created_by",
            "updated_by",
            "created_at",
            "updated_at",
        ]


class PMGenerateDueSerializer(serializers.Serializer):
    due_until = serializers.DateField(required=False)
    plan_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        required=False,
        allow_empty=True,
    )


class MaintenanceChecklistResultInputSerializer(serializers.Serializer):
    template_item = serializers.PrimaryKeyRelatedField(queryset=MaintenanceChecklistTemplateItem.objects.none())
    value_text = serializers.CharField(required=False, allow_blank=True)
    value_number = serializers.DecimalField(
        max_digits=14,
        decimal_places=3,
        required=False,
        allow_null=True,
    )
    is_ok = serializers.BooleanField(required=False, default=True)
    remarks = serializers.CharField(required=False, allow_blank=True)

    def __init__(self, *args, **kwargs):
        execution = kwargs.pop("execution", None)
        super().__init__(*args, **kwargs)
        if execution:
            self.fields["template_item"].queryset = execution.pm_plan.checklist_items.filter(is_active=True)


class PMExecutionCompleteSerializer(serializers.Serializer):
    checklist_results = serializers.ListField(
        child=serializers.DictField(),
        required=False,
        allow_empty=True,
    )
    technician_remarks = serializers.CharField(required=False, allow_blank=True)
    completion_remarks = serializers.CharField(required=False, allow_blank=True)
    root_cause = serializers.CharField(required=False, allow_blank=True)
    corrective_action = serializers.CharField(required=False, allow_blank=True)
    preventive_action = serializers.CharField(required=False, allow_blank=True)
    remarks = serializers.CharField(required=False, allow_blank=True)


class PMExecutionSkipSerializer(serializers.Serializer):
    skip_reason = serializers.CharField()

    def validate_skip_reason(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("Skip reason is required.")
        return value


class SpareRequestSerializer(CompanyScopedModelSerializer):
    work_order_no = serializers.CharField(source="work_order.work_order_no", read_only=True)
    work_order_title = serializers.CharField(source="work_order.title", read_only=True)
    asset = serializers.IntegerField(source="work_order.asset_id", read_only=True)
    asset_code = serializers.CharField(source="work_order.asset.asset_code", read_only=True)
    asset_name = serializers.CharField(source="work_order.asset.name", read_only=True)
    spare_name = serializers.CharField(source="spare.name", read_only=True)
    spare_part_number = serializers.CharField(source="spare.part_number", read_only=True)
    spare_sap_item_code = serializers.CharField(source="spare.sap_item_code", read_only=True)
    spare_uom = serializers.CharField(source="spare.uom", read_only=True)
    requested_by_name = serializers.CharField(source="requested_by.full_name", read_only=True, default="")
    pending_issue_qty = serializers.DecimalField(max_digits=14, decimal_places=3, read_only=True)
    available_to_consume_qty = serializers.DecimalField(max_digits=14, decimal_places=3, read_only=True)
    total_cost = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)

    class Meta:
        model = SpareRequest
        fields = [
            "id",
            "company",
            "work_order",
            "work_order_no",
            "work_order_title",
            "asset",
            "asset_code",
            "asset_name",
            "spare",
            "spare_name",
            "spare_part_number",
            "spare_sap_item_code",
            "spare_uom",
            "status",
            "requested_qty",
            "issued_qty",
            "consumed_qty",
            "returned_qty",
            "pending_issue_qty",
            "available_to_consume_qty",
            "total_cost",
            "requested_by",
            "requested_by_name",
            "required_by",
            "purpose",
            "store_remarks",
            "is_active",
            "created_by",
            "created_by_name",
            "updated_by",
            "updated_by_name",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "status",
            "issued_qty",
            "consumed_qty",
            "returned_qty",
            "requested_by",
            "created_by",
            "updated_by",
            "created_at",
            "updated_at",
        ]

    def validate(self, attrs):
        company = self._company()
        work_order = attrs.get("work_order", getattr(self.instance, "work_order", None))
        spare = attrs.get("spare", getattr(self.instance, "spare", None))
        if work_order and company and work_order.company_id != company.id:
            raise serializers.ValidationError({"work_order": "Work order must belong to current company."})
        if spare and company and spare.company_id != company.id:
            raise serializers.ValidationError({"spare": "Spare must belong to current company."})
        if work_order and spare and spare.compatible_assets.exists():
            if not spare.compatible_assets.filter(pk=work_order.asset_id).exists():
                raise serializers.ValidationError(
                    {"spare": "Spare is not marked compatible with this work order asset."}
                )
        return attrs


class SpareMovementSerializer(CompanyScopedModelSerializer):
    work_order_no = serializers.SerializerMethodField()
    asset_code = serializers.SerializerMethodField()
    spare_name = serializers.CharField(source="spare.name", read_only=True)
    spare_part_number = serializers.CharField(source="spare.part_number", read_only=True)
    spare_uom = serializers.CharField(source="spare.uom", read_only=True)
    performed_by_name = serializers.CharField(source="performed_by.full_name", read_only=True, default="")
    line_total = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)

    class Meta:
        model = SpareMovement
        fields = [
            "id",
            "company",
            "spare_request",
            "work_order",
            "work_order_no",
            "asset_code",
            "spare",
            "spare_name",
            "spare_part_number",
            "spare_uom",
            "movement_type",
            "quantity",
            "unit_cost",
            "line_total",
            "remarks",
            "performed_by",
            "performed_by_name",
            "is_active",
            "created_by",
            "created_by_name",
            "updated_by",
            "updated_by_name",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "company",
            "spare_request",
            "work_order",
            "spare",
            "movement_type",
            "quantity",
            "unit_cost",
            "performed_by",
            "created_by",
            "updated_by",
            "created_at",
            "updated_at",
        ]

    def get_work_order_no(self, obj):
        return obj.work_order.work_order_no if obj.work_order else ""

    def get_asset_code(self, obj):
        return obj.work_order.asset.asset_code if obj.work_order else ""


class SpareRequestActionSerializer(serializers.Serializer):
    quantity = serializers.DecimalField(max_digits=14, decimal_places=3, min_value=Decimal("0.001"))
    remarks = serializers.CharField(required=False, allow_blank=True)


class SpareIssueSerializer(SpareRequestActionSerializer):
    unit_cost = serializers.DecimalField(
        max_digits=14,
        decimal_places=2,
        min_value=Decimal("0.00"),
        required=False,
        allow_null=True,
    )


class WorkOrderSpareRequestSerializer(serializers.Serializer):
    spare = serializers.PrimaryKeyRelatedField(queryset=MaintenanceSpare.objects.none())
    requested_qty = serializers.DecimalField(max_digits=14, decimal_places=3, min_value=Decimal("0.001"))
    required_by = serializers.DateField(required=False, allow_null=True)
    purpose = serializers.CharField(required=False, allow_blank=True)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        request = self.context.get("request")
        if request and hasattr(request, "company"):
            self.fields["spare"].queryset = MaintenanceSpare.objects.filter(
                company=request.company.company,
                is_active=True,
            )


class MaintenanceWorkOrderPhotoSerializer(serializers.ModelSerializer):
    work_order_no = serializers.CharField(source="work_order.work_order_no", read_only=True)
    asset_code = serializers.CharField(source="work_order.asset.asset_code", read_only=True)

    class Meta:
        model = MaintenanceWorkOrderPhoto
        fields = [
            "id",
            "work_order",
            "work_order_no",
            "asset_code",
            "photo_type",
            "photo",
            "caption",
            "taken_on",
            "is_active",
            "created_by",
            "updated_by",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["created_by", "updated_by", "created_at", "updated_at"]

    def validate_work_order(self, value):
        request = self.context.get("request")
        company = request.company.company if request and hasattr(request, "company") else None
        if company and value.company_id != company.id:
            raise serializers.ValidationError("Work order must belong to current company.")
        return value


class MaintenanceWorkOrderAssignSerializer(serializers.Serializer):
    assigned_to = CompanyUserPrimaryKeyRelatedField()
    target_date = serializers.DateField(required=False, allow_null=True)


class MaintenanceWorkOrderCompleteSerializer(serializers.Serializer):
    technician_remarks = serializers.CharField(required=False, allow_blank=True)
    completion_remarks = serializers.CharField()
    root_cause = serializers.CharField(required=False, allow_blank=True)
    corrective_action = serializers.CharField(required=False, allow_blank=True)
    preventive_action = serializers.CharField(required=False, allow_blank=True)
    downtime_reason = serializers.CharField(required=False, allow_blank=True)
    end_time = serializers.DateTimeField(required=False, allow_null=True)


class MaintenanceWorkOrderApprovalSerializer(serializers.Serializer):
    closure_remarks = serializers.CharField(required=False, allow_blank=True)


class MaintenanceWorkOrderStatusSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=WorkOrderStatus.choices)
    remarks = serializers.CharField(required=False, allow_blank=True)


class MaintenanceQrAssignSerializer(serializers.Serializer):
    qr_code = serializers.CharField(required=False, allow_blank=True, max_length=150)


class MaintenanceScanWorkOrderCreateSerializer(serializers.Serializer):
    code = serializers.CharField(max_length=150)
    title = serializers.CharField(max_length=200)
    problem_statement = serializers.CharField()
    priority = serializers.ChoiceField(
        choices=MaintenancePriority.choices,
        default=MaintenancePriority.HIGH,
    )
    impact = serializers.ChoiceField(
        choices=WorkImpact.choices,
        default=WorkImpact.DEGRADED,
    )
    target_date = serializers.DateField(required=False, allow_null=True)
    assigned_to = CompanyUserPrimaryKeyRelatedField(required=False, allow_null=True)


class MaintenanceGateLinkSerializer(CompanyScopedModelSerializer):
    gate_entry_no = serializers.CharField(source="gate_entry.work_order_number", read_only=True)
    vehicle_entry = serializers.IntegerField(source="gate_entry.vehicle_entry_id", read_only=True)
    vehicle_entry_no = serializers.CharField(source="gate_entry.vehicle_entry.entry_no", read_only=True)
    supplier_name = serializers.CharField(source="gate_entry.supplier_name", read_only=True)
    material_description = serializers.CharField(source="gate_entry.material_description", read_only=True)
    part_number = serializers.CharField(source="gate_entry.part_number", read_only=True)
    requested_quantity = serializers.DecimalField(
        source="gate_entry.quantity",
        max_digits=10,
        decimal_places=2,
        read_only=True,
    )
    asset_code = serializers.CharField(source="asset.asset_code", read_only=True, default="")
    asset_name = serializers.CharField(source="asset.name", read_only=True, default="")
    work_order_no = serializers.CharField(source="work_order.work_order_no", read_only=True, default="")
    work_order_title = serializers.CharField(source="work_order.title", read_only=True, default="")
    spare_part_number = serializers.CharField(source="spare.part_number", read_only=True, default="")
    spare_name = serializers.CharField(source="spare.name", read_only=True, default="")
    spare_uom = serializers.CharField(source="spare.uom", read_only=True, default="")
    spare_is_critical = serializers.BooleanField(source="spare.is_critical", read_only=True, default=False)
    received_by_name = serializers.CharField(source="received_by.full_name", read_only=True, default="")

    class Meta:
        model = MaintenanceGateLink
        fields = [
            "id",
            "company",
            "gate_entry",
            "gate_entry_no",
            "vehicle_entry",
            "vehicle_entry_no",
            "supplier_name",
            "material_description",
            "part_number",
            "requested_quantity",
            "asset",
            "asset_code",
            "asset_name",
            "work_order",
            "work_order_no",
            "work_order_title",
            "spare",
            "spare_part_number",
            "spare_name",
            "spare_uom",
            "spare_is_critical",
            "qc_required",
            "qc_status",
            "grpo_reference",
            "grpo_doc_entry",
            "grpo_doc_num",
            "receipt_status",
            "received_quantity",
            "received_at",
            "received_by",
            "received_by_name",
            "notes",
            "is_active",
            "created_by",
            "created_by_name",
            "updated_by",
            "updated_by_name",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "created_by",
            "updated_by",
            "received_at",
            "received_by",
            "created_at",
            "updated_at",
        ]


class MaintenanceSpareReceiptSerializer(CompanyScopedModelSerializer):
    gate_entry = serializers.IntegerField(source="gate_link.gate_entry_id", read_only=True)
    gate_entry_no = serializers.CharField(source="gate_link.gate_entry.work_order_number", read_only=True)
    vehicle_entry = serializers.IntegerField(source="gate_link.gate_entry.vehicle_entry_id", read_only=True)
    asset_code = serializers.CharField(source="asset.asset_code", read_only=True, default="")
    asset_name = serializers.CharField(source="asset.name", read_only=True, default="")
    work_order_no = serializers.CharField(source="work_order.work_order_no", read_only=True, default="")
    spare_part_number = serializers.CharField(source="spare.part_number", read_only=True)
    spare_name = serializers.CharField(source="spare.name", read_only=True)
    spare_uom = serializers.CharField(source="spare.uom", read_only=True)
    received_by_name = serializers.CharField(source="received_by.full_name", read_only=True, default="")

    class Meta:
        model = MaintenanceSpareReceipt
        fields = [
            "id",
            "company",
            "gate_link",
            "gate_entry",
            "gate_entry_no",
            "vehicle_entry",
            "asset",
            "asset_code",
            "asset_name",
            "work_order",
            "work_order_no",
            "spare",
            "spare_part_number",
            "spare_name",
            "spare_uom",
            "quantity",
            "unit_cost",
            "qc_status",
            "grpo_reference",
            "grpo_doc_entry",
            "grpo_doc_num",
            "invoice_number",
            "received_at",
            "received_by",
            "received_by_name",
            "remarks",
            "is_active",
            "created_by",
            "created_by_name",
            "updated_by",
            "updated_by_name",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "company",
            "gate_link",
            "asset",
            "work_order",
            "spare",
            "received_by",
            "created_by",
            "updated_by",
            "created_at",
            "updated_at",
        ]


class MaintenanceGateReceiptActionSerializer(serializers.Serializer):
    quantity = serializers.DecimalField(
        max_digits=14,
        decimal_places=3,
        min_value=Decimal("0.001"),
        required=False,
    )
    unit_cost = serializers.DecimalField(
        max_digits=14,
        decimal_places=2,
        min_value=Decimal("0.00"),
        required=False,
    )
    qc_status = serializers.ChoiceField(choices=GateQCStatus.choices, required=False)
    grpo_reference = serializers.CharField(required=False, allow_blank=True)
    grpo_doc_entry = serializers.IntegerField(required=False, allow_null=True, min_value=1)
    grpo_doc_num = serializers.CharField(required=False, allow_blank=True)
    remarks = serializers.CharField(required=False, allow_blank=True)


class MaintenanceVendorVisitSerializer(CompanyScopedModelSerializer):
    work_order_no = serializers.CharField(source="work_order.work_order_no", read_only=True)
    work_order_title = serializers.CharField(source="work_order.title", read_only=True)
    asset_code = serializers.CharField(source="asset.asset_code", read_only=True)
    asset_name = serializers.CharField(source="asset.name", read_only=True)
    asset_warranty_end_date = serializers.DateField(source="asset.warranty_end_date", read_only=True)
    asset_amc_vendor = serializers.CharField(source="asset.amc_vendor", read_only=True)
    asset_amc_end_date = serializers.DateField(source="asset.amc_end_date", read_only=True)
    person_gate_entry_name = serializers.CharField(source="person_gate_entry.name_snapshot", read_only=True, default="")
    material_gate_entry_no = serializers.CharField(
        source="material_gate_entry.work_order_number",
        read_only=True,
        default="",
    )

    class Meta:
        model = MaintenanceVendorVisit
        fields = [
            "id",
            "company",
            "work_order",
            "work_order_no",
            "work_order_title",
            "asset",
            "asset_code",
            "asset_name",
            "asset_warranty_end_date",
            "asset_amc_vendor",
            "asset_amc_end_date",
            "vendor_code",
            "vendor_name",
            "contact_person",
            "contact_phone",
            "status",
            "planned_start",
            "planned_end",
            "actual_start",
            "actual_end",
            "person_gate_entry",
            "person_gate_entry_name",
            "material_gate_entry",
            "material_gate_entry_no",
            "service_report_attachment",
            "invoice_number",
            "invoice_attachment",
            "remarks",
            "is_active",
            "created_by",
            "created_by_name",
            "updated_by",
            "updated_by_name",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["created_by", "updated_by", "created_at", "updated_at"]

    def validate(self, attrs):
        company = self._company()
        work_order = attrs.get("work_order", getattr(self.instance, "work_order", None))
        asset = attrs.get("asset", getattr(self.instance, "asset", None))
        material_gate_entry = attrs.get(
            "material_gate_entry",
            getattr(self.instance, "material_gate_entry", None),
        )

        if work_order and company and work_order.company_id != company.id:
            raise serializers.ValidationError({"work_order": "Work order must belong to current company."})
        if not asset and work_order:
            attrs["asset"] = work_order.asset
            asset = work_order.asset
        if asset and company and asset.company_id != company.id:
            raise serializers.ValidationError({"asset": "Asset must belong to current company."})
        if work_order and asset and work_order.asset_id != asset.id:
            raise serializers.ValidationError({"asset": "Asset must match the selected work order."})
        if material_gate_entry and company and material_gate_entry.vehicle_entry.company_id != company.id:
            raise serializers.ValidationError(
                {"material_gate_entry": "Material gate entry must belong to current company."}
            )

        planned_start = attrs.get("planned_start", getattr(self.instance, "planned_start", None))
        planned_end = attrs.get("planned_end", getattr(self.instance, "planned_end", None))
        actual_start = attrs.get("actual_start", getattr(self.instance, "actual_start", None))
        actual_end = attrs.get("actual_end", getattr(self.instance, "actual_end", None))
        if planned_start and planned_end and planned_start > planned_end:
            raise serializers.ValidationError({"planned_end": "Planned end must be after planned start."})
        if actual_start and actual_end and actual_start > actual_end:
            raise serializers.ValidationError({"actual_end": "Actual end must be after actual start."})

        vendor_name = attrs.get("vendor_name")
        if vendor_name is not None:
            attrs["vendor_name"] = vendor_name.strip()

        return attrs


class MaintenanceOptionsSerializer(serializers.Serializer):
    statuses = serializers.SerializerMethodField()
    priorities = serializers.SerializerMethodField()
    pm_frequencies = serializers.SerializerMethodField()
    pm_execution_statuses = serializers.SerializerMethodField()
    checklist_input_types = serializers.SerializerMethodField()
    hierarchy_levels = serializers.SerializerMethodField()
    document_types = serializers.SerializerMethodField()
    work_types = serializers.SerializerMethodField()
    work_statuses = serializers.SerializerMethodField()
    work_impacts = serializers.SerializerMethodField()
    work_photo_types = serializers.SerializerMethodField()
    spare_request_statuses = serializers.SerializerMethodField()
    spare_movement_types = serializers.SerializerMethodField()
    gate_qc_statuses = serializers.SerializerMethodField()
    gate_receipt_statuses = serializers.SerializerMethodField()
    vendor_visit_statuses = serializers.SerializerMethodField()
    categories = AssetCategorySerializer(many=True)
    locations = AssetLocationSerializer(many=True)
    departments = AssetDepartmentSerializer(many=True)
    spare_categories = SpareCategorySerializer(many=True)
    users = MaintenanceUserOptionSerializer(many=True)
    production_machines = ProductionMachineOptionSerializer(many=True)

    def get_statuses(self, _obj):
        return choices_payload(AssetStatus.choices)

    def get_priorities(self, _obj):
        return choices_payload(MaintenancePriority.choices)

    def get_pm_frequencies(self, _obj):
        return choices_payload(PMFrequency.choices)

    def get_pm_execution_statuses(self, _obj):
        return choices_payload(PMExecutionStatus.choices)

    def get_checklist_input_types(self, _obj):
        return choices_payload(ChecklistInputType.choices)

    def get_hierarchy_levels(self, _obj):
        return choices_payload(AssetHierarchyLevel.choices)

    def get_document_types(self, _obj):
        return choices_payload(AssetDocumentType.choices)

    def get_work_types(self, _obj):
        return choices_payload(WorkType.choices)

    def get_work_statuses(self, _obj):
        return choices_payload(WorkOrderStatus.choices)

    def get_work_impacts(self, _obj):
        return choices_payload(WorkImpact.choices)

    def get_work_photo_types(self, _obj):
        return choices_payload(WorkOrderPhotoType.choices)

    def get_spare_request_statuses(self, _obj):
        return choices_payload(SpareRequestStatus.choices)

    def get_spare_movement_types(self, _obj):
        return choices_payload(SpareMovementType.choices)

    def get_gate_qc_statuses(self, _obj):
        return choices_payload(GateQCStatus.choices)

    def get_gate_receipt_statuses(self, _obj):
        return choices_payload(GateReceiptStatus.choices)

    def get_vendor_visit_statuses(self, _obj):
        return choices_payload(VendorVisitStatus.choices)
