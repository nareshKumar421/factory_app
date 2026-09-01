import calendar
import datetime as dt
from decimal import Decimal

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.utils import timezone

from gate_core.models import BaseModel

from .constants import (
    AssetDocumentType,
    AssetHierarchyLevel,
    AssetStatus,
    ChecklistInputType,
    MaterialIndentDocType,
    MaterialIndentPriority,
    MaterialIndentStatus,
    FireEquipmentStatus,
    FireEquipmentType,
    FireIssueStatus,
    FireReportStatus,
    FireReturnCondition,
    FireShiftType,
    GateQCStatus,
    GateReceiptStatus,
    MaintenancePriority,
    PMExecutionStatus,
    PMFrequency,
    SpareMovementType,
    SpareRequestStatus,
    SafetyFineStatus,
    VendorVisitStatus,
    WorkCompletionType,
    WorkImpact,
    WorkOrderLogAction,
    WorkOrderPhotoType,
    WorkOrderStatus,
    WorkPermitApprovalRole,
    WorkPermitStatus,
    WorkPermitType,
    WorkType,
)


def _add_months(value, months):
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def next_pm_due_date(value, frequency):
    if frequency == PMFrequency.DAILY:
        return value + timezone.timedelta(days=1)
    if frequency == PMFrequency.WEEKLY:
        return value + timezone.timedelta(days=7)
    if frequency == PMFrequency.MONTHLY:
        return _add_months(value, 1)
    if frequency == PMFrequency.QUARTERLY:
        return _add_months(value, 3)
    if frequency == PMFrequency.HALF_YEARLY:
        return _add_months(value, 6)
    if frequency == PMFrequency.YEARLY:
        return _add_months(value, 12)
    return value


class MaintenancePermission(models.Model):
    """Sentinel model for module-level permissions that do not need a table."""

    class Meta:
        managed = False
        default_permissions = ()
        permissions = [
            ("can_view_maintenance_module", "Can view Maintenance module"),
            ("can_view_maintenance_dashboard", "Can view Maintenance dashboard"),
            ("can_manage_maintenance_settings", "Can manage Maintenance settings"),
            ("can_view_work_order", "Can view Maintenance work orders"),
            ("can_manage_work_order", "Can manage Maintenance work orders"),
            ("can_create_work_order", "Can create Maintenance work orders"),
            ("can_assign_work_order", "Can assign Maintenance work orders"),
            ("can_start_work_order", "Can start Maintenance work orders"),
            ("can_complete_work_order", "Can complete Maintenance work orders"),
            ("can_approve_work_order", "Can approve Maintenance work order closure"),
            ("can_close_work_order", "Can close Maintenance work orders"),
            ("can_view_pm", "Can view preventive maintenance"),
            ("can_manage_pm", "Can manage preventive maintenance"),
            ("can_view_spare", "Can view Maintenance spares"),
            ("can_manage_spare", "Can manage Maintenance spares"),
            ("can_view_fire", "Can view Maintenance fire store"),
            ("can_manage_fire", "Can manage Maintenance fire store"),
            ("can_view_fire_report", "Can view Fire shift reports"),
            ("can_manage_fire_report", "Can manage Fire shift reports"),
            ("can_review_fire_report", "Can review Fire shift reports"),
            ("can_view_fire_issue", "Can view Fire equipment issue register"),
            ("can_manage_fire_issue", "Can manage Fire equipment issue register"),
            ("can_view_safety_fine", "Can view Safety fines"),
            ("can_manage_safety_fine", "Can manage Safety fines"),
            ("can_view_material_indent", "Can view Material indents"),
            ("can_manage_material_indent", "Can raise Material indents"),
            ("can_review_material_indent", "Can review/issue Material indents (store)"),
            ("can_approve_material_indent", "Can approve Material indent purchases"),
            ("can_purchase_material_indent", "Can purchase Material indents"),
            ("can_gatein_material_indent", "Can gate-in purchased Material indents"),
            ("can_receive_material_indent", "Can receive Material indents into store stock"),
            ("can_view_work_permit", "Can view Work permits"),
            ("can_manage_work_permit", "Can manage Work permits"),
            ("can_issue_work_permit", "Can issue Work permits"),
            ("can_approve_work_permit", "Can approve Work permits"),
            ("can_accept_work_permit", "Can accept Work permits"),
            ("can_close_work_permit", "Can close Work permits"),
            ("can_view_vendor", "Can view Maintenance vendors"),
            ("can_manage_vendor", "Can manage Maintenance vendors"),
            ("can_view_maintenance_reports", "Can view Maintenance reports"),
            ("can_view_daily_electricity", "Can view Daily electricity register"),
            ("can_manage_daily_electricity", "Can manage Daily electricity register"),
            ("can_view_electricity_meter", "Can view Electricity meter master"),
            ("can_manage_electricity_meter", "Can manage Electricity meter master"),
            ("can_add_daily_electricity", "Can record Daily electricity readings"),
            ("can_edit_daily_electricity", "Can correct Daily electricity readings"),
            ("can_delete_daily_electricity", "Can delete Daily electricity readings"),
            ("can_view_daily_wastage", "Can view Daily wastage register"),
            ("can_manage_daily_wastage", "Can manage Daily wastage register"),
        ]


class CompanyMasterModel(BaseModel):
    company = models.ForeignKey(
        "company.Company",
        on_delete=models.PROTECT,
        related_name="%(class)s_records",
    )
    name = models.CharField(max_length=150)
    description = models.TextField(blank=True, default="")

    class Meta:
        abstract = True
        ordering = ["name"]

    def __str__(self):
        return self.name


class AssetCategory(CompanyMasterModel):
    class Meta(CompanyMasterModel.Meta):
        unique_together = ("company", "name")
        verbose_name = "Asset Category"
        verbose_name_plural = "Asset Categories"


class AssetLocation(CompanyMasterModel):
    area = models.CharField(max_length=120, blank=True, default="")
    line = models.CharField(max_length=120, blank=True, default="")

    class Meta(CompanyMasterModel.Meta):
        unique_together = ("company", "name", "area", "line")
        verbose_name = "Asset Location"
        verbose_name_plural = "Asset Locations"


class AssetDepartment(CompanyMasterModel):
    department_code = models.CharField(max_length=50, blank=True, default="")

    class Meta(CompanyMasterModel.Meta):
        unique_together = ("company", "name")
        verbose_name = "Asset Department"
        verbose_name_plural = "Asset Departments"


class SpareCategory(CompanyMasterModel):
    class Meta(CompanyMasterModel.Meta):
        unique_together = ("company", "name")
        verbose_name = "Maintenance Spare Category"
        verbose_name_plural = "Maintenance Spare Categories"


class Asset(BaseModel):
    company = models.ForeignKey(
        "company.Company",
        on_delete=models.PROTECT,
        related_name="maintenance_assets",
    )
    asset_code = models.CharField(max_length=80)
    name = models.CharField(max_length=200)
    category = models.ForeignKey(
        AssetCategory,
        on_delete=models.PROTECT,
        related_name="assets",
    )
    location = models.ForeignKey(
        AssetLocation,
        on_delete=models.PROTECT,
        related_name="assets",
    )
    department = models.ForeignKey(
        "accounts.Department",
        on_delete=models.PROTECT,
        related_name="maintenance_assets",
    )
    parent_asset = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="child_assets",
    )
    production_machine = models.ForeignKey(
        "production_execution.Machine",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="maintenance_assets",
    )
    hierarchy_level = models.CharField(
        max_length=20,
        choices=AssetHierarchyLevel.choices,
        default=AssetHierarchyLevel.MACHINE,
    )
    area = models.CharField(max_length=120, blank=True, default="")
    line = models.CharField(max_length=120, blank=True, default="")
    status = models.CharField(
        max_length=20,
        choices=AssetStatus.choices,
        default=AssetStatus.RUNNING,
    )
    make = models.CharField(max_length=120, blank=True, default="")
    model = models.CharField(max_length=120, blank=True, default="")
    serial_number = models.CharField(max_length=120, blank=True, default="")
    purchase_date = models.DateField(null=True, blank=True)
    warranty_start_date = models.DateField(null=True, blank=True)
    warranty_end_date = models.DateField(null=True, blank=True)
    amc_vendor = models.CharField(max_length=200, blank=True, default="")
    amc_start_date = models.DateField(null=True, blank=True)
    amc_end_date = models.DateField(null=True, blank=True)
    responsible_person = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="maintenance_assets_responsible",
    )
    qr_code = models.CharField(max_length=150, blank=True, default="")
    description = models.TextField(blank=True, default="")
    deactivated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["asset_code"]
        unique_together = ("company", "asset_code")
        permissions = [
            ("can_deactivate_asset", "Can deactivate Maintenance asset"),
        ]
        verbose_name = "Maintenance Asset"
        verbose_name_plural = "Maintenance Assets"

    def __str__(self):
        return f"{self.asset_code} - {self.name}"

    def deactivate(self, user=None):
        self.is_active = False
        self.status = AssetStatus.RETIRED
        self.deactivated_at = timezone.now()
        if user and getattr(user, "is_authenticated", False):
            self.updated_by = user
        self.save(update_fields=["is_active", "status", "deactivated_at", "updated_by", "updated_at"])


