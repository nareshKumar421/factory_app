"""Backfill ProductionRun.item_code from the stored product name via SAP.

Runs are not created from SAP production orders, so the BOM is fetched by
item code (OITT/ITT1). Older runs stored only the product *name*; this command
resolves each run's SAP item code from that name and saves it, so BOM requests
and auto-populate work.

Usage:
    python manage.py backfill_run_item_codes            # all companies, all runs missing item_code
    python manage.py backfill_run_item_codes --company JIVO_OIL
    python manage.py backfill_run_item_codes --run-id 57
    python manage.py backfill_run_item_codes --dry-run
"""
from django.core.management.base import BaseCommand

from production_execution.models import ProductionRun
from production_execution.services.sap_reader import ProductionOrderReader, SAPReadError


class Command(BaseCommand):
    help = "Resolve and store ProductionRun.item_code from the product name via SAP."

    def add_arguments(self, parser):
        parser.add_argument('--company', help='Limit to a single company code.')
        parser.add_argument('--run-id', type=int, help='Backfill a single run by id.')
        parser.add_argument('--dry-run', action='store_true',
                            help='Report what would change without saving.')

    def handle(self, *args, **opts):
        qs = ProductionRun.objects.select_related('company').filter(
            item_code='', product__gt='',
        ).order_by('company__code', 'id')
        if opts.get('company'):
            qs = qs.filter(company__code=opts['company'])
        if opts.get('run_id'):
            qs = qs.filter(id=opts['run_id'])

        dry = opts.get('dry_run')
        readers = {}
        resolved = skipped = failed = 0

        for run in qs:
            code_for_company = run.company.code
            reader = readers.get(code_for_company)
            if reader is None:
                try:
                    reader = ProductionOrderReader(code_for_company)
                except SAPReadError as e:
                    self.stderr.write(self.style.ERROR(
                        f"[{code_for_company}] SAP init failed: {e}"))
                    readers[code_for_company] = None
                    continue
                readers[code_for_company] = reader
            if reader is None:
                failed += 1
                continue

            try:
                code = reader.resolve_item_code_by_name(run.product)
            except SAPReadError as e:
                self.stderr.write(self.style.ERROR(
                    f"run {run.id} '{run.product}': lookup failed: {e}"))
                failed += 1
                continue

            if not code:
                self.stdout.write(self.style.WARNING(
                    f"run {run.id} [{code_for_company}] '{run.product}': "
                    f"no unique item code — SKIPPED"))
                skipped += 1
                continue

            self.stdout.write(
                f"run {run.id} [{code_for_company}] '{run.product}' -> {code}")
            if not dry:
                run.item_code = code
                run.save(update_fields=['item_code', 'updated_at'])
            resolved += 1

        verb = 'Would resolve' if dry else 'Resolved'
        self.stdout.write(self.style.SUCCESS(
            f"{verb} {resolved}, skipped {skipped}, failed {failed}."))
