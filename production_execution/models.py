from django.db import models
from django.conf import settings


# ---------------------------------------------------------------------------
# Choices / Enums
# ---------------------------------------------------------------------------

class MachineType(models.TextChoices):
    FILLER = "FILLER", "Filler"
    CAPPER = "CAPPER", "Capper"
    CONVEYOR = "CONVEYOR", "Conveyor"
    LABELER = "LABELER", "Labeler"
    CODING = "CODING", "Coding"
    SHRINK_PACK = "SHRINK_PACK", "Shrink Pack"
    STICKER_LABELER = "STICKER_LABELER", "Sticker Labeler"
    TAPPING_MACHINE = "TAPPING_MACHINE", "Tapping Machine"


class RunStatus(models.TextChoices):
    DRAFT = "DRAFT", "Draft"
    IN_PROGRESS = "IN_PROGRESS", "In Progress"
    COMPLETED = "COMPLETED", "Completed"


class MachineStatus(models.TextChoices):
    RUNNING = "RUNNING", "Running"
    IDLE = "IDLE", "Idle"
    BREAKDOWN = "BREAKDOWN", "Breakdown"
    CHANGEOVER = "CHANGEOVER", "Changeover"


class BreakdownType(models.TextChoices):
    """Kept for legacy reference only. Use BreakdownCategory model instead."""
    LINE = "LINE", "Line"
    EXTERNAL = "EXTERNAL", "External"


class ChecklistFrequency(models.TextChoices):
    DAILY = "DAILY", "Daily"
    WEEKLY = "WEEKLY", "Weekly"
    MONTHLY = "MONTHLY", "Monthly"


class ChecklistStatus(models.TextChoices):
    OK = "OK", "OK"
    NOT_OK = "NOT_OK", "Not OK"
    NA = "NA", "N/A"


class ClearanceResult(models.TextChoices):
    YES = "YES", "Yes"
    NO = "NO", "No"
    NA = "NA", "N/A"


class ClearanceStatus(models.TextChoices):
    DRAFT = "DRAFT", "Draft"
    SUBMITTED = "SUBMITTED", "Submitted"
    CLEARED = "CLEARED", "Cleared"
    NOT_CLEARED = "NOT_CLEARED", "Not Cleared"


class WasteApprovalStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    PARTIALLY_APPROVED = "PARTIALLY_APPROVED", "Partially Approved"
    FULLY_APPROVED = "FULLY_APPROVED", "Fully Approved"


class ShiftChoice(models.TextChoices):
    MORNING = "MORNING", "Morning"
    AFTERNOON = "AFTERNOON", "Afternoon"
    NIGHT = "NIGHT", "Night"


# ---------------------------------------------------------------------------
# Level 1 — Master Data
# ---------------------------------------------------------------------------

class ProductionLine(models.Model):
    company = models.ForeignKey(
        'company.Company', on_delete=models.PROTECT,
        related_name='production_lines'
    )
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True, default='')
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']
        unique_together = ('company', 'name')
        verbose_name = 'Production Line'
        verbose_name_plural = 'Production Lines'

    def __str__(self):
        return self.name


