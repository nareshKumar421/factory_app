"""Snapshot ``litres_per_piece`` (SAP OITM.SalPackUn) onto existing runs.

Runs created from now on take the snapshot at creation, but every historical
run has it null, so the production dashboards would show "—" for their litres.
This fills them in from the item master.

Litres produced by a run = cases x ``pieces_per_case`` (OITM.SalFactor2) x
``litres_per_piece`` (OITM.SalPackUn). SalPackUn is the volume of one billed
piece and is the single source of litres across the app — the SKU name is not:
it states the piece volume and the carton size separately and lies about both
(a "1 LTR + 1 LTR COMBO" piece holds two litres, a CSD "1 LTR 16 PCS" carton
sixteen).

Idempotent: only runs where ``litres_per_piece`` IS NULL are touched. Runs whose
SKU holds no liquid (``U_IsLitre`` = 'N' in SAP — cartons, caps, powders) have
no SalPackUn to read and are reported as skipped; their litres stay "—", which
is correct rather than zero.

Usage:
    python manage.py backfill_run_litres_per_piece --dry-run
    python manage.py backfill_run_litres_per_piece
    python manage.py backfill_run_litres_per_piece --company JIVO_BEVERAGES
"""
from decimal import Decimal

from django.core.management.base import BaseCommand

from production_execution.models import ProductionRun
from production_execution.services.sap_reader import ProductionOrderReader, SAPReadError


class Command(BaseCommand):
    help = "Snapshot litres per piece (SAP OITM.SalPackUn) onto runs missing it."

    def add_arguments(self, parser):
        parser.add_argument('--company', help='Limit to a single company code.')
        parser.add_argument('--dry-run', action='store_true',
                            help='Report what would change without saving.')

    def handle(self, *args, **opts):
        dry = opts.get('dry_run')

        runs = ProductionRun.objects.select_related('company').filter(
            litres_per_piece__isnull=True,
        ).order_by('company__code', 'id')
        if opts.get('company'):
            runs = runs.filter(company__code=opts['company'])

        # One batched SalPackUn lookup per company.
        codes_by_company = {}
        for run in runs:
            if run.item_code.strip():
                codes_by_company.setdefault(run.company.code, set()).add(run.item_code.strip())

        litres = {}  # (company_code, item_code) -> float
        for company_code, codes in codes_by_company.items():
            try:
                reader = ProductionOrderReader(company_code)
                litre_map = reader.get_litres_per_piece_map(sorted(codes))
            except SAPReadError as e:
                self.stderr.write(self.style.ERROR(
                    f"[{company_code}] SAP lookup failed, company skipped: {e}"))
                continue
            for code, value in litre_map.items():
                litres[(company_code, code)] = value
            self.stdout.write(f"[{company_code}] SalPackUn for "
                              f"{len(litre_map)}/{len(codes)} items")

        filled = skipped = 0
        for run in runs:
            item_code = run.item_code.strip()
            value = litres.get((run.company.code, item_code))
            if not value:
                reason = 'no item code' if not item_code else 'no SalPackUn (not a litre item)'
                self.stdout.write(self.style.WARNING(
                    f"run {run.id} [{run.company.code}] '{item_code or '-'}': "
                    f"{reason} — SKIPPED"))
                skipped += 1
                continue

            per_case = float(run.pieces_per_case or 1) * value
            self.stdout.write(
                f"run {run.id} [{run.company.code}] {item_code}: "
                f"{value} L/piece x {run.pieces_per_case or 1} pcs = {per_case} L/case")
            if not dry:
                run.litres_per_piece = Decimal(str(value))
                run.save(update_fields=['litres_per_piece', 'updated_at'])
            filled += 1

        verb = 'Would fill' if dry else 'Filled'
        self.stdout.write(self.style.SUCCESS(
            f"{verb} {filled} run(s), skipped {skipped}."))
