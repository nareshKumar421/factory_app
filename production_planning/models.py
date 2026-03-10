from django.db import models
from django.conf import settings
from django.core.validators import MinValueValidator
from decimal import Decimal


class SAPSyncStatus(models.TextChoices):
    NOT_POSTED = "NOT_POSTED", "Not Posted to SAP"
    POSTED = "POSTED", "Posted to SAP"
    FAILED = "FAILED", "SAP Posting Failed"


class PlanStatus(models.TextChoices):
    DRAFT = "DRAFT", "Draft"                   # Created locally, not posted to SAP
    OPEN = "OPEN", "Open"                       # Posted to SAP, awaiting weekly plans
    IN_PROGRESS = "IN_PROGRESS", "In Progress"  # Weekly plans created, production running
    COMPLETED = "COMPLETED", "Completed"        # Closed by manager
    CLOSED = "CLOSED", "Closed"                 # Fully confirmed and locked
    CANCELLED = "CANCELLED", "Cancelled"


class WeeklyPlanStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    IN_PROGRESS = "IN_PROGRESS", "In Progress"
    COMPLETED = "COMPLETED", "Completed"


class ShiftChoice(models.TextChoices):
    MORNING = "MORNING", "Morning"
    AFTERNOON = "AFTERNOON", "Afternoon"
    NIGHT = "NIGHT", "Night"


class ProductionPlan(models.Model):
    """
    A production plan created in Sampooran by the planner.
    After creation (DRAFT) it is posted to SAP as a Production Order (OWOR).
    One plan = one finished product + its required raw materials (BOM).

    Flow:
        User fills form → DRAFT saved locally
        → POST /post-to-sap/ → SAP OWOR created → status becomes OPEN
        → Planner adds weekly plans → IN_PROGRESS
        → Manager closes → COMPLETED
    """
    company = models.ForeignKey(
        'company.Company',
        on_delete=models.PROTECT,
        related_name='production_plans'
    )

    # Finished product (selected from SAP item dropdown)
    item_code = models.CharField(max_length=50, help_text="Finished product ItemCode")
    item_name = models.CharField(max_length=255, help_text="Finished product name")
    uom = models.CharField(max_length=20, blank=True, default='', help_text="Unit of measure")
    warehouse_code = models.CharField(
        max_length=20, blank=True, default='',
        help_text="Production warehouse code"
    )

    # Quantities
    planned_qty = models.DecimalField(
        max_digits=12, decimal_places=3,
        validators=[MinValueValidator(Decimal('0.001'))],
        help_text="Total planned production quantity"
    )
    completed_qty = models.DecimalField(
        max_digits=12, decimal_places=3, default=Decimal('0'),
        help_text="Total produced so far (sum of daily entries)"
    )

    # Dates
    target_start_date = models.DateField(help_text="Planned production start date")
    due_date = models.DateField(help_text="Required completion date")

    # Plan status
    status = models.CharField(
        max_length=20,
        choices=PlanStatus.choices,
        default=PlanStatus.DRAFT
    )

    # SAP sync — filled after posting to SAP
    sap_posting_status = models.CharField(
        max_length=20,
        choices=SAPSyncStatus.choices,
        default=SAPSyncStatus.NOT_POSTED
    )
    sap_doc_entry = models.IntegerField(null=True, blank=True, help_text="SAP OWOR DocEntry")
    sap_doc_num = models.IntegerField(null=True, blank=True, help_text="SAP OWOR DocNum")
    sap_status = models.CharField(
        max_length=2, null=True, blank=True,
        help_text="SAP order status after posting: P=Planned, R=Released"
    )
    sap_error_message = models.TextField(null=True, blank=True)

    # Optional
    branch_id = models.IntegerField(null=True, blank=True, help_text="SAP BPLId")
    remarks = models.TextField(blank=True, default='')

    # Audit
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='created_production_plans'
    )
    closed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='closed_production_plans'
    )
    closed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-due_date', 'item_code']
        verbose_name = 'Production Plan'
        verbose_name_plural = 'Production Plans'
        permissions = [
            ('can_create_production_plan', 'Can create production plan'),
            ('can_edit_production_plan', 'Can edit production plan'),
            ('can_delete_production_plan', 'Can delete production plan'),
            ('can_post_plan_to_sap', 'Can post production plan to SAP'),
            ('can_view_production_plan', 'Can view production plans'),
            ('can_manage_weekly_plan', 'Can create and manage weekly plans'),
            ('can_add_daily_production', 'Can add daily production entries'),
            ('can_view_daily_production', 'Can view daily production entries'),
            ('can_close_production_plan', 'Can close production plans'),
        ]

    def __str__(self):
        doc_ref = f"SAP#{self.sap_doc_num}" if self.sap_doc_num else "Draft"
        return f"{doc_ref} — {self.item_name} ({self.due_date})"

    @property
    def progress_percent(self):
        if not self.planned_qty:
            return 0.0
        return round(float(self.completed_qty) / float(self.planned_qty) * 100, 1)

    def recompute_completed_qty(self):
        from django.db.models import Sum
        total = DailyProductionEntry.objects.filter(
            weekly_plan__production_plan=self
        ).aggregate(total=Sum('produced_qty'))['total'] or Decimal('0')
        self.completed_qty = total
        self.save(update_fields=['completed_qty', 'updated_at'])


