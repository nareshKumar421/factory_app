"""Seed the standard per-line speed presets (LineSkuConfig).

Speeds were specified by production in BOTTLES PER MINUTE and are stored
here in bottles/hr (x60), matching the rated_speed unit:

    clearpack   1 LTR 80/min   5 LTR 50/min
    JP          1 LTR 90/min
    10 head     1 LTR 35/min   2 LTR 21/min   5 LTR 15/min
    6 head      1 LTR 18/min   2 LTR 12/min   5 LTR 10/min
    pouch line  Hitech 30/min  Samarpan 40/min

Lines are matched by name pattern against ProductionLine (all companies, or
--company). Unmatched patterns are reported so the run can be repeated after
the line is created/renamed. Idempotent: a line that already has an active
config with the same name is skipped.

Usage:
    python manage.py seed_line_speed_configs --dry-run
    python manage.py seed_line_speed_configs
    python manage.py seed_line_speed_configs --company JIVO_OIL
"""
import re

from django.core.management.base import BaseCommand

from production_execution.models import LineSkuConfig, ProductionLine

# (line-name regex, [(config title, bottles per MINUTE)])
SEED = [
    (r'clear\s*pack', [('1 LTR', 80), ('5 LTR', 50)]),
    (r'\bjp\b', [('1 LTR', 90)]),
    (r'10\s*-?\s*head', [('1 LTR', 35), ('2 LTR', 21), ('5 LTR', 15)]),
    (r'6\s*-?\s*head', [('1 LTR', 18), ('2 LTR', 12), ('5 LTR', 10)]),
    (r'pouch', [('Hitech', 30), ('Samarpan', 40)]),
]


class Command(BaseCommand):
    help = "Seed standard line speed presets (bottles/min spec stored as bottles/hr)."

    def add_arguments(self, parser):
        parser.add_argument('--company', help='Limit to a single company code.')
        parser.add_argument('--dry-run', action='store_true',
                            help='Report what would be created without saving.')

    def handle(self, *args, **opts):
        dry = opts.get('dry_run')
        lines = ProductionLine.objects.select_related('company').filter(is_active=True)
        if opts.get('company'):
            lines = lines.filter(company__code=opts['company'])
        lines = list(lines.order_by('company__code', 'name'))

        created = skipped = 0
        for pattern, presets in SEED:
            matched = [l for l in lines if re.search(pattern, l.name, re.IGNORECASE)]
            if not matched:
                self.stdout.write(self.style.WARNING(
                    f"No active line matches /{pattern}/ — presets NOT created: "
                    f"{', '.join(name for name, _ in presets)}"))
                continue
            for line in matched:
                for title, per_minute in presets:
                    exists = LineSkuConfig.objects.filter(
                        company=line.company, line=line,
                        config_name=title, is_active=True,
                    ).exists()
                    if exists:
                        self.stdout.write(
                            f"[{line.company.code}] {line.name} / '{title}': "
                            f"already exists — skipped")
                        skipped += 1
                        continue
                    per_hour = per_minute * 60
                    self.stdout.write(
                        f"[{line.company.code}] {line.name} / '{title}': "
                        f"{per_minute}/min -> {per_hour} bottles/hr")
                    if not dry:
                        LineSkuConfig.objects.create(
                            company=line.company, line=line,
                            config_name=title, rated_speed=per_hour,
                        )
                    created += 1

        verb = 'Would create' if dry else 'Created'
        self.stdout.write(self.style.SUCCESS(
            f"{verb} {created} config(s), skipped {skipped} existing."))