class MaintenanceSpare(BaseModel):
    company = models.ForeignKey(
        "company.Company",
        on_delete=models.PROTECT,
        related_name="maintenance_spares",
    )
    category = models.ForeignKey(
        SpareCategory,
        on_delete=models.PROTECT,
        related_name="spares",
    )
    name = models.CharField(max_length=200)
    part_number = models.CharField(max_length=100)
    sap_item_code = models.CharField(max_length=100, blank=True, default="")
    uom = models.CharField(max_length=30, default="NOS")
    compatible_assets = models.ManyToManyField(
        Asset,
        blank=True,
        related_name="compatible_spares",
    )
    is_critical = models.BooleanField(default=False)
    minimum_stock = models.DecimalField(
        max_digits=14,
        decimal_places=3,
        default=Decimal("0.000"),
        validators=[MinValueValidator(Decimal("0.000"))],
    )
    reorder_level = models.DecimalField(
        max_digits=14,
        decimal_places=3,
        default=Decimal("0.000"),
        validators=[MinValueValidator(Decimal("0.000"))],
    )
    current_stock = models.DecimalField(
        max_digits=14,
        decimal_places=3,
        default=Decimal("0.000"),
        validators=[MinValueValidator(Decimal("0.000"))],
    )
    unit_cost = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    storage_location = models.CharField(max_length=120, blank=True, default="")
    description = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["part_number", "name"]
        unique_together = ("company", "part_number")
        verbose_name = "Maintenance Spare"
        verbose_name_plural = "Maintenance Spares"

    def __str__(self):
        return f"{self.part_number} - {self.name}"

    @property
    def is_low_stock(self):
        return self.current_stock <= self.reorder_level

    @property
    def is_below_minimum(self):
        return self.current_stock <= self.minimum_stock

    @property
    def reorder_shortage_qty(self):
        if self.current_stock >= self.reorder_level:
            return Decimal("0.000")
        return self.reorder_level - self.current_stock


class AssetPhoto(BaseModel):
    asset = models.ForeignKey(Asset, on_delete=models.CASCADE, related_name="photos")
    photo = models.FileField(upload_to="maintenance/assets/photos/")
    caption = models.CharField(max_length=200, blank=True, default="")
    taken_on = models.DateField(default=timezone.localdate)
    is_monthly_photo = models.BooleanField(default=True)

    class Meta:
        ordering = ["-taken_on", "-created_at"]
        verbose_name = "Asset Photo"
        verbose_name_plural = "Asset Photos"

    def __str__(self):
        return f"{self.asset.asset_code} photo {self.taken_on}"


class AssetDocument(BaseModel):
    asset = models.ForeignKey(Asset, on_delete=models.CASCADE, related_name="documents")
    document_type = models.CharField(
        max_length=30,
        choices=AssetDocumentType.choices,
        default=AssetDocumentType.OTHER,
    )
    title = models.CharField(max_length=200)
    document = models.FileField(upload_to="maintenance/assets/documents/")
    document_date = models.DateField(null=True, blank=True)
    notes = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-document_date", "-created_at"]
        verbose_name = "Asset Document"
        verbose_name_plural = "Asset Documents"

    def __str__(self):
        return f"{self.asset.asset_code} - {self.title}"


