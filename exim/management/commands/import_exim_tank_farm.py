"""
Copy EXIM's tank farm and every oil lot into this project, together.

Usage:
    python manage.py import_exim_tank_farm                    # dry run: says what it would do
    python manage.py import_exim_tank_farm --commit
    python manage.py import_exim_tank_farm --commit --company JIVO_OIL

It reads EXIM's database through the read-only ``exim`` alias (EXIM_DB_NAME and
friends in config/settings.py) and never writes to it. What it copies, and what
it leaves alone, is set out in ``exim.tank_farm_import``: EXIM's figures as they
are, a re-run updates in place, and an oil, tank or lot changed here since it
was copied is not overwritten.

It is a DRY RUN unless ``--commit`` is given. Run ``manage.py migrate exim`` first.
"""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError, connections, transaction

from exim.licence_import import LicenceImportProblem, resolve_company
from exim.tank_farm_import import import_tank_farm, read_exim

ORDER = [
    "oils", "tanks", "temporary vendors", "lots", "shortages", "tank log",
    "lot history", "contract history", "dashboard order",
]


class Command(BaseCommand):
    help = "Copy EXIM's tank farm and oil lots across. A dry run unless --commit is given."

    def add_arguments(self, parser):
        parser.add_argument("--commit", action="store_true", help="Write the changes. Without it nothing is saved.")
        parser.add_argument("--company", default="JIVO_OIL",
                            help="The company the tank farm belongs to here (default: JIVO_OIL).")
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
            raise CommandError(f"Could not read EXIM's tank farm through {alias!r}: {exc}") from exc

        with transaction.atomic():
            report = import_tank_farm(snapshot, company=company)
            if not commit:
                transaction.set_rollback(True)

        self.stdout.write(f"Into {company.code}:")
        for table in ORDER:
            counts = report.counts.get(table)
            if not counts:
                continue
            parts = ", ".join(f"{n} {action}" for action, n in sorted(counts.items()) if n)
            self.stdout.write(f"  {table:<18} {parts or 'nothing'}")
        if report.notes:
            self.stdout.write("")
            for note in report.notes:
                self.stdout.write(self.style.WARNING(f"  {note}"))
        if not commit:
            self.stdout.write(self.style.WARNING("\nDRY RUN - nothing was written. Re-run with --commit."))
