"""
Import the cash sheet workbook into a company's cash book.

    python manage.py import_cash_sheet --file "Cash sheet (Arvinder sir).xlsx" --dry-run
    python manage.py import_cash_sheet --file "Cash sheet (Arvinder sir).xlsx" --yes

``--dry-run`` reads, checks and reports without touching the database at all --
it does not even need one -- so the whole file can be argued with before a row
is written. It prints the date calibration, every G/L head that is a judgement
call, and any row whose own Balance cell disagrees with the arithmetic.

The real run prints the database it is about to write to and refuses to move
without ``--yes``. Everything happens in one transaction: either the whole book
lands or none of it does.

WHAT IT DOES WITH THE SHEET
---------------------------
* Rows go in in the sheet's own order, because that is the order its Balance
  column follows -- entry dates run backwards all over it (a voucher is written
  down when it reaches the custodian, not when it was spent). The import then
  checks its own closing balance against the sheet's last Balance cell.
* The sheet's bunch numbers are kept, not renumbered, so the Bunch column reads
  as the paper does. Its Send Date becomes the bunch's ``sent_at`` and its Sign
  Date becomes ``decided_at`` -- the app has no signature, so approval is what
  that column becomes. A bunch with a sign date is imported approved.
* Departments are created as needed. The sheet's "Wg", "wg" and "WG" are one.
* G/L words become SAP account codes -- see ``cash_book.sheet_gl_map``. A word
  with no mapping stops the import and is named; nothing is filed against a
  head nobody chose.
* Dates are read as ``cash_book.sheet_import`` explains: the workbook was typed
  ``dd/mm`` but partly entered under an ``mm/dd`` locale, so part of it is
  transposed. The boundary is inferred from the file, not hardcoded.
"""

from datetime import datetime, time
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from accounts.models import Department
from cash_book import services, sheet_gl_map, sheet_import
from cash_book.models import BunchStatus, CashBunch, CashDirection, CashEntry
from company.models import Company

User = get_user_model()

DEFAULT_SHEET = "Cash details 04-06-2026"


def _noon(on):
    """The sheet holds dates; the model holds times. Midday, so no timezone
    shift can push a bunch onto the day before it was sent."""
    return timezone.make_aware(datetime.combine(on, time(12, 0)))