class MaintenanceWorkOrder(BaseModel):
    company = models.ForeignKey(
        "company.Company",
        on_delete=models.PROTECT,
        related_name="maintenance_work_orders",
    )
    work_order_no = models.CharField(max_length=80)
    work_type = models.CharField(
        max_length=30,
        choices=WorkType.choices,
        default=WorkType.COMPLAINT,
    )
    status = models.CharField(
        max_length=30,
        choices=WorkOrderStatus.choices,
        default=WorkOrderStatus.OPEN,
    )
    priority = models.CharField(
        max_length=20,
        choices=MaintenancePriority.choices,
        default=MaintenancePriority.NORMAL,
    )
    asset = models.ForeignKey(
        Asset,
        on_delete=models.PROTECT,
        related_name="work_orders",
        null=True,
        blank=True,
    )
    # Typed-in asset for machines that are not in the asset master yet.
    asset_text = models.CharField(max_length=200, blank=True, default="")
    department = models.ForeignKey(
        "accounts.Department",
        on_delete=models.PROTECT,
        related_name="maintenance_work_orders",
    )
    area = models.CharField(max_length=120, blank=True, default="")
    line = models.CharField(max_length=120, blank=True, default="")
    title = models.CharField(max_length=200)
    problem_statement = models.TextField()
    impact = models.CharField(
        max_length=30,
        choices=WorkImpact.choices,
        default=WorkImpact.NO_IMPACT,
    )
    impact_notes = models.TextField(blank=True, default="")
    downtime_reason = models.TextField(blank=True, default="")
    production_run = models.ForeignKey(
        "production_execution.ProductionRun",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="maintenance_work_orders",
    )
    production_breakdown = models.OneToOneField(
        "production_execution.MachineBreakdown",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="maintenance_work_order",
    )
    reported_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="maintenance_work_orders_reported",
    )
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="maintenance_work_orders_assigned",
    )
    # Whoever the work was handed to, as typed on the assign form. The FK above
    # is filled in as well when the typed name matches a user of the company.
    assigned_to_text = models.CharField(max_length=150, blank=True, default="")
    #: How many times the raiser sent the job back for rework.
    rework_count = models.PositiveIntegerField(default=0)
    target_date = models.DateField(null=True, blank=True)
    start_time = models.DateTimeField(null=True, blank=True)
    end_time = models.DateTimeField(null=True, blank=True)
    technician_remarks = models.TextField(blank=True, default="")
    completion_remarks = models.TextField(blank=True, default="")
    root_cause = models.TextField(blank=True, default="")
    corrective_action = models.TextField(blank=True, default="")
    preventive_action = models.TextField(blank=True, default="")
    closure_remarks = models.TextField(blank=True, default="")
    completed_at = models.DateTimeField(null=True, blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="maintenance_work_orders_approved",
    )
    closed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="maintenance_work_orders_closed",
    )

    class Meta:
        ordering = ["-created_at"]
        unique_together = ("company", "work_order_no")
        verbose_name = "Maintenance Work Order"
        verbose_name_plural = "Maintenance Work Orders"

    def __str__(self):
        return f"{self.work_order_no} - {self.title}"

    @classmethod
    def next_work_order_no(cls, company):
        date_part = timezone.localdate().strftime("%Y%m%d")
        prefix = f"MWO-{date_part}"
        last = (
            cls.objects.filter(company=company, work_order_no__startswith=prefix)
            .order_by("-work_order_no")
            .first()
        )
        next_number = 1
        if last:
            suffix = last.work_order_no.rsplit("-", 1)[-1]
            if suffix.isdigit():
                next_number = int(suffix) + 1
        return f"{prefix}-{next_number:04d}"

    @property
    def assigned_to_display(self):
        if self.assigned_to:
            return self.assigned_to.full_name or self.assigned_to.email
        return self.assigned_to_text

    def is_verifier(self, user):
        """The raiser checks the job before it can be approved or sent back."""
        return bool(user and self.reported_by_id and self.reported_by_id == user.id)

    @property
    def response_time_minutes(self):
        if not self.start_time:
            return None
        return int((self.start_time - self.created_at).total_seconds() // 60)

    @property
    def repair_time_minutes(self):
        if not self.start_time or not self.end_time:
            return None
        return int((self.end_time - self.start_time).total_seconds() // 60)

    @property
    def downtime_minutes(self):
        if not self.end_time:
            return None
        return int((self.end_time - self.created_at).total_seconds() // 60)


class PreventiveMaintenancePlan(BaseModel):
    company = models.ForeignKey(
        "company.Company",
        on_delete=models.PROTECT,
        related_name="maintenance_pm_plans",
    )
    plan_code = models.CharField(max_length=80)
    title = models.CharField(max_length=200)
    asset = models.ForeignKey(Asset, on_delete=models.PROTECT, related_name="pm_plans")
    frequency = models.CharField(max_length=20, choices=PMFrequency.choices)
    work_type = models.CharField(
        max_length=30,
        choices=WorkType.choices,
        default=WorkType.PREVENTIVE,
    )
    priority = models.CharField(
        max_length=20,
        choices=MaintenancePriority.choices,
        default=MaintenancePriority.NORMAL,
    )
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="maintenance_pm_plans_assigned",
    )
    start_date = models.DateField(default=timezone.localdate)
    next_due_date = models.DateField()
    last_generated_date = models.DateField(null=True, blank=True)
    advance_days = models.PositiveSmallIntegerField(default=0)
    auto_create_work_order = models.BooleanField(default=True)
    checklist_required = models.BooleanField(default=True)
    description = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["next_due_date", "plan_code"]
        unique_together = ("company", "plan_code")
        indexes = [
            models.Index(fields=["company", "next_due_date"]),
            models.Index(fields=["asset", "frequency"]),
        ]
        verbose_name = "Preventive Maintenance Plan"
        verbose_name_plural = "Preventive Maintenance Plans"

    def __str__(self):
        return f"{self.plan_code} - {self.title}"

    @classmethod
    def next_plan_code(cls, company):
        date_part = timezone.localdate().strftime("%Y%m%d")
        prefix = f"PM-{date_part}"
        last = (
            cls.objects.filter(company=company, plan_code__startswith=prefix)
            .order_by("-plan_code")
            .first()
        )
        next_number = 1
        if last:
            suffix = last.plan_code.rsplit("-", 1)[-1]
            if suffix.isdigit():
                next_number = int(suffix) + 1
        return f"{prefix}-{next_number:04d}"

    def next_due_after(self, value):
        return next_pm_due_date(value, self.frequency)

    @property
    def is_due(self):
        return self.is_active and self.next_due_date <= timezone.localdate() + timezone.timedelta(
            days=self.advance_days
        )


class MaintenanceChecklistTemplateItem(BaseModel):
    company = models.ForeignKey(
        "company.Company",
        on_delete=models.PROTECT,
        related_name="maintenance_checklist_template_items",
    )
    pm_plan = models.ForeignKey(
        PreventiveMaintenancePlan,
        on_delete=models.CASCADE,
        related_name="checklist_items",
    )
    task = models.CharField(max_length=250)
    input_type = models.CharField(
        max_length=20,
        choices=ChecklistInputType.choices,
        default=ChecklistInputType.CHECKBOX,
    )
    is_required = models.BooleanField(default=True)
    expected_text = models.CharField(max_length=200, blank=True, default="")
    min_value = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    max_value = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    uom = models.CharField(max_length=30, blank=True, default="")
    safety_critical = models.BooleanField(default=False)
    sort_order = models.PositiveSmallIntegerField(default=1)

    class Meta:
        ordering = ["sort_order", "id"]
        indexes = [models.Index(fields=["company", "pm_plan", "sort_order"])]
        verbose_name = "Maintenance Checklist Template Item"
        verbose_name_plural = "Maintenance Checklist Template Items"

    def __str__(self):
        return f"{self.pm_plan.plan_code} - {self.task}"


class PreventiveMaintenanceExecution(BaseModel):
    company = models.ForeignKey(
        "company.Company",
        on_delete=models.PROTECT,
        related_name="maintenance_pm_executions",
    )
    pm_plan = models.ForeignKey(
        PreventiveMaintenancePlan,
        on_delete=models.PROTECT,
        related_name="executions",
    )
    work_order = models.OneToOneField(
        MaintenanceWorkOrder,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="pm_execution",
    )
    asset = models.ForeignKey(Asset, on_delete=models.PROTECT, related_name="pm_executions")
    due_date = models.DateField()
    status = models.CharField(
        max_length=20,
        choices=PMExecutionStatus.choices,
        default=PMExecutionStatus.PENDING,
    )
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    skipped_at = models.DateTimeField(null=True, blank=True)
    skip_reason = models.TextField(blank=True, default="")
    completed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="maintenance_pm_executions_completed",
    )
    remarks = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["due_date", "-created_at"]
        unique_together = ("company", "pm_plan", "due_date")
        indexes = [
            models.Index(fields=["company", "status", "due_date"]),
            models.Index(fields=["asset", "due_date"]),
        ]
        verbose_name = "Preventive Maintenance Execution"
        verbose_name_plural = "Preventive Maintenance Executions"

    def __str__(self):
        return f"{self.pm_plan.plan_code} due {self.due_date}"

    @property
    def is_overdue(self):
        return self.status == PMExecutionStatus.PENDING and self.due_date < timezone.localdate()

    @property
    def effective_status(self):
        return PMExecutionStatus.OVERDUE if self.is_overdue else self.status


class MaintenanceChecklistResult(BaseModel):
    company = models.ForeignKey(
        "company.Company",
        on_delete=models.PROTECT,
        related_name="maintenance_checklist_results",
    )
    execution = models.ForeignKey(
        PreventiveMaintenanceExecution,
        on_delete=models.CASCADE,
        related_name="results",
    )
    template_item = models.ForeignKey(
        MaintenanceChecklistTemplateItem,
        on_delete=models.PROTECT,
        related_name="results",
    )
    task_snapshot = models.CharField(max_length=250)
    input_type = models.CharField(max_length=20, choices=ChecklistInputType.choices)
    value_text = models.TextField(blank=True, default="")
    value_number = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    is_ok = models.BooleanField(default=True)
    remarks = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["template_item__sort_order", "id"]
        unique_together = ("execution", "template_item")
        verbose_name = "Maintenance Checklist Result"
        verbose_name_plural = "Maintenance Checklist Results"

    def __str__(self):
        return f"{self.execution} - {self.task_snapshot}"


class SpareRequest(BaseModel):
    company = models.ForeignKey(
        "company.Company",
        on_delete=models.PROTECT,
        related_name="maintenance_spare_requests",
    )
    work_order = models.ForeignKey(
        MaintenanceWorkOrder,
        on_delete=models.PROTECT,
        related_name="spare_requests",
    )
    spare = models.ForeignKey(
        MaintenanceSpare,
        on_delete=models.PROTECT,
        related_name="requests",
    )
    status = models.CharField(
        max_length=30,
        choices=SpareRequestStatus.choices,
        default=SpareRequestStatus.REQUESTED,
    )
    requested_qty = models.DecimalField(
        max_digits=14,
        decimal_places=3,
        validators=[MinValueValidator(Decimal("0.001"))],
    )
    issued_qty = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0.000"))
    consumed_qty = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0.000"))
    returned_qty = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0.000"))
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="maintenance_spare_requests",
    )
    required_by = models.DateField(null=True, blank=True)
    purpose = models.TextField(blank=True, default="")
    store_remarks = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Maintenance Spare Request"
        verbose_name_plural = "Maintenance Spare Requests"

    def __str__(self):
        return f"{self.work_order.work_order_no} - {self.spare.part_number}"

    @property
    def pending_issue_qty(self):
        pending = self.requested_qty - self.issued_qty
        return max(pending, Decimal("0.000"))

    @property
    def available_to_consume_qty(self):
        available = self.issued_qty - self.consumed_qty - self.returned_qty
        return max(available, Decimal("0.000"))

    @property
    def total_cost(self):
        return (self.consumed_qty * self.spare.unit_cost).quantize(Decimal("0.01"))

    def refresh_status(self):
        if self.status == SpareRequestStatus.CANCELLED:
            return
        if self.issued_qty <= 0:
            self.status = SpareRequestStatus.REQUESTED
        elif self.available_to_consume_qty <= 0 and self.pending_issue_qty <= 0:
            self.status = SpareRequestStatus.CLOSED
        elif self.consumed_qty > 0 or self.returned_qty > 0:
            self.status = SpareRequestStatus.PARTIALLY_CONSUMED
        elif self.pending_issue_qty <= 0:
            self.status = SpareRequestStatus.ISSUED
        else:
            self.status = SpareRequestStatus.PARTIALLY_ISSUED


class SpareMovement(BaseModel):
    company = models.ForeignKey(
        "company.Company",
        on_delete=models.PROTECT,
        related_name="maintenance_spare_movements",
    )
    spare_request = models.ForeignKey(
        SpareRequest,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="movements",
    )
    work_order = models.ForeignKey(
        MaintenanceWorkOrder,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="spare_movements",
    )
    spare = models.ForeignKey(
        MaintenanceSpare,
        on_delete=models.PROTECT,
        related_name="movements",
    )
    movement_type = models.CharField(max_length=20, choices=SpareMovementType.choices)
    quantity = models.DecimalField(
        max_digits=14,
        decimal_places=3,
        validators=[MinValueValidator(Decimal("0.001"))],
    )
    unit_cost = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    remarks = models.TextField(blank=True, default="")
    performed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="maintenance_spare_movements",
    )

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Maintenance Spare Movement"
        verbose_name_plural = "Maintenance Spare Movements"

    def __str__(self):
        return f"{self.movement_type} {self.quantity} {self.spare.part_number}"

    @property
    def line_total(self):
        return (self.quantity * self.unit_cost).quantize(Decimal("0.01"))


class FireCategory(CompanyMasterModel):
    """Fire department store item category. Mirrors SpareCategory."""

    class Meta(CompanyMasterModel.Meta):
        unique_together = ("company", "name")
        verbose_name = "Maintenance Fire Category"
        verbose_name_plural = "Maintenance Fire Categories"


