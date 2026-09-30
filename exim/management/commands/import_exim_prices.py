"""
Copy EXIM's price history: its daily commodity prices and its Jivo rates.

Usage:
    python manage.py import_exim_prices                    # dry run: says what it would do
    python manage.py import_exim_prices --commit
    python manage.py import_exim_prices --commit --company JIVO_OIL

It reads EXIM's database through the read-only ``exim`` alias and never writes
to it. A day already held here is left alone, so a re-run only fills the days
still missing (see ``exim.price_import``).

It is a DRY RUN unless ``--commit`` is given. Run ``manage.py migrate exim`` first.
"""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError, connections, transaction

from exim.price_import import import_prices, read_exim
from exim.licence_import import LicenceImportProblem, resolve_company


class Command(BaseCommand):
    help = "Copy EXIM's oil price history across. A dry run unless --commit is given."

    def add_arguments(self, parser):
        parser.add_argument("--commit", action="store_true", help="Write the changes. Without it nothing is saved.")
        parser.add_argument("--company", default="JIVO_OIL",
                            help="The company the prices are kept under here (default: JIVO_OIL).")
        parser.add_argument("--database", default="exim",
                            help="The database alias EXIM is read through (default: exim).")

    def handle(self, *args, **options):
        alias = options["database"]
        if alias not in settings.DATABASES:
            raise CommandError(
                f"There is no {alias!r} database configured. Set EXIM_DB_NAME, "
                "EXIM_DB_HOST, EXIM_DB_USER and EXIM_DB_PASSWORD (see config/settings.py)."
            )
        commit = options["commit"]
        if not commit:
            self.stdout.write(self.style.WARNING("DRY RUN - nothing will be written\n"))
        try:
            company = resolve_company(options["company"])
        except LicenceImportProblem as exc:
            raise CommandError(str(exc)) from exc
        try:
            with connections[alias].cursor() as cursor:
                snapshot = read_exim(cursor)
        except DatabaseError as exc:
            raise CommandError(f"Could not read EXIM's prices through {alias!r}: {exc}") from exc

        with transaction.atomic():
            report = import_prices(snapshot, company=company)
            if not commit:
                transaction.set_rollback(True)

        self.stdout.write(f"Into {company.code}:")
        for table, label in (("prices", "commodity prices"), ("rates", "pack rates")):
            parts = ", ".join(f"{n} {action}" for action, n in sorted(report.counts[table].items()) if n)
            self.stdout.write(f"  {label:<18} {parts or 'nothing'}")
        if not commit:
            self.stdout.write(self.style.WARNING("\nDRY RUN - nothing was written. Re-run with --commit."))
