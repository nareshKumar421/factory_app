"""
factory_expense/models.py

What the wall board needs that is not already recorded somewhere else.

**This app deliberately stores no rates.** Every "what does a thing cost"
question is answered by ``cost_master.CostRate`` — the factory-wide rate
catalog behind Admin › Cost Master — resolved most-specific-first and
effective-dated. An earlier draft of this module grew its own labour-rate and
department-salary tables; they were a second cost master, and two cost masters
eventually disagree. They were removed in migration 0002.

What is left is only the things Cost Master is the wrong home for:

* :class:`MonthlyBudget`          — a *target*, not a rate. Cost Master answers
  what something costs; a budget answers what we planned to spend, and mixing
  the two would fill the rate catalog with rows that are not rates.
* :class:`FactoryExpenseSettings` — how the board itself computes and behaves:
  which panels show, how often it polls, what counts as maintenance spend.

Everything else the board draws on already exists: headcount from the gate
register, units and cost from the Daily Electricity readings, spares and
indents from the maintenance module.
"""

from datetime import date
from decimal import Decimal

from django.core.validators import MinValueValidator
from django.db import models

from gate_core.models.base import BaseModel

from .constants import ExpenseBucket


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