class MaintenanceFire(BaseModel):
    """Fire department store item. A standalone copy of MaintenanceSpare."""

    company = models.ForeignKey(
        "company.Company",
        on_delete=models.PROTECT,
        related_name="maintenance_fire_items",
    )
    category = models.ForeignKey(
        FireCategory,
        on_delete=models.PROTECT,
        related_name="fire_items",
    )
    name = models.CharField(max_length=200)
    part_number = models.CharField(max_length=100)
    sap_item_code = models.CharField(max_length=100, blank=True, default="")
    uom = models.CharField(max_length=30, default="NOS")
    compatible_assets = models.ManyToManyField(
        Asset,
        blank=True,
        related_name="compatible_fire_items",
    )
    is_critical = models.BooleanField(default=False)
    minimum_stock = models.DecimalField(
        max_digits=14,
        decimal_places=3,
        default=Decimal("0.000"),
        validators=[MinValueValidator(Decimal("0.000"))],
    )
    reorder_level = models.DecimalField(
        max_digits=14,
        decimal_places=3,
        default=Decimal("0.000"),
        validators=[MinValueValidator(Decimal("0.000"))],
    )
    current_stock = models.DecimalField(
        max_digits=14,
        decimal_places=3,
        default=Decimal("0.000"),
        validators=[MinValueValidator(Decimal("0.000"))],
    )
    unit_cost = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    storage_location = models.CharField(max_length=120, blank=True, default="")
    description = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["part_number", "name"]
        unique_together = ("company", "part_number")
        verbose_name = "Maintenance Fire Item"
        verbose_name_plural = "Maintenance Fire Items"

    def __str__(self):
        return f"{self.part_number} - {self.name}"

    @property
    def is_low_stock(self):
        return self.current_stock <= self.reorder_level

    @property
    def is_below_minimum(self):
        return self.current_stock <= self.minimum_stock

    @property
    def reorder_shortage_qty(self):
        if self.current_stock >= self.reorder_level:
            return Decimal("0.000")
        return self.reorder_level - self.current_stock


class FireRequest(BaseModel):
    """Fire store request raised against a work order. Mirrors SpareRequest."""

    company = models.ForeignKey(
        "company.Company",
        on_delete=models.PROTECT,
        related_name="maintenance_fire_requests",
    )
    work_order = models.ForeignKey(
        MaintenanceWorkOrder,
        on_delete=models.PROTECT,
        related_name="fire_requests",
    )
    fire_item = models.ForeignKey(
        MaintenanceFire,
        on_delete=models.PROTECT,
        related_name="requests",
    )
    status = models.CharField(
        max_length=30,
        choices=SpareRequestStatus.choices,
        default=SpareRequestStatus.REQUESTED,
    )
    requested_qty = models.DecimalField(
        max_digits=14,
        decimal_places=3,
        validators=[MinValueValidator(Decimal("0.001"))],
    )
    issued_qty = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0.000"))
    consumed_qty = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0.000"))
    returned_qty = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal("0.000"))
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="maintenance_fire_requests",
    )
    required_by = models.DateField(null=True, blank=True)
    purpose = models.TextField(blank=True, default="")
    store_remarks = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Maintenance Fire Request"
        verbose_name_plural = "Maintenance Fire Requests"

    def __str__(self):
        return f"{self.work_order.work_order_no} - {self.fire_item.part_number}"

    @property
    def pending_issue_qty(self):
        pending = self.requested_qty - self.issued_qty
        return max(pending, Decimal("0.000"))

    @property
    def available_to_consume_qty(self):
        available = self.issued_qty - self.consumed_qty - self.returned_qty
        return max(available, Decimal("0.000"))

    @property
    def total_cost(self):
        return (self.consumed_qty * self.fire_item.unit_cost).quantize(Decimal("0.01"))

    def refresh_status(self):
        if self.status == SpareRequestStatus.CANCELLED:
            return
        if self.issued_qty <= 0:
            self.status = SpareRequestStatus.REQUESTED
        elif self.available_to_consume_qty <= 0 and self.pending_issue_qty <= 0:
            self.status = SpareRequestStatus.CLOSED
        elif self.consumed_qty > 0 or self.returned_qty > 0:
            self.status = SpareRequestStatus.PARTIALLY_CONSUMED
        elif self.pending_issue_qty <= 0:
            self.status = SpareRequestStatus.ISSUED
        else:
            self.status = SpareRequestStatus.PARTIALLY_ISSUED


class FireMovement(BaseModel):
    """Fire store stock ledger entry. Mirrors SpareMovement."""

    company = models.ForeignKey(
        "company.Company",
        on_delete=models.PROTECT,
        related_name="maintenance_fire_movements",
    )
    fire_request = models.ForeignKey(
        FireRequest,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="movements",
    )
    work_order = models.ForeignKey(
        MaintenanceWorkOrder,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="fire_movements",
    )
    fire_item = models.ForeignKey(
        MaintenanceFire,
        on_delete=models.PROTECT,
        related_name="movements",
    )
    movement_type = models.CharField(max_length=20, choices=SpareMovementType.choices)
    quantity = models.DecimalField(
        max_digits=14,
        decimal_places=3,
        validators=[MinValueValidator(Decimal("0.001"))],
    )
    unit_cost = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    remarks = models.TextField(blank=True, default="")
    performed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="maintenance_fire_movements",
    )

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Maintenance Fire Movement"
        verbose_name_plural = "Maintenance Fire Movements"

    def __str__(self):
        return f"{self.movement_type} {self.quantity} {self.fire_item.part_number}"

    @property
    def line_total(self):
        return (self.quantity * self.unit_cost).quantize(Decimal("0.01"))


class FireShiftReport(BaseModel):
    """A fire-department shift inspection report (e.g. daily Day/Night rounds)."""

    company = models.ForeignKey(
        "company.Company",
        on_delete=models.PROTECT,
        related_name="maintenance_fire_reports",
    )
    report_date = models.DateField(default=timezone.localdate)
    shift = models.CharField(
        max_length=10,
        choices=FireShiftType.choices,
        default=FireShiftType.DAY,
    )
    area = models.CharField(max_length=150, blank=True, default="")
    status = models.CharField(
        max_length=20,
        choices=FireReportStatus.choices,
        default=FireReportStatus.SUBMITTED,
    )
    summary_remarks = models.TextField(blank=True, default="")
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="maintenance_fire_reports_submitted",
    )
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="maintenance_fire_reports_reviewed",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_remarks = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-report_date", "-created_at"]
        indexes = [
            models.Index(fields=["company", "report_date", "shift"]),
            models.Index(fields=["company", "status"]),
        ]
        verbose_name = "Fire Shift Report"
        verbose_name_plural = "Fire Shift Reports"

    def __str__(self):
        return f"{self.report_date} {self.get_shift_display()} fire report"


class FireShiftReportItem(BaseModel):
    """One inspected piece of fire equipment within a shift report."""

    company = models.ForeignKey(
        "company.Company",
        on_delete=models.PROTECT,
        related_name="maintenance_fire_report_items",
    )
    report = models.ForeignKey(
        FireShiftReport,
        on_delete=models.CASCADE,
        related_name="items",
    )
    asset = models.ForeignKey(
        Asset,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="fire_report_items",
    )
    equipment_name = models.CharField(max_length=200)
    equipment_type = models.CharField(
        max_length=20,
        choices=FireEquipmentType.choices,
        default=FireEquipmentType.OTHER,
    )
    status = models.CharField(
        max_length=20,
        choices=FireEquipmentStatus.choices,
        default=FireEquipmentStatus.OK,
    )
    reading = models.CharField(max_length=120, blank=True, default="")
    remarks = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["id"]
        verbose_name = "Fire Shift Report Item"
        verbose_name_plural = "Fire Shift Report Items"

    def __str__(self):
        return f"{self.equipment_name} ({self.status})"


class FireShiftReportPhoto(BaseModel):
    """A photo evidencing the state of a fire equipment item (pump, hydrant…)."""

    item = models.ForeignKey(
        FireShiftReportItem,
        on_delete=models.CASCADE,
        related_name="photos",
    )
    photo = models.FileField(upload_to="maintenance/fire-reports/photos/")
    caption = models.CharField(max_length=200, blank=True, default="")
    taken_on = models.DateField(default=timezone.localdate)

    class Meta:
        ordering = ["-taken_on", "-created_at"]
        verbose_name = "Fire Shift Report Photo"
        verbose_name_plural = "Fire Shift Report Photos"

    def __str__(self):
        return f"{self.item.equipment_name} photo {self.taken_on}"


class FireShiftReportAttachment(BaseModel):
    """A report-level file attachment (signed sheet, PDF, any document)."""

    report = models.ForeignKey(
        FireShiftReport,
        on_delete=models.CASCADE,
        related_name="attachments",
    )
    file = models.FileField(upload_to="maintenance/fire-reports/attachments/")
    title = models.CharField(max_length=200, blank=True, default="")

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Fire Shift Report Attachment"
        verbose_name_plural = "Fire Shift Report Attachments"

    def __str__(self):
        return self.title or f"attachment {self.pk}"


