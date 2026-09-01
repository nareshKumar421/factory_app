"""
factory_expense/models.py

What the admin configures, so the wall board has numbers to show.

The board itself stores nothing: labour headcount comes from the gate register,
electricity from the Daily Electricity readings, maintenance from spare
movements and material indents. What none of those hold is *what a thing
costs* — the gate counts people, not rupees — and that is what lives here.

Four settings models, all company-scoped:

* :class:`LabourRateConfig`        — what one labourer costs for one day.
* :class:`DepartmentSalaryConfig`  — the monthly salary bill, department by department.
* :class:`MonthlyBudget`           — the target each bucket is measured against.
* :class:`FactoryExpenseSettings`  — how the board itself computes and behaves.

Electricity has no settings model on purpose: the maintenance module's
``ElectricityMeter`` master already carries the rate per unit and the grid
multiplying factor, and duplicating them here would let the two disagree.
"""

from datetime import date
from decimal import Decimal

from django.core.validators import MinValueValidator
from django.db import models

from gate_core.models.base import BaseModel

from .constants import ExpenseBucket, RateShift


def month_start(value: date) -> date:
    """The first of ``value``'s month — how every monthly row is keyed."""
    return value.replace(day=1)


class FactoryExpensePermission(models.Model):
    """Sentinel that carries the module's permissions. Never holds a row."""

    class Meta:
        managed = False
        default_permissions = ()
        permissions = [
            ("can_view_factory_expense", "Can view the Factory Expense board"),
            ("can_configure_factory_expense", "Can configure the Factory Expense board"),
        ]


class LabourRateConfig(BaseModel):
    """What one labourer costs for one day, effective from a date.

    Resolved most-specific-first at board time: a row naming both a department
    and a shift beats one naming only a shift, which beats the company-wide
    ``ANY`` row. Among equally specific rows the latest ``effective_from`` on or
    before the work date wins.

    A rate change is a NEW row with a later ``effective_from``; the superseded
    row stays. That is what keeps yesterday's board reproducible — re-opening
    last month prices it at last month's rate, not at today's.
    """

    company = models.ForeignKey(
        "company.Company",
        on_delete=models.PROTECT,
        related_name="factory_labour_rates",
    )
    department = models.ForeignKey(
        "accounts.Department",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="factory_labour_rates",
        help_text="Leave empty for the company-wide default rate.",
    )
    shift = models.CharField(
        max_length=6,
        choices=RateShift.choices,
        default=RateShift.ANY,
        help_text="ANY is the fallback; a DAY or NIGHT row overrides it for that shift.",
    )
    rate_per_person_per_day = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0"))],
        help_text="What one labourer costs for one full shift.",
    )
    effective_from = models.DateField(
        help_text="First work date this rate applies to. Earlier dates keep the older rate.",
    )
    notes = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        ordering = ["company_id", "-effective_from", "department_id", "shift"]
        constraints = [
            models.UniqueConstraint(
                fields=["company", "department", "shift", "effective_from"],
                name="uniq_labour_rate_per_scope_date",
            ),
        ]
        verbose_name = "Labour Rate"
        verbose_name_plural = "Labour Rates"

    def __str__(self):
        scope = self.department.name if self.department else "All departments"
        return f"{scope} / {self.shift}: ₹{self.rate_per_person_per_day} from {self.effective_from}"

    @property
    def specificity(self) -> int:
        """How hard this row competes: department + shift beats shift beats nothing."""
        return (1 if self.department_id else 0) + (1 if self.shift != RateShift.ANY else 0)