class Command(BaseCommand):
    help = "Import the cash sheet workbook into a company's cash book."

    def add_arguments(self, parser):
        parser.add_argument("--file", required=True, help="Path to the .xlsx.")
        parser.add_argument("--sheet", default=DEFAULT_SHEET)
        parser.add_argument("--company", default="JIVO_OIL")
        parser.add_argument("--custodian-email", default="")
        parser.add_argument("--approver-email", default="")
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Read and check the file, write nothing. Needs no database.",
        )
        parser.add_argument(
            "--reset",
            action="store_true",
            help="Delete this company's existing cash entries and bunches first.",
        )
        parser.add_argument(
            "--yes",
            action="store_true",
            help="Required for a real run. Writes to whatever settings point at.",
        )

    # ------------------------------------------------------------------

    def handle(self, *args, **options):
        rows = self._read(options["file"], options["sheet"])
        mapped, unmapped = self._map_heads(rows)
        self._report(rows, unmapped)

        if options["dry_run"]:
            self.stdout.write(self.style.SUCCESS("Dry run -- nothing written."))
            return

        if unmapped:
            raise CommandError(
                f"{len(unmapped)} G/L head(s) have no SAP account: "
                f"{', '.join(sorted(unmapped))}. Add them to "
                f"cash_book/sheet_gl_map.py before importing."
            )

        from django.db import connection

        target = connection.settings_dict
        self.stdout.write(
            self.style.WARNING(
                f"Database: {target.get('HOST') or 'local'}/{target.get('NAME')}"
            )
        )
        if not options["yes"]:
            raise CommandError(
                "Refusing to write without --yes. Check the database above first."
            )

        company = Company.objects.filter(code=options["company"]).first()
        if company is None:
            raise CommandError(
                f"No company with code {options['company']}. Known: "
                f"{', '.join(Company.objects.values_list('code', flat=True))}"
            )
        custodian = self._user(options["custodian_email"], "custodian")
        approver = (
            self._user(options["approver_email"], "approver")
            if options["approver_email"]
            else custodian
        )

        self._clear_existing(company, options["reset"])

        with transaction.atomic():
            departments = self._departments(rows)
            created = self._load_entries(company, custodian, rows, mapped, departments)
            self._load_bunches(company, custodian, approver, rows, created)
            self._verify(company, rows)

        self.stdout.write(
            self.style.SUCCESS(
                f"Imported {len(rows)} entries and "
                f"{len({r['bunch'] for r in rows if r['bunch']})} bunches into "
                f"{company.code}. Closing balance "
                f"{services.current_balance(company):,.2f}."
            )
        )

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    def _read(self, path, sheet_name):
        try:
            import openpyxl
        except ImportError as exc:  # pragma: no cover - environment problem
            raise CommandError(
                "openpyxl is needed to read the workbook: pip install openpyxl"
            ) from exc

        try:
            workbook = openpyxl.load_workbook(path, data_only=True, read_only=True)
        except FileNotFoundError as exc:
            raise CommandError(f"No such workbook: {path}") from exc
        if sheet_name not in workbook.sheetnames:
            raise CommandError(
                f"No sheet called {sheet_name!r}. This workbook has: "
                f"{', '.join(workbook.sheetnames)}"
            )
        try:
            rows = sheet_import.read_rows(workbook[sheet_name])
        except sheet_import.SheetError as exc:
            raise CommandError(f"The sheet could not be read: {exc}") from exc
        if not rows:
            raise CommandError(f"{sheet_name!r} holds no dated rows.")
        return rows

    def _map_heads(self, rows):
        """Resolve every G/L word. Receipts need none, whatever they say."""
        mapped, unmapped = {}, set()
        for row in rows:
            if row["in"] is not None:
                continue
            word = row["gl"]
            resolved = sheet_gl_map.resolve(word)
            if resolved is None:
                unmapped.add(word or "(blank)")
            else:
                mapped[row["excel_row"]] = resolved
        return mapped, unmapped

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    def _report(self, rows, unmapped):
        out, write = self.stdout, self.stdout.write
        payments = [r for r in rows if r["out"] is not None]
        receipts = [r for r in rows if r["in"] is not None]
        bunches = {r["bunch"] for r in rows if r["bunch"]}

        write(f"Rows            : {len(rows)}")
        write(f"  payments      : {len(payments)}")
        write(f"  receipts      : {len(receipts)}")
        write(f"Bunches         : {len(bunches)}")
        write(
            f"Dates           : {min(r['date'] for r in rows)} .. "
            f"{max(r['date'] for r in rows)}"
        )
        write(f"Closing balance : {sheet_import.running_balances(rows)[-1]:,.2f}")

        mismatches = sheet_import.check_balances(rows)
        if mismatches:
            write(
                self.style.ERROR(
                    f"\n{len(mismatches)} row(s) whose Balance cell disagrees with "
                    f"the arithmetic:"
                )
            )
            for row, stated, rebuilt in mismatches[:10]:
                write(
                    f"   sheet row {row['excel_row']}: says {stated:,.2f}, "
                    f"computes {rebuilt:,.2f}"
                )
        else:
            write(
                self.style.SUCCESS(
                    "Every Balance cell agrees with the arithmetic before it."
                )
            )

        impossible = sheet_import.check_bunch_dating(rows)
        if impossible:
            write(
                self.style.WARNING(
                    f"\n{len(impossible)} row(s) dated after the day their own bunch "
                    f"was signed. Imported as written -- correct them in the app:"
                )
            )
            for row in impossible:
                write(
                    f"   sheet row {row['excel_row']} (Sr. {row['serial']}): spent "
                    f"{row['date']}, bunch {row['bunch']} signed {row['sign_date']} "
                    f"| {row['detail'][:60]}"
                )

        if unmapped:
            write(
                self.style.ERROR(
                    f"\n{len(unmapped)} G/L head(s) with no SAP account: "
                    f"{', '.join(sorted(unmapped))}"
                )
            )

        judgement = {
            row["gl"].strip().lower()
            for row in rows
            if row["out"] is not None
            and (sheet_gl_map.resolve(row["gl"]) or (None, None, True))[2] is False
        }
        if judgement:
            write(
                self.style.WARNING(
                    "\nG/L heads that are a judgement call -- no SAP account of that "
                    "name. Check these before trusting the filing:"
                )
            )
            for word in sorted(judgement):
                code, name, _ = sheet_gl_map.resolve(word)
                count = sum(
                    1
                    for r in rows
                    if r["out"] is not None and r["gl"].strip().lower() == word
                )
                write(f"   {word:<22} -> {code} {name}   ({count} rows)")
        write("")

    # ------------------------------------------------------------------
    # Writing
    # ------------------------------------------------------------------

    def _user(self, email, role):
        if email:
            user = User.objects.filter(email=email).first()
            if user is None:
                raise CommandError(f"No user with email {email} to act as {role}.")
            return user
        user = User.objects.filter(is_superuser=True).order_by("id").first()
        if user is None:
            raise CommandError(f"No superuser to act as {role}. Pass --{role}-email.")
        self.stdout.write(f"  {role}: {user.email}")
        return user

    def _clear_existing(self, company, reset):
        existing = CashEntry.objects.filter(company=company)
        if not existing.exists():
            return
        if not reset:
            raise CommandError(
                f"{company.code} already has {existing.count()} cash entries. "
                f"Pass --reset to replace them."
            )
        with transaction.atomic():
            existing.delete()
            CashBunch.objects.filter(company=company).delete()
        self.stdout.write(self.style.WARNING("Cleared the existing book."))

    def _departments(self, rows):
        names = sorted({row["department"] for row in rows if row["department"]})
        resolved = {}
        for name in names:
            department, created = Department.objects.get_or_create(name=name)
            resolved[name] = department
            if created:
                self.stdout.write(f"  created department {name}")
        return resolved

    def _load_entries(self, company, custodian, rows, mapped, departments):
        """Write the rows in the sheet's order -- the balance follows it."""
        created = {}
        for row in rows:
            is_receipt = row["in"] is not None
            code, name = ("", "")
            if not is_receipt:
                code, name, _ = mapped[row["excel_row"]]
            created[row["excel_row"]] = services.record_entry(
                user=custodian,
                company=company,
                entry_date=row["date"],
                direction=CashDirection.IN if is_receipt else CashDirection.OUT,
                amount=Decimal(str(row["in"] if is_receipt else row["out"])),
                detail=row["detail"] or "(no detail given)",
                item=row["item"][:120],
                # A receipt belongs to no department, whatever the sheet wrote
                # in that column -- the service clears it either way.
                department=None if is_receipt else departments.get(row["department"]),
                gl_account_code=code,
                gl_account_name=name,
            )
        return created

    def _load_bunches(self, company, custodian, approver, rows, created):
        """Bundle each bunch, approve it, then date it as the sheet dates it.

        Sent through the service layer rather than built by hand, so the
        imported book is in a state the application itself could have produced.
        The timestamps are corrected afterwards because the services stamp
        *now* -- right for a bunch really being sent, wrong for one being
        reproduced from June.
        """
        grouped = {}
        for row in rows:
            if row["bunch"] is not None:
                grouped.setdefault(row["bunch"], []).append(row)

        for number, bunch_rows in grouped.items():
            entry_ids = [created[row["excel_row"]].id for row in bunch_rows]
            bunch = services.send_for_approval(
                user=custodian, company=company, entry_ids=entry_ids
            )

            sent_on = max(
                (r["send_date"] for r in bunch_rows if r["send_date"]), default=None
            )
            signed_on = max(
                (r["sign_date"] for r in bunch_rows if r["sign_date"]), default=None
            )
            if signed_on:
                services.approve_bunch(user=approver, bunch=bunch)

            bunch.number = number
            if sent_on:
                bunch.sent_at = _noon(sent_on)
            if signed_on:
                bunch.decided_at = _noon(signed_on)
            bunch.save(update_fields=["number", "sent_at", "decided_at"])

        self.stdout.write(f"  {len(grouped)} bunches")

    def _verify(self, company, rows):
        """The sheet's own closing balance is the check on the import."""
        expected = Decimal(str(sheet_import.running_balances(rows)[-1]))
        balance = services.current_balance(company)
        if balance != expected:
            raise CommandError(
                f"Closing balance is {balance}, but the sheet computes {expected}. "
                f"Nothing has been written."
            )
        approved = CashEntry.objects.filter(
            company=company, bunch__status=BunchStatus.APPROVED
        ).count()
        pending = CashBunch.objects.filter(
            company=company, status=BunchStatus.PENDING
        ).count()
        unsent = CashEntry.objects.filter(company=company, bunch__isnull=True).count()
        self.stdout.write(
            f"  balance {balance:,.2f} | {approved} entries approved | "
            f"{pending} bunches still pending | {unsent} entries never bunched"
        )
