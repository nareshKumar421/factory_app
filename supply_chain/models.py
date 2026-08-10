"""Reference data and policy for the Smart Supply Chain system.

The brief's chain is seven steps:

    1 demand -> 2 stock floor -> 3 FG gap -> 4 BOM explosion -> 5 material need
    -> 6 lead-time timing -> 7 capacity feasibility

**Steps 1-5 already exist.** ``sales_planning_requirement`` calls the HANA
procedure ``SALES PLANNING VS REQUIREMENT_WEEKLY`` and stores, per item,
``planned_qty``, ``base_required_qty`` (the BOM-exploded requirement),
``min_stock``, ``stock_in_hand``, ``required_qty``, ``open_po_qty`` and
``net_shortage_qty``. This app does not re-implement any of that; it reads those
rows.

**Steps 6 and 7 do not exist anywhere**, and neither does the reference data they
need. That is what lives here: the three datasets the departments supply through
the Excel template, plus the policy knobs for the decisions the brief left open.
"""
from decimal import Decimal

from django.core.validators import MinValueValidator
from django.db import models

# One hour of running, in minutes — capacity maths is done in hours.
MINUTES_PER_HOUR = Decimal("60")


class MaterialType(models.TextChoices):
    PACKAGING = "PACKAGING", "Packaging"
    RAW = "RAW", "Oil / Raw Material"


class FloorBasis(models.TextChoices):
    """What the brief's "35% of the three-month sales trend" is 35% *of*.

    The brief does not say, and the two readings differ by 3x, so it is a
    setting rather than a constant. Monthly average is the default: the floor is
    a month's worth of buffer, which is the only reading under which "minimum
    stock" is comparable to a monthly plan.
    """

    MONTHLY_AVERAGE = "MONTHLY_AVERAGE", "35% of the monthly average"
    THREE_MONTH_TOTAL = "THREE_MONTH_TOTAL", "35% of the three-month total"


class FloorSource(models.TextChoices):
    """Where the safety-stock floor actually comes from.

    ``PROCEDURE`` is the pre-existing behaviour: whatever ``min_stock`` the HANA
    procedure returned, whose relationship to the brief's 35% is unverified.
    ``POLICY`` recomputes it here from the sales trend, which is the only way the
    brief's rule is actually enforced — but it only takes effect for items that
    have a :class:`SalesTrend` on file, so turning it on changes nothing until the
    trend data is loaded.
    """

    PROCEDURE = "PROCEDURE", "min_stock as returned by the HANA procedure"
    POLICY = "POLICY", "Recomputed here from the 3-month sales trend"


class FloorConvention(models.TextChoices):
    """Which way the floor was applied — the contradiction in the brief.

    Step 3 says compare stock against "demand PLUS the floor"; step 5 says
    subtract stock "AND its own floor". Rather than guess, the audit infers which
    the procedure actually used from the numbers it returns.
    """

    ADDITIVE = "ADDITIVE", "required = demand + floor − stock (step 3's reading)"
    SUBTRACTIVE = "SUBTRACTIVE", "required = demand − floor − stock (step 5's reading)"
    INDETERMINATE = "INDETERMINATE", "Cannot be told apart from these numbers"


class AlarmState(models.TextChoices):
    """Step 6's output — the reason the whole system exists."""

    OVERDUE = "OVERDUE", "Overdue — the order-by date has passed"
    ORDER_NOW = "ORDER_NOW", "Order now — inside the lead time"
    SCHEDULED = "SCHEDULED", "Can wait — order by the stated date"
    NO_LEAD_TIME = "NO_LEAD_TIME", "Unknown — no lead time on file"
    COVERED = "COVERED", "Covered — nothing to order"