class Machine(models.Model):
    company = models.ForeignKey(
        'company.Company', on_delete=models.PROTECT,
        related_name='machines'
    )
    name = models.CharField(max_length=200)
    machine_type = models.CharField(max_length=30, choices=MachineType.choices)
    line = models.ForeignKey(
        ProductionLine, on_delete=models.PROTECT,
        related_name='machines'
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['line', 'name']
        verbose_name = 'Machine'
        verbose_name_plural = 'Machines'

    def __str__(self):
        return f"{self.name} ({self.get_machine_type_display()})"


class MachineChecklistTemplate(models.Model):
    company = models.ForeignKey(
        'company.Company', on_delete=models.PROTECT,
        related_name='checklist_templates'
    )
    machine_type = models.CharField(max_length=30, choices=MachineType.choices)
    task = models.CharField(max_length=500)
    frequency = models.CharField(max_length=10, choices=ChecklistFrequency.choices)
    sort_order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['machine_type', 'frequency', 'sort_order']
        verbose_name = 'Machine Checklist Template'
        verbose_name_plural = 'Machine Checklist Templates'

    def __str__(self):
        return f"{self.get_machine_type_display()} — {self.task[:50]}"


class BreakdownCategory(models.Model):
    """Configurable breakdown types (e.g. INTERNAL, MACHINE, LINE)."""
    company = models.ForeignKey(
        'company.Company', on_delete=models.PROTECT,
        related_name='breakdown_categories'
    )
    name = models.CharField(max_length=100)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']
        unique_together = ('company', 'name')
        verbose_name = 'Breakdown Category'
        verbose_name_plural = 'Breakdown Categories'

    def __str__(self):
        return self.name


class LineSkuConfig(models.Model):
    """
    Predefined configuration preset for a production line.
    Multiple configs can exist per line (e.g., different SKUs, shifts, speeds).
    Users pick a config when starting a production run.
    """
    company = models.ForeignKey(
        'company.Company', on_delete=models.PROTECT,
        related_name='line_sku_configs'
    )
    line = models.ForeignKey(
        ProductionLine, on_delete=models.PROTECT,
        related_name='sku_configs'
    )
    config_name = models.CharField(
        max_length=300, default='Default',
        help_text="Display name for this preset (e.g., 'Olive Oil 1L - Day Shift')"
    )
    sku_code = models.CharField(
        max_length=100, blank=True, default='',
        help_text="SAP Item Code (SKU). Optional."
    )
    sku_name = models.CharField(
        max_length=300, blank=True, default='',
        help_text="Product / SKU name"
    )
    rated_speed = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        help_text="Rated speed in cases/hr"
    )
    labour_count = models.PositiveIntegerField(
        default=0, help_text="Standard labour count"
    )
    other_manpower_count = models.PositiveIntegerField(
        default=0, help_text="Standard other manpower count"
    )
    supervisor = models.CharField(
        max_length=200, blank=True, default='',
        help_text="Default supervisor name"
    )
    operators = models.CharField(
        max_length=500, blank=True, default='',
        help_text="Default engineer/operator names"
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['line', 'config_name']
        verbose_name = 'Line Config'
        verbose_name_plural = 'Line Configs'

    def __str__(self):
        return f"{self.line.name} — {self.config_name}"


# ---------------------------------------------------------------------------
# Level 2 — Transaction Data
# ---------------------------------------------------------------------------

class ProductionRun(models.Model):
    company = models.ForeignKey(
        'company.Company', on_delete=models.PROTECT,
        related_name='production_runs'
    )
    sap_doc_entry = models.IntegerField(
        null=True, blank=True,
        help_text="SAP OWOR DocEntry — links run to SAP production order"
    )
    run_number = models.PositiveSmallIntegerField()
    date = models.DateField()
    line = models.ForeignKey(
        ProductionLine, on_delete=models.PROTECT,
        related_name='production_runs'
    )
    product = models.CharField(
        max_length=200, blank=True, default='',
        help_text="Product name (auto-filled from SAP ItemName)"
    )
    required_qty = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        help_text="Required production quantity — BOM scales to this"
    )
    warehouse_approval_status = models.CharField(
        max_length=25,
        choices=[
            ('NOT_REQUESTED', 'Not Requested'),
            ('PENDING', 'Pending'),
            ('APPROVED', 'Approved'),
            ('PARTIALLY_APPROVED', 'Partially Approved'),
            ('REJECTED', 'Rejected'),
        ],
        default='NOT_REQUESTED',
        help_text="Warehouse BOM approval status"
    )
    rated_speed = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        help_text="Rated speed (cases/hr)"
    )
    machines = models.ManyToManyField(
        Machine, blank=True, related_name='production_runs',
        help_text="Machines used in this run"
    )
    labour_count = models.PositiveIntegerField(
        default=0, help_text="Number of labourers"
    )
    other_manpower_count = models.PositiveIntegerField(
        default=0, help_text="Other manpower count"
    )
    supervisor = models.CharField(max_length=200, blank=True, default='')
    operators = models.CharField(
        max_length=500, blank=True, default='',
        help_text="Engineer/operator names"
    )

    # Summary fields (auto-computed from child records)
    total_production = models.DecimalField(
        max_digits=12, decimal_places=1, default=0,
        help_text="Total cases produced (entered at completion)"
    )
    total_running_minutes = models.PositiveIntegerField(
        default=0, help_text="Total running minutes from segments"
    )
    total_breakdown_time = models.PositiveIntegerField(
        default=0, help_text="Total breakdown minutes"
    )

    rejected_qty = models.DecimalField(
        max_digits=12, decimal_places=1, default=0,
        help_text="Rejected quantity from QC failures"
    )
    reworked_qty = models.DecimalField(
        max_digits=12, decimal_places=1, default=0,
        help_text="Reworked quantity from QC failures"
    )

    # SAP Goods Receipt sync fields
    sap_receipt_doc_entry = models.IntegerField(
        null=True, blank=True,
        help_text="SAP Goods Receipt DocEntry after successful post"
    )
    sap_sync_status = models.CharField(
        max_length=20,
        choices=[
            ('NOT_APPLICABLE', 'Not Applicable'),
            ('PENDING', 'Pending'),
            ('SUCCESS', 'Success'),
            ('FAILED', 'Failed'),
        ],
        default='NOT_APPLICABLE',
        help_text="SAP goods receipt sync status"
    )
    sap_sync_error = models.TextField(
        blank=True, default='',
        help_text="Error message if SAP goods receipt post fails"
    )

    status = models.CharField(
        max_length=20, choices=RunStatus.choices, default=RunStatus.DRAFT
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='created_production_runs'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-date', 'line', 'run_number']
        unique_together = ('company', 'date', 'run_number')
        verbose_name = 'Production Run'
        verbose_name_plural = 'Production Runs'
        permissions = [
            ('can_manage_production_lines', 'Can manage production lines'),
            ('can_manage_machines', 'Can manage machines'),
            ('can_manage_checklist_templates', 'Can manage checklist templates'),
            ('can_view_production_run', 'Can view production runs'),
            ('can_create_production_run', 'Can create production runs'),
            ('can_edit_production_run', 'Can edit production runs'),
            ('can_complete_production_run', 'Can complete production runs'),
            ('can_view_production_log', 'Can view production logs'),
            ('can_edit_production_log', 'Can create/edit production logs'),
            ('can_view_breakdown', 'Can view breakdowns'),
            ('can_create_breakdown', 'Can create breakdowns'),
            ('can_edit_breakdown', 'Can edit breakdowns'),
            ('can_view_material_usage', 'Can view material usage'),
            ('can_create_material_usage', 'Can create material usage'),
            ('can_edit_material_usage', 'Can edit material usage'),
            ('can_view_machine_runtime', 'Can view machine runtime'),
            ('can_create_machine_runtime', 'Can create machine runtime'),
            ('can_view_manpower', 'Can view manpower'),
            ('can_create_manpower', 'Can create manpower'),
            ('can_view_line_clearance', 'Can view line clearance'),
            ('can_create_line_clearance', 'Can create line clearance'),
            ('can_approve_line_clearance_qa', 'Can QA-approve line clearance'),
            ('can_view_machine_checklist', 'Can view machine checklists'),
            ('can_create_machine_checklist', 'Can create machine checklist entries'),
            ('can_view_waste_log', 'Can view waste logs'),
            ('can_create_waste_log', 'Can create waste logs'),
            ('can_approve_waste_engineer', 'Can engineer-approve waste'),
            ('can_approve_waste_am', 'Can AM-approve waste'),
            ('can_approve_waste_store', 'Can store-approve waste'),
            ('can_approve_waste_hod', 'Can HOD-approve waste'),
            ('can_view_reports', 'Can view production reports'),
        ]

    def __str__(self):
        return f"Run #{self.run_number} — {self.date} — {self.line.name}"


class ProductionSegment(models.Model):
    """A running period within a production run. Closed when a breakdown occurs
    or the run is completed."""
    production_run = models.ForeignKey(
        ProductionRun, on_delete=models.CASCADE, related_name='segments'
    )
    start_time = models.DateTimeField()
    end_time = models.DateTimeField(null=True, blank=True)
    produced_cases = models.DecimalField(
        max_digits=12, decimal_places=1, default=0,
        help_text="Cases produced during this running period"
    )
    is_active = models.BooleanField(
        default=True, help_text="True if this segment is currently running"
    )
    remarks = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['start_time']
        verbose_name = 'Production Segment'
        verbose_name_plural = 'Production Segments'

    @property
    def duration_minutes(self):
        if self.end_time and self.start_time:
            return int((self.end_time - self.start_time).total_seconds() / 60)
        return 0

    def __str__(self):
        status = "ACTIVE" if self.is_active else "CLOSED"
        return f"Segment {status} — {self.produced_cases} cases"


class MachineBreakdown(models.Model):
    production_run = models.ForeignKey(
        ProductionRun, on_delete=models.CASCADE, related_name='breakdowns'
    )
    machine = models.ForeignKey(
        Machine, on_delete=models.PROTECT, related_name='breakdowns'
    )
    start_time = models.DateTimeField()
    end_time = models.DateTimeField(null=True, blank=True)
    breakdown_minutes = models.PositiveIntegerField(default=0)
    breakdown_category = models.ForeignKey(
        BreakdownCategory, on_delete=models.PROTECT,
        related_name='breakdowns',
        null=True, blank=True,
        help_text="Configurable breakdown type"
    )
    is_active = models.BooleanField(
        default=True, help_text="True if breakdown is ongoing"
    )
    is_unrecovered = models.BooleanField(default=False)
    reason = models.CharField(max_length=500)
    remarks = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['start_time']
        verbose_name = 'Machine Breakdown'
        verbose_name_plural = 'Machine Breakdowns'

    def __str__(self):
        return f"{self.machine.name} — {self.reason[:50]}"


class ProductionMaterialUsage(models.Model):
    production_run = models.ForeignKey(
        ProductionRun, on_delete=models.CASCADE, related_name='material_usages'
    )
    material_code = models.CharField(max_length=50, blank=True, default='')
    material_name = models.CharField(max_length=255)
    opening_qty = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    issued_qty = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    closing_qty = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    wastage_qty = models.DecimalField(
        max_digits=12, decimal_places=3, default=0,
        help_text="Calculated: opening + issued - closing"
    )
    uom = models.CharField(max_length=20, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['material_name']
        verbose_name = 'Production Material Usage'
        verbose_name_plural = 'Production Material Usages'

    def __str__(self):
        return f"{self.material_name} — {self.opening_qty} {self.uom}"


class MachineRuntime(models.Model):
    production_run = models.ForeignKey(
        ProductionRun, on_delete=models.CASCADE, related_name='machine_runtimes'
    )
    machine = models.ForeignKey(
        Machine, on_delete=models.PROTECT, related_name='runtimes',
        null=True, blank=True
    )
    machine_type = models.CharField(max_length=30, choices=MachineType.choices)
    runtime_minutes = models.PositiveIntegerField(default=0)
    downtime_minutes = models.PositiveIntegerField(default=0)
    remarks = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['machine_type']
        verbose_name = 'Machine Runtime'
        verbose_name_plural = 'Machine Runtimes'

    def __str__(self):
        return f"{self.get_machine_type_display()} — {self.runtime_minutes}min"


class ProductionManpower(models.Model):
    production_run = models.ForeignKey(
        ProductionRun, on_delete=models.CASCADE, related_name='manpower_entries'
    )
    shift = models.CharField(max_length=20, choices=ShiftChoice.choices)
    worker_count = models.PositiveIntegerField(default=0)
    supervisor = models.CharField(max_length=200, blank=True, default='')
    engineer = models.CharField(max_length=200, blank=True, default='')
    remarks = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['shift']
        unique_together = ('production_run', 'shift')
        verbose_name = 'Production Manpower'
        verbose_name_plural = 'Production Manpower'

    def __str__(self):
        return f"{self.get_shift_display()} — {self.worker_count} workers"


# ---------------------------------------------------------------------------
# Level 3 — Quality & Maintenance
# ---------------------------------------------------------------------------

class LineClearance(models.Model):
    company = models.ForeignKey(
        'company.Company', on_delete=models.PROTECT,
        related_name='line_clearances'
    )
    production_run = models.ForeignKey(
        ProductionRun, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='line_clearances',
        help_text="Links clearance to a specific production run"
    )
    date = models.DateField()
    line = models.ForeignKey(
        ProductionLine, on_delete=models.PROTECT,
        related_name='line_clearances'
    )
    document_id = models.CharField(
        max_length=50, blank=True, default='',
        help_text="e.g., PRD-OIL-FRM-15-00-00-04"
    )

    verified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='verified_clearances'
    )
    qa_approved = models.BooleanField(default=False)
    qa_approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='qa_approved_clearances'
    )
    qa_approved_at = models.DateTimeField(null=True, blank=True)

    all_checks_passed = models.BooleanField(default=False)
    production_supervisor_sign = models.CharField(
        max_length=200, blank=True, default=''
    )

    status = models.CharField(
        max_length=20, choices=ClearanceStatus.choices,
        default=ClearanceStatus.DRAFT
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='created_clearances'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-date']
        verbose_name = 'Line Clearance'
        verbose_name_plural = 'Line Clearances'

    def __str__(self):
        return f"Clearance — {self.line.name} — {self.date}"


class LineClearanceItem(models.Model):
    clearance = models.ForeignKey(
        LineClearance, on_delete=models.CASCADE, related_name='items'
    )
    checkpoint = models.CharField(max_length=500)
    sort_order = models.PositiveIntegerField(default=0)
    result = models.CharField(
        max_length=5, choices=ClearanceResult.choices,
        default=ClearanceResult.NA
    )
    remarks = models.TextField(blank=True, default='')

    class Meta:
        ordering = ['sort_order']
        verbose_name = 'Line Clearance Item'
        verbose_name_plural = 'Line Clearance Items'

    def __str__(self):
        return f"{self.sort_order}. {self.checkpoint[:50]}"


class MachineChecklistEntry(models.Model):
    company = models.ForeignKey(
        'company.Company', on_delete=models.PROTECT,
        related_name='machine_checklist_entries'
    )
    machine = models.ForeignKey(
        Machine, on_delete=models.PROTECT, related_name='checklist_entries'
    )
    machine_type = models.CharField(max_length=30, choices=MachineType.choices)
    date = models.DateField()
    month = models.PositiveSmallIntegerField()
    year = models.PositiveSmallIntegerField()
    template = models.ForeignKey(
        MachineChecklistTemplate, on_delete=models.PROTECT,
        related_name='entries'
    )
    task_description = models.CharField(max_length=500)
    frequency = models.CharField(
        max_length=10, choices=ChecklistFrequency.choices
    )
    status = models.CharField(
        max_length=10, choices=ChecklistStatus.choices,
        default=ChecklistStatus.NA
    )
    operator = models.CharField(max_length=200, blank=True, default='')
    shift_incharge = models.CharField(max_length=200, blank=True, default='')
    remarks = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['date', 'machine_type']
        unique_together = ('machine', 'template', 'date')
        verbose_name = 'Machine Checklist Entry'
        verbose_name_plural = 'Machine Checklist Entries'

    def __str__(self):
        return f"{self.machine.name} — {self.task_description[:50]} — {self.date}"


class WasteLog(models.Model):
    production_run = models.ForeignKey(
        ProductionRun, on_delete=models.CASCADE, related_name='waste_logs'
    )
    material_code = models.CharField(max_length=50, blank=True, default='')
    material_name = models.CharField(max_length=255)
    wastage_qty = models.DecimalField(max_digits=12, decimal_places=3)
    uom = models.CharField(max_length=20, blank=True, default='')
    reason = models.TextField(blank=True, default='')

    # Sequential approval: Engineer -> AM -> Store -> HOD
    engineer_sign = models.CharField(max_length=200, blank=True, default='')
    engineer_signed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='engineer_signed_waste'
    )
    engineer_signed_at = models.DateTimeField(null=True, blank=True)

    am_sign = models.CharField(max_length=200, blank=True, default='')
    am_signed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='am_signed_waste'
    )
    am_signed_at = models.DateTimeField(null=True, blank=True)

    store_sign = models.CharField(max_length=200, blank=True, default='')
    store_signed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='store_signed_waste'
    )
    store_signed_at = models.DateTimeField(null=True, blank=True)

    hod_sign = models.CharField(max_length=200, blank=True, default='')
    hod_signed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='hod_signed_waste'
    )
    hod_signed_at = models.DateTimeField(null=True, blank=True)

    wastage_approval_status = models.CharField(
        max_length=20, choices=WasteApprovalStatus.choices,
        default=WasteApprovalStatus.PENDING
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Waste Log'
        verbose_name_plural = 'Waste Logs'

    def __str__(self):
        return f"{self.material_name} — {self.wastage_qty} {self.uom}"


# ---------------------------------------------------------------------------
# Resource Tracking Models
# ---------------------------------------------------------------------------

class ResourceElectricity(models.Model):
    production_run = models.ForeignKey(
        ProductionRun, on_delete=models.CASCADE, related_name='electricity_usage'
    )
    description = models.CharField(max_length=200, blank=True, default='')
    units_consumed = models.DecimalField(max_digits=12, decimal_places=3)
    rate_per_unit = models.DecimalField(max_digits=12, decimal_places=4)
    total_cost = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='electricity_entries'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Electricity Usage'
        verbose_name_plural = 'Electricity Usages'

    def save(self, *args, **kwargs):
        from decimal import Decimal
        self.total_cost = Decimal(str(self.units_consumed)) * Decimal(str(self.rate_per_unit))
        super().save(*args, **kwargs)

    def __str__(self):
        return f"Electricity — {self.units_consumed} units @ {self.rate_per_unit}"


class ResourceWater(models.Model):
    production_run = models.ForeignKey(
        ProductionRun, on_delete=models.CASCADE, related_name='water_usage'
    )
    description = models.CharField(max_length=200, blank=True, default='')
    volume_consumed = models.DecimalField(max_digits=12, decimal_places=3)
    rate_per_unit = models.DecimalField(max_digits=12, decimal_places=4)
    total_cost = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='water_entries'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Water Usage'
        verbose_name_plural = 'Water Usages'

    def save(self, *args, **kwargs):
        from decimal import Decimal
        self.total_cost = Decimal(str(self.volume_consumed)) * Decimal(str(self.rate_per_unit))
        super().save(*args, **kwargs)

    def __str__(self):
        return f"Water — {self.volume_consumed} L @ {self.rate_per_unit}"


class ResourceGas(models.Model):
    production_run = models.ForeignKey(
        ProductionRun, on_delete=models.CASCADE, related_name='gas_usage'
    )
    description = models.CharField(max_length=200, blank=True, default='')
    qty_consumed = models.DecimalField(max_digits=12, decimal_places=3)
    rate_per_unit = models.DecimalField(max_digits=12, decimal_places=4)
    total_cost = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='gas_entries'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Gas Usage'
        verbose_name_plural = 'Gas Usages'

    def save(self, *args, **kwargs):
        from decimal import Decimal
        self.total_cost = Decimal(str(self.qty_consumed)) * Decimal(str(self.rate_per_unit))
        super().save(*args, **kwargs)

    def __str__(self):
        return f"Gas — {self.qty_consumed} units @ {self.rate_per_unit}"


class ResourceCompressedAir(models.Model):
    production_run = models.ForeignKey(
        ProductionRun, on_delete=models.CASCADE, related_name='compressed_air_usage'
    )
    description = models.CharField(max_length=200, blank=True, default='')
    units_consumed = models.DecimalField(max_digits=12, decimal_places=3)
    rate_per_unit = models.DecimalField(max_digits=12, decimal_places=4)
    total_cost = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='compressed_air_entries'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Compressed Air Usage'
        verbose_name_plural = 'Compressed Air Usages'

    def save(self, *args, **kwargs):
        from decimal import Decimal
        self.total_cost = Decimal(str(self.units_consumed)) * Decimal(str(self.rate_per_unit))
        super().save(*args, **kwargs)

    def __str__(self):
        return f"Compressed Air — {self.units_consumed} units @ {self.rate_per_unit}"


class ResourceLabour(models.Model):
    production_run = models.ForeignKey(
        ProductionRun, on_delete=models.CASCADE, related_name='labour_entries'
    )
    description = models.CharField(
        max_length=200, blank=True, default='',
        help_text="e.g., Skilled labourers, Helpers"
    )
    worker_count = models.PositiveIntegerField(default=1)
    hours_worked = models.DecimalField(max_digits=8, decimal_places=2)
    rate_per_hour = models.DecimalField(max_digits=12, decimal_places=4)
    total_cost = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='labour_entries_created'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Labour Entry'
        verbose_name_plural = 'Labour Entries'

    def save(self, *args, **kwargs):
        from decimal import Decimal
        self.total_cost = (
            Decimal(str(self.worker_count))
            * Decimal(str(self.hours_worked))
            * Decimal(str(self.rate_per_hour))
        )
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.worker_count} workers @ {self.rate_per_hour}/hr — {self.description}"


