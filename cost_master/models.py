from decimal import Decimal

from django.db import models

from company.models import Company
from accounts.models import Department
from gate_core.models.base import BaseModel


class CostBasis(models.TextChoices):
    PER_DAY = 'PER_DAY', 'Per Day (fixed)'
    PER_PERSON_DAY = 'PER_PERSON_DAY', 'Per Person per Day'
    PER_HOUR = 'PER_HOUR', 'Per Hour'
    PER_MONTH = 'PER_MONTH', 'Per Month'
    PER_UNIT = 'PER_UNIT', 'Per Electricity Unit'
    PER_CASE = 'PER_CASE', 'Per Case'
    PER_BOTTLE = 'PER_BOTTLE', 'Per Bottle'
    PER_KG = 'PER_KG', 'Per Kg'
    PER_LITRE = 'PER_LITRE', 'Per Litre'
    FLAT = 'FLAT', 'Flat Amount'


class CostScope(models.TextChoices):
    FACTORY = 'FACTORY', 'Factory-wide'
    COMPANY = 'COMPANY', 'Company-wide'
    DEPARTMENT = 'DEPARTMENT', 'Department-wide'
    VALUE = 'VALUE', 'Specific value'


class CostType(BaseModel):
    """One kind of cost the factory incurs (labour, machine electricity, ...).

    The central catalog: every rate anywhere in the app should be an instance
    of one of these types. The per-module cost masters (blowing,
    production_execution) each grew their own category enum; this table is the
    single registry they converge on.
    """
    code = models.SlugField(
        max_length=60, unique=True,
        help_text='Stable identifier consumers resolve rates by, e.g. '
                  '"labour-contract". Never reused for a different cost.'
    )
    name = models.CharField(max_length=150)
    description = models.TextField(blank=True, default='')
    default_basis = models.CharField(
        max_length=20, choices=CostBasis.choices, default=CostBasis.PER_DAY,
        help_text='Pre-selected basis when a rate is entered for this type.'
    )
    is_credit = models.BooleanField(
        default=False, help_text='True if this reduces cost (e.g. scrap recovery).'
    )

    class Meta:
        ordering = ['name']
        verbose_name = 'Cost Type'
        verbose_name_plural = 'Cost Types'
        permissions = [
            ('can_view_cost_master', 'Can view cost master'),
            ('can_manage_cost_master', 'Can manage cost master'),
        ]

    def __str__(self):
        return f"{self.code} — {self.name}"


class CostRate(BaseModel):
    """A dated rate for a cost type at one scope.

    Scope fields by ``scope``:
      FACTORY    — no company, department or value_key (applies everywhere)
      COMPANY    — ``company`` required
      DEPARTMENT — ``department`` required; ``company`` optional (set = that
                   company's department rate, NULL = the department in every company)
      VALUE      — ``value_key`` required (a free identifier such as
                   "machine:BM-01"); ``company`` optional context

    A rate change is a NEW row with a later ``effective_from``; superseded rows
    are kept so a recompute of a past period reprices at that period's rate
    (the lesson from blowing migration 0019 — never overwrite rates in place).
    """
    cost_type = models.ForeignKey(
        CostType, on_delete=models.PROTECT, related_name='rates'
    )
    scope = models.CharField(max_length=15, choices=CostScope.choices)
    company = models.ForeignKey(
        Company, on_delete=models.PROTECT, null=True, blank=True,
        related_name='cost_master_rates'
    )
    department = models.ForeignKey(
        Department, on_delete=models.PROTECT, null=True, blank=True,
        related_name='cost_master_rates'
    )
    value_key = models.CharField(
        max_length=200, blank=True, default='',
        help_text='Identifier the rate is particular to, e.g. "machine:BM-01".'
    )
    basis = models.CharField(max_length=20, choices=CostBasis.choices)
    rate = models.DecimalField(max_digits=15, decimal_places=4, default=Decimal('0'))
    effective_from = models.DateField(
        help_text='Applies on or after this date. To change a rate, add a row '
                  'with a later date rather than editing this one.'
    )
    notes = models.CharField(max_length=200, blank=True, default='')

    class Meta:
        ordering = ['cost_type_id', 'scope', '-effective_from']
        verbose_name = 'Cost Rate'
        verbose_name_plural = 'Cost Rates'
        indexes = [
            # The resolve path: every run recalculation filters on cost type +
            # company + date; VALUE lookups add the key.
            models.Index(fields=['cost_type', 'company', 'effective_from'],
                         name='idx_cost_rate_resolve'),
            models.Index(fields=['value_key'], name='idx_cost_rate_value_key'),
        ]
        constraints = [
            # One rate per type per scope per DATE. Nullable company splits each
            # scope into a NULL and a NOT NULL constraint (NULLs never collide
            # in a unique index).
            models.UniqueConstraint(
                fields=['cost_type', 'effective_from'],
                condition=models.Q(scope='FACTORY', is_active=True),
                name='uniq_active_factory_cost_rate_per_date'),
            models.UniqueConstraint(
                fields=['cost_type', 'company', 'effective_from'],
                condition=models.Q(scope='COMPANY', is_active=True),
                name='uniq_active_company_cost_rate_per_date'),
            models.UniqueConstraint(
                fields=['cost_type', 'department', 'effective_from'],
                condition=models.Q(scope='DEPARTMENT', company__isnull=True,
                                   is_active=True),
                name='uniq_active_department_cost_rate_per_date'),
            models.UniqueConstraint(
                fields=['cost_type', 'company', 'department', 'effective_from'],
                condition=models.Q(scope='DEPARTMENT', company__isnull=False,
                                   is_active=True),
                name='uniq_active_company_department_cost_rate_per_date'),
            models.UniqueConstraint(
                fields=['cost_type', 'value_key', 'effective_from'],
                condition=models.Q(scope='VALUE', company__isnull=True,
                                   is_active=True),
                name='uniq_active_value_cost_rate_per_date'),
            models.UniqueConstraint(
                fields=['cost_type', 'company', 'value_key', 'effective_from'],
                condition=models.Q(scope='VALUE', company__isnull=False,
                                   is_active=True),
                name='uniq_active_company_value_cost_rate_per_date'),
        ]

    def __str__(self):
        return (f"{self.cost_type.code} @ {self.rate} "
                f"({self.scope_label}, from {self.effective_from})")

    @property
    def scope_label(self) -> str:
        if self.scope == CostScope.FACTORY:
            return 'Factory-wide'
        if self.scope == CostScope.COMPANY:
            return self.company.code if self.company_id else 'Company?'
        if self.scope == CostScope.DEPARTMENT:
            dept = self.department.name if self.department_id else 'Department?'
            return f"{self.company.code} / {dept}" if self.company_id else dept
        prefix = f"{self.company.code} / " if self.company_id else ''
        return f"{prefix}{self.value_key}"
