"""
Blowing module — bottle-making from preform on blowing machines.

Each ``BlowingRun`` is one machine + preform-spec session on a date (the analogue
of a spreadsheet row in Linear.xlsx). Costs are auto-computed from a governed
``BlowingRateConfig`` snapshot copied onto the run; see services/cost_calculator.py.
"""
from decimal import Decimal

from django.conf import settings
from django.db import models

from gate_core.models.base import BaseModel
from company.models import Company


class RunStatus(models.TextChoices):
    DRAFT = 'DRAFT', 'Draft'
    IN_PROGRESS = 'IN_PROGRESS', 'In Progress'
    COMPLETED = 'COMPLETED', 'Completed'


class WarehouseApprovalStatus(models.TextChoices):
    NOT_REQUESTED = 'NOT_REQUESTED', 'Not Requested'
    PENDING = 'PENDING', 'Pending'
    APPROVED = 'APPROVED', 'Approved'
    PARTIALLY_APPROVED = 'PARTIALLY_APPROVED', 'Partially Approved'
    REJECTED = 'REJECTED', 'Rejected'


# ===========================================================================
# Master / configuration data
# ===========================================================================

class BlowingMachine(BaseModel):
    """A blowing machine. Currently one per company (oil / beverages)."""
    company = models.ForeignKey(
        Company, on_delete=models.PROTECT, related_name='blowing_machines'
    )
    name = models.CharField(max_length=150)
    heads = models.PositiveSmallIntegerField(
        null=True, blank=True, help_text='Number of blowing heads (bottles per cycle)'
    )
    sap_warehouse_code = models.CharField(
        max_length=20, blank=True, default='',
        help_text='SAP production warehouse for this machine (later postings phase)'
    )
    depreciation_per_day = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0'),
        help_text='Straight-line machine depreciation per operating day (fixed cost)'
    )

    class Meta:
        unique_together = ('company', 'name')
        ordering = ['company_id', 'name']
        verbose_name = 'Blowing Machine'
        verbose_name_plural = 'Blowing Machines'

    def __str__(self):
        return f"{self.company.code} — {self.name}"


class PreformSpec(BaseModel):
    """A preform variant (make + gram) with its box conversion and SAP mapping."""
    company = models.ForeignKey(
        Company, on_delete=models.PROTECT, related_name='preform_specs'
    )
    make = models.CharField(max_length=100, help_text='e.g. Frystal')
    gram = models.DecimalField(
        max_digits=6, decimal_places=2, help_text='Preform weight in grams'
    )
    preforms_per_box = models.PositiveIntegerField(
        help_text='Preform pieces in one box (for box -> grams conversion)'
    )
    preform_rate_per_bottle = models.DecimalField(
        max_digits=10, decimal_places=4, default=Decimal('0'),
        help_text='Preform cost per bottle (per piece) — each preform variant has its own rate'
    )
    sap_item_code = models.CharField(max_length=50, blank=True, default='')
    sap_item_name = models.CharField(max_length=200, blank=True, default='')
    # Sheet1 economics (optional reference)
    bottle_weight_g = models.DecimalField(
        max_digits=6, decimal_places=2, null=True, blank=True
    )
    bottles_per_kg = models.DecimalField(
        max_digits=8, decimal_places=2, null=True, blank=True
    )
    # Mould economics — amortized per good bottle (wear-based, ~variable)
    mould_cost = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        help_text='Mould/tooling capital cost for this size'
    )
    mould_life_bottles = models.PositiveIntegerField(
        null=True, blank=True, help_text='Expected mould life in bottles (shots)'
    )
    # Standard (target) values for variance analysis (Phase 3)
    # Stores a BLOWING (conversion) cost target, not a fully-loaded one — the
    # variance report grades `blowing_cost_per_bottle` against it. Column name
    # kept as-is to avoid a rename migration on live data.
    std_make_cost_per_bottle = models.DecimalField(
        max_digits=12, decimal_places=4, null=True, blank=True,
        help_text='Target blowing (conversion) cost per bottle — excludes the preform'
    )
    std_reject_pct = models.DecimalField(
        max_digits=6, decimal_places=3, null=True, blank=True,
        help_text='Target rejection %'
    )
    std_units_per_bottle = models.DecimalField(
        max_digits=10, decimal_places=6, null=True, blank=True,
        help_text='Target electricity units per bottle'
    )

    class Meta:
        unique_together = ('company', 'make', 'gram')
        ordering = ['company_id', 'make', 'gram']
        verbose_name = 'Preform Spec'
        verbose_name_plural = 'Preform Specs'

    def __str__(self):
        return f"{self.make} {self.gram}g ({self.company.code})"


