import calendar
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
            ("can_view_vendor", "Can view Maintenance vendors"),
            ("can_manage_vendor", "Can manage Maintenance vendors"),
            ("can_view_maintenance_reports", "Can view Maintenance reports"),
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
        AssetDepartment,
        on_delete=models.PROTECT,
        related_name="assets",
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
    asset = models.ForeignKey(Asset, on_delete=models.PROTECT, related_name="work_orders")
    department = models.ForeignKey(
        AssetDepartment,
        on_delete=models.PROTECT,
        related_name="work_orders",
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
