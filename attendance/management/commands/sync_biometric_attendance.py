"""
Roll stored punches up into the daily attendance sheet.

    # today
    python manage.py sync_biometric_attendance

    # a range, e.g. after the agent was down for a week
    python manage.py sync_biometric_attendance --date-from 2026-09-01 --date-to 2026-09-17

    # the usual nightly job: today and the day before, because a late punch-out
    # lands after midnight and a day is not final until the next one starts
    python manage.py sync_biometric_attendance --days 2

The punches come from :mod:`attendance.punch_store` -- our own database -- where
an agent inside the plant puts them, because the punch machines are not reachable
from this server. This command no longer talks to them. The name is kept because
schedulers and runbooks already say it.

Safe to re-run over any range. Corrections made by hand are preserved -- only
the machine's own columns are refreshed -- so a re-sync repairs punch data
without undoing anybody's decision. See :mod:`attendance.services`.

**It refuses to run on a stale punch mirror**, and exits non-zero so a scheduler
notices. This is the whole safety mechanism now. Rolling up punches the agent
has not delivered does not fail -- it quietly writes ABSENT for three hundred
people, and payroll is run from the result. Being unable to ask used to look
different from nobody turning up; it has to keep looking different.
Override with ``--allow-stale`` when that is genuinely what you want, e.g. when
back-filling a range you know the agent has already covered.
"""

from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from attendance import punch_store
from attendance.services import sync_range
from company.models import Company


class Command(BaseCommand):
    help = "Roll stored punches up into the daily attendance sheet."

    def add_arguments(self, parser):
        parser.add_argument("--date-from", help="YYYY-MM-DD. Defaults to --days back from today.")
        parser.add_argument("--date-to", help="YYYY-MM-DD. Defaults to today.")
        parser.add_argument(
            "--days",
            type=int,
            default=1,
            help="How many days back from --date-to to cover when --date-from is not given.",
        )
        parser.add_argument("--company", help="Limit to one company code.")
        parser.add_argument(
            "--quiet-progress",
            action="store_true",
            help="Print only the totals, not a line per day.",
        )
        parser.add_argument(
            "--allow-stale",
            action="store_true",
            help="Roll up even if the punch agent has not reported recently. "
            "Marks people absent for days whose punches never arrived -- say so deliberately.",
        )

    def handle(self, *args, **options):
        today = timezone.localdate()
        date_to = (
            timezone.datetime.strptime(options["date_to"], "%Y-%m-%d").date()
            if options["date_to"]
            else today
        )
        date_from = (
            timezone.datetime.strptime(options["date_from"], "%Y-%m-%d").date()
            if options["date_from"]
            else date_to - timedelta(days=max(options["days"], 1) - 1)
        )
        if date_from > date_to:
            raise CommandError("--date-from is after --date-to.")

        company = None
        if options["company"]:
            company = Company.objects.filter(code=options["company"]).first()
            if company is None:
                raise CommandError(f"No company with code {options['company']}.")

        def progress(day, created, updated, kept):
            if not options["quiet_progress"]:
                note = f", {kept} corrections kept" if kept else ""
                self.stdout.write(f"  {day}: {created} new, {updated} refreshed{note}")

        # The stale check, before anything is written. See the module docstring:
        # an absent punch mirror and an absent workforce are indistinguishable
        # once the rows are written, so the refusal has to come first.
        source = punch_store.health()
        if not source["reachable"] and not options["allow_stale"]:
            raise CommandError(
                f"{source['detail']} Refusing to roll up: this would mark people absent for "
                f"days whose punches never arrived. Re-run with --allow-stale if that is "
                f"genuinely what you want."
            )
        if not source["reachable"]:
            self.stdout.write(self.style.WARNING(f"Punch data is not current: {source['detail']}"))

        self.stdout.write(f"Rolling up punches {date_from} .. {date_to}")
        totals = sync_range(date_from, date_to, company=company, progress=progress)

        self.stdout.write(
            self.style.SUCCESS(
                f"{totals['punches']} punches over {totals['days']} day(s): "
                f"{totals['created']} rows created, {totals['updated']} refreshed, "
                f"{totals['kept_overrides']} corrections preserved."
            )
        )
