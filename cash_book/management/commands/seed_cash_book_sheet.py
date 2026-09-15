"""
Load the petty-cash spreadsheet this module replaces into a cash book.

DEMO / DEV DATA. It writes 24 entries and 4 bunches so the register can be
looked at with real rows in it. It is not a migration and nothing here belongs
on the live database unless somebody deliberately decides it does -- which is
why it prints the database it is about to write to and refuses to move without
``--yes``.

Usage::

    python manage.py seed_cash_book_sheet --yes
    python manage.py seed_cash_book_sheet --yes --reset      # replace
    python manage.py seed_cash_book_sheet --company JIVO_OIL --yes

WHAT IS FAITHFUL TO THE SHEET
-----------------------------
* All 24 rows, in the sheet's own Sr. no. order. That order is what the balance
  column follows -- the dates run 06/04, 06/05, 06/03, 06/03, 06/05 while the
  balance falls steadily -- so entering them in any other order would produce a
  different book. The closing balance is asserted at the end: 86,743.00, which
  is what row 24 reads. If the arithmetic ever stops matching, the command
  fails rather than leaving a book that disagrees with the sheet.
* Dates are mm/dd/yyyy. "06/04/2026" is 4 June 2026, not 6 April: read the
  other way the entries would run March to December while the bunches were
  signed in between, and every sign date would fall before its own entry.
* The sheet's own bunch numbers (17570, 36972, 23342, 9620) are kept rather
  than renumbered from 1, so the Bunch column reads exactly as the paper does.
  A consequence worth knowing: the next bunch raised on a seeded book is 36973,
  because numbers are allocated as max + 1.
* Its "Send Date" is the bunch's ``sent_at`` and its "Sign Date" is
  ``decided_at`` -- all four bunches were signed, so all four are approved.

WHAT IS A JUDGEMENT CALL
------------------------
The sheet writes its G/L heads as words ("Refreshment", "R&M", "Advacne"). This
module holds SAP account codes, so each word had to be resolved against
``JIVO_OIL_HANADB.OACT``. Most are exact (REFRESHMENT 5630004, STAFF WELFARE
5630003). Four are not, and are marked ``# GUESS`` below -- they should be
checked by whoever keeps the book before any of this is taken as correct.
"""

from datetime import date, datetime, time
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from accounts.models import Department
from company.models import Company

from cash_book import services
from cash_book.models import BunchStatus, CashBunch, CashDirection, CashEntry

User = get_user_model()

#: What the sheet's last row reads. The whole load is checked against it.
EXPECTED_CLOSING_BALANCE = Decimal("86743.00")

#: The three departments the sheet spends against. None of them is in
#: ``accounts.Department`` yet -- that table holds IT, Ecom, Account, Store and
#: so on -- so they are created here. "Wg" and "WG" in the sheet are one
#: department, spelled two ways.
DEPARTMENTS = ("Canola", "WG", "Common")

#: The sheet's bunches: number -> (send date, sign date). Both columns hold the
#: same date on every row of the sheet, but they are separate fields here
#: because they are separate events.
BUNCHES = {
    17570: (date(2026, 6, 5), date(2026, 6, 5)),
    36972: (date(2026, 6, 11), date(2026, 6, 11)),
    23342: (date(2026, 6, 12), date(2026, 6, 12)),
    9620: (date(2026, 6, 12), date(2026, 6, 12)),
}