class BlowingRateConfig(BaseModel):
    """
    Governed rate card. The latest active row effective on or before a run's date
    is snapshotted onto the run at creation, so historical runs keep the rates
    they were costed at.
    """
    company = models.ForeignKey(
        Company, on_delete=models.PROTECT, related_name='blowing_rate_configs'
    )
    effective_from = models.DateField()
    operator_rate_per_day = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0'))
    labour_rate_per_day = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0'))
    electricity_rate_per_unit = models.DecimalField(max_digits=10, decimal_places=4, default=Decimal('0'))
    scrap_rate_per_bottle = models.DecimalField(max_digits=10, decimal_places=4, default=Decimal('0'))
    packing_rate_per_bottle = models.DecimalField(max_digits=10, decimal_places=4, default=Decimal('0'))
    # Fixed costs per operating day (absorbed over the day's good output)
    maintenance_per_day = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))
    factory_overhead_per_day = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))
    qa_cost_per_day = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))

    class Meta:
        ordering = ['company_id', '-effective_from']
        verbose_name = 'Blowing Rate Config'
        verbose_name_plural = 'Blowing Rate Configs'

    def __str__(self):
        return f"{self.company.code} rates from {self.effective_from}"


# ===========================================================================
# Cost Master — catalog of per-category rates (replaces BlowingRateConfig).
# Mirrors production_execution.CostRate: company-wide defaults + per-machine
# overrides, resolved live at cost-compute time (not effective-dated).
# ===========================================================================

class BlowingCostCategory(models.TextChoices):
    OPERATOR = 'OPERATOR', 'Operator'
    LABOUR = 'LABOUR', 'Labour (contract + own)'
    ELECTRICITY_MACHINE = 'ELECTRICITY_MACHINE', 'Electricity — Machine (metered)'
    ELECTRICITY_UTILITY = 'ELECTRICITY_UTILITY', 'Electricity — Utility (metered)'
    PACKING = 'PACKING', 'Packing'
    SCRAP_RECOVERY = 'SCRAP_RECOVERY', 'Scrap Recovery'
    # Derived cost line (not a Cost Master rate): rejected bottles' preform value.
    WASTAGE = 'WASTAGE', 'Wastage (rejected preforms)'
    # Not a cost — the editable industry benchmark for blowing cost/bottle.
    BENCHMARK_BLOWING_PER_BOTTLE = 'BENCHMARK_BLOWING_PER_BOTTLE', 'Industry Blowing Cost / Bottle (benchmark)'


class BlowingCostBasis(models.TextChoices):
    PER_DAY = 'PER_DAY', 'Per Day (fixed)'
    PER_PERSON_DAY = 'PER_PERSON_DAY', 'Per Person per Day'
    PER_UNIT = 'PER_UNIT', 'Per Electricity Unit'
    PER_BOTTLE = 'PER_BOTTLE', 'Per Bottle'


class BlowingCostRate(BaseModel):
    """One rate row in the blowing Cost Master. ``machine`` NULL = company-wide
    default; set = per-machine override. Resolved override-first at compute time."""
    company = models.ForeignKey(
        Company, on_delete=models.PROTECT, related_name='blowing_cost_rates'
    )
    machine = models.ForeignKey(
        BlowingMachine, on_delete=models.CASCADE, null=True, blank=True,
        related_name='cost_rates',
        help_text='NULL = company-wide default; set = per-machine override.'
    )
    category = models.CharField(max_length=40, choices=BlowingCostCategory.choices)
    basis = models.CharField(max_length=20, choices=BlowingCostBasis.choices)
    rate = models.DecimalField(max_digits=15, decimal_places=4, default=Decimal('0'))
    is_credit = models.BooleanField(
        default=False, help_text='True if this reduces cost (e.g. scrap recovery).'
    )
    label = models.CharField(max_length=200, blank=True, default='')

    class Meta:
        ordering = ['company_id', 'machine_id', 'category']
        verbose_name = 'Blowing Cost Rate'
        verbose_name_plural = 'Blowing Cost Rates'
        constraints = [
            models.UniqueConstraint(
                fields=['company', 'category'],
                condition=models.Q(machine__isnull=True, is_active=True),
                name='uniq_active_global_blowing_cost_rate_per_category'),
            models.UniqueConstraint(
                fields=['company', 'machine', 'category'],
                condition=models.Q(machine__isnull=False, is_active=True),
                name='uniq_active_machine_blowing_cost_rate_per_category'),
        ]

    def __str__(self):
        scope = self.machine.name if self.machine_id else 'global'
        return f"{self.company.code} {self.category} @ {self.rate} ({scope})"


