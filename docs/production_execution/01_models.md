# Production Execution — Data Models

> All models live in `production_execution/models.py`
> All models are company-scoped via FK to `company.Company`
> Follows same patterns as `production_planning/models.py`

---

## Choices / Enums

```python
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
```

---

## Level 1 — Master Data

### ProductionLine

```python
class ProductionLine(models.Model):
    company = models.ForeignKey('company.Company', on_delete=models.PROTECT, related_name='production_lines')
    name = models.CharField(max_length=100)          # e.g., "Line-1", "Line-2"
    description = models.TextField(blank=True, default='')
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']
        unique_together = ('company', 'name')
        verbose_name = 'Production Line'
        verbose_name_plural = 'Production Lines'
```

### Machine

```python
class Machine(models.Model):
    company = models.ForeignKey('company.Company', on_delete=models.PROTECT, related_name='machines')
    name = models.CharField(max_length=200)           # e.g., "10-Head Filler", "Tapping Machine"
    machine_type = models.CharField(max_length=30, choices=MachineType.choices)
    line = models.ForeignKey(ProductionLine, on_delete=models.PROTECT, related_name='machines')
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['line', 'name']
        verbose_name = 'Machine'
        verbose_name_plural = 'Machines'
```

### MachineChecklistTemplate

```python
class MachineChecklistTemplate(models.Model):
    company = models.ForeignKey('company.Company', on_delete=models.PROTECT, related_name='checklist_templates')
    machine_type = models.CharField(max_length=30, choices=MachineType.choices)
    task = models.CharField(max_length=500)            # e.g., "Clean oil storage tank"
    frequency = models.CharField(max_length=10, choices=ChecklistFrequency.choices)
    sort_order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['machine_type', 'frequency', 'sort_order']
        verbose_name = 'Machine Checklist Template'
        verbose_name_plural = 'Machine Checklist Templates'
```

---

## Level 2 — Transaction Data

### ProductionRun

The **central entity** of this module. All hourly logs, breakdowns, material usage, machine runtime, manpower, and waste attach to a Run.

```python
class ProductionRun(models.Model):
    company = models.ForeignKey('company.Company', on_delete=models.PROTECT, related_name='production_runs')
    production_plan = models.ForeignKey(
        'production_planning.ProductionPlan',
        on_delete=models.PROTECT,
        related_name='production_runs'
    )
    run_number = models.PositiveSmallIntegerField()    # Run 1, 2, 3 within a day
    date = models.DateField()
    line = models.ForeignKey(ProductionLine, on_delete=models.PROTECT, related_name='production_runs')
    brand = models.CharField(max_length=200, blank=True, default='')
    pack = models.CharField(max_length=100, blank=True, default='')
    sap_order_no = models.CharField(max_length=50, blank=True, default='')
    rated_speed = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True,
                                       help_text="Rated speed (units/min)")

    # Summary fields (auto-computed from child records)
    total_production = models.PositiveIntegerField(default=0, help_text="Total cases produced")
    total_minutes_pe = models.PositiveIntegerField(default=0, help_text="Total production equipment minutes")
    total_minutes_me = models.PositiveIntegerField(default=0, help_text="Total machine efficiency minutes")
    total_breakdown_time = models.PositiveIntegerField(default=0, help_text="Total breakdown minutes")
    line_breakdown_time = models.PositiveIntegerField(default=0, help_text="Line-specific breakdown minutes")
    external_breakdown_time = models.PositiveIntegerField(default=0, help_text="External breakdown minutes")
    unrecorded_time = models.PositiveIntegerField(default=0, help_text="Unaccounted minutes")

    status = models.CharField(max_length=20, choices=RunStatus.choices, default=RunStatus.DRAFT)
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
            # See 03_permissions.md for full list
        ]
```

### ProductionLog (Hourly Entry)

```python
class ProductionLog(models.Model):
    production_run = models.ForeignKey(ProductionRun, on_delete=models.CASCADE, related_name='logs')
    time_slot = models.CharField(max_length=20)        # e.g., "07:00-08:00"
    time_start = models.TimeField()
    time_end = models.TimeField()
    produced_cases = models.PositiveIntegerField(default=0)
    machine_status = models.CharField(max_length=20, choices=MachineStatus.choices, default=MachineStatus.RUNNING)
    recd_minutes = models.PositiveIntegerField(default=0, help_text="Recorded minutes of production")
    breakdown_detail = models.CharField(max_length=500, blank=True, default='')
    remarks = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['time_start']
        unique_together = ('production_run', 'time_start')
        verbose_name = 'Production Log'
        verbose_name_plural = 'Production Logs'
```

### MachineBreakdown

```python
class MachineBreakdown(models.Model):
    production_run = models.ForeignKey(ProductionRun, on_delete=models.CASCADE, related_name='breakdowns')
    machine = models.ForeignKey(Machine, on_delete=models.PROTECT, related_name='breakdowns')
    start_time = models.DateTimeField()
    end_time = models.DateTimeField(null=True, blank=True)
    breakdown_minutes = models.PositiveIntegerField(default=0)
    type = models.CharField(max_length=10, choices=BreakdownType.choices)
    is_unrecovered = models.BooleanField(default=False)
    reason = models.CharField(max_length=500)          # e.g., "sticker change", "power cut"
    remarks = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['start_time']
        verbose_name = 'Machine Breakdown'
        verbose_name_plural = 'Machine Breakdowns'
```

