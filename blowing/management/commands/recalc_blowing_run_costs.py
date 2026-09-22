"""Re-cost existing blowing runs against the Cost Master as it stands now.

A run's cost is computed when the run is created, edited or completed — and a
completed run cannot be edited, so nothing re-costs it afterwards. Correcting a
rate in the Cost Master therefore leaves every run already booked at the old
one. This is the way back:

    python manage.py recalc_blowing_run_costs --company JIVO_OIL --date-from 2026-09-01
    python manage.py recalc_blowing_run_costs --run 71

Named for the app rather than ``recalc_run_costs`` (production_execution's
command) because Django's command registry is flat — two apps defining the same
name means one silently shadows the other, and re-costing the wrong module's
runs is not a mistake that announces itself.

Rates resolve as of each run's OWN date, so this reprices history at the rate
that applied then, not at today's. A run dated before a new rate's
``effective_from`` keeps the older rate — that is the point, not a bug.
"""
from django.core.management.base import BaseCommand

from blowing.models import BlowingRun
from blowing.services.cost_calculator import recalculate_run_cost


class Command(BaseCommand):
    help = "Recompute BlowingRunCost from the Cost Master for existing runs."

    def add_arguments(self, parser):
        parser.add_argument("--company", help="Company code (e.g. JIVO_OIL). Default: all.")
        parser.add_argument("--date-from", help="ISO date (inclusive).")
        parser.add_argument("--date-to", help="ISO date (inclusive).")
        parser.add_argument("--run", type=int, help="A single run id.")
        parser.add_argument("--machine", type=int, help="Blowing machine id.")
        parser.add_argument(
            "--completed-only", action="store_true",
            help="Only runs with status COMPLETED (default: all runs).",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report what each run's cost would become without writing it.",
        )

    def handle(self, *args, **opts):
        qs = BlowingRun.objects.select_related('company', 'machine', 'preform_spec')
        if opts.get("run"):
            qs = qs.filter(id=opts["run"])
        if opts.get("company"):
            qs = qs.filter(company__code=opts["company"])
        if opts.get("date_from"):
            qs = qs.filter(date__gte=opts["date_from"])
        if opts.get("date_to"):
            qs = qs.filter(date__lte=opts["date_to"])
        if opts.get("machine"):
            qs = qs.filter(machine_id=opts["machine"])
        if opts.get("completed_only"):
            qs = qs.filter(status="COMPLETED")

        dry_run = opts.get("dry_run")
        total = qs.count()
        self.stdout.write(
            f"{'Costing (dry run)' if dry_run else 'Recalculating cost for'} {total} run(s)..."
        )
        ok = err = 0
        for run in qs.order_by('id').iterator():
            before = getattr(getattr(run, 'cost_summary', None), 'blowing_cost', None)
            try:
                if dry_run:
                    from blowing.services.cost_calculator import compute_run_cost
                    after = compute_run_cost(run)['blowing_cost']
                else:
                    recalculate_run_cost(run)
                    after = run.cost_summary.blowing_cost
                ok += 1
                if before is None or before != after:
                    self.stdout.write(
                        f"  run {run.id} ({run.date}): blowing cost "
                        f"{'—' if before is None else before} → {after}"
                    )
            except Exception as exc:  # noqa: BLE001 - report and carry on
                err += 1
                self.stderr.write(f"  run {run.id}: {exc}")
        verb = 'would be recalculated' if dry_run else 'recalculated'
        self.stdout.write(self.style.SUCCESS(f"Done. {ok} {verb}, {err} failed."))
