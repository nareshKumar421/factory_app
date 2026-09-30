"""
Copy EXIM's oil contract terms: each PO's delivery terms, freight and brokerage.

Usage:
    python manage.py import_exim_contracts                 # dry run: says what it would do
    python manage.py import_exim_contracts --commit
    python manage.py import_exim_contracts --commit --company JIVO_OIL

It reads EXIM's database through the read-only ``exim`` alias and never writes
to it. The contracts themselves are SAP's purchase orders, read live; only the
terms SAP does not keep are copied (see ``exim.contract_import``). A re-run
updates in place and leaves alone a PO whose terms were changed here.

It is a DRY RUN unless ``--commit`` is given. Run ``manage.py migrate exim`` first.
"""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError, connections, transaction

from exim.contract_import import import_contract_terms, read_exim
from exim.licence_import import LicenceImportProblem, resolve_company


class Command(BaseCommand):
    help = "Copy EXIM's oil contract terms across. A dry run unless --commit is given."

    def add_arguments(self, parser):
        parser.add_argument("--commit", action="store_true", help="Write the changes. Without it nothing is saved.")
        parser.add_argument("--company", default="JIVO_OIL",
                            help="The company the contracts belong to here (default: JIVO_OIL).")
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
            raise CommandError(f"Could not read EXIM's contracts through {alias!r}: {exc}") from exc

        with transaction.atomic():
            report = import_contract_terms(snapshot, company=company)
            if not commit:
                transaction.set_rollback(True)

        parts = ", ".join(f"{n} {action}" for action, n in sorted(report.counts.items()) if n)
        self.stdout.write(f"Into {company.code}:\n  contract terms     {parts or 'nothing'}")
        if report.notes:
            self.stdout.write("")
            for note in report.notes:
                self.stdout.write(self.style.WARNING(f"  {note}"))
        if not commit:
            self.stdout.write(self.style.WARNING("\nDRY RUN - nothing was written. Re-run with --commit."))