# Sr., bunch, date, department, G/L code, G/L name, item, detail, out, in
#
# A `None` bunch is a cash receipt, which the sheet never bunches -- money
# arriving in the box is not a voucher anybody approves.
#
# Four details end in an ellipsis: the spreadsheet column was cut off in the
# screenshot they were transcribed from, and the rest was not invented.
SHEET = [
    (1, None, date(2026, 6, 4), None, "", "", "Cash",
     "Cash receive by ATM card", None, "50000.00"),
    (2, 17570, date(2026, 6, 4), "Canola", "5630004", "REFRESHMENT", "Refreshment",
     "Cash paid to Ravi kumar for refreshment exp for some visitor at site",
     "6000.00", None),
    (3, 17570, date(2026, 6, 5), "Canola", "5630004", "REFRESHMENT", "Refreshment",
     "Cash paid to Ravi kumar for refreshment exp for some visitor at site",
     "6000.00", None),
    (4, 17570, date(2026, 6, 3), "Canola", "5630004", "REFRESHMENT", "Refreshment",
     "Cash paid to Akash for refreshment exp for some visitor at site",
     "2000.00", None),
    # GUESS: the sheet says "Installation" and means an air-conditioner fitted
    # at site. SAP has no installation-expense head; the only INSTALLATION
    # account is a fixed asset for a shipping container.
    (5, 17570, date(2026, 6, 3), "Canola", "5650001",
     "REPAIR & MAINTENANCE OFFICE & BUILDING", "Installation",
     "Cash paid to Amit kumar for A.c installation at site", "2770.00", None),
    (6, 17570, date(2026, 6, 5), "Canola", "5670001",
     "FREIGHT AND CARTAGE OUTWARD-INDIRECT EXP", "Freight",
     "Cash paid to Jasmeet ji for freight charge (ghaziabad to Transport) "
     "bill no.128 party mech te…", "800.00", None),
    (7, None, date(2026, 6, 6), None, "", "", "Cash",
     "Cash receive by ATM card", None, "50000.00"),
    (8, 36972, date(2026, 6, 6), "Canola", "11133325",
     "SHAHRUKH KHAN ADVANCE JWPL2885", "Advance",
     "Cash paid Advance to Sharukh khan for personal use "
     "(deduct of june and july month) sa…", "10000.00", None),
    (9, 36972, date(2026, 6, 5), "Canola", "5630003", "STAFF WELFARE", "Staffwellfair",
     "Cash paid to Sukhmit ji for purchase matters and pillow and etc. "
     "for some visitor at site", "5650.00", None),
    (10, 36972, date(2026, 6, 3), "Canola", "5630004", "REFRESHMENT", "Vegetable",
     "Cash paid to Sukhmit ji for purchase vegetable for kitchen use (qty.173kg)",
     "4230.00", None),
    (11, 36972, date(2026, 6, 5), "Canola", "5630004", "REFRESHMENT", "Parsad",
     "Cash paid to Sukhmit ji for purchase sweets for ardas at site", "480.00", None),
    (12, 23342, date(2026, 6, 5), "WG", "5650016",
     "REPAIR AND MAINTENANCE PLANT & MACHINERY", "DP switch",
     "Cash paid to Sukhmit ji for purchase DP switch for sidel machine use (qty.1pcs.)",
     "470.00", None),
    (13, 36972, date(2026, 6, 6), "Canola", "5630004", "REFRESHMENT", "Refreshment",
     "Cash paid to Honey singh for purchase jeera and chai for kitchen use",
     "4800.00", None),
    (14, 9620, date(2026, 6, 6), "Canola", "5650016",
     "REPAIR AND MAINTENANCE PLANT & MACHINERY", "Ghee machine",
     "Cash paid to Saurabh kumar for site visit charge for desi ghee machine",
     "1500.00", None),
    (15, None, date(2026, 6, 6), None, "", "", "Cash",
     "Cash receive by ATM card", None, "50000.00"),
    (16, 36972, date(2026, 6, 10), "Canola", "5100015", "CONSUMABLE/DIRECT EXPENSE",
     "Plug cap",
     "Cash paid to Manoj ji for purchase plug for oil bottle use (qty.5pkt.) "
     "party-pyara lal bill no.119…", "1298.00", None),
    (17, 23342, date(2026, 6, 10), "WG", "5680028", "FREIGHT INWARD-INDIRECT", "Freight",
     "Cash paid to Sanjay sir for freight charge for RFC machine for beverage "
     "plant use (bill no.65 party-J.Pe…", "9000.00", None),
    # GUESS: SAP holds a per-employee advance account and has no "Amit pal".
    # The nearest are AMIT ADVANCE JWPL2383 / 2608 / 2625, AMIT KUMAR and
    # AMIT RAO. The first is used; it is the one row here most likely wrong.
    (18, 36972, date(2026, 6, 10), "Canola", "11133194", "AMIT ADVANCE JWPL2383",
     "Advance", "Cash paid advance to Amit pal (deduct of June month salary)",
     "5000.00", None),
    (19, 36972, date(2026, 6, 6), "Canola", "5630004", "REFRESHMENT", "Refreshment",
     "Cash paid to Ekam preet singh for refreshment exp. for some visitor at site",
     "588.00", None),
    # GUESS: a litre of refined oil drawn as a plant sample. SAP has no sample
    # head; LAB AND TESTING is where a sample drawn for checking belongs.
    (20, 36972, date(2026, 6, 10), "Canola", "5680013", "LAB AND TESTING", "Sample",
     "Cash paid to Jasmeet ji for purchase refind oil for plant use as sample "
     "(qty.1 ltr.)", "151.00", None),
    (21, 23342, date(2026, 6, 10), "WG", "5650016",
     "REPAIR AND MAINTENANCE PLANT & MACHINERY", "Belt",
     "Cash paid to Jasmeet ji for purchase belt for wg plant use (qty.2 pcs.)",
     "350.00", None),
    # GUESS: the sheet books a 1,500-rupee hot gun to "F/A". ELECTRICAL
    # APPLIANCES is the fixed-asset head it fits; it may well belong in R&M.
    (22, 36972, date(2026, 6, 10), "Canola", "1205001", "ELECTRICAL APPLIANCES",
     "Hot gun",
     "Cash paid to Jasmeet ji for purchase hot gun for oil plant use (qty.1pcs.)",
     "1500.00", None),
    (23, 36972, date(2026, 6, 10), "Common", "5650016",
     "REPAIR AND MAINTENANCE PLANT & MACHINERY", "Drill bit",
     "Cash paid to Jasmeet ji for purchase drill bit for plant use (qty.1pcs.)",
     "600.00", None),
    (24, 23342, date(2026, 6, 10), "WG", "5650016",
     "REPAIR AND MAINTENANCE PLANT & MACHINERY", "Transistor",
     "Cash paid to Jameet ji for purchase transistor for wg plant use qty.1pcs.",
     "70.00", None),
]


