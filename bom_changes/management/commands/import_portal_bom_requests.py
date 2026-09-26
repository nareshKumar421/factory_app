"""
Bring SAP Portal's BOM requests (its ``ZBOM_REQUESTS`` HANA table) into BOM Changes.

    python manage.py import_portal_bom_requests --from-file zbom_requests.json --dry-run
    python manage.py import_portal_bom_requests --from-file zbom_requests.json --yes
    python manage.py import_portal_bom_requests --from-file zbom_requests.json \\
        --default-company JIVO_OIL --yes

The file is the table exported as JSON, one object per row keyed by the
portal's own column names -- the export SELECT is in ``bom_changes/docs/README.md``.
This command never reads SAP or the portal's table itself.

``--dry-run`` reads and checks the whole file without touching the database at
all (it does not need one) and prints every judgement call: rows it would skip
and why, and notes on rows it would import. Whether a row was imported before
is only known to the real run.

The real run prints the database it is about to write to and refuses to move
without ``--yes``. Everything is one transaction: either every importable row
lands or none does.

Idempotent on ``legacy_portal_id`` (the portal's ``ID``): a row already imported
is left alone, so the command can be run again after the portal has taken more
requests. A row whose portal status has moved on since it was imported is
reported, not overwritten -- decide by hand which side is right.

Rows with an empty ``COMPANY`` predate the portal's company column; they are
reported and skipped unless ``--default-company`` names the company they
belong to (the portal wrote them to its default company, usually Oil).

Imported requests keep their status. An open one (PENDING, L1/L2/L3_APPROVED)
joins this app's queue at the level it had reached; the portal approvals behind
it are kept as text and do not count toward the same-person rule, because
portal users are not JI logins.
"""

from collections import Counter

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.utils import timezone

from company.models import Company

from ...models import BOMChangeApproval, BOMChangeLine, BOMChangeRequest
from ...portal_import import RowProblem, company_codes_by_db, load_rows, parse_row


class Command(BaseCommand):
    help = "Import SAP Portal's ZBOM_REQUESTS rows (exported as JSON) as BOM change requests."

    def add_arguments(self, parser):
        parser.add_argument("--from-file", required=True, help="JSON export of ZBOM_REQUESTS.")
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Read and check the file only; never touches the database.",
        )
        parser.add_argument(
            "--default-company",
            help="Company code (e.g. JIVO_OIL) for rows whose COMPANY is empty.",
        )
        parser.add_argument(
            "--yes", action="store_true", help="Write, after checking the database printed first."
        )

    def handle(self, *args, **options):
        db_to_code = company_codes_by_db()
        default_company = (options.get("default_company") or "").strip().upper() or None
        if default_company and default_company not in set(db_to_code.values()):
            raise CommandError(
                f"--default-company {default_company} is not one of settings.COMPANY_DB "
                f"({', '.join(sorted(set(db_to_code.values()))) or 'none configured'})."
            )

        try:
            rows = load_rows(options["from_file"])
        except (OSError, ValueError) as e:
            raise CommandError(f"Cannot read {options['from_file']}: {e}")

        parsed, problems = [], []
        seen = set()
        for index, row in enumerate(rows, start=1):
            try:
                item = parse_row(row, db_to_code, default_company)
            except RowProblem as e:
                label = row.get("ID") if isinstance(row, dict) else f"#{index}"
                problems.append((label, str(e)))
                continue
            if item.legacy_id in seen:
                problems.append((item.legacy_id, "ID appears twice in the file"))
                continue
            seen.add(item.legacy_id)
            parsed.append(item)

        self._report_file(rows, parsed, problems)

        if options["dry_run"]:
            self.stdout.write(self.style.WARNING("DRY RUN - nothing was written."))
            return

        target = connection.settings_dict
        self.stdout.write(
            f"Target database: {target.get('ENGINE')} host={target.get('HOST') or '-'} "
            f"name={target.get('NAME')}"
        )
        if not options["yes"]:
            raise CommandError("Refusing to write without --yes. Check the database above first.")

        companies = {c.code: c for c in Company.objects.filter(code__in={p.company_code for p in parsed})}
        existing = dict(
            BOMChangeRequest.objects.filter(
                legacy_portal_id__in=[p.legacy_id for p in parsed]
            ).values_list("legacy_portal_id", "status")
        )

        counts = Counter()
        moved_on = []
        with transaction.atomic():
            for item in parsed:
                company = companies.get(item.company_code)
                if company is None:
                    counts["skipped: company not in this database"] += 1
                    self.stdout.write(f"  skip {item.legacy_id}: no Company {item.company_code} here")
                    continue
                if item.legacy_id in existing:
                    counts["already imported"] += 1
                    if existing[item.legacy_id] != item.header["status"]:
                        moved_on.append((item.legacy_id, existing[item.legacy_id], item.header["status"]))
                    continue
                self._create(company, item)
                counts["imported"] += 1
                counts["lines"] += len(item.lines)
                counts["decisions"] += len(item.approvals)

        for legacy_id, here, portal in moved_on:
            self.stdout.write(
                self.style.WARNING(
                    f"  {legacy_id}: {here} here but {portal} in the portal file - not touched"
                )
            )
        self.stdout.write(
            self.style.SUCCESS(
                "Done: "
                + ", ".join(f"{key} {value}" for key, value in sorted(counts.items()))
                + f", skipped in the file {len(problems)}."
            )
        )

    def _create(self, company, item):
        header = dict(item.header)
        if header["submitted_at"] is None:
            header["submitted_at"] = timezone.now()
        request = BOMChangeRequest.objects.create(company=company, **header)
        BOMChangeLine.objects.bulk_create([BOMChangeLine(request=request, **line) for line in item.lines])
        BOMChangeApproval.objects.bulk_create(
            [
                BOMChangeApproval(
                    request=request,
                    **{**approval, "decided_at": approval["decided_at"] or header["submitted_at"]},
                )
                for approval in item.approvals
            ]
        )

    def _report_file(self, rows, parsed, problems):
        self.stdout.write(f"Rows in the file: {len(rows)}")
        by_company = Counter(p.company_code for p in parsed)
        by_status = Counter(p.header["status"] for p in parsed)
        self.stdout.write(
            f"Importable: {len(parsed)} "
            f"({', '.join(f'{k} {v}' for k, v in sorted(by_company.items())) or '-'}; "
            f"{', '.join(f'{k} {v}' for k, v in sorted(by_status.items())) or '-'})"
        )
        self.stdout.write(
            f"Lines: {sum(len(p.lines) for p in parsed)}, "
            f"decisions: {sum(len(p.approvals) for p in parsed)}"
        )
        for item in parsed:
            for note in item.notes:
                self.stdout.write(f"  note {item.legacy_id}: {note}")
        if problems:
            self.stdout.write(self.style.WARNING(f"Skipped: {len(problems)}"))
            for label, reason in problems:
                self.stdout.write(f"  skip {label}: {reason}")
