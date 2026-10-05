"""Load the dispatch desk's transport fare workbook into the Freight Benchmarks.

    python manage.py import_freight_benchmarks "UPDATED TRANSPORT FARE.xlsx" --dry-run
    python manage.py import_freight_benchmarks "UPDATED TRANSPORT FARE.xlsx"

Only the benchmark columns are read; the transporters' own rates beside them are
not (see `dispatch_plans.freight_benchmark_import` for how the sheets are told
apart). For every destination the workbook lists, its rates become the
workbook's -- a slab left blank there loses its rate here. Destinations the
workbook does not list are left as they are and named in the report.

`--dry-run` does the whole write inside a transaction and rolls it back, so the
counts it prints are the real ones.
"""

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from dispatch_plans.freight_benchmark_import import apply_workbook, parse_workbook


class _DryRun(Exception):
    pass


class Command(BaseCommand):
    help = "Import benchmark freight (destination x slab) from the transport fare workbook."

    def add_arguments(self, parser):
        parser.add_argument("path", help="The .xlsx workbook.")
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would change and write nothing.",
        )
        parser.add_argument(
            "--user",
            help="Email to stamp as having made the change (default: nobody).",
        )

    def handle(self, *args, **options):
        user = None
        if options["user"]:
            user = get_user_model().objects.filter(email=options["user"]).first()
            if user is None:
                raise CommandError(f"No user with email {options['user']}.")

        try:
            parsed = parse_workbook(options["path"])
        except FileNotFoundError:
            raise CommandError(f"No such file: {options['path']}")

        for block in parsed.skipped_blocks:
            self.stdout.write(f"skipped  {block}")
        for problem in parsed.problems:
            self.stdout.write(self.style.WARNING(f"problem  {problem}"))
        if not parsed.destinations:
            raise CommandError("The workbook has no benchmark rows this could read.")

        result = None
        try:
            with transaction.atomic():
                result = apply_workbook(parsed, user=user)
                if options["dry_run"]:
                    raise _DryRun
        except _DryRun:
            pass

        verb = "would be " if options["dry_run"] else ""
        rated = sum(1 for d in parsed.destinations if d.rates)
        self.stdout.write(
            f"read     {len(parsed.destinations)} destinations "
            f"({len(parsed.destinations) - rated} with no benchmark yet)"
        )
        if result.slabs_created:
            self.stdout.write(f"slabs    {verb}created: {', '.join(result.slabs_created)}")
        self.stdout.write(
            f"places   {result.destinations_created} {verb}created, "
            f"{result.destinations_updated} {verb}updated, "
            f"{result.destinations_unchanged} unchanged"
        )
        self.stdout.write(
            f"rates    {result.rates_created} {verb}added, "
            f"{result.rates_changed} {verb}changed, "
            f"{result.rates_removed} {verb}removed"
        )
        for name in result.not_in_workbook:
            self.stdout.write(f"kept     {name} -- not in the workbook, left as it is")

        if options["dry_run"]:
            self.stdout.write(self.style.WARNING("Dry run: nothing was written."))
        else:
            self.stdout.write(self.style.SUCCESS("Imported."))