# ===========================================================================
# Transaction — the run
# ===========================================================================

class BlowingRun(BaseModel):
    company = models.ForeignKey(
        Company, on_delete=models.PROTECT, related_name='blowing_runs'
    )
    run_number = models.PositiveSmallIntegerField(
        help_text='Sequential per company per date'
    )
    date = models.DateField()
    machine = models.ForeignKey(
        BlowingMachine, on_delete=models.PROTECT, related_name='runs'
    )
    preform_spec = models.ForeignKey(
        PreformSpec, on_delete=models.PROTECT, related_name='runs'
    )

    # --- operator inputs -------------------------------------------------
    preform_boxes_used = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0'))
    machine_start_reading = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    machine_stop_reading = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    utility_units = models.DecimalField(
        max_digits=12, decimal_places=4, default=Decimal('0'),
        help_text='Utility electricity units the operator reads per run (metered × ₹/unit).'
    )
    utility_cost = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0'),
        help_text='Deprecated — utility electricity is now metered from utility_units × ₹/unit.'
    )
    total_counter_production = models.PositiveIntegerField(default=0)
    rejection_pcs = models.PositiveIntegerField(default=0)
    operator_count = models.PositiveIntegerField(default=0)
    contract_labour_count = models.PositiveIntegerField(default=0)
    own_labour_count = models.PositiveIntegerField(default=0)
    scrap_carton_value = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal('0'),
        help_text='Manually entered carton-scrap recovery value'
    )
    remarks = models.TextField(blank=True, default='')

    # --- rate snapshot (copied from BlowingRateConfig at creation) -------
    rate_config = models.ForeignKey(
        BlowingRateConfig, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='runs'
    )
    operator_rate_per_day = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0'))
    labour_rate_per_day = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0'))
    electricity_rate_per_unit = models.DecimalField(max_digits=10, decimal_places=4, default=Decimal('0'))
    # snapshotted from the run's PreformSpec (each preform variant has its own rate)
    preform_rate_per_bottle = models.DecimalField(max_digits=10, decimal_places=4, default=Decimal('0'))
    scrap_rate_per_bottle = models.DecimalField(max_digits=10, decimal_places=4, default=Decimal('0'))
    packing_rate_per_bottle = models.DecimalField(max_digits=10, decimal_places=4, default=Decimal('0'))
    # fixed-cost snapshot (from rate config + machine + preform spec at creation)
    maintenance_per_day = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))
    factory_overhead_per_day = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))
    qa_cost_per_day = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))
    machine_depreciation_per_day = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))
    mould_cost = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))
    mould_life_bottles = models.PositiveIntegerField(default=0)

    # --- computed / denormalized (set in save()) ------------------------
    machine_units = models.DecimalField(max_digits=14, decimal_places=4, default=Decimal('0'))
    total_units = models.DecimalField(max_digits=14, decimal_places=4, default=Decimal('0'))
    preform_used_g = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0'))
    rejection_pct = models.DecimalField(max_digits=7, decimal_places=4, default=Decimal('0'))
    total_manpower = models.PositiveIntegerField(default=0)

    # --- SAP link (reserved for later postings phase) -------------------
    sap_preform_item_code = models.CharField(max_length=50, blank=True, default='')
    sap_bottle_item_code = models.CharField(max_length=50, blank=True, default='')

    # --- lifecycle (segments/breakdowns accumulate here) ----------------
    total_running_minutes = models.PositiveIntegerField(default=0)
    total_breakdown_time = models.PositiveIntegerField(default=0)
    warehouse_approval_status = models.CharField(
        max_length=25, choices=WarehouseApprovalStatus.choices,
        default=WarehouseApprovalStatus.NOT_REQUESTED,
    )

    status = models.CharField(max_length=20, choices=RunStatus.choices, default=RunStatus.DRAFT)

    class Meta:
        ordering = ['-date', 'company_id', 'run_number']
        unique_together = ('company', 'date', 'run_number')
        verbose_name = 'Blowing Run'
        verbose_name_plural = 'Blowing Runs'
        indexes = [
            models.Index(fields=['company', 'date']),
            models.Index(fields=['status']),
        ]
        permissions = [
            ('can_view_blowing_run', 'Can view blowing run'),
            ('can_create_blowing_run', 'Can create blowing run'),
            ('can_edit_blowing_run', 'Can edit blowing run'),
            ('can_complete_blowing_run', 'Can complete blowing run'),
            ('can_manage_blowing_machines', 'Can manage blowing machines'),
            ('can_manage_preform_specs', 'Can manage preform specs'),
            ('can_manage_blowing_rate_config', 'Can manage blowing rate config'),
            ('can_view_blowing_reports', 'Can view blowing reports'),
            ('can_create_blowing_breakdown', 'Can create blowing breakdown'),
            ('can_manage_blowing_breakdown_categories', 'Can manage blowing breakdown categories'),
            ('can_request_preform', 'Can request preform from warehouse'),
            ('can_approve_preform_request', 'Can approve preform request'),
        ]

    def __str__(self):
        return f"{self.company.code} {self.date} run {self.run_number}"

    def _recompute_derived(self):
        """Pure-arithmetic quantity fields derived from this run's own inputs."""
        start = self.machine_start_reading
        stop = self.machine_stop_reading
        if start is not None and stop is not None:
            self.machine_units = stop - start
        else:
            self.machine_units = Decimal('0')
        # Both electricity buckets are metered in units.
        self.total_units = self.machine_units + (self.utility_units or Decimal('0'))

        spec = self.preform_spec
        boxes = self.preform_boxes_used or Decimal('0')
        self.preform_used_g = boxes * Decimal(spec.preforms_per_box) * spec.gram

        prod = self.total_counter_production or 0
        if prod > 0:
            self.rejection_pct = (Decimal(self.rejection_pcs) / Decimal(prod)) * Decimal('100')
        else:
            self.rejection_pct = Decimal('0')

        self.total_manpower = (
            self.operator_count + self.contract_labour_count + self.own_labour_count
        )

    def save(self, *args, **kwargs):
        self._recompute_derived()
        super().save(*args, **kwargs)


