from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone


PROCEDURE_NAME = "SALES PLANNING VS REQUIREMENT_WEEKLY"


class SalesPlanningRequirementPermission(models.Model):
    """Sentinel model for dashboard permissions."""

    class Meta:
        managed = False
        default_permissions = ()
        permissions = [
            (
                "can_view_sales_planning_requirement",
                "Can view Sales Planning vs Requirement dashboard",
            ),
            (
                "can_refresh_sales_planning_requirement",
                "Can refresh Sales Planning vs Requirement data",
            ),
        ]


class SalesPlanningRequirementRefreshRun(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        RUNNING = "running", "Running"
        SUCCESS = "success", "Success"
        FAILED = "failed", "Failed"

    class TriggeredBy(models.TextChoices):
        MANUAL = "manual", "Manual"
        SCHEDULED = "scheduled", "Scheduled"
        COMMAND = "command", "Command"

    company_code = models.CharField(max_length=50, db_index=True)
    source_schema = models.CharField(max_length=128)
    procedure_name = models.CharField(max_length=255, default=PROCEDURE_NAME)
    forecast_id = models.IntegerField(null=True, blank=True)
    forecast_name = models.CharField(max_length=255, blank=True)
    forecast_start_date = models.DateField(null=True, blank=True)
    forecast_end_date = models.DateField(null=True, blank=True)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    triggered_by = models.CharField(
        max_length=20,
        choices=TriggeredBy.choices,
        default=TriggeredBy.MANUAL,
    )
    started_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="sales_planning_requirement_refresh_runs",
    )
    started_at = models.DateTimeField(default=timezone.now)
    completed_at = models.DateTimeField(null=True, blank=True)
    rows_loaded = models.PositiveIntegerField(default=0)
    column_metadata = models.JSONField(default=list, blank=True)
    procedure_parameters = models.JSONField(default=dict, blank=True)
    error_message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-started_at"]
        indexes = [
            models.Index(fields=["company_code", "status"]),
            models.Index(fields=["company_code", "-started_at"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["company_code"],
                condition=Q(status="running"),
                name="uniq_running_sales_planning_refresh",
            ),
        ]

    @property
    def duration_seconds(self):
        if not self.completed_at:
            return None
        return round((self.completed_at - self.started_at).total_seconds(), 2)

    def mark_success(self, rows_loaded: int) -> None:
        self.status = self.Status.SUCCESS
        self.rows_loaded = rows_loaded
        self.completed_at = timezone.now()
        self.error_message = ""
        self.save(
            update_fields=[
                "status",
                "rows_loaded",
                "completed_at",
                "error_message",
                "updated_at",
            ]
        )

    def mark_failed(self, message: str) -> None:
        self.status = self.Status.FAILED
        self.completed_at = timezone.now()
        self.error_message = (message or "Refresh failed")[:4000]
        self.save(update_fields=["status", "completed_at", "error_message", "updated_at"])


class SalesPlanningRequirementRow(models.Model):
    company_code = models.CharField(max_length=50, db_index=True)
    source_schema = models.CharField(max_length=128)
    forecast_id = models.IntegerField(null=True, blank=True)
    forecast_name = models.CharField(max_length=255, blank=True)
    forecast_start_date = models.DateField(null=True, blank=True)
    forecast_end_date = models.DateField(null=True, blank=True)
    planning_month = models.CharField(max_length=255, blank=True)
    item_code = models.CharField(max_length=100, db_index=True)
    item_name = models.CharField(max_length=500, blank=True)
    planned_qty = models.DecimalField(max_digits=24, decimal_places=6, null=True, blank=True)
    base_required_qty = models.DecimalField(
        max_digits=24,
        decimal_places=6,
        null=True,
        blank=True,
        help_text="Forecast/BOM requirement before final stock adjustment where available.",
    )
    min_stock = models.DecimalField(max_digits=24, decimal_places=6, default=0)
    stock_in_hand = models.DecimalField(max_digits=24, decimal_places=6, default=0)
    required_qty = models.DecimalField(
        max_digits=24,
        decimal_places=6,
        help_text="Final quantity required after min stock and current stock adjustments.",
    )
    received_qty = models.DecimalField(
        max_digits=24,
        decimal_places=6,
        null=True,
        blank=True,
        help_text=(
            "Quantity received on posted GRPOs within the forecast's dates. "
            "Null on rows loaded before it was read."
        ),
    )
    open_po_qty = models.DecimalField(max_digits=24, decimal_places=6, default=0)
    net_shortage_qty = models.DecimalField(
        max_digits=24,
        decimal_places=6,
        default=0,
        help_text="Required quantity still not covered by open purchase orders.",
    )
    report_execution_at = models.DateTimeField(null=True, blank=True)
    raw_payload = models.JSONField(default=dict, blank=True)
    refresh_run = models.ForeignKey(
        SalesPlanningRequirementRefreshRun,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="rows",
    )
    loaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["item_code"]
        indexes = [
            models.Index(fields=["company_code", "forecast_id"]),
            models.Index(fields=["company_code", "item_code"]),
            models.Index(fields=["company_code", "-net_shortage_qty"]),
            models.Index(fields=["loaded_at"]),
        ]

    def __str__(self):
        return f"{self.company_code} {self.item_code} {self.required_qty}"