class WorkPermit(BaseModel):
    """A permit-to-work (PTW) safety clearance for a hazardous maintenance job.

    Mirrors the paper form "Work Permit — Moon Beverages, Greater Noida"
    (SMS-FRM-02-11-01). Progresses through a sign-off lifecycle; each transition
    stamps the acting user and time.
    """

    company = models.ForeignKey(
        "company.Company",
        on_delete=models.PROTECT,
        related_name="maintenance_work_permits",
    )
    serial_no = models.CharField(max_length=40, blank=True, default="")
    permit_types = models.JSONField(default=list, blank=True)
    status = models.CharField(
        max_length=20,
        choices=WorkPermitStatus.choices,
        default=WorkPermitStatus.DRAFT,
    )

    # 1. Validity — valid_date is the start; valid_to (optional) extends it across
    #    multiple days. time_start/time_end are the daily window on those days.
    valid_date = models.DateField(default=timezone.localdate)
    valid_to = models.DateField(null=True, blank=True)
    time_start = models.TimeField(null=True, blank=True)
    time_end = models.TimeField(null=True, blank=True)

    # 2-4. Parties
    issuing_dept = models.CharField(max_length=150, blank=True, default="")
    issuer_name = models.CharField(max_length=200, blank=True, default="")
    issuer_phone = models.CharField(max_length=50, blank=True, default="")
    issued_to_name = models.CharField(max_length=200, blank=True, default="")
    issued_to_phone = models.CharField(max_length=50, blank=True, default="")
    cross_ref = models.CharField(max_length=150, blank=True, default="")

    # 5-6. Job
    job_location = models.CharField(max_length=200)
    job_description = models.TextField()

    # 7-8. Hazards + method statement
    hazards_identified = models.JSONField(default=list, blank=True)
    control_measures = models.TextField(blank=True, default="")

    # 9. Isolations
    electrical_isolation_required = models.BooleanField(default=False)
    electrical_isolation_detail = models.CharField(max_length=300, blank=True, default="")
    service_isolation_required = models.BooleanField(default=False)
    service_isolation_detail = models.CharField(max_length=300, blank=True, default="")
    process_isolation_required = models.BooleanField(default=False)
    process_isolation_detail = models.CharField(max_length=300, blank=True, default="")

    # 10-11. Checklists
    ppe = models.JSONField(default=list, blank=True)
    precautions = models.JSONField(default=list, blank=True)

    # 12. Authorization matrix helpers
    modification_authorization_required = models.BooleanField(default=False)
    fire_watcher_name = models.CharField(max_length=200, blank=True, default="")

    # 13. Acceptance
    accepted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="maintenance_work_permits_accepted",
    )
    accepted_at = models.DateTimeField(null=True, blank=True)
    contractor_company = models.CharField(max_length=200, blank=True, default="")

    # 14. Energization
    service_energization = models.BooleanField(default=False)
    process_energization = models.BooleanField(default=False)
    electrical_energization = models.BooleanField(default=False)

    # 15/17. Completion + handover
    completion_type = models.CharField(
        max_length=20,
        choices=WorkCompletionType.choices,
        blank=True,
        default="",
    )
    completed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="maintenance_work_permits_completed",
    )
    completed_at = models.DateTimeField(null=True, blank=True)
    closure_time = models.DateTimeField(null=True, blank=True)
    handover_by = models.CharField(max_length=200, blank=True, default="")
    handover_to = models.CharField(max_length=200, blank=True, default="")

    # Maintenance -> Fire-head approval flow tracking.
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="maintenance_work_permits_submitted",
    )
    submitted_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="maintenance_work_permits_approved",
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    started_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="maintenance_work_permits_started",
    )
    started_at = models.DateTimeField(null=True, blank=True)
    expired_at = models.DateTimeField(null=True, blank=True)
    renewed_from = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="renewals",
    )

    class Meta:
        ordering = ["-valid_date", "-created_at"]
        indexes = [
            models.Index(fields=["company", "status"]),
            models.Index(fields=["company", "valid_date"]),
        ]
        verbose_name = "Work Permit"
        verbose_name_plural = "Work Permits"

    def __str__(self):
        return self.serial_no or f"work permit {self.pk}"

    @property
    def expires_at(self):
        """The moment the permit's validity window closes.

        Spans valid_date .. valid_to (defaults to valid_date for a single-day
        permit). time_end sets the closing time on the last day; without it the
        permit is valid to the end of that day.
        """
        end_date = self.valid_to or self.valid_date
        end_time = self.time_end or dt.time.max
        naive = dt.datetime.combine(end_date, end_time)
        if timezone.is_naive(naive):
            return timezone.make_aware(naive)
        return naive

    @property
    def is_expired_now(self):
        return timezone.now() > self.expires_at

    # Statuses that a validity lapse can move to EXPIRED.
    EXPIRABLE_STATUSES = ("SUBMITTED", "APPROVED", "IN_PROGRESS")

    @classmethod
    def next_serial_no(cls, company):
        date_part = timezone.localdate().strftime("%Y%m%d")
        prefix = f"D-{date_part}"
        last = (
            cls.objects.filter(company=company, serial_no__startswith=prefix)
            .order_by("-serial_no")
            .first()
        )
        next_number = 1
        if last:
            suffix = last.serial_no.rsplit("-", 1)[-1]
            if suffix.isdigit():
                next_number = int(suffix) + 1
        return f"{prefix}-{next_number:04d}"


class WorkPermitWorker(BaseModel):
    """One employee/contractor recorded on the job (section 16 of the form)."""

    permit = models.ForeignKey(
        WorkPermit,
        on_delete=models.CASCADE,
        related_name="workers",
    )
    name = models.CharField(max_length=200)
    role = models.CharField(max_length=150, blank=True, default="")
    signed = models.BooleanField(default=False)
    signed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["id"]
        verbose_name = "Work Permit Worker"
        verbose_name_plural = "Work Permit Workers"

    def __str__(self):
        return self.name


class WorkPermitAttachment(BaseModel):
    """A permit-level file attachment (method statement, drawings, signed sheet)."""

    permit = models.ForeignKey(
        WorkPermit,
        on_delete=models.CASCADE,
        related_name="attachments",
    )
    file = models.FileField(upload_to="maintenance/work-permits/attachments/")
    title = models.CharField(max_length=200, blank=True, default="")

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Work Permit Attachment"
        verbose_name_plural = "Work Permit Attachments"

    def __str__(self):
        return self.title or f"attachment {self.pk}"


class WorkPermitApproval(BaseModel):
    """A single sign-off in the authorization matrix (section 12)."""

    permit = models.ForeignKey(
        WorkPermit,
        on_delete=models.CASCADE,
        related_name="approvals",
    )
    role = models.CharField(max_length=30, choices=WorkPermitApprovalRole.choices)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="maintenance_work_permit_approvals",
    )
    approved_at = models.DateTimeField(default=timezone.now)
    remarks = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["approved_at", "id"]
        unique_together = ("permit", "role")
        verbose_name = "Work Permit Approval"
        verbose_name_plural = "Work Permit Approvals"

    def __str__(self):
        return f"{self.get_role_display()} on {self.permit_id}"


class SafetyViolationType(CompanyMasterModel):
    """A safety violation category with its standard fine, e.g. 'No Helmet' = 500."""

    default_fine_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )

    class Meta(CompanyMasterModel.Meta):
        unique_together = ("company", "name")
        verbose_name = "Safety Violation Type"
        verbose_name_plural = "Safety Violation Types"


class SafetyFine(BaseModel):
    """A PPE / safety violation recorded against a worker, with a monetary fine.

    Raised by the Fire Department Head when a worker is found on the floor without
    required PPE. Settled later as PAID or WAIVED.
    """

    company = models.ForeignKey(
        "company.Company",
        on_delete=models.PROTECT,
        related_name="maintenance_safety_fines",
    )
    fine_no = models.CharField(max_length=40, blank=True, default="")
    violation_type = models.ForeignKey(
        SafetyViolationType,
        on_delete=models.PROTECT,
        related_name="fines",
    )

    # Offender — free text (there is no employee master), optionally linked to a department.
    offender_name = models.CharField(max_length=200)
    employee_code = models.CharField(max_length=50, blank=True, default="")
    contractor_company = models.CharField(max_length=200, blank=True, default="")
    contact = models.CharField(max_length=50, blank=True, default="")
    department = models.ForeignKey(
        "accounts.Department",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="maintenance_safety_fines",
    )

    # Violation detail
    occurred_at = models.DateTimeField(default=timezone.now)
    location = models.CharField(max_length=200, blank=True, default="")
    ppe_missing = models.JSONField(default=list, blank=True)
    description = models.TextField(blank=True, default="")

    fine_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    status = models.CharField(
        max_length=20,
        choices=SafetyFineStatus.choices,
        default=SafetyFineStatus.PENDING,
    )

    issued_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="maintenance_safety_fines_issued",
    )
    issued_at = models.DateTimeField(default=timezone.now)

    settled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="maintenance_safety_fines_settled",
    )
    settled_at = models.DateTimeField(null=True, blank=True)
    settlement_remarks = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-occurred_at", "-created_at"]
        indexes = [
            models.Index(fields=["company", "status"]),
            models.Index(fields=["company", "occurred_at"]),
        ]
        verbose_name = "Safety Fine"
        verbose_name_plural = "Safety Fines"

    def __str__(self):
        return f"{self.fine_no or self.pk} - {self.offender_name}"

    @classmethod
    def next_fine_no(cls, company):
        date_part = timezone.localdate().strftime("%Y%m%d")
        prefix = f"SF-{date_part}"
        last = (
            cls.objects.filter(company=company, fine_no__startswith=prefix)
            .order_by("-fine_no")
            .first()
        )
        next_number = 1
        if last:
            suffix = last.fine_no.rsplit("-", 1)[-1]
            if suffix.isdigit():
                next_number = int(suffix) + 1
        return f"{prefix}-{next_number:04d}"