class BlowingRunCost(models.Model):
    """Auto-computed cost summary for a run (see services/cost_calculator.py)."""
    run = models.OneToOneField(
        BlowingRun, on_delete=models.CASCADE, related_name='cost_summary'
    )
    # --- conversion cost (spreadsheet-compatible) -----------------------
    operator_cost = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0'))
    labour_cost = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0'))
    wastage_cost = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0'))
    electricity_cost = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0'))
    total_cost = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0'))

    scrap_bottle_value = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0'))
    scrap_total = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0'))
    net_cost = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0'))

    blowing_cost_per_bottle = models.DecimalField(max_digits=12, decimal_places=6, default=Decimal('0'))
    packing_cost = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0'))
    per_bottle_cost = models.DecimalField(max_digits=12, decimal_places=6, default=Decimal('0'))

    # --- fully-loaded make cost (Phase 1) -------------------------------
    good_bottles = models.PositiveIntegerField(default=0)
    preform_cost = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0'))
    mould_amortization = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0'))
    machine_depreciation = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0'))
    maintenance_cost = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0'))
    overhead_cost = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0'))
    qa_cost = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0'))
    # fixed/variable split (for breakeven)
    variable_cost_total = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0'))
    fixed_cost_total = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0'))
    fully_loaded_cost = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0'))
    variable_cost_per_bottle = models.DecimalField(max_digits=12, decimal_places=6, default=Decimal('0'))
    make_cost_per_bottle = models.DecimalField(max_digits=12, decimal_places=6, default=Decimal('0'))

    # --- two-bucket model (preform vs blowing) --------------------------
    # Blowing cost = operator + labour + electricity(machine + utility) + wastage
    #                + packing − scrap. Preform cost is the resin (per-bottle) side.
    electricity_machine_cost = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0'))
    electricity_utility_cost = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0'))
    blowing_cost = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0'))
    preform_cost_per_bottle = models.DecimalField(max_digits=12, decimal_places=6, default=Decimal('0'))
    total_per_bottle_cost = models.DecimalField(max_digits=12, decimal_places=6, default=Decimal('0'))
    # Editable industry benchmark for blowing cost/bottle (default 0.50) + market price.
    benchmark_blowing_per_bottle = models.DecimalField(max_digits=12, decimal_places=6, default=Decimal('0'))
    market_price_per_bottle = models.DecimalField(
        max_digits=12, decimal_places=6, null=True, blank=True,
        help_text='Effective landed buy price for the run\'s bottle (null = no buy price set)'
    )

    calculated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Blowing Run Cost'
        verbose_name_plural = 'Blowing Run Costs'

    def __str__(self):
        return f"Cost for run {self.run_id}: net={self.net_cost}"