### ProductionMaterialUsage (Yield Data)

```python
class ProductionMaterialUsage(models.Model):
    production_run = models.ForeignKey(ProductionRun, on_delete=models.CASCADE, related_name='material_usages')
    material_code = models.CharField(max_length=50, blank=True, default='')
    material_name = models.CharField(max_length=255)   # e.g., "Bottle (500gm)", "Cap"
    opening_qty = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    issued_qty = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    closing_qty = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    wastage_qty = models.DecimalField(max_digits=12, decimal_places=3, default=0,
                                       help_text="Calculated: opening + issued - closing - produced_equivalent")
    uom = models.CharField(max_length=20, blank=True, default='')
    batch_number = models.PositiveSmallIntegerField(default=1, help_text="Batch/shift: 1, 2, 3")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['batch_number', 'material_name']
        verbose_name = 'Production Material Usage'
        verbose_name_plural = 'Production Material Usages'
```

**Standard materials tracked per run:**
- Bottle (with gram weight)
- Cap
- Front Label
- Back Label
- Tikki
- Shrink
- Carton

### MachineRuntime

```python
class MachineRuntime(models.Model):
    production_run = models.ForeignKey(ProductionRun, on_delete=models.CASCADE, related_name='machine_runtimes')
    machine = models.ForeignKey(Machine, on_delete=models.PROTECT, related_name='runtimes', null=True, blank=True)
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
```

### ProductionManpower

```python
class ProductionManpower(models.Model):
    production_run = models.ForeignKey(ProductionRun, on_delete=models.CASCADE, related_name='manpower_entries')
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
```

---

## Level 3 — Quality & Maintenance

### LineClearance

```python
class LineClearance(models.Model):
    company = models.ForeignKey('company.Company', on_delete=models.PROTECT, related_name='line_clearances')
    date = models.DateField()
    line = models.ForeignKey(ProductionLine, on_delete=models.PROTECT, related_name='line_clearances')
    production_plan = models.ForeignKey(
        'production_planning.ProductionPlan',
        on_delete=models.PROTECT,
        related_name='line_clearances'
    )
    document_id = models.CharField(max_length=50, blank=True, default='',
                                    help_text="e.g., PRD-OIL-FRM-15-00-00-04")

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

    production_supervisor_sign = models.CharField(max_length=200, blank=True, default='')
    production_incharge_sign = models.CharField(max_length=200, blank=True, default='')

    status = models.CharField(max_length=20, choices=ClearanceStatus.choices, default=ClearanceStatus.DRAFT)
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
```

### LineClearanceItem

```python
class LineClearanceItem(models.Model):
    clearance = models.ForeignKey(LineClearance, on_delete=models.CASCADE, related_name='items')
    checkpoint = models.CharField(max_length=500)
    sort_order = models.PositiveIntegerField(default=0)
    result = models.CharField(max_length=5, choices=ClearanceResult.choices, default=ClearanceResult.NA)
    remarks = models.TextField(blank=True, default='')

    class Meta:
        ordering = ['sort_order']
        verbose_name = 'Line Clearance Item'
        verbose_name_plural = 'Line Clearance Items'
```

**Standard checklist items (auto-created when a clearance is created):**

1. Previous product, labels and packaging materials removed
2. Machine/equipment cleaned and free from product residues
3. Utensils, scoops and accessories cleaned and available
4. Packaging area free from previous batch coding material
5. Work area (tables, conveyors, floor) cleaned and sanitized
6. Waste bins emptied and cleaned
7. Required packaging material verified against BOM
8. Coding machine updated with correct product/batch details
9. Environmental conditions (temperature/humidity) within limits

### MachineChecklistEntry

```python
class MachineChecklistEntry(models.Model):
    company = models.ForeignKey('company.Company', on_delete=models.PROTECT, related_name='machine_checklist_entries')
    machine = models.ForeignKey(Machine, on_delete=models.PROTECT, related_name='checklist_entries')
    machine_type = models.CharField(max_length=30, choices=MachineType.choices)
    date = models.DateField()
    month = models.PositiveSmallIntegerField()
    year = models.PositiveSmallIntegerField()
    template = models.ForeignKey(MachineChecklistTemplate, on_delete=models.PROTECT, related_name='entries')
    task_description = models.CharField(max_length=500)  # Denormalized from template
    frequency = models.CharField(max_length=10, choices=ChecklistFrequency.choices)
    status = models.CharField(max_length=10, choices=ChecklistStatus.choices, default=ChecklistStatus.NA)
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
```

### WasteLog

```python
class WasteLog(models.Model):
    production_run = models.ForeignKey(ProductionRun, on_delete=models.CASCADE, related_name='waste_logs')
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
        max_length=20, choices=WasteApprovalStatus.choices, default=WasteApprovalStatus.PENDING
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Waste Log'
        verbose_name_plural = 'Waste Logs'
```

---

## Entity Relationship Summary

```
company.Company
    ├── ProductionLine
    │       └── Machine
    ├── MachineChecklistTemplate
    ├── MachineChecklistEntry
    ├── LineClearance
    │       └── LineClearanceItem
    └── ProductionRun
            ├── ProductionLog (12 hourly entries)
            ├── MachineBreakdown (0..N)
            ├── ProductionMaterialUsage (7+ per batch, up to 3 batches)
            ├── MachineRuntime (8 machine types)
            ├── ProductionManpower (1 per shift)
            └── WasteLog (0..N)
```
