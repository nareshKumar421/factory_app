"""
The shared body of ``import_portal_customers`` and ``import_portal_vendors``.

    python manage.py import_portal_customers --from-file zcust_portal.json --dry-run
    python manage.py import_portal_customers --from-file zcust_portal.json \\
        --actor ops@example.com --yes

``--dry-run`` reads and checks the whole file and says what a real run would
do (it reads the database to see what is already imported; it writes nothing).
A real run prints the database it is about to write to and refuses to move
without ``--yes`` and an ``--actor`` (the person who owns the import, recorded
on every imported row's history). The load is one transaction: every row lands
or none does, and files already written are removed if it fails.

Re-running is safe: a row whose ``ID`` was imported before is left alone. With
``--update`` it is refreshed from the file instead — but only while nobody has
acted on it in JI; a row verified, edited or rejected here is reported and kept.

See ``partner_onboarding/services/portal_import.py`` for how each column lands.
The module name starts with an underscore, so Django does not list it as a
command of its own.
"""

from collections import Counter

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from partner_onboarding.constants import PUBLIC_COMPANY_CODES, RegistrationStatus
from partner_onboarding.services.portal_import import (
    ImportFileError,
    apply_plan,
    load_rows,
    plan_rows,
    preview,
)

OUTCOMES = ("created", "updated", "already imported", "changed in JI", "skipped", "invalid")


class PortalImportCommand(BaseCommand):
    family = None

    def add_arguments(self, parser):
        parser.add_argument("--from-file", required=True, help="JSON export of the portal table.")
        parser.add_argument(
            "--dry-run", action="store_true", help="Read and check the file; write nothing."
        )
        parser.add_argument(
            "--default-company",
            choices=PUBLIC_COMPANY_CODES,
            default="",
            help="Company for rows whose COMPANY is blank or not a known SAP database (else they are skipped).",
        )
        parser.add_argument(
            "--update",
            action="store_true",
            help="Refresh rows imported before, unless someone has acted on them in JI.",
        )
        parser.add_argument(
            "--skip-invalid",
            action="store_true",
            help="Import the valid rows even if some rows cannot be read.",
        )
        parser.add_argument("--actor", default="", help="Email of the person who owns this import.")
        parser.add_argument(
            "--yes", action="store_true", help="Required for a real run. Writes to whatever settings point at."
        )

    def handle(self, *args, **options):
        family = self.family
        try:
            rows = load_rows(options["from_file"])
        except ImportFileError as exc:
            raise CommandError(str(exc)) from exc
        planned = plan_rows(rows, family, default_company=options["default_company"])
        self.stdout.write(f"{family.portal_table}: {len(planned)} row(s) in {options['from_file']}.")
        self._report_rows(planned, options["verbosity"])
        invalid = [row for row in planned if row.error]

        if options["dry_run"]:
            self._summary(preview(planned, family, update=options["update"]), "Would")
            self.stdout.write(self.style.SUCCESS("Dry run - nothing written."))
            return

        if invalid and not options["skip_invalid"]:
            raise CommandError(
                f"{len(invalid)} row(s) cannot be read (listed above). Fix the file, or pass --skip-invalid."
            )
        target = connection.settings_dict
        self.stdout.write(self.style.WARNING(f"Database: {target.get('HOST') or 'local'}/{target.get('NAME')}"))
        if not options["yes"]:
            raise CommandError("Refusing to write without --yes. Check the database above first.")
        if not options["actor"]:
            raise CommandError("Say who owns this import with --actor <email>.")
        actor = get_user_model().objects.filter(email__iexact=options["actor"]).first()
        if actor is None:
            raise CommandError(f"No user with email {options['actor']}.")

        counts = apply_plan(planned, family, actor=actor, update=options["update"])
        self._summary(counts, "Did")
        self.stdout.write(self.style.SUCCESS("Import committed."))

    def _report_rows(self, planned, verbosity):
        for row in planned:
            label = f"ID {row.legacy_id if row.legacy_id is not None else '?'}"
            if row.error:
                self.stdout.write(self.style.ERROR(f"  {label}: cannot import - {row.error}"))
            elif row.skip:
                self.stdout.write(self.style.WARNING(f"  {label}: skipped - {row.skip}"))
            elif row.notes and verbosity >= 1:
                self.stdout.write(f"  {label} ({row.card_name}):")
                for note in row.notes:
                    self.stdout.write(f"      {note}")

    def _summary(self, counts: Counter, verb: str):
        statuses = [value for value, _ in RegistrationStatus.choices]
        extra = sorted({status for _, status in counts if status not in statuses})
        columns = statuses + extra
        self.stdout.write("")
        self.stdout.write(f"{verb:<18}" + "".join(f"{status:>10}" for status in columns) + f"{'total':>8}")
        for outcome in OUTCOMES:
            row = [counts.get((outcome, status), 0) for status in columns]
            if any(row):
                self.stdout.write(f"{outcome:<18}" + "".join(f"{n:>10}" for n in row) + f"{sum(row):>8}")