class DepartmentSalaryConfig(BaseModel):
    """The monthly salary bill for one department in one month.

    Typed in by the admin rather than read from anywhere — FactoryFlow has no
    payroll. ``employee_count`` is optional and exists only so the board can
    show cost per head; leaving it at zero hides that figure instead of
    dividing by nothing.

    The board spreads ``monthly_amount`` evenly across the month's days, so a
    part-month view is an accrual rather than a full month's bill landing on
    the first.
    """

    company = models.ForeignKey(
        "company.Company",
        on_delete=models.PROTECT,
        related_name="factory_department_salaries",
    )
    department = models.ForeignKey(
        "accounts.Department",
        on_delete=models.PROTECT,
        related_name="factory_department_salaries",
    )
    month = models.DateField(help_text="Any date in the month; stored as the 1st.")
    employee_count = models.PositiveIntegerField(
        default=0,
        help_text="Optional. Drives the cost-per-employee figure; 0 hides it.",
    )
    monthly_amount = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0"))],
        help_text="Total salary cost for this department for the whole month.",
    )
    notes = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        ordering = ["-month", "department__name"]
        constraints = [
            models.UniqueConstraint(
                fields=["company", "department", "month"],
                name="uniq_department_salary_per_month",
            ),
        ]
        verbose_name = "Department Salary"
        verbose_name_plural = "Department Salaries"

    def __str__(self):
        return f"{self.department.name} {self.month:%b %Y}: ₹{self.monthly_amount}"

    def save(self, *args, **kwargs):
        if self.month:
            self.month = month_start(self.month)
        super().save(*args, **kwargs)


class MonthlyBudget(BaseModel):
    """The monthly target one bucket is measured against.

    Optional throughout. A bucket with no budget row simply shows no variance
    chip on the board rather than comparing against zero and reading as 100%
    over.
    """

    company = models.ForeignKey(
        "company.Company",
        on_delete=models.PROTECT,
        related_name="factory_expense_budgets",
    )
    bucket = models.CharField(max_length=20, choices=ExpenseBucket.choices)
    month = models.DateField(help_text="Any date in the month; stored as the 1st.")
    amount = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0"))],
    )
    notes = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        ordering = ["-month", "bucket"]
        constraints = [
            models.UniqueConstraint(
                fields=["company", "bucket", "month"],
                name="uniq_expense_budget_per_bucket_month",
            ),
        ]
        verbose_name = "Monthly Budget"
        verbose_name_plural = "Monthly Budgets"

    def __str__(self):
        return f"{self.get_bucket_display()} {self.month:%b %Y}: ₹{self.amount}"

    def save(self, *args, **kwargs):
        if self.month:
            self.month = month_start(self.month)
        super().save(*args, **kwargs)


class FactoryExpenseSettings(BaseModel):
    """How the board computes and behaves, for one company.

    One row per company, created on first read so the board never 404s on a
    company nobody has configured yet.
    """

    company = models.OneToOneField(
        "company.Company",
        on_delete=models.CASCADE,
        related_name="factory_expense_settings",
    )

    # --- which panels the wall shows -------------------------------------
    show_labour = models.BooleanField(default=True)
    show_salary = models.BooleanField(default=True)
    show_electricity = models.BooleanField(default=True)
    show_maintenance = models.BooleanField(default=True)

    # --- what counts as maintenance spend --------------------------------
    maintenance_include_spares = models.BooleanField(
        default=True,
        help_text="Count spares issued or consumed on work orders, at their unit cost.",
    )
    maintenance_include_indents = models.BooleanField(
        default=True,
        help_text="Count material indents once a company has been selected or the goods have moved.",
    )

    # --- electricity ------------------------------------------------------
    electricity_only_company_meters = models.BooleanField(
        default=True,
        help_text=(
            "Count only meters tagged with this company. Turn off to show every "
            "meter on the campus, including untagged ones."
        ),
    )

    # --- the wall itself --------------------------------------------------
    refresh_seconds = models.PositiveIntegerField(
        default=60,
        validators=[MinValueValidator(15)],
        help_text="How often the board re-reads itself. Minimum 15 seconds.",
    )
    rotate_seconds = models.PositiveIntegerField(
        default=12,
        validators=[MinValueValidator(4)],
        help_text="How long each panel of a rotating list holds before it scrolls.",
    )

    class Meta:
        verbose_name = "Factory Expense Settings"
        verbose_name_plural = "Factory Expense Settings"

    def __str__(self):
        return f"Expense board settings — {self.company.code}"