class SupplyChainPolicy(models.Model):
    """Per-company answers to the decisions the brief leaves open.

    Every field here is a question the brief raises but does not settle. They are
    configuration rather than constants precisely because getting them wrong
    changes every number downstream, and the business has not chosen yet.
    """

    company_code = models.CharField(max_length=50, unique=True, db_index=True)

    floor_percent = models.DecimalField(
        max_digits=6, decimal_places=2, default=Decimal("35.00"),
        validators=[MinValueValidator(Decimal("0"))],
        help_text="Safety-stock floor as a percentage of the sales trend. The brief says 35%.",
    )
    floor_basis = models.CharField(
        max_length=20, choices=FloorBasis.choices, default=FloorBasis.MONTHLY_AVERAGE,
        help_text="What the percentage applies to. The brief does not say; the two differ by 3x.",
    )
    urgency_window_days = models.PositiveIntegerField(
        default=7,
        help_text="Order-by dates inside this many days read as ORDER_NOW rather than SCHEDULED.",
    )
    use_net_of_open_po = models.BooleanField(
        default=True,
        help_text=(
            "Drive alarms from net_shortage_qty (requirement less open purchase orders) "
            "rather than the gross requirement. Off means already-ordered material is "
            "ordered again every cycle."
        ),
    )
    apply_moq_rounding = models.BooleanField(
        default=True,
        help_text="Round each order quantity up to the supplier's MOQ. The template collects MOQ.",
    )
    floor_source = models.CharField(
        max_length=20, choices=FloorSource.choices, default=FloorSource.POLICY,
        help_text=(
            "POLICY enforces the brief's own floor rule, but only for items with a "
            "SalesTrend on file — everything else keeps the procedure's min_stock, so "
            "switching this changes nothing until trend data is loaded."
        ),
    )
    include_changeover_in_capacity = models.BooleanField(
        default=True,
        help_text=(
            "Deduct changeover time from a machine's available hours. The template collects "
            "changeover minutes; ignoring them passes plans the floor cannot run."
        ),
    )

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = "Supply chain policies"

    def __str__(self):
        return f"{self.company_code} policy"

    @classmethod
    def for_company(cls, company_code):
        """The company's policy, or an unsaved default — never None.

        Reading a policy must not require someone to have configured one first,
        or every screen breaks until an admin visits it.
        """
        return cls.objects.filter(company_code=company_code).first() or cls(
            company_code=company_code
        )

    def stock_floor(self, three_month_sales):
        """The safety-stock floor implied by three months of sales.

        Kept here rather than in the engine because it IS the policy: the brief's
        headline rule, with the ambiguity it left behind made explicit.
        """
        sales = Decimal(three_month_sales or 0)
        if sales <= 0:
            return Decimal("0")
        base = sales / Decimal("3") if self.floor_basis == FloorBasis.MONTHLY_AVERAGE else sales
        return (base * self.floor_percent / Decimal("100")).quantize(Decimal("0.000001"))


class SalesTrend(models.Model):
    """Three months of actual sales per item — the base of the brief's 35% floor.

    The brief says minimum stock is "derived from sales — automatic", and the HANA
    procedure does return a ``min_stock``. But nothing states that its number is
    the brief's 35%, and the two cannot be reconciled without the sales figure the
    percentage applies to. Holding the trend locally is what lets the floor be
    computed, checked, and actually enforced rather than assumed.
    """

    company_code = models.CharField(max_length=50, db_index=True)
    item_code = models.CharField(max_length=100, db_index=True)
    item_name = models.CharField(max_length=255, blank=True)
    three_month_qty = models.DecimalField(
        max_digits=24, decimal_places=6, default=0,
        help_text="Total units sold over the trailing three months.",
    )
    period_start = models.DateField(null=True, blank=True)
    period_end = models.DateField(null=True, blank=True)
    source = models.CharField(
        max_length=40, blank=True, help_text="Where the figure came from, e.g. ERP or CSV."
    )
    captured_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["item_code"]
        constraints = [
            models.UniqueConstraint(
                fields=["company_code", "item_code"], name="uniq_sc_sales_trend_item"
            )
        ]

    def __str__(self):
        return f"{self.item_code} 3m={self.three_month_qty}"