class ResourceMachineCost(models.Model):
    production_run = models.ForeignKey(
        ProductionRun, on_delete=models.CASCADE, related_name='machine_cost_entries'
    )
    machine_name = models.CharField(max_length=200)
    hours_used = models.DecimalField(max_digits=8, decimal_places=2)
    rate_per_hour = models.DecimalField(max_digits=12, decimal_places=4)
    total_cost = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='machine_cost_entries_created'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Machine Cost Entry'
        verbose_name_plural = 'Machine Cost Entries'

    def save(self, *args, **kwargs):
        from decimal import Decimal
        self.total_cost = Decimal(str(self.hours_used)) * Decimal(str(self.rate_per_hour))
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.machine_name} — {self.hours_used}h @ {self.rate_per_hour}"


class ResourceOverhead(models.Model):
    production_run = models.ForeignKey(
        ProductionRun, on_delete=models.CASCADE, related_name='overhead_entries'
    )
    expense_name = models.CharField(max_length=200)
    amount = models.DecimalField(max_digits=15, decimal_places=2)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='overhead_entries_created'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Overhead Entry'
        verbose_name_plural = 'Overhead Entries'

    def __str__(self):
        return f"{self.expense_name} — {self.amount}"


# ---------------------------------------------------------------------------
# Cost Management Model
# ---------------------------------------------------------------------------

