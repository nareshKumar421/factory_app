"""
Copy EXIM's export licences (Advance Authorisation and DFIA) into this project.

Usage:
    python manage.py import_exim_licences                    # dry run: says what it would do
    python manage.py import_exim_licences --commit
    python manage.py import_exim_licences --commit --company JIVO_OIL

It reads EXIM's database through the read-only ``exim`` alias (EXIM_DB_NAME and
friends in config/settings.py) and never writes to it. What it does with each
licence is set out in ``exim.licence_import``; in short, EXIM's figures are
copied as they are, a re-run updates in place, and a licence changed here since
it was copied is left alone.

It is a DRY RUN unless ``--commit`` is given. Run ``manage.py migrate exim`` first.
"""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError, connections, transaction

from exim.licence_import import (
    LicenceImportProblem,
    import_licences,
    read_exim,
    resolve_company,
)


class Command(BaseCommand):
    help = "Copy EXIM's export licences across. A dry run unless --commit is given."

    def add_arguments(self, parser):
        parser.add_argument(
            "--commit", action="store_true",
            help="Write the changes. Without it nothing is saved.",
        )
        parser.add_argument(
            "--company", default="JIVO_OIL",
            help="The company the licences belong to here (default: JIVO_OIL).",
        )
        parser.add_argument(
            "--database", default="exim",
            help="The database alias EXIM is read through (default: exim).",
        )

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
            raise CommandError(f"Could not read EXIM's licences through {alias!r}: {exc}") from exc

        with transaction.atomic():
            report = import_licences(snapshot, company=company)
            if not commit:
                transaction.set_rollback(True)

        self._print(report, company)
        if not commit:
            self.stdout.write(self.style.WARNING("\nDRY RUN - nothing was written. Re-run with --commit."))

    def _print(self, report, company):
        styles = {
            "create": self.style.SUCCESS,
            "update": self.style.MIGRATE_HEADING,
            "unchanged": lambda s: s,
            "skip": self.style.WARNING,
            "conflict": self.style.ERROR,
        }
        for r in report.results:
            parts = []
            if r.lines_added or r.lines_updated or r.lines_removed:
                parts.append(f"lines +{r.lines_added} ~{r.lines_updated} -{r.lines_removed}")
            parts += r.notes
            line = f"  {r.action:<9} {r.ref:<24} {'; '.join(parts) or 'no change'}"
            self.stdout.write(styles[r.action](line))

        self.stdout.write("")
        self.stdout.write(
            f"EXIM licences: {len(report.results)} into {company.code} - "
            f"{report.count('create')} created, {report.count('update')} updated, "
            f"{report.count('unchanged')} unchanged, {report.count('skip')} skipped, "
            f"{report.count('conflict')} conflict(s)"
        )
        for table, n in sorted(report.orphans.items()):
            self.stdout.write(self.style.WARNING(f"  {n} line(s) in {table} belong to no licence; not copied"))
        if report.discrepancies:
            self.stdout.write(
                "Copied as EXIM stored them, though EXIM's own rule (first leg less 3.1%) "
                "gives a different obligation:"
            )
            for ref, stored, rule in report.discrepancies:
                self.stdout.write(f"  {ref:<24} stored {stored}  rule {rule}")