class MaterialLeadTime(models.Model):
    """Template sheet 1 — Procurement (Packaging) + Procurement (Oils / Raw Material).

    Lead time is defined by the template as order placed -> material USABLE in
    production, which is wider than supplier transit and is the number step 6
    needs.
    """

    company_code = models.CharField(max_length=50, db_index=True)
    material_code = models.CharField(max_length=100, db_index=True)
    material_name = models.CharField(max_length=255, blank=True)
    material_type = models.CharField(
        max_length=20, choices=MaterialType.choices, default=MaterialType.PACKAGING
    )
    category = models.CharField(max_length=120, blank=True, help_text="Category / spec, e.g. Cap")
    supplier_name = models.CharField(max_length=200, blank=True)
    lead_time_days = models.PositiveIntegerField(
        default=0, help_text="Order placed -> material usable in production."
    )
    moq = models.DecimalField(max_digits=24, decimal_places=6, default=0)
    unit = models.CharField(max_length=30, blank=True)
    remarks = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["material_code"]
        constraints = [
            models.UniqueConstraint(
                fields=["company_code", "material_code"], name="uniq_sc_lead_time_material"
            )
        ]

    def __str__(self):
        return f"{self.material_code} ({self.lead_time_days}d)"


class MachineCapacity(models.Model):
    """Template sheet 2 — Production / Infrastructure.

    Effective monthly capacity is a formula in the template, not an entered
    number, so it is derived here too rather than stored: a stored copy silently
    goes stale the first time someone edits a shift pattern.
    """

    company_code = models.CharField(max_length=50, db_index=True)
    machine_id = models.CharField(max_length=50, db_index=True)
    name = models.CharField(max_length=150, blank=True)
    location = models.CharField(max_length=120, blank=True)
    pack_type = models.CharField(max_length=80, blank=True)
    pack_size_range = models.CharField(max_length=80, blank=True)

    output_per_hour = models.DecimalField(max_digits=18, decimal_places=4, default=0)
    shift_hours = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    shifts_per_day = models.DecimalField(max_digits=6, decimal_places=2, default=0)
    working_days_per_month = models.DecimalField(max_digits=6, decimal_places=2, default=0)
    changeover_minutes = models.DecimalField(max_digits=8, decimal_places=2, default=0)

    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["machine_id"]
        verbose_name_plural = "Machine capacities"
        constraints = [
            models.UniqueConstraint(
                fields=["company_code", "machine_id"], name="uniq_sc_machine"
            )
        ]

    def __str__(self):
        return f"{self.machine_id} {self.name}".strip()

    @property
    def available_hours(self):
        """Running hours a month, before changeover."""
        return Decimal(self.shift_hours) * Decimal(self.shifts_per_day) * Decimal(
            self.working_days_per_month
        )

    def effective_capacity_units(self, changeover_count=0, include_changeover=True):
        """Units this machine can produce in a month.

        ``changeover_count`` is how many times it must change over — one per SKU
        scheduled on it. The template collects changeover minutes and the brief's
        step 7 never uses them; a capacity check that ignores changeover passes
        plans the floor cannot actually run.
        """
        hours = self.available_hours
        if include_changeover and changeover_count:
            hours -= (Decimal(self.changeover_minutes) * Decimal(changeover_count)) / MINUTES_PER_HOUR
        if hours <= 0:
            return Decimal("0")
        return hours * Decimal(self.output_per_hour)