class ProductionRunCost(models.Model):
    production_run = models.OneToOneField(
        ProductionRun, on_delete=models.CASCADE, related_name='cost_summary'
    )
    raw_material_cost = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    labour_cost = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    machine_cost = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    electricity_cost = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    water_cost = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    gas_cost = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    compressed_air_cost = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    overhead_cost = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    total_cost = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    produced_qty = models.DecimalField(max_digits=15, decimal_places=3, default=0)
    per_unit_cost = models.DecimalField(max_digits=15, decimal_places=4, default=0)
    calculated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Production Run Cost'
        verbose_name_plural = 'Production Run Costs'

    def __str__(self):
        return f"Cost for Run #{self.production_run.run_number} — Per Unit: {self.per_unit_cost}"


# ---------------------------------------------------------------------------
# QC Check Models
# ---------------------------------------------------------------------------

class QCResult(models.TextChoices):
    PASS = "PASS", "Pass"
    FAIL = "FAIL", "Fail"
    NA = "NA", "N/A"


class FinalQCResult(models.TextChoices):
    PASS = "PASS", "Pass"
    FAIL = "FAIL", "Fail"
    CONDITIONAL = "CONDITIONAL", "Conditional"


class InProcessQCCheck(models.Model):
    production_run = models.ForeignKey(
        ProductionRun, on_delete=models.CASCADE, related_name='inprocess_qc_checks'
    )
    checked_at = models.DateTimeField()
    parameter = models.CharField(max_length=200)
    acceptable_min = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True)
    acceptable_max = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True)
    actual_value = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True)
    result = models.CharField(max_length=10, choices=QCResult.choices, default=QCResult.NA)
    remarks = models.TextField(blank=True, default='')
    checked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='inprocess_qc_checks'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['checked_at']
        verbose_name = 'In-Process QC Check'
        verbose_name_plural = 'In-Process QC Checks'

    def __str__(self):
        return f"{self.parameter} — {self.result} @ {self.checked_at}"


class FinalQCCheck(models.Model):
    production_run = models.OneToOneField(
        ProductionRun, on_delete=models.CASCADE, related_name='final_qc'
    )
    checked_at = models.DateTimeField()
    overall_result = models.CharField(
        max_length=15, choices=FinalQCResult.choices, default=FinalQCResult.PASS
    )
    parameters = models.JSONField(
        default=list,
        help_text="List of {name, expected, actual, result} dicts"
    )
    remarks = models.TextField(blank=True, default='')
    checked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='final_qc_checks'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Final QC Check'
        verbose_name_plural = 'Final QC Checks'

    def __str__(self):
        return f"Final QC for Run #{self.production_run.run_number} — {self.overall_result}"