class SafetyFinePhoto(BaseModel):
    """Photo evidence of the safety violation."""

    fine = models.ForeignKey(
        SafetyFine,
        on_delete=models.CASCADE,
        related_name="photos",
    )
    photo = models.FileField(upload_to="maintenance/safety-fines/photos/")
    caption = models.CharField(max_length=200, blank=True, default="")

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Safety Fine Photo"
        verbose_name_plural = "Safety Fine Photos"

    def __str__(self):
        return self.caption or f"photo {self.pk}"


class MaterialIndent(BaseModel):
    """A material indent (requisition) raised by a department for items to leave via the gate.

    Mirrors the paper 'Material Indent Form'. Once approved by a higher authority it
    generates a Returnable/Non-returnable Gate Pass (returnable_items app) which then
    appears in the gate's Material Out screen for gate-out.
    """

    company = models.ForeignKey(
        "company.Company",
        on_delete=models.PROTECT,
        related_name="maintenance_material_indents",
    )
    indent_no = models.CharField(max_length=40, blank=True, default="")
    indent_date = models.DateField(default=timezone.localdate)
    purpose = models.CharField(max_length=200, blank=True, default="")
    department = models.ForeignKey(
        "accounts.Department",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="maintenance_material_indents",
    )
    requested_by_name = models.CharField(max_length=200, blank=True, default="")
    contact_no = models.CharField(max_length=50, blank=True, default="")
    #: Chosen by the requester; drives whether the generated gate pass is returnable.
    is_returnable = models.BooleanField(default=False)
    status = models.CharField(
        max_length=30,
        choices=MaterialIndentStatus.choices,
        default=MaterialIndentStatus.DRAFT,
    )
    remarks = models.TextField(blank=True, default="")

    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="maintenance_material_indents_submitted",
    )
    submitted_at = models.DateTimeField(null=True, blank=True)
    # Store engineer review (issue from stock / forward shortfall for purchase).
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="maintenance_material_indents_reviewed",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    store_remarks = models.TextField(blank=True, default="")

    # Higher-authority purchase approval.
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="maintenance_material_indents_approved",
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    decision_remarks = models.TextField(blank=True, default="")

    # Quotation round — the purchaser collects prices from several companies and
    # sends them back for the approver to pick one.
    quotations_submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="maintenance_material_indents_quoted",
    )
    quotations_submitted_at = models.DateTimeField(null=True, blank=True)
    #: The company the approver chose to buy from.
    selected_quotation = models.ForeignKey(
        "MaterialIndentQuotation",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="selected_for_indents",
    )
    quotation_selected_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="maintenance_material_indents_quotation_selected",
    )
    quotation_selected_at = models.DateTimeField(null=True, blank=True)
    #: Why that company was chosen, or why the quotes were sent back for more.
    quotation_remarks = models.TextField(blank=True, default="")

    # Purchaser (procurement) — status-only completion.
    purchased_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="maintenance_material_indents_purchased",
    )
    purchased_at = models.DateTimeField(null=True, blank=True)
    purchase_remarks = models.TextField(blank=True, default="")

    # Gate-in of the purchased goods (vehicle recorded by the gate; invoice/bill
    # go on MaterialIndentAttachment).
    gatein_vehicle_number = models.CharField(max_length=30, blank=True, default="")
    gatein_driver_name = models.CharField(max_length=200, blank=True, default="")
    gatein_driver_mobile = models.CharField(max_length=20, blank=True, default="")
    gate_in_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="maintenance_material_indents_gated_in",
    )
    gate_in_at = models.DateTimeField(null=True, blank=True)

    # Store receipt into Store/Spares stock.
    received_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="maintenance_material_indents_received",
    )
    received_at = models.DateTimeField(null=True, blank=True)

    #: Legacy link (unused in the store/purchase flow) — kept for old records.
    generated_gate_pass = models.ForeignKey(
        "returnable_items.ReturnableGatePass",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="source_material_indents",
    )

    class Meta:
        ordering = ["-indent_date", "-created_at"]
        indexes = [
            models.Index(fields=["company", "status"]),
            models.Index(fields=["company", "indent_date"]),
        ]
        verbose_name = "Material Indent"
        verbose_name_plural = "Material Indents"

    def __str__(self):
        return self.indent_no or f"material indent {self.pk}"

    @classmethod
    def next_indent_no(cls, company):
        date_part = timezone.localdate().strftime("%Y%m%d")
        prefix = f"MI-{date_part}"
        last = (
            cls.objects.filter(company=company, indent_no__startswith=prefix)
            .order_by("-indent_no")
            .first()
        )
        next_number = 1
        if last:
            suffix = last.indent_no.rsplit("-", 1)[-1]
            if suffix.isdigit():
                next_number = int(suffix) + 1
        return f"{prefix}-{next_number:04d}"


class MaterialIndentItem(BaseModel):
    """One line of a material indent (a requested item)."""

    indent = models.ForeignKey(
        MaterialIndent,
        on_delete=models.CASCADE,
        related_name="items",
    )
    line_num = models.PositiveIntegerField(default=1)
    particulars = models.CharField(max_length=250)
    specification = models.CharField(max_length=250, blank=True, default="")
    quantity = models.DecimalField(
        max_digits=14,
        decimal_places=3,
        default=Decimal("1.000"),
        validators=[MinValueValidator(Decimal("0.001"))],
    )
    unit = models.CharField(max_length=20, blank=True, default="NOS")
    priority = models.CharField(
        max_length=10,
        choices=MaterialIndentPriority.choices,
        default=MaterialIndentPriority.NORMAL,
    )
    #: How much the store could issue from stock; the rest is the purchase shortfall.
    issued_quantity = models.DecimalField(
        max_digits=14,
        decimal_places=3,
        default=Decimal("0.000"),
        validators=[MinValueValidator(Decimal("0.000"))],
    )
    #: How much of the purchased shortfall was received into store stock, and where.
    received_quantity = models.DecimalField(
        max_digits=14,
        decimal_places=3,
        default=Decimal("0.000"),
        validators=[MinValueValidator(Decimal("0.000"))],
    )
    received_spare = models.ForeignKey(
        "MaintenanceSpare",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="material_indent_receipts",
    )
    remarks = models.CharField(max_length=250, blank=True, default="")

    class Meta:
        ordering = ["line_num", "id"]
        verbose_name = "Material Indent Item"
        verbose_name_plural = "Material Indent Items"

    def __str__(self):
        return f"{self.particulars} ({self.quantity})"

    @property
    def shortfall_quantity(self):
        short = self.quantity - self.issued_quantity
        return short if short > Decimal("0.000") else Decimal("0.000")


class MaterialIndentQuotation(BaseModel):
    """One company's price offer for the purchase shortfall of an indent.

    The purchaser raises one of these per company quoting for the same items,
    with a rate per line; the approver compares them and picks the company to
    buy from. Companies are typed in rather than picked from a master — spare
    parts are routinely quoted by vendors who exist nowhere else yet.
    """

    indent = models.ForeignKey(
        MaterialIndent,
        on_delete=models.CASCADE,
        related_name="quotations",
    )
    company_name = models.CharField(max_length=200)
    contact_person = models.CharField(max_length=200, blank=True, default="")
    contact_no = models.CharField(max_length=50, blank=True, default="")
    gstin = models.CharField(max_length=20, blank=True, default="")
    #: The vendor's own reference for the quote, and the date they gave it.
    quotation_no = models.CharField(max_length=60, blank=True, default="")
    quotation_date = models.DateField(null=True, blank=True)
    #: Delivery promise and payment terms, both of which sway the decision.
    delivery_days = models.PositiveIntegerField(null=True, blank=True)
    payment_terms = models.CharField(max_length=200, blank=True, default="")
    #: Freight, packing and the like — added on top of the line total.
    other_charges = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    remarks = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["company_name", "id"]
        verbose_name = "Material Indent Quotation"
        verbose_name_plural = "Material Indent Quotations"

    def __str__(self):
        return f"{self.company_name} — {self.indent.indent_no}"

    @property
    def lines_total(self):
        """Sum of rate x quantity across the quoted lines."""
        return sum((line.amount for line in self.lines.all()), Decimal("0.00"))

    @property
    def total_amount(self):
        """What this company would cost in full, freight included."""
        return self.lines_total + (self.other_charges or Decimal("0.00"))

    @property
    def is_selected(self):
        return self.indent.selected_quotation_id == self.pk


