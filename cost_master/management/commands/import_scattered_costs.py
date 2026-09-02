"""
Copy every cost rate already set around the app into the central Cost Master.

Usage::

    python manage.py import_scattered_costs             # dry run: report only
    python manage.py import_scattered_costs --commit    # actually write

Connects to whatever database the .env points at — the target HOST/NAME is
printed first, so check it before passing --commit.

Sources imported (idempotent — re-running upserts the same rows):

  1. blowing.BlowingCostRate           — full effective-dated history.
                                          machine NULL → COMPANY scope;
                                          per-machine  → VALUE "machine:<name>".
  2. production_execution.CostRate     — not dated at source; effective_from =
                                          the row's created_at date.
                                          line NULL → COMPANY scope;
                                          per-line  → VALUE "line:<name>".
                                          NOTE: its PER_UNIT means "Per Case"
                                          → mapped to PER_CASE.
  3. maintenance.ElectricityMeter      — rate_per_unit → VALUE "meter:<name>",
                                          company set when the meter is
                                          attributed to exactly one company.
  4. maintenance.SafetyViolationType   — default_fine_amount → FLAT rate at
                                          VALUE "violation:<name>" per company.

Deliberately NOT imported (and why):
  - blowing.BlowingRateConfig            legacy rate card; the blowing cost
                                         calculator no longer reads it.
  - blowing.PreformSpec / BottleBuyPrice item purchase-price masters, not
                                         cost rates.
  - BlowingMachine.depreciation_per_day  zeroed out in the cost calculator.
  - production_execution Resource*       per-run manual entries, transactional.
  - maintenance spare/fire unit_cost     inventory valuation, not a rate.

The source tables keep working untouched — their engines still read their own
rows. This import just makes the central master the complete catalog.
"""
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.conf import settings
from django.db import transaction

from blowing.models import BlowingCostRate
from production_execution.models import CostRate as ProductionCostRate
from maintenance.models import ElectricityMeter, SafetyViolationType

from cost_master import services
# The category→code catalog and basis maps are shared with the cost engines
# (cost_master/codes.py) so writer and readers can never drift apart.
from cost_master.codes import (
    BLOWING_COST_TYPES as BLOWING_TYPES,
    PRODUCTION_COST_TYPES as PRODUCTION_TYPES,
    PRODUCTION_BASIS_TO_CENTRAL as PRODUCTION_BASIS_MAP,
)
from cost_master.models import CostRate, CostScope, CostType


class DryRunRollback(Exception):
    pass