class MaterialMachineMap(models.Model):
    """Template sheet 3 — Production. Which SKU runs on which machine."""

    company_code = models.CharField(max_length=50, db_index=True)
    sku_code = models.CharField(max_length=100, db_index=True)
    sku_name = models.CharField(max_length=255, blank=True)
    brand = models.CharField(max_length=80, blank=True)
    pack_type = models.CharField(max_length=80, blank=True)
    pack_size = models.CharField(max_length=80, blank=True)

    primary_machine_id = models.CharField(max_length=50, db_index=True)
    alternate_machine_ids = models.CharField(
        max_length=200, blank=True, help_text="Comma-separated, as in the template."
    )
    output_on_primary = models.DecimalField(
        max_digits=18, decimal_places=4, default=0,
        help_text="Units/hour for THIS SKU. Falls back to the machine's own rate when 0.",
    )

    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["sku_code"]
        constraints = [
            models.UniqueConstraint(
                fields=["company_code", "sku_code"], name="uniq_sc_sku_machine"
            )
        ]

    def __str__(self):
        return f"{self.sku_code} -> {self.primary_machine_id}"

    @property
    def alternates(self):
        return [m.strip() for m in (self.alternate_machine_ids or "").split(",") if m.strip()]


class ReferenceImport(models.Model):
    """Audit of one upload of the reference template, so a bad sheet is traceable."""

    company_code = models.CharField(max_length=50, db_index=True)
    filename = models.CharField(max_length=255, blank=True)
    lead_times_loaded = models.PositiveIntegerField(default=0)
    machines_loaded = models.PositiveIntegerField(default=0)
    mappings_loaded = models.PositiveIntegerField(default=0)
    examples_skipped = models.PositiveIntegerField(
        default=0, help_text="Grey italic template example rows, which must never load as data."
    )
    warnings = models.JSONField(default=list, blank=True)
    imported_by = models.CharField(max_length=150, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.company_code} import {self.created_at:%Y-%m-%d}"


class AlarmSubscription(models.Model):
    """Who gets alarmed, and about what.

    The brief asks for alarms but never says who receives them, through which
    channel, or at what threshold — so it is data, not a hard-coded recipient
    list. Each row targets everyone holding a Django permission (which is how the
    rest of this codebase addresses a department), and chooses how loud an alarm
    has to be before it is worth interrupting them.
    """

    company_code = models.CharField(max_length=50, db_index=True)
    label = models.CharField(
        max_length=120, blank=True, help_text="e.g. Packaging Procurement"
    )
    permission_codename = models.CharField(
        max_length=100,
        help_text="Everyone holding this permission is notified, directly or via a group.",
    )
    include_overdue = models.BooleanField(default=True)
    include_order_now = models.BooleanField(default=True)
    include_missing_lead_time = models.BooleanField(
        default=False,
        help_text="Materials with no lead time on file — chase the reference data.",
    )
    include_capacity = models.BooleanField(
        default=False, help_text="Also alarm when a line is over capacity."
    )
    material_type = models.CharField(
        max_length=20, choices=MaterialType.choices, blank=True,
        help_text="Limit to packaging or raw material. Blank = everything.",
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["company_code", "label"]

    def __str__(self):
        return f"{self.label or self.permission_codename} ({self.company_code})"


class AlarmDispatch(models.Model):
    """One alarm send — so a digest is not repeated and a silent cron is visible."""

    company_code = models.CharField(max_length=50, db_index=True)
    subscription = models.ForeignKey(
        AlarmSubscription, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="dispatches",
    )
    digest = models.CharField(
        max_length=64, db_index=True,
        help_text="Fingerprint of the alarm content, so an unchanged digest is not re-sent.",
    )
    title = models.CharField(max_length=255, blank=True)
    body = models.TextField(blank=True)
    recipients = models.PositiveIntegerField(default=0)
    overdue_count = models.PositiveIntegerField(default=0)
    order_now_count = models.PositiveIntegerField(default=0)
    sent_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-sent_at"]
        indexes = [models.Index(fields=["company_code", "-sent_at"])]

    def __str__(self):
        return f"{self.company_code} alarm {self.sent_at:%Y-%m-%d %H:%M}"


class SupplyChainPermission(models.Model):
    """Permission holder only — no table rows are ever created."""

    class Meta:
        managed = False
        default_permissions = ()
        permissions = [
            ("can_view_supply_chain", "Can view the supply chain dashboard"),
            ("can_manage_supply_chain_reference", "Can upload/edit supply chain reference data"),
        ]
