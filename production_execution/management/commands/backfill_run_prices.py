"""
Backfill BOM material unit prices (SAP LastPurPrc) onto production runs whose
material lines were created without a price snapshot, then recompute their cost.

Runs created before the material price snapshot existed (or before SAP had a
price for the item) keep ``unit_price = NULL`` on every material line, so their
run cost shows ₹0 raw material. This re-runs the same snapshot the app does at
run creation and recalculates the derived cost.

Usage:
    python manage.py backfill_run_prices 63                 # one run (by DB id)
    python manage.py backfill_run_prices 63 71 88           # several runs
    python manage.py backfill_run_prices --all-missing      # every run missing prices
    python manage.py backfill_run_prices --all-missing --company JIVO_OIL
"""
from django.core.management.base import BaseCommand

from production_execution.models import ProductionRun
from production_execution.services.cost_calculator import recalculate_run_cost
from production_execution.services.production_service import ProductionExecutionService


class Command(BaseCommand):
    help = (
        "Snapshot BOM material prices from SAP and recalculate cost for runs "
        "whose material lines have no unit_price."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            'run_ids', nargs='*', type=int,
            help='Specific production run id(s) to backfill.',
        )
        parser.add_argument(
            '--all-missing', action='store_true',
            help='Backfill every run that has material lines with no unit_price.',
        )
        parser.add_argument(
            '--company', type=str, default=None,
            help='Limit --all-missing to a single company code.',
        )

    def handle(self, *args, **opts):
        run_ids = list(opts['run_ids'])

        if opts['all_missing']:
            qs = ProductionRun.objects.filter(
                material_usages__unit_price__isnull=True
            ).distinct()
            if opts['company']:
                qs = qs.filter(company__code=opts['company'])
            run_ids = list(qs.values_list('id', flat=True))

        if not run_ids:
            self.stdout.write("No runs to process.")
            return

        self.stdout.write(f"Processing {len(run_ids)} run(s)...")
        ok = failed = 0
        for rid in run_ids:
            try:
                run = ProductionRun.objects.select_related('company').get(id=rid)
            except ProductionRun.DoesNotExist:
                self.stderr.write(f"  Run id {rid}: not found")
                failed += 1
                continue

            service = ProductionExecutionService(run.company.code)
            try:
                service._snapshot_material_prices(run)
            except Exception as e:  # SAP unreachable / no BOM / read error
                self.stderr.write(f"  Run #{run.run_number} (id {rid}): SAP price fetch failed: {e}")
                failed += 1
                continue

            recalculate_run_cost(run)

            total = run.material_usages.count()
            priced = run.material_usages.exclude(unit_price__isnull=True).count()
            cost = getattr(run, 'cost_summary', None)
            raw = cost.raw_material_cost if cost else 0
            net = cost.net_cost if cost else 0
            self.stdout.write(self.style.SUCCESS(
                f"  Run #{run.run_number} (id {rid}): priced {priced}/{total} lines "
                f"-> raw_material={raw}, net={net}"
            ))
            ok += 1

        self.stdout.write(f"Done. {ok} succeeded, {failed} failed.")
