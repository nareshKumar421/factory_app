"""One-time conversion of rated speeds from cases/hr to bottles/hr.

Historically ``rated_speed`` (on ProductionRun and LineSkuConfig) was entered
in cases/hr. The field is now defined as bottles/hr, with the bottles-per-case
factor (SAP OITM.SalFactor2) stored on ``pieces_per_case``. This command
converts existing rows:

    rated_speed   = old rated_speed x SalFactor2
    pieces_per_case = SalFactor2

Idempotent: only rows where ``pieces_per_case`` IS NULL are touched, and the
factor is stored in the same save — running the command twice never
double-multiplies. Rows whose item has no SalFactor2 in SAP (or no item code
at all) are left untouched and reported, so their speeds stay in the old unit
until fixed by hand.

Only rows created before ``--before`` (default: today) are converted — a run
created after the per-bottle deploy already holds bottles/hr even when its
``pieces_per_case`` is null (SAP was down at creation), and multiplying it
would corrupt it. When running this later than deploy day, pass the deploy
date explicitly.

Usage:
    python manage.py backfill_rated_speed_per_bottle --dry-run   # report only
    python manage.py backfill_rated_speed_per_bottle
    python manage.py backfill_rated_speed_per_bottle --company JIVO_BEVERAGES
    python manage.py backfill_rated_speed_per_bottle --before 2026-08-06
"""
import datetime

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from production_execution.models import LineSkuConfig, ProductionRun
from production_execution.services.sap_reader import ProductionOrderReader, SAPReadError


class Command(BaseCommand):
    help = "Convert rated_speed from cases/hr to bottles/hr using SAP OITM.SalFactor2."

    def add_arguments(self, parser):
        parser.add_argument('--company', help='Limit to a single company code.')
        parser.add_argument('--dry-run', action='store_true',
                            help='Report what would change without saving.')
        parser.add_argument('--before',
                            help='Only convert rows created before this date '
                                 '(YYYY-MM-DD, default today) — i.e. rows from '
                                 'the cases/hr era.')

    def handle(self, *args, **opts):
        dry = opts.get('dry_run')
        if opts.get('before'):
            try:
                cutoff = datetime.date.fromisoformat(opts['before'])
            except ValueError:
                raise CommandError('--before must be YYYY-MM-DD')
        else:
            cutoff = timezone.localdate()

        runs = ProductionRun.objects.select_related('company').filter(
            pieces_per_case__isnull=True,
            created_at__date__lt=cutoff,
        ).order_by('company__code', 'id')
        configs = LineSkuConfig.objects.select_related('company').filter(
            pieces_per_case__isnull=True,
            created_at__date__lt=cutoff,
        ).order_by('company__code', 'id')
        if opts.get('company'):
            runs = runs.filter(company__code=opts['company'])
            configs = configs.filter(company__code=opts['company'])

        # One batched SalFactor2 lookup per company.
        codes_by_company = {}
        for run in runs:
            if run.item_code.strip():
                codes_by_company.setdefault(run.company.code, set()).add(run.item_code.strip())
        for cfg in configs:
            if cfg.sku_code.strip():
                codes_by_company.setdefault(cfg.company.code, set()).add(cfg.sku_code.strip())

        factors = {}  # (company_code, item_code) -> int
        for company_code, codes in codes_by_company.items():
            try:
                reader = ProductionOrderReader(company_code)
                factor_map = reader.get_pieces_per_case_map(sorted(codes))
            except SAPReadError as e:
                self.stderr.write(self.style.ERROR(
                    f"[{company_code}] SAP lookup failed, company skipped: {e}"))
                continue
            for code, factor in factor_map.items():
                factors[(company_code, code)] = factor
            self.stdout.write(f"[{company_code}] SalFactor2 for "
                              f"{len(factor_map)}/{len(codes)} items:")
            for code in sorted(codes):
                factor = factor_map.get(code)
                mark = str(factor) if factor else 'MISSING'
                self.stdout.write(f"    {code}: {mark}")

        converted = skipped = 0
        for label, qs, code_attr in (('run', runs, 'item_code'),
                                     ('config', configs, 'sku_code')):
            for obj in qs:
                item_code = getattr(obj, code_attr).strip()
                factor = factors.get((obj.company.code, item_code))
                if not factor:
                    reason = 'no item code' if not item_code else 'no SalFactor2'
                    self.stdout.write(self.style.WARNING(
                        f"{label} {obj.id} [{obj.company.code}] "
                        f"'{item_code or '-'}' speed={obj.rated_speed}: "
                        f"{reason} — SKIPPED (still cases/hr!)"))
                    skipped += 1
                    continue

                old_speed = obj.rated_speed
                new_speed = old_speed * factor if old_speed is not None else None
                self.stdout.write(
                    f"{label} {obj.id} [{obj.company.code}] {item_code}: "
                    f"{old_speed} cases/hr -> {new_speed} bottles/hr (x{factor})")
                if not dry:
                    obj.pieces_per_case = factor
                    obj.rated_speed = new_speed
                    obj.save(update_fields=['pieces_per_case', 'rated_speed', 'updated_at'])
                converted += 1

        verb = 'Would convert' if dry else 'Converted'
        self.stdout.write(self.style.SUCCESS(
            f"{verb} {converted} row(s), skipped {skipped}."))