class BlowingRunCostLine(models.Model):
    """Per-category cost line for a run (mirrors ProductionRunCostLine). Drives
    the expandable 'Blowing cost' breakdown in the UI."""
    run = models.ForeignKey(
        BlowingRun, on_delete=models.CASCADE, related_name='cost_lines'
    )
    category = models.CharField(max_length=40, choices=BlowingCostCategory.choices)
    basis = models.CharField(max_length=20, blank=True, default='')
    quantity = models.DecimalField(max_digits=18, decimal_places=4, default=Decimal('0'))
    rate = models.DecimalField(max_digits=15, decimal_places=4, default=Decimal('0'))
    amount = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal('0'))
    is_credit = models.BooleanField(default=False)
    note = models.CharField(max_length=200, blank=True, default='')

    class Meta:
        ordering = ['run_id', 'category']
        unique_together = ('run', 'category')
        verbose_name = 'Blowing Run Cost Line'
        verbose_name_plural = 'Blowing Run Cost Lines'

    def __str__(self):
        return f"run {self.run_id} {self.category}: {self.amount}"


# ===========================================================================
# Buy side (Phase 2) — landed cost of a purchased finished bottle
# ===========================================================================

class BottleBuyPrice(BaseModel):
    """
    Purchase (outsource) price for a finished blown bottle of a given size, used
    for the make-vs-buy comparison. Landed cost = price + freight + duties +
    carrying + QA allowance + risk premium.
    """
    company = models.ForeignKey(
        Company, on_delete=models.PROTECT, related_name='bottle_buy_prices'
    )
    preform_spec = models.ForeignKey(
        PreformSpec, on_delete=models.PROTECT, related_name='buy_prices',
        help_text='The bottle size this quote is for'
    )
    supplier_name = models.CharField(max_length=200, blank=True, default='')
    effective_from = models.DateField()
    buy_price = models.DecimalField(
        max_digits=12, decimal_places=4, help_text='Supplier unit price per bottle'
    )
    freight_per_bottle = models.DecimalField(max_digits=12, decimal_places=4, default=Decimal('0'))
    duties_per_bottle = models.DecimalField(max_digits=12, decimal_places=4, default=Decimal('0'))
    carrying_pct_annual = models.DecimalField(
        max_digits=6, decimal_places=2, default=Decimal('0'),
        help_text='Inventory carrying cost %/yr (typ. 18-25)'
    )
    inventory_days = models.PositiveIntegerField(
        default=30, help_text='Avg days of bottle inventory held (for carrying cost)'
    )
    qa_allowance_pct = models.DecimalField(
        max_digits=6, decimal_places=2, default=Decimal('0'),
        help_text='Incoming reject / QA allowance %'
    )
    risk_premium_per_bottle = models.DecimalField(max_digits=12, decimal_places=4, default=Decimal('0'))

    class Meta:
        ordering = ['company_id', 'preform_spec_id', '-effective_from']
        verbose_name = 'Bottle Buy Price'
        verbose_name_plural = 'Bottle Buy Prices'

    def __str__(self):
        return f"{self.preform_spec} buy @ {self.buy_price} ({self.company.code})"

    @property
    def landed_cost_per_bottle(self) -> Decimal:
        price = self.buy_price + self.freight_per_bottle + self.duties_per_bottle
        carrying = price * (self.carrying_pct_annual / Decimal('100')) * (Decimal(self.inventory_days) / Decimal('365'))
        qa = price * (self.qa_allowance_pct / Decimal('100'))
        return price + carrying + qa + self.risk_premium_per_bottle