class PlanMaterialRequirement(models.Model):
    """
    Raw material (BOM component) required for a production plan.
    Entered by planner and posted to SAP as ProductionOrderLines.
    """
    production_plan = models.ForeignKey(
        ProductionPlan,
        on_delete=models.CASCADE,
        related_name='materials'
    )
    component_code = models.CharField(max_length=50, help_text="Raw material ItemCode")
    component_name = models.CharField(max_length=255, help_text="Raw material name")
    required_qty = models.DecimalField(
        max_digits=12, decimal_places=3,
        validators=[MinValueValidator(Decimal('0.001'))]
    )
    uom = models.CharField(max_length=20, blank=True, default='')
    warehouse_code = models.CharField(max_length=20, blank=True, default='')

    class Meta:
        ordering = ['component_code']
        verbose_name = 'Plan Material Requirement'
        verbose_name_plural = 'Plan Material Requirements'

    def __str__(self):
        return f"{self.component_name} — {self.required_qty} {self.uom}"


class WeeklyPlan(models.Model):
    """
    Planner's weekly target breakdown of a production plan.
    """
    production_plan = models.ForeignKey(
        ProductionPlan,
        on_delete=models.CASCADE,
        related_name='weekly_plans'
    )
    week_number = models.PositiveSmallIntegerField(help_text="Week number: 1, 2, 3, 4 ...")
    week_label = models.CharField(max_length=100, blank=True, default='')
    start_date = models.DateField()
    end_date = models.DateField()
    target_qty = models.DecimalField(
        max_digits=12, decimal_places=3,
        validators=[MinValueValidator(Decimal('0.001'))]
    )
    produced_qty = models.DecimalField(
        max_digits=12, decimal_places=3, default=Decimal('0')
    )
    status = models.CharField(
        max_length=20,
        choices=WeeklyPlanStatus.choices,
        default=WeeklyPlanStatus.PENDING
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='created_weekly_plans'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('production_plan', 'week_number')
        ordering = ['week_number']
        verbose_name = 'Weekly Plan'
        verbose_name_plural = 'Weekly Plans'

    def __str__(self):
        return f"Week {self.week_number} — {self.production_plan.item_name}"

    @property
    def progress_percent(self):
        if not self.target_qty:
            return 0.0
        return round(float(self.produced_qty) / float(self.target_qty) * 100, 1)

    def recompute_produced_qty(self):
        from django.db.models import Sum
        total = self.daily_entries.aggregate(total=Sum('produced_qty'))['total'] or Decimal('0')
        self.produced_qty = total
        if total == 0:
            self.status = WeeklyPlanStatus.PENDING
        elif total >= self.target_qty:
            self.status = WeeklyPlanStatus.COMPLETED
        else:
            self.status = WeeklyPlanStatus.IN_PROGRESS
        self.save(update_fields=['produced_qty', 'status', 'updated_at'])


class DailyProductionEntry(models.Model):
    """
    Production team's daily log — how much was produced each day.
    """
    weekly_plan = models.ForeignKey(
        WeeklyPlan,
        on_delete=models.CASCADE,
        related_name='daily_entries'
    )
    production_date = models.DateField()
    produced_qty = models.DecimalField(
        max_digits=12, decimal_places=3,
        validators=[MinValueValidator(Decimal('0.001'))]
    )
    shift = models.CharField(
        max_length=20,
        choices=ShiftChoice.choices,
        null=True, blank=True
    )
    remarks = models.TextField(blank=True, default='')
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='daily_production_entries'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('weekly_plan', 'production_date', 'shift')
        ordering = ['-production_date']
        verbose_name = 'Daily Production Entry'
        verbose_name_plural = 'Daily Production Entries'

    def __str__(self):
        return f"{self.production_date} — {self.produced_qty} units"
