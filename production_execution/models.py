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


# ---------------------------------------------------------------------------
# Level 2 — Transaction Data
# ---------------------------------------------------------------------------

class ProductionRun(models.Model):
    company = models.ForeignKey(
        'company.Company', on_delete=models.PROTECT,
        related_name='production_runs'
    )
    production_plan = models.ForeignKey(
        'production_planning.ProductionPlan',
        on_delete=models.PROTECT,
        related_name='production_runs'
    )
    run_number = models.PositiveSmallIntegerField()
    date = models.DateField()
    line = models.ForeignKey(
        ProductionLine, on_delete=models.PROTECT,
        related_name='production_runs'
    )
    brand = models.CharField(max_length=200, blank=True, default='')
    pack = models.CharField(max_length=100, blank=True, default='')
    sap_order_no = models.CharField(max_length=50, blank=True, default='')
    rated_speed = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        help_text="Rated speed (units/min)"
    )

    # Summary fields (auto-computed from child records)
    total_production = models.PositiveIntegerField(
        default=0, help_text="Total cases produced"
    )
    total_minutes_pe = models.PositiveIntegerField(
        default=0, help_text="Total production equipment minutes"
    )
    total_minutes_me = models.PositiveIntegerField(
        default=0, help_text="Total machine efficiency minutes"
    )
    total_breakdown_time = models.PositiveIntegerField(
        default=0, help_text="Total breakdown minutes"
    )
    line_breakdown_time = models.PositiveIntegerField(
        default=0, help_text="Line-specific breakdown minutes"
    )
    external_breakdown_time = models.PositiveIntegerField(
        default=0, help_text="External breakdown minutes"
    )
    unrecorded_time = models.PositiveIntegerField(
        default=0, help_text="Unaccounted minutes"
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
        unique_together = ('company', 'production_plan', 'date', 'run_number')
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


class ProductionLog(models.Model):
    production_run = models.ForeignKey(
        ProductionRun, on_delete=models.CASCADE, related_name='logs'
    )
    time_slot = models.CharField(max_length=20)
    time_start = models.TimeField()
    time_end = models.TimeField()
    produced_cases = models.PositiveIntegerField(default=0)
    machine_status = models.CharField(
        max_length=20, choices=MachineStatus.choices, default=MachineStatus.RUNNING
    )
    recd_minutes = models.PositiveIntegerField(
        default=0, help_text="Recorded minutes of production"
    )
    breakdown_detail = models.CharField(max_length=500, blank=True, default='')
    remarks = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['time_start']
        unique_together = ('production_run', 'time_start')
        verbose_name = 'Production Log'
        verbose_name_plural = 'Production Logs'

    def __str__(self):
        return f"{self.time_slot} — {self.produced_cases} cases"


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
    type = models.CharField(max_length=10, choices=BreakdownType.choices)
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
    batch_number = models.PositiveSmallIntegerField(
        default=1, help_text="Batch/shift: 1, 2, 3"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['batch_number', 'material_name']
        verbose_name = 'Production Material Usage'
        verbose_name_plural = 'Production Material Usages'

    def __str__(self):
        return f"{self.material_name} (Batch {self.batch_number})"


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
    date = models.DateField()
    line = models.ForeignKey(
        ProductionLine, on_delete=models.PROTECT,
        related_name='line_clearances'
    )
    production_plan = models.ForeignKey(
        'production_planning.ProductionPlan',
        on_delete=models.PROTECT,
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

    production_supervisor_sign = models.CharField(
        max_length=200, blank=True, default=''
    )
    production_incharge_sign = models.CharField(
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