class BlowingAuditLog(models.Model):
    """
    Lightweight edit trail for master data without effective-date history
    (machines, preform specs). Effective-dated entities (rate config, buy price)
    keep history via their rows and don't need this.
    """
    class Action(models.TextChoices):
        CREATE = 'CREATE', 'Created'
        UPDATE = 'UPDATE', 'Updated'

    company = models.ForeignKey(
        Company, on_delete=models.CASCADE, related_name='blowing_audit_logs'
    )
    entity_type = models.CharField(max_length=40)   # 'machine' | 'preform_spec'
    entity_id = models.PositiveIntegerField()
    action = models.CharField(max_length=10, choices=Action.choices)
    changes = models.JSONField(default=dict)        # {field: {"old": .., "new": ..}}
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='blowing_audit_logs'
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=['company', 'entity_type', 'entity_id'])]
        verbose_name = 'Blowing Audit Log'
        verbose_name_plural = 'Blowing Audit Logs'

    def __str__(self):
        return f"{self.action} {self.entity_type}#{self.entity_id}"


# ===========================================================================
# Run lifecycle — segments, breakdowns, warehouse gate
# ===========================================================================

class BlowingBreakdownCategory(BaseModel):
    """Configurable breakdown reason categories (per company)."""
    company = models.ForeignKey(
        Company, on_delete=models.PROTECT, related_name='blowing_breakdown_categories'
    )
    name = models.CharField(max_length=100)

    class Meta:
        unique_together = ('company', 'name')
        ordering = ['company_id', 'name']
        verbose_name = 'Blowing Breakdown Category'
        verbose_name_plural = 'Blowing Breakdown Categories'

    def __str__(self):
        return self.name


class BlowingSegment(models.Model):
    """A running period of a blowing run (analogue of ProductionSegment)."""
    blowing_run = models.ForeignKey(
        BlowingRun, on_delete=models.CASCADE, related_name='segments'
    )
    start_time = models.DateTimeField()
    end_time = models.DateTimeField(null=True, blank=True)
    produced_pcs = models.DecimalField(max_digits=12, decimal_places=1, default=Decimal('0'))
    is_active = models.BooleanField(default=True)
    remarks = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['start_time']
        verbose_name = 'Blowing Segment'
        verbose_name_plural = 'Blowing Segments'

    @property
    def duration_minutes(self) -> int:
        if self.start_time and self.end_time:
            return int((self.end_time - self.start_time).total_seconds() / 60)
        return 0

    def __str__(self):
        return f"Segment of run {self.blowing_run_id} ({'active' if self.is_active else 'closed'})"


class BlowingBreakdown(models.Model):
    """A breakdown period of a blowing run (analogue of MachineBreakdown)."""
    blowing_run = models.ForeignKey(
        BlowingRun, on_delete=models.CASCADE, related_name='breakdowns'
    )
    machine = models.ForeignKey(
        BlowingMachine, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='breakdowns'
    )
    breakdown_category = models.ForeignKey(
        BlowingBreakdownCategory, on_delete=models.PROTECT, null=True, blank=True,
        related_name='breakdowns'
    )
    start_time = models.DateTimeField()
    end_time = models.DateTimeField(null=True, blank=True)
    breakdown_minutes = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    is_unrecovered = models.BooleanField(default=False)
    reason = models.CharField(max_length=500, blank=True, default='')
    remarks = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['start_time']
        verbose_name = 'Blowing Breakdown'
        verbose_name_plural = 'Blowing Breakdowns'

    def __str__(self):
        return f"Breakdown of run {self.blowing_run_id}"


# NOTE: preform requests are now warehouse.BOMRequest rows (linked via
# BOMRequest.blowing_run) so they share the Warehouse → BOM Requests queue with
# production. The old BlowingPreformRequest model was removed.
