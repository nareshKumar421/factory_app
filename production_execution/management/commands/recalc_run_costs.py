"""Backfill derived run costs (Cost Master x execution time) for existing runs.

The costing engine only recomputes a run's cost when it's triggered (run
complete / resource save). After changing rates in the Cost Master, or after
first enabling the engine, run this to recompute historical runs so the
dashboard cost section reflects the current rates.

    python manage.py recalc_run_costs --company JIVO_OIL --date-from 2026-07-01
"""
from django.core.management.base import BaseCommand

from production_execution.models import ProductionRun
from production_execution.services.cost_calculator import recalculate_run_cost


class Command(BaseCommand):
    help = "Recompute ProductionRunCost from the Cost Master for existing runs."

    def add_arguments(self, parser):
        parser.add_argument("--company", help="Company code (e.g. JIVO_OIL). Default: all.")
        parser.add_argument("--date-from", help="ISO date (inclusive).")
        parser.add_argument("--date-to", help="ISO date (inclusive).")
        parser.add_argument("--line", type=int, help="Production line id.")
        parser.add_argument(
            "--completed-only", action="store_true",
            help="Only runs with status COMPLETED (default: all runs).",
        )

    def handle(self, *args, **opts):
        qs = ProductionRun.objects.all()
        if opts.get("company"):
            qs = qs.filter(company__code=opts["company"])
        if opts.get("date_from"):
            qs = qs.filter(date__gte=opts["date_from"])
        if opts.get("date_to"):
            qs = qs.filter(date__lte=opts["date_to"])
        if opts.get("line"):
            qs = qs.filter(line_id=opts["line"])
        if opts.get("completed_only"):
            qs = qs.filter(status="COMPLETED")

        total = qs.count()
        self.stdout.write(f"Recalculating cost for {total} run(s)...")
        ok = err = 0
        for run in qs.iterator():
            try:
                recalculate_run_cost(run)
                ok += 1
            except Exception as exc:  # noqa: BLE001 - report and continue
                err += 1
                self.stderr.write(f"  run {run.id}: {exc}")
        self.stdout.write(self.style.SUCCESS(f"Done. {ok} recalculated, {err} failed."))