class Command(BaseCommand):
    help = "Import all cost rates scattered around the app into the central Cost Master."

    def add_arguments(self, parser):
        parser.add_argument(
            '--commit', action='store_true',
            help='Write the rows. Without this the command reports what it would do and rolls back.',
        )

    def handle(self, *args, **options):
        db = settings.DATABASES['default']
        self.stdout.write(self.style.WARNING(
            f"Database: {db.get('NAME')} @ {db.get('HOST') or 'localhost'} "
            f"({'COMMIT' if options['commit'] else 'DRY RUN — nothing will be written'})"
        ))

        self.types_created = 0
        self.rates_created = 0
        self.rates_updated = 0

        try:
            with transaction.atomic():
                self._import_blowing()
                self._import_production()
                self._import_electricity_meters()
                self._import_safety_fines()
                if not options['commit']:
                    raise DryRunRollback()
        except DryRunRollback:
            pass

        verb = 'Wrote' if options['commit'] else 'Would write'
        self.stdout.write(self.style.SUCCESS(
            f"{verb}: {self.types_created} cost types, "
            f"{self.rates_created} new rates, {self.rates_updated} updated rates."
        ))
        if not options['commit']:
            self.stdout.write("Re-run with --commit to write.")

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _get_type(self, meta) -> CostType:
        code, name, basis, is_credit, description = meta
        cost_type, created = CostType.objects.get_or_create(
            code=code,
            defaults={'name': name, 'default_basis': basis,
                      'is_credit': is_credit, 'description': description},
        )
        if created:
            self.types_created += 1
        return cost_type

    def _upsert(self, cost_type, scope, rate, effective_from,
                company_id=None, department_id=None, value_key='',
                basis=None, notes=''):
        existed = CostRate.objects.filter(
            cost_type=cost_type, scope=scope, company_id=company_id,
            department_id=department_id, value_key=value_key,
            effective_from=effective_from, is_active=True,
        ).exists()
        services.upsert_rate({
            'cost_type_id': cost_type.id,
            'scope': scope,
            'company_id': company_id,
            'department_id': department_id,
            'value_key': value_key,
            'basis': basis or cost_type.default_basis,
            'rate': rate,
            'notes': notes[:200],
            'effective_from': effective_from,
        })
        if existed:
            self.rates_updated += 1
        else:
            self.rates_created += 1

    # ------------------------------------------------------------------
    # sources
    # ------------------------------------------------------------------
    def _import_blowing(self):
        rows = (BlowingCostRate.objects.filter(is_active=True)
                .select_related('machine', 'company')
                .order_by('effective_from'))
        count = 0
        for row in rows:
            meta = BLOWING_TYPES.get(row.category)
            if meta is None:
                self.stdout.write(self.style.WARNING(
                    f"  blowing: unknown category {row.category!r}, skipped (id={row.id})"))
                continue
            cost_type = self._get_type(meta)
            if row.machine_id:
                scope, value_key = CostScope.VALUE, f"machine:{row.machine.name}"
            else:
                scope, value_key = CostScope.COMPANY, ''
            self._upsert(
                cost_type, scope, row.rate, row.effective_from,
                company_id=row.company_id, value_key=value_key,
                basis=row.basis,
                notes=row.label or 'Imported from blowing Cost Master',
            )
            count += 1
        self.stdout.write(f"blowing Cost Master: {count} dated rate rows")

    def _import_production(self):
        rows = (ProductionCostRate.objects.filter(is_active=True)
                .select_related('line', 'company'))
        count = 0
        for row in rows:
            meta = PRODUCTION_TYPES.get(row.category)
            if meta is None:
                self.stdout.write(self.style.WARNING(
                    f"  production: unknown category {row.category!r}, skipped (id={row.id})"))
                continue
            cost_type = self._get_type(meta)
            if row.line_id:
                scope, value_key = CostScope.VALUE, f"line:{row.line.name}"
            else:
                scope, value_key = CostScope.COMPANY, ''
            self._upsert(
                cost_type, scope, row.rate, row.created_at.date(),
                company_id=row.company_id, value_key=value_key,
                basis=PRODUCTION_BASIS_MAP.get(row.basis, 'PER_CASE'),
                notes=row.label or 'Imported from production-execution Cost Master',
            )
            count += 1
        self.stdout.write(f"production-execution Cost Master: {count} rate rows")

    def _import_electricity_meters(self):
        meter_type = None
        count = 0
        for meter in (ElectricityMeter.objects.filter(is_active=True)
                      .prefetch_related('companies')):
            if meter.rate_per_unit <= Decimal('0'):
                continue
            if meter_type is None:
                meter_type = self._get_type((
                    'electricity-meter-unit-rate', 'Electricity — Meter Unit Rate',
                    'PER_UNIT', False,
                    'Default ₹/unit each meter costs its daily readings at.'))
            attributed = list(meter.companies.all())
            company_id = attributed[0].id if len(attributed) == 1 else None
            self._upsert(
                meter_type, CostScope.VALUE, meter.rate_per_unit,
                meter.created_at.date(),
                company_id=company_id, value_key=f"meter:{meter.name}",
                notes='Imported from maintenance electricity meters',
            )
            count += 1
        self.stdout.write(f"maintenance electricity meters: {count} meter rates")

    def _import_safety_fines(self):
        fine_type = None
        count = 0
        for violation in (SafetyViolationType.objects.filter(is_active=True)
                          .select_related('company')):
            if violation.default_fine_amount <= Decimal('0'):
                continue
            if fine_type is None:
                fine_type = self._get_type((
                    'safety-violation-fine', 'Safety — Violation Fine',
                    'FLAT', False,
                    'Default fine per safety violation type.'))
            self._upsert(
                fine_type, CostScope.VALUE, violation.default_fine_amount,
                violation.created_at.date(),
                company_id=violation.company_id,
                value_key=f"violation:{violation.name}",
                notes='Imported from maintenance safety violation types',
            )
            count += 1
        self.stdout.write(f"maintenance safety violation fines: {count} fine rates")
