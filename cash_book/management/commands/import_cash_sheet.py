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
* The advance summary block in columns M-P becomes the outstanding advances.
  It is the complete list of who holds the factory's cash -- see
  ``cash_book.sheet_advances``. A negative row is the other direction: they
  spent their own money and the factory owes them. Where a person's dedicated
  tab closes at exactly what a block row carries, the tab is the detail behind
  that row and the row is skipped; counting both would double it.
* The sheet's Department column becomes a branch -- Canola to Oil, WG to
  Beverage, Mart and anything unrecognised to Common. Branches are created if
  the company has none yet; see ``cash_book.sheet_import.BRANCH_ALIASES``.
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
from django.utils.text import slugify

from cash_book import (
    services,
    sheet_advances,
    sheet_gl_map,
    sheet_import,
    sheet_ledgers,
)
from cash_book.models import (
    AdvanceDirection,
    AdvanceEntry,
    AtmAccount,
    AtmReceipt,
    CashBranch,
    CashBunch,
    CashDirection,
    CashEntry,
    EntryApprovalStatus,
)
from company.models import Company

User = get_user_model()

DEFAULT_SHEET = "Cash details 04-06-2026"


def created_by_id(created, entry_id):
    """The entry object behind an id, out of what the import just wrote."""
    for entry in created.values():
        if entry.id == entry_id:
            return entry
    raise KeyError(entry_id)


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
        rows, card, ledgers, block = self._read(options["file"], options["sheet"])
        mapped, unmapped = self._map_heads(rows)
        self._report(rows, unmapped)
        self._report_ledgers(card, ledgers)
        self._report_block(block, ledgers, rows)

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
            branches = self._branches(company, rows)
            created = self._load_entries(company, custodian, rows, mapped, branches)
            self._load_bunches(company, custodian, approver, rows, created)
            if card:
                self._load_card(company, custodian, card, created)
            holders = {}
            for ledger in ledgers:
                holders.update(
                    self._load_people(company, custodian, ledger, rows, created)
                )
            self._load_advance_block(company, custodian, block, holders)
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

        # The other two registers are optional: a workbook without them still
        # imports, it just has no card and nobody holding a float.
        card = None
        atm_sheets = [n for n in workbook.sheetnames if n.lower().startswith("atm")]
        if atm_sheets:
            try:
                card = sheet_ledgers.read_atm_sheet(workbook[atm_sheets[0]])
            except sheet_ledgers.SheetError as exc:
                raise CommandError(f"The card sheet could not be read: {exc}") from exc

        ledgers = []
        for name in sheet_ledgers.person_sheet_names(workbook):
            try:
                ledgers.append(sheet_ledgers.read_person_sheet(workbook[name], name))
            except sheet_ledgers.SheetError as exc:
                raise CommandError(f"{name!r} could not be read: {exc}") from exc

        # The advance summary block rides in columns M-P of the register's own
        # tab, past its last header -- see cash_book.sheet_advances.
        block = sheet_advances.read_advance_block(workbook[sheet_name])

        return rows, card, ledgers, block

    def _report_block(self, block, ledgers, rows):
        """The advance list, and how it sits against the book."""
        if not block:
            self.stdout.write("\nAdvance list    : none found in columns M-P")
            return

        out = sum(row["amount"] for row in block if row["amount"] > 0)
        owed = sum(row["amount"] for row in block if row["amount"] < 0)
        net = sheet_advances.block_total(block)

        self.stdout.write("")
        self.stdout.write(f"Advance list    : {len(block)} people (columns M-P)")
        self.stdout.write(f"  out with them : {out:>12,.2f}")
        self.stdout.write(f"  we owe them   : {owed:>12,.2f}")
        self.stdout.write(f"  net           : {net:>12,.2f}")

        # The proof that the block is the whole list: what the book says is
        # still ours, less what is out with people, is the notes in the box.
        closing = sheet_import.running_balances(rows)[-1] if rows else 0.0
        self.stdout.write(
            f"  so the box holds {closing - net:,.2f} of the book's {closing:,.2f}"
        )

        matched = self._block_matches(block, ledgers)
        for row in block:
            tab = matched.get(row["excel_row"])
            note = ""
            if tab == "SAME":
                note = "  <- a tab closes at exactly this; the tab is its detail"
            elif tab:
                note = f"  <- {tab} has a tab too; it will be brought to this figure"
            self.stdout.write(
                f"  {row['person']:<16} {row['amount']:>12,.2f}{note}"
            )

    def _block_matches(self, block, ledgers):
        """Which block rows a dedicated person tab already accounts for.

        Two ways a tab and a row can be the same money. By amount, to the
        rupee -- which is conclusive, and is how "Tiwari ji" is recognised as
        the pot the ``bunty in out`` tab details. Or by name, which is not
        conclusive at all, so it only means the tab is brought to the figure
        the list carries rather than added to it.
        """
        by_amount = {
            round(ledger["stated_balance"], 2): ledger["person"]
            for ledger in ledgers
            if ledger["stated_balance"] is not None
        }
        by_name = {
            ledger["person"].split()[0].lower(): ledger["person"]
            for ledger in ledgers
            if ledger["person"]
        }
        matches = {}
        for row in block:
            if round(row["amount"], 2) in by_amount:
                matches[row["excel_row"]] = "SAME"
                continue
            first = (row["person"].split() or [""])[0].lower()
            if first in by_name:
                matches[row["excel_row"]] = by_name[first]
        return matches

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

    def _report_ledgers(self, card, ledgers):
        """What the other two registers hold, before anything is written."""
        write = self.stdout.write

        if card:
            movements = card["movements"]
            write("")
            write(f"Card            : {card['name']}")
            write(f"  opening       : {card['opening_balance']:,.2f}")
            write(f"  movements     : {len(movements)}")
            if movements:
                running = _running(card)
                write(f"  closing       : {running[-1]:,.2f}")
                drift = [
                    row
                    for row, balance in zip(movements, running)
                    if row["stated_balance"] is not None
                    and abs(balance - row["stated_balance"]) > 0.01
                ]
                if drift:
                    write(
                        self.style.ERROR(
                            f"  {len(drift)} row(s) disagree with the sheet's own "
                            f"Cl Bal column"
                        )
                    )
                else:
                    write("  every row agrees with the sheet's own Cl Bal column")

        if ledgers:
            write("")
            write(f"Person ledgers  : {len(ledgers)}")
            for ledger in ledgers:
                net = sum(
                    row["amount"] if row["direction"] == "GIVEN" else -row["amount"]
                    for row in ledger["rows"]
                )
                stated = ledger["stated_balance"]
                agrees = stated is None or abs(net - stated) <= 0.01
                write(
                    f"  {ledger['person']:<16} {len(ledger['rows']):>3} rows, "
                    f"holding {net:>12,.2f}"
                    + ("" if agrees else f"  (the sheet says {stated:,.2f})")
                )
                if not agrees:
                    write(
                        self.style.ERROR(
                            "    that disagrees with the tab's own Total column"
                        )
                    )
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
            AdvanceEntry.objects.filter(company=company).delete()
            AtmReceipt.objects.filter(account__company=company).delete()
            AtmAccount.objects.filter(company=company).delete()
        self.stdout.write(
            self.style.WARNING("Cleared the existing book, card and advances.")
        )

    def _branches(self, company, rows):
        names = sorted({row["branch"] for row in rows})
        resolved = {}
        for name in names:
            branch, created = CashBranch.objects.get_or_create(
                company=company, name=name
            )
            resolved[name] = branch
            if created:
                self.stdout.write(f"  created branch {name}")
        return resolved

    def _load_entries(self, company, custodian, rows, mapped, branches):
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
                # A receipt belongs to no branch, whatever the sheet wrote in
                # that column -- the service clears it either way.
                branch=None if is_receipt else branches.get(row["branch"]),
                gl_account_code=code,
                gl_account_name=name,
            )
        return created

    def _load_bunches(self, company, custodian, approver, rows, created):
        """Approve each batch's vouchers, then bundle them as the sheet did.

        In that order, because a bunch is now the paperwork that follows the
        decision: only approved payments can be bundled. The sheet recorded
        both facts in one row -- its Sign Date and its Send Date -- so both are
        replayed here, the approval onto the entries and the send onto the
        batch.
        """
        grouped = {}
        for row in rows:
            if row["bunch"] is not None:
                grouped.setdefault(row["bunch"], []).append(row)

        for number, bunch_rows in grouped.items():
            entry_ids = [created[row["excel_row"]].id for row in bunch_rows]
            signed_on = max(
                (r["sign_date"] for r in bunch_rows if r["sign_date"]), default=None
            )
            sent_on = max(
                (r["send_date"] for r in bunch_rows if r["send_date"]), default=None
            )

            payments = [
                entry_id
                for entry_id in entry_ids
                if created_by_id(created, entry_id).direction == CashDirection.OUT
            ]
            if signed_on and payments:
                services.decide_entries(
                    user=approver,
                    company=company,
                    entry_ids=payments,
                    approve=True,
                )
                CashEntry.objects.filter(id__in=payments).update(
                    approval_decided_at=_noon(signed_on)
                )

            # Only the approved payments go in the batch; a receipt was never
            # a voucher and the sheet never sent one.
            if not payments:
                continue
            bunch = services.create_bunch(
                user=custodian, company=company, entry_ids=payments
            )
            # The number allocated by create_bunch stands. The sheet's own
            # "Bunch" figure is the batch total, not an identifier, and is
            # recomputed from the entries whenever it is wanted.
            if sent_on:
                bunch.sent_at = _noon(sent_on)
                bunch.sent_by = custodian
                bunch.save(update_fields=["sent_at", "sent_by"])

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
            company=company, approval_state=EntryApprovalStatus.APPROVED
        ).count()
        pending = CashEntry.objects.filter(
            company=company, approval_state=EntryApprovalStatus.PENDING
        ).count()
        unsent = CashEntry.objects.filter(company=company, bunch__isnull=True).count()
        self.stdout.write(
            f"  balance {balance:,.2f} | {approved} approved | "
            f"{pending} still awaiting | {unsent} never bunched"
        )

    # ------------------------------------------------------------------
    # The card
    # ------------------------------------------------------------------

    def _load_card(self, company, user, card, created):
        """Create the card, its receipts, and point the cash at it.

        A withdrawal is not written as a row of its own -- it *is* one of the
        cash receipts already imported, so the work here is finding which one
        and naming the card on it. Matched exactly on date and amount first,
        then on amount alone taking the nearest date, because the two sheets
        were filled in by hand on different days and drift by one or two.
        """
        account, _ = AtmAccount.objects.get_or_create(
            company=company,
            name=card["name"],
            defaults={
                "opening_balance": Decimal(str(card["opening_balance"])),
                "created_by": user,
                "updated_by": user,
            },
        )

        receipts = 0
        for movement in card["movements"]:
            if movement["kind"] != "RECEIPT":
                continue
            services.record_atm_receipt(
                user=user,
                account=account,
                received_on=movement["date"],
                amount=Decimal(str(movement["amount"])),
                detail="Paid onto the card",
            )
            receipts += 1

        # Cash receipts still looking for a card, by amount.
        pool = {}
        for row, entry in created.items():
            if entry.direction == CashDirection.IN:
                pool.setdefault(round(float(entry.amount), 2), []).append(entry)

        linked = drifted = unmatched = 0
        for movement in card["movements"]:
            if movement["kind"] != "WITHDRAWAL":
                continue
            candidates = pool.get(round(movement["amount"], 2)) or []
            if not candidates:
                unmatched += 1
                self.stdout.write(
                    self.style.WARNING(
                        f"   card row {movement['excel_row']}: no cash receipt of "
                        f"{movement['amount']:,.2f} to attach the withdrawal to"
                    )
                )
                continue
            exact = [e for e in candidates if e.entry_date == movement["date"]]
            entry = exact[0] if exact else min(
                candidates, key=lambda e: abs((e.entry_date - movement["date"]).days)
            )
            if not exact:
                drifted += 1
            candidates.remove(entry)
            entry.atm_account = account
            entry.updated_by = user
            entry.save(update_fields=["atm_account", "updated_by", "updated_at"])
            linked += 1

        self.stdout.write(
            f"  card {account.name}: {receipts} payments on, {linked} withdrawals "
            f"linked ({drifted} matched on a nearby date), {unmatched} unmatched"
        )
        if unmatched:
            self.stdout.write(
                self.style.WARNING(
                    f"   the card therefore reads high by the value of those "
                    f"{unmatched} withdrawal(s) -- add the missing cash-in to fix it"
                )
            )
        return account

    # ------------------------------------------------------------------
    # The people
    # ------------------------------------------------------------------

    def _person(self, name, made):
        """The app user behind a name on a ledger, created if there is none.

        Advance holders are app users, and none of the sheet's people have a
        login -- they are workers and contractors. They are created here with
        an unusable password and an obviously synthetic address, so they can
        hold an advance without anybody mistaking them for somebody who can
        sign in.
        """
        User = get_user_model()
        existing = User.objects.filter(full_name__iexact=name).first()
        if existing:
            return existing

        email = f"{slugify(name)}@cash-book.local"
        user = User.objects.filter(email=email).first()
        if user is None:
            user = User(email=email, full_name=name, is_active=False)
            user.set_unusable_password()
            user.save()
            made.append(name)
        return user

    def _load_people(self, company, user, ledger, rows, created):
        """Hand out the floats, and attribute what cleared them.

        A row saying money went out becomes an advance. A row saying it came
        back is the interesting one, and is read in three passes:

        1. a batch of vouchers whose amount is a bunch total -- so that whole
           bunch's expenses are what this person explained, and they are
           pointed at them;
        2. otherwise a single expense of the same amount not yet attributed;
        3. otherwise cash handed back, which is the only reading left.

        Pass 3 is where the sheet stops being machine-readable -- it is an
        informal running account, not a ledger -- so the counts are printed.
        """
        made = []
        person = self._person(ledger["person"], made)
        if made:
            self.stdout.write(
                self.style.WARNING(
                    f"  created a login-less person to hold a float: {made[0]}"
                )
            )

        entries = list(created.values())
        by_bunch = {}
        for row, entry in zip(rows, entries):
            if row["bunch"]:
                by_bunch.setdefault(row["bunch"], []).append(entry)
        bunch_by_total = {
            round(sum(float(e.amount) for e in group), 2): number
            for number, group in by_bunch.items()
        }
        spare = {}
        for entry in entries:
            if entry.direction == CashDirection.OUT and entry.advance_holder_id is None:
                spare.setdefault(round(float(entry.amount), 2), []).append(entry)

        given = by_batch = by_single = returned = 0
        fallback_date = min(r["date"] for r in rows)

        for row in ledger["rows"]:
            when = row["date"] or fallback_date

            if row["direction"] == "GIVEN":
                services.record_advance(
                    user=user,
                    company=company,
                    person=person,
                    entry_date=when,
                    direction=AdvanceDirection.GIVEN,
                    amount=Decimal(str(row["amount"])),
                    detail=row["detail"],
                )
                given += 1
                continue

            amount = round(row["amount"], 2)
            number = bunch_by_total.get(amount) if row["is_voucher"] else None
            if number is not None and by_bunch.get(number):
                for entry in by_bunch.pop(number):
                    entry.advance_holder = person
                    entry.updated_by = user
                    entry.save(
                        update_fields=["advance_holder", "updated_by", "updated_at"]
                    )
                    # It is spoken for now, so it cannot also settle a
                    # single-expense row further down the ledger.
                    waiting = spare.get(round(float(entry.amount), 2))
                    if waiting and entry in waiting:
                        waiting.remove(entry)
                by_batch += 1
                continue

            candidates = spare.get(amount) or []
            if candidates:
                entry = candidates.pop(0)
                entry.advance_holder = person
                entry.updated_by = user
                entry.save(
                    update_fields=["advance_holder", "updated_by", "updated_at"]
                )
                by_single += 1
                continue

            services.record_advance(
                user=user,
                company=company,
                person=person,
                entry_date=when,
                direction=AdvanceDirection.RETURNED,
                amount=Decimal(str(row["amount"])),
                detail=(
                    f"{row['detail']} (no matching voucher batch in the register)"
                    if row["is_voucher"]
                    else row["detail"]
                ),
            )
            returned += 1

        balance = services.advance_balance(company, person)
        self.stdout.write(
            f"  {ledger['person']}: {given} handed out, {by_batch} cleared by a "
            f"voucher batch, {by_single} by a single expense, {returned} as cash "
            f"handed back | holding {balance:,.2f}"
        )

        stated = ledger["stated_balance"]
        if stated is not None and abs(float(balance) - stated) > 0.01:
            raise CommandError(
                f"{ledger['person']} comes out holding {balance}, but the tab's "
                f"own Total column says {stated}. Nothing has been written."
            )

        # Keyed on the first name, which is how the summary list writes people.
        return {ledger["person"].split()[0].lower(): (person, balance)}

    def _load_advance_block(self, company, user, block, holders):
        """Load the advance summary list -- who is holding the factory's cash.

        The list is the complete picture, so every row has to end up as
        somebody's balance. Three cases, and the order matters:

        1. **A tab closes at exactly this figure.** The same pot written down
           twice -- "Tiwari ji ... 21,626.00" is the ``bunty in out`` tab to
           the rupee. The tab already produced that balance out of its own
           movements, so the row is left alone. Counting both would double it.
        2. **Somebody with a tab, at a different figure.** The list is the
           authority, so they are brought to it with one entry that says so
           rather than the two figures being added together.
        3. **Everybody else.** A person the book has not met, holding what the
           row says.

        A negative row is the other direction -- they spent their own money
        and the factory owes them -- which is a RETURNED entry: the same
        ledger, read the other way.
        """
        if not block:
            return

        by_amount = {
            round(float(balance), 2): first
            for first, (_, balance) in holders.items()
        }
        made, skipped, adjusted, fresh = [], 0, 0, 0

        for row in block:
            amount = Decimal(str(row["amount"]))

            # 1. the tab is this row's detail
            if round(row["amount"], 2) in by_amount:
                skipped += 1
                continue

            first = (row["person"].split() or [""])[0].lower()
            known = holders.get(first)

            if known:
                # 2. bring them to the figure the list carries
                person, balance = known
                gap = amount - balance
                if gap == 0:
                    skipped += 1
                    continue
                services.record_advance(
                    user=user,
                    company=company,
                    person=person,
                    entry_date=row["date"],
                    direction=(
                        AdvanceDirection.GIVEN
                        if gap > 0
                        else AdvanceDirection.RETURNED
                    ),
                    amount=abs(gap),
                    detail=(
                        f"{row['detail']} (brought to the {row['amount']:,.2f} the "
                        f"advance list carries; their own tab nets {balance:,.2f})"
                    ),
                )
                adjusted += 1
                continue

            # 3. somebody the book has not met
            person = self._person(row["person"], made)
            services.record_advance(
                user=user,
                company=company,
                person=person,
                entry_date=row["date"],
                direction=(
                    AdvanceDirection.GIVEN
                    if row["amount"] > 0
                    else AdvanceDirection.RETURNED
                ),
                amount=abs(amount),
                detail=row["detail"] + (f" [{row['note']}]" if row["note"] else ""),
            )
            fresh += 1

        self.stdout.write(
            f"  advance list: {fresh} people added, {adjusted} brought to the "
            f"list's figure, {skipped} already detailed by their own tab"
        )
        if made:
            self.stdout.write(
                self.style.WARNING(
                    f"  created {len(made)} login-less people to hold a float: "
                    f"{', '.join(made)}"
                )
            )


def _running(card):
    """The card's balance after each movement, for the dry run's check."""
    balance = card["opening_balance"]
    out = []
    for movement in card["movements"]:
        balance += (
            movement["amount"]
            if movement["kind"] == "RECEIPT"
            else -movement["amount"]
        )
        out.append(round(balance, 2))
    return out
