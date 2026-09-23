"""
Put approved leave onto the attendance sheet.

The second half of :mod:`leave.projection`. Leave is approved in advance, but
``DailyAttendance`` rows do not exist until ``sync_biometric_attendance`` has
run for that date -- so an approval made in September for a date in October
writes nothing at the time, and this is what picks it up once the row appears.

**Run it after the sync, every time.** On its own it does nothing useful; run
before the sync it finds no rows to write to. The pairing is the whole design::

    python manage.py sync_biometric_attendance --days 2
    python manage.py project_approved_leave --days 2

Suggested cron, following the sync's own nightly slot::

    30 2 * * *  cd /path/to/factory_app && ./venv/bin/python manage.py sync_biometric_attendance --days 2 --quiet-progress \
                && ./venv/bin/python manage.py project_approved_leave --days 2

Idempotent over any range: a day already projected is skipped, so re-running
repairs a partial run without writing anything twice. Safe to run over a wide
window to catch up after an outage.
"""

from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from company.models import Company

from ...projection import project_range


class Command(BaseCommand):
    help = "Write approved leave onto the attendance sheet (run after the punch sync)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            help="Project today and the N-1 days before it. Mirrors the sync's own flag.",
        )
        parser.add_argument("--date-from", help="Start of an explicit range (YYYY-MM-DD).")
        parser.add_argument("--date-to", help="End of an explicit range (YYYY-MM-DD).")
        parser.add_argument(
            "--company", help="Restrict to one company code, e.g. JIVO_OIL."
        )

    def handle(self, *args, **options):
        date_from, date_to = self._window(options)

        company = None
        if options.get("company"):
            company = Company.objects.filter(code=options["company"]).first()
            if company is None:
                raise CommandError(f"No company with code {options['company']!r}.")

        self.stdout.write(f"Projecting approved leave {date_from} .. {date_to}")
        written = project_range(date_from, date_to, company=company)

        if written:
            self.stdout.write(
                self.style.SUCCESS(f"{written} leave day(s) written to the sheet.")
            )
        else:
            self.stdout.write(
                "Nothing to project -- every approved day in that window was "
                "already on the sheet, or its attendance row does not exist yet."
            )

    def _window(self, options):
        from datetime import date as date_cls

        if options.get("days"):
            if options["days"] < 1:
                raise CommandError("--days must be at least 1.")
            today = timezone.localdate()
            return today - timedelta(days=options["days"] - 1), today

        if options.get("date_from") and options.get("date_to"):
            try:
                date_from = date_cls.fromisoformat(options["date_from"])
                date_to = date_cls.fromisoformat(options["date_to"])
            except ValueError as exc:
                raise CommandError(f"Dates must be YYYY-MM-DD: {exc}") from exc
            if date_to < date_from:
                raise CommandError("--date-to is before --date-from.")
            return date_from, date_to

        raise CommandError("Pass either --days N, or both --date-from and --date-to.")