class MaterialIndentQuotationLine(BaseModel):
    """One company's rate for one item of the indent."""

    quotation = models.ForeignKey(
        MaterialIndentQuotation,
        on_delete=models.CASCADE,
        related_name="lines",
    )
    item = models.ForeignKey(
        MaterialIndentItem,
        on_delete=models.CASCADE,
        related_name="quotation_lines",
    )
    #: How many this company is quoting for. Defaults to the item's purchase
    #: shortfall, but a vendor may only be able to supply part of it.
    quantity = models.DecimalField(
        max_digits=14,
        decimal_places=3,
        default=Decimal("0.000"),
        validators=[MinValueValidator(Decimal("0.000"))],
    )
    unit_price = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    remarks = models.CharField(max_length=250, blank=True, default="")

    class Meta:
        ordering = ["item__line_num", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["quotation", "item"],
                name="uniq_quotation_line_per_item",
            )
        ]
        verbose_name = "Material Indent Quotation Line"
        verbose_name_plural = "Material Indent Quotation Lines"

    def __str__(self):
        return f"{self.item.particulars} @ {self.unit_price}"

    @property
    def amount(self):
        return (self.quantity or Decimal("0.000")) * (self.unit_price or Decimal("0.00"))


class MaterialIndentAttachment(BaseModel):
    """Invoice / bill / other document attached when purchased goods arrive."""

    indent = models.ForeignKey(
        MaterialIndent,
        on_delete=models.CASCADE,
        related_name="attachments",
    )
    #: Set when the file is the written quote behind a quotation row, so the
    #: approver sees the proof next to the price.
    quotation = models.ForeignKey(
        MaterialIndentQuotation,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="attachments",
    )
    file = models.FileField(upload_to="maintenance/material-indents/attachments/")
    doc_type = models.CharField(
        max_length=20,
        choices=MaterialIndentDocType.choices,
        default=MaterialIndentDocType.INVOICE,
    )
    title = models.CharField(max_length=200, blank=True, default="")

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Material Indent Attachment"
        verbose_name_plural = "Material Indent Attachments"

    def __str__(self):
        return self.title or f"attachment {self.pk}"


class FireEquipmentIssue(BaseModel):
    """Issue-and-return register for fire gear (helmet, suit, boots) given to a person."""

    company = models.ForeignKey(
        "company.Company",
        on_delete=models.PROTECT,
        related_name="maintenance_fire_issues",
    )
    issued_to_name = models.CharField(max_length=200)
    employee_code = models.CharField(max_length=50, blank=True, default="")
    department = models.CharField(max_length=150, blank=True, default="")
    contact = models.CharField(max_length=50, blank=True, default="")
    issued_at = models.DateTimeField(default=timezone.now)
    expected_return = models.DateTimeField(null=True, blank=True)
    returned_at = models.DateTimeField(null=True, blank=True)
    purpose = models.TextField(blank=True, default="")
    status = models.CharField(
        max_length=20,
        choices=FireIssueStatus.choices,
        default=FireIssueStatus.ISSUED,
    )
    issued_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="maintenance_fire_issues",
    )
    remarks = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-issued_at", "-created_at"]
        indexes = [
            models.Index(fields=["company", "status"]),
            models.Index(fields=["company", "issued_at"]),
        ]
        verbose_name = "Fire Equipment Issue"
        verbose_name_plural = "Fire Equipment Issues"

    def __str__(self):
        return f"{self.issued_to_name} - {self.issued_at:%Y-%m-%d}"

    @property
    def is_overdue(self):
        if self.status == FireIssueStatus.RETURNED or not self.expected_return:
            return False
        return self.expected_return < timezone.now()

    def refresh_status(self):
        items = list(self.items.all())
        total_issued = sum((item.quantity_issued for item in items), Decimal("0.000"))
        total_returned = sum((item.quantity_returned for item in items), Decimal("0.000"))
        if total_returned <= 0:
            self.status = FireIssueStatus.ISSUED
            self.returned_at = None
        elif total_returned >= total_issued:
            self.status = FireIssueStatus.RETURNED
            if not self.returned_at:
                self.returned_at = timezone.now()
        else:
            self.status = FireIssueStatus.PARTIALLY_RETURNED
            self.returned_at = None


class FireEquipmentIssueItem(BaseModel):
    """One line of gear on an issue slip; optionally linked to a Fire store item."""

    company = models.ForeignKey(
        "company.Company",
        on_delete=models.PROTECT,
        related_name="maintenance_fire_issue_items",
    )
    issue = models.ForeignKey(
        FireEquipmentIssue,
        on_delete=models.CASCADE,
        related_name="items",
    )
    fire_item = models.ForeignKey(
        MaintenanceFire,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="issue_lines",
    )
    equipment_name = models.CharField(max_length=200)
    quantity_issued = models.DecimalField(
        max_digits=14,
        decimal_places=3,
        validators=[MinValueValidator(Decimal("0.001"))],
    )
    quantity_returned = models.DecimalField(
        max_digits=14,
        decimal_places=3,
        default=Decimal("0.000"),
    )
    return_condition = models.CharField(
        max_length=20,
        choices=FireReturnCondition.choices,
        default=FireReturnCondition.OK,
    )
    remarks = models.CharField(max_length=250, blank=True, default="")

    class Meta:
        ordering = ["id"]
        verbose_name = "Fire Equipment Issue Item"
        verbose_name_plural = "Fire Equipment Issue Items"

    def __str__(self):
        return f"{self.equipment_name} x {self.quantity_issued}"

    @property
    def pending_return_qty(self):
        return max(self.quantity_issued - self.quantity_returned, Decimal("0.000"))


class MaintenanceGateLink(BaseModel):
    company = models.ForeignKey(
        "company.Company",
        on_delete=models.PROTECT,
        related_name="maintenance_gate_links",
    )
    gate_entry = models.OneToOneField(
        "maintenance_gatein.MaintenanceGateEntry",
        on_delete=models.CASCADE,
        related_name="maintenance_link",
    )
    asset = models.ForeignKey(
        Asset,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="gate_links",
    )
    work_order = models.ForeignKey(
        MaintenanceWorkOrder,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="gate_links",
    )
    spare = models.ForeignKey(
        MaintenanceSpare,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="gate_links",
    )
    qc_required = models.BooleanField(default=False)
    qc_status = models.CharField(
        max_length=20,
        choices=GateQCStatus.choices,
        default=GateQCStatus.NOT_REQUIRED,
    )
    grpo_reference = models.CharField(max_length=120, blank=True, default="")
    grpo_doc_entry = models.PositiveIntegerField(null=True, blank=True)
    grpo_doc_num = models.CharField(max_length=50, blank=True, default="")
    receipt_status = models.CharField(
        max_length=20,
        choices=GateReceiptStatus.choices,
        default=GateReceiptStatus.NOT_RECEIVED,
    )
    received_quantity = models.DecimalField(
        max_digits=14,
        decimal_places=3,
        default=Decimal("0.000"),
        validators=[MinValueValidator(Decimal("0.000"))],
    )
    received_at = models.DateTimeField(null=True, blank=True)
    received_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="maintenance_gate_receipts_received",
    )
    notes = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["company", "receipt_status"]),
            models.Index(fields=["company", "qc_status"]),
            models.Index(fields=["asset"]),
            models.Index(fields=["work_order"]),
            models.Index(fields=["spare"]),
        ]
        verbose_name = "Maintenance Gate Link"
        verbose_name_plural = "Maintenance Gate Links"

    def __str__(self):
        return f"{self.gate_entry.work_order_number} maintenance link"


class MaintenanceSpareReceipt(BaseModel):
    company = models.ForeignKey(
        "company.Company",
        on_delete=models.PROTECT,
        related_name="maintenance_spare_receipts",
    )
    gate_link = models.OneToOneField(
        MaintenanceGateLink,
        on_delete=models.PROTECT,
        related_name="spare_receipt",
    )
    asset = models.ForeignKey(
        Asset,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="spare_receipts",
    )
    work_order = models.ForeignKey(
        MaintenanceWorkOrder,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="spare_receipts",
    )
    spare = models.ForeignKey(
        MaintenanceSpare,
        on_delete=models.PROTECT,
        related_name="gate_receipts",
    )
    quantity = models.DecimalField(
        max_digits=14,
        decimal_places=3,
        validators=[MinValueValidator(Decimal("0.001"))],
    )
    unit_cost = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    qc_status = models.CharField(
        max_length=20,
        choices=GateQCStatus.choices,
        default=GateQCStatus.NOT_REQUIRED,
    )
    grpo_reference = models.CharField(max_length=120, blank=True, default="")
    grpo_doc_entry = models.PositiveIntegerField(null=True, blank=True)
    grpo_doc_num = models.CharField(max_length=50, blank=True, default="")
    invoice_number = models.CharField(max_length=100, blank=True, default="")
    received_at = models.DateTimeField(default=timezone.now)
    received_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="maintenance_spare_receipts_received",
    )
    remarks = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-received_at", "-created_at"]
        verbose_name = "Maintenance Spare Receipt"
        verbose_name_plural = "Maintenance Spare Receipts"

    def __str__(self):
        return f"{self.quantity} {self.spare.part_number} from {self.gate_link.gate_entry.work_order_number}"


