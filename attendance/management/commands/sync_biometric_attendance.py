"""
Roll punch-machine reads up into the daily attendance sheet.

    # today
    python manage.py sync_biometric_attendance

    # a range, e.g. after the LAN link was down for a week
    python manage.py sync_biometric_attendance --date-from 2026-09-01 --date-to 2026-09-17

    # the usual nightly job: today and the day before, because a late punch-out
    # lands after midnight and a day is not final until the next one starts
    python manage.py sync_biometric_attendance --days 2

Safe to re-run over any range. Corrections made by hand are preserved -- only
the machine's own columns are refreshed -- so a re-sync repairs punch data
without undoing anybody's decision. See :mod:`attendance.services`.

Exits non-zero if the punch database cannot be reached, so a scheduler notices.
A silent failure here is the expensive one: nobody's punches arrive and three
hundred people read as absent.
"""

from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from attendance import biometrics
from attendance.services import sync_range
from company.models import Company


class Command(BaseCommand):
    help = "Sync punch-machine reads into the daily attendance sheet."

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

        self.stdout.write(f"Syncing punches {date_from} .. {date_to}")
        try:
            totals = sync_range(date_from, date_to, company=company, progress=progress)
        except biometrics.BiometricsUnavailable as exc:
            # Not a stack trace: this is nearly always the LAN link or the box
            # being off, and whoever reads it needs the sentence, not the frames.
            raise CommandError(str(exc)) from exc

        self.stdout.write(
            self.style.SUCCESS(
                f"{totals['punches']} punches over {totals['days']} day(s): "
                f"{totals['created']} rows created, {totals['updated']} refreshed, "
                f"{totals['kept_overrides']} corrections preserved."
            )
        )