def _noon(on: date):
    """The sheet holds dates, the model holds times. Midday, so no timezone
    shift can push a bunch onto the day before it was sent."""
    return timezone.make_aware(datetime.combine(on, time(12, 0)))


class Command(BaseCommand):
    help = "Load the petty-cash spreadsheet into a company's cash book (demo data)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--company",
            default="JIVO_OIL",
            help="Company code whose book to load. Default JIVO_OIL -- the "
            "sheet's departments are Canola (oil) and WG, and it is one box.",
        )
        parser.add_argument(
            "--custodian-email",
            default="",
            help="Who the entries are recorded by. Defaults to the first superuser.",
        )
        parser.add_argument(
            "--approver-email",
            default="",
            help="Who approved the bunches. Defaults to the custodian.",
        )
        parser.add_argument(
            "--reset",
            action="store_true",
            help="Delete this company's existing cash entries and bunches first.",
        )
        parser.add_argument(
            "--yes",
            action="store_true",
            help="Required. Writes rows to whatever database settings point at.",
        )

    def handle(self, *args, **options):
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

        existing = CashEntry.objects.filter(company=company)
        if existing.exists():
            if not options["reset"]:
                raise CommandError(
                    f"{company.code} already has {existing.count()} cash entries. "
                    f"Pass --reset to replace them."
                )
            with transaction.atomic():
                existing.delete()
                CashBunch.objects.filter(company=company).delete()
            self.stdout.write(self.style.WARNING("Cleared the existing book."))

        with transaction.atomic():
            departments = self._departments()
            entries = self._load_entries(company, custodian, departments)
            self._load_bunches(company, custodian, approver, entries)
            self._verify(company)

        self.stdout.write(
            self.style.SUCCESS(
                f"Loaded {len(SHEET)} entries and {len(BUNCHES)} bunches into "
                f"{company.code}. Closing balance "
                f"{services.current_balance(company):,.2f}."
            )
        )

    # ------------------------------------------------------------------

    def _user(self, email, role):
        if email:
            user = User.objects.filter(email=email).first()
            if user is None:
                raise CommandError(f"No user with email {email} to act as {role}.")
            return user
        user = User.objects.filter(is_superuser=True).order_by("id").first()
        if user is None:
            raise CommandError(
                f"No superuser to act as {role}. Pass --{role}-email."
            )
        self.stdout.write(f"  {role}: {user.email}")
        return user

    def _departments(self):
        """The sheet's three departments, created if the app has not got them."""
        resolved = {}
        for name in DEPARTMENTS:
            department, created = Department.objects.get_or_create(name=name)
            resolved[name] = department
            if created:
                self.stdout.write(f"  created department {name}")
        return resolved

    def _load_entries(self, company, custodian, departments):
        """Write the rows in the sheet's own order -- the balance follows it."""
        entries = {}
        for (
            serial,
            bunch_number,
            entry_date,
            department_name,
            gl_code,
            gl_name,
            item,
            detail,
            cash_out,
            cash_in,
        ) in SHEET:
            entries[serial] = (
                services.record_entry(
                    user=custodian,
                    company=company,
                    entry_date=entry_date,
                    direction=CashDirection.IN if cash_in else CashDirection.OUT,
                    amount=Decimal(cash_in or cash_out),
                    detail=detail,
                    item=item,
                    department=departments.get(department_name),
                    gl_account_code=gl_code,
                    gl_account_name=gl_name,
                ),
                bunch_number,
            )
        return entries

    def _load_bunches(self, company, custodian, approver, entries):
        """Bundle each bunch, approve it, then date it as the sheet dates it.

        Sent through the service layer rather than built by hand, so the seeded
        book is in a state the application itself could have produced. The two
        timestamps are corrected afterwards because the services stamp *now* --
        which is right for a bunch somebody is really sending, and wrong for
        one being reproduced from June.
        """
        for number, (sent_on, signed_on) in BUNCHES.items():
            entry_ids = [
                entry.id
                for entry, bunch_number in entries.values()
                if bunch_number == number
            ]
            bunch = services.send_for_approval(
                user=custodian, company=company, entry_ids=entry_ids
            )
            services.approve_bunch(user=approver, bunch=bunch)

            bunch.number = number
            bunch.sent_at = _noon(sent_on)
            bunch.decided_at = _noon(signed_on)
            bunch.save(update_fields=["number", "sent_at", "decided_at"])
            self.stdout.write(
                f"  bunch {number}: {len(entry_ids)} entries, approved {signed_on}"
            )

    def _verify(self, company):
        """The sheet's own closing balance is the check on the transcription."""
        balance = services.current_balance(company)
        if balance != EXPECTED_CLOSING_BALANCE:
            raise CommandError(
                f"Closing balance is {balance}, but the sheet's last row reads "
                f"{EXPECTED_CLOSING_BALANCE}. The transcription is wrong -- "
                f"nothing has been written."
            )
        approved = CashEntry.objects.filter(
            company=company, bunch__status=BunchStatus.APPROVED
        ).count()
        unsent = CashEntry.objects.filter(company=company, bunch__isnull=True).count()
        self.stdout.write(
            f"  balance {balance:,.2f} · {approved} entries approved · "
            f"{unsent} receipts left unbunched"
        )