class MaintenanceVendorVisit(BaseModel):
    company = models.ForeignKey(
        "company.Company",
        on_delete=models.PROTECT,
        related_name="maintenance_vendor_visits",
    )
    work_order = models.ForeignKey(
        MaintenanceWorkOrder,
        on_delete=models.PROTECT,
        related_name="vendor_visits",
    )
    asset = models.ForeignKey(
        Asset,
        on_delete=models.PROTECT,
        related_name="vendor_visits",
    )
    vendor_code = models.CharField(max_length=80, blank=True, default="")
    vendor_name = models.CharField(max_length=200)
    contact_person = models.CharField(max_length=120, blank=True, default="")
    contact_phone = models.CharField(max_length=30, blank=True, default="")
    status = models.CharField(
        max_length=20,
        choices=VendorVisitStatus.choices,
        default=VendorVisitStatus.PLANNED,
    )
    planned_start = models.DateTimeField(null=True, blank=True)
    planned_end = models.DateTimeField(null=True, blank=True)
    actual_start = models.DateTimeField(null=True, blank=True)
    actual_end = models.DateTimeField(null=True, blank=True)
    person_gate_entry = models.ForeignKey(
        "person_gatein.EntryLog",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="maintenance_vendor_visits",
    )
    material_gate_entry = models.ForeignKey(
        "maintenance_gatein.MaintenanceGateEntry",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="vendor_visits",
    )
    service_report_attachment = models.FileField(
        upload_to="maintenance/vendor-visits/service-reports/",
        null=True,
        blank=True,
    )
    invoice_number = models.CharField(max_length=100, blank=True, default="")
    invoice_attachment = models.FileField(
        upload_to="maintenance/vendor-visits/invoices/",
        null=True,
        blank=True,
    )
    remarks = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-planned_start", "-created_at"]
        indexes = [
            models.Index(fields=["company", "status"]),
            models.Index(fields=["work_order"]),
            models.Index(fields=["asset"]),
            models.Index(fields=["vendor_code"]),
        ]
        verbose_name = "Maintenance Vendor Visit"
        verbose_name_plural = "Maintenance Vendor Visits"

    def __str__(self):
        return f"{self.vendor_name} - {self.work_order.work_order_no}"


class MaintenanceWorkOrderPhoto(BaseModel):
    work_order = models.ForeignKey(
        MaintenanceWorkOrder,
        on_delete=models.CASCADE,
        related_name="photos",
    )
    photo_type = models.CharField(
        max_length=20,
        choices=WorkOrderPhotoType.choices,
        default=WorkOrderPhotoType.GENERAL,
    )
    photo = models.FileField(upload_to="maintenance/work-orders/photos/")
    caption = models.CharField(max_length=200, blank=True, default="")
    taken_on = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["photo_type", "-taken_on", "-created_at"]
        verbose_name = "Maintenance Work Order Photo"
        verbose_name_plural = "Maintenance Work Order Photos"

    def __str__(self):
        return f"{self.work_order.work_order_no} {self.photo_type} photo"


class MaintenanceWorkOrderLog(BaseModel):
    """Hand-off trail: who did what to a work order, and what they said.

    The work order carries one field per remark (technician, completion,
    closure), which the raise -> complete -> send back -> complete loop would
    otherwise overwrite on every round. Each hand-off appends a row here so the
    conversation survives.
    """

    work_order = models.ForeignKey(
        MaintenanceWorkOrder,
        on_delete=models.CASCADE,
        related_name="logs",
    )
    action = models.CharField(max_length=20, choices=WorkOrderLogAction.choices)
    remarks = models.TextField(blank=True, default="")
    #: Status the work order moved to, for reading the trail without guessing.
    status = models.CharField(
        max_length=30,
        choices=WorkOrderStatus.choices,
        blank=True,
        default="",
    )

    class Meta:
        ordering = ["created_at", "id"]
        verbose_name = "Maintenance Work Order Log"
        verbose_name_plural = "Maintenance Work Order Logs"

    def __str__(self):
        return f"{self.work_order.work_order_no} {self.action}"


# ---------------------------------------------------------------------------
# Daily utility registers (factory-wide, not company-scoped)
# ---------------------------------------------------------------------------

class ElectricityMeter(BaseModel):
    """Master list of meters read in the Daily Electricity register.

    The register itself stays factory-wide (one page, one entry per meter per
    day), but a meter is tagged with the companies it feeds so its units and
    cost can be attributed. A meter feeding shared plant carries several
    companies (typically Oil + Beverages, which share the campus); Jivo Mart
    sits on its own supply, so its meters carry Mart alone. Leaving the tag
    empty means "not attributed yet": the meter still shows on the unfiltered
    register but drops out of a company-filtered view.
    """

    name = models.CharField(max_length=150, unique=True)
    meter_number = models.CharField(max_length=100, blank=True, default="")
    location = models.CharField(max_length=200, blank=True, default="")
    companies = models.ManyToManyField(
        "company.Company",
        blank=True,
        related_name="electricity_meters",
        help_text=(
            "Companies this meter serves. Pick more than one for a meter shared "
            "between companies; leave empty if it is not attributed to any."
        ),
    )
    rate_per_unit = models.DecimalField(
        max_digits=12,
        decimal_places=4,
        default=Decimal("0"),
        validators=[MinValueValidator(Decimal("0"))],
        help_text="Default rate per unit used to cost new readings",
    )
    multiplying_factor = models.DecimalField(
        max_digits=10,
        decimal_places=4,
        default=Decimal("1"),
        validators=[MinValueValidator(Decimal("0.0001"))],
        help_text=(
            "Grid multiplying factor (MF). The meter dial under-reads, so the "
            "day's dial difference is multiplied by this to get the billed "
            "units. 1 means the dial is read as-is."
        ),
    )

    class Meta:
        ordering = ["name"]
        verbose_name = "Electricity Meter"
        verbose_name_plural = "Electricity Meters"

    def __str__(self):
        return self.name


class DailyElectricityReading(BaseModel):
    """One reading per meter per day; units and cost are derived on save.

    ``units_consumed`` is the *billed* figure: the dial difference multiplied by
    the meter's grid multiplying factor. Both the factor and the rate are
    snapshotted from the meter at entry time so later master changes never
    reprice history.
    """

    meter = models.ForeignKey(
        ElectricityMeter,
        on_delete=models.PROTECT,
        related_name="daily_readings",
    )
    date = models.DateField()
    opening_reading = models.DecimalField(max_digits=14, decimal_places=2)
    closing_reading = models.DecimalField(max_digits=14, decimal_places=2)
    # Snapshot of the meter's grid MF; the dial difference is multiplied by it.
    multiplying_factor = models.DecimalField(
        max_digits=10,
        decimal_places=4,
        default=Decimal("1"),
        validators=[MinValueValidator(Decimal("0.0001"))],
    )
    units_consumed = models.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal("0")
    )
    # Snapshot of the meter rate at entry time so later rate changes do not
    # silently reprice history.
    rate_per_unit = models.DecimalField(
        max_digits=12, decimal_places=4, default=Decimal("0")
    )
    total_cost = models.DecimalField(
        max_digits=15, decimal_places=2, default=Decimal("0")
    )
    remarks = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-date", "meter__name"]
        constraints = [
            models.UniqueConstraint(
                fields=["meter", "date"], name="uniq_meter_reading_per_day"
            ),
        ]
        verbose_name = "Daily Electricity Reading"
        verbose_name_plural = "Daily Electricity Readings"

    @property
    def dial_difference(self) -> Decimal:
        """What the dial itself moved, before the multiplying factor."""
        return Decimal(str(self.closing_reading)) - Decimal(str(self.opening_reading))

    def save(self, *args, **kwargs):
        self.units_consumed = self.dial_difference * Decimal(
            str(self.multiplying_factor or 1)
        )
        self.total_cost = self.units_consumed * Decimal(str(self.rate_per_unit))
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.meter.name} @ {self.date}: {self.units_consumed} units"


class DailyWastageLog(BaseModel):
    """Simple daily wastage register — record-only, no approval chain."""

    date = models.DateField()
    material_name = models.CharField(max_length=255)
    qty = models.DecimalField(
        max_digits=12,
        decimal_places=3,
        validators=[MinValueValidator(Decimal("0"))],
    )
    uom = models.CharField(max_length=20, blank=True, default="")
    reason = models.TextField(blank=True, default="")
    photo = models.ImageField(
        upload_to="maintenance/daily-wastage/", null=True, blank=True
    )

    class Meta:
        ordering = ["-date", "-created_at"]
        verbose_name = "Daily Wastage Log"
        verbose_name_plural = "Daily Wastage Logs"

    def __str__(self):
        return f"{self.date} — {self.material_name}: {self.qty} {self.uom}"
