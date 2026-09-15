"""
Tests for the sheet loader.

The point of these is the transcription, not the plumbing. The spreadsheet's
own balance column is the check: enter its 24 rows in its own order and the
book has to close at 86,743.00, with the same figure against each of the rows
the screenshot shows. If a digit was mistyped anywhere, one of these fails.
"""

from decimal import Decimal
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from accounts.models import Department
from company.models import Company

from . import services
from .models import BunchStatus, CashBunch, CashDirection, CashEntry

User = get_user_model()


class SeedCashBookSheetTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        cls.superuser = User.objects.create(
            email="boss@example.com", is_superuser=True, is_staff=True
        )

    def seed(self, **kwargs):
        out = StringIO()
        call_command("seed_cash_book_sheet", yes=True, stdout=out, stderr=out, **kwargs)
        return out.getvalue()

    def entries(self):
        """The book in its own order, so row N is the sheet's Sr. no. N."""
        return list(CashEntry.objects.filter(company=self.company).order_by("id"))

    # -- the transcription -------------------------------------------------

    def test_the_book_closes_where_the_sheet_closes(self):
        self.seed()
        self.assertEqual(
            services.current_balance(self.company), Decimal("86743.00")
        )

    def test_every_balance_in_the_screenshot_is_reproduced(self):
        """The whole Balance column, row by row, as the sheet prints it."""
        self.seed()
        expected = [
            "50000.00", "44000.00", "38000.00", "36000.00", "33230.00",
            "32430.00", "82430.00", "72430.00", "66780.00", "62550.00",
            "62070.00", "61600.00", "56800.00", "55300.00", "105300.00",
            "104002.00", "95002.00", "90002.00", "89414.00", "89263.00",
            "88913.00", "87413.00", "86813.00", "86743.00",
        ]
        self.assertEqual(
            [str(entry.balance_after) for entry in self.entries()], expected
        )

    def test_all_twenty_four_rows_land(self):
        self.seed()
        rows = self.entries()
        self.assertEqual(len(rows), 24)
        self.assertEqual(
            sum(1 for row in rows if row.direction == CashDirection.IN), 3
        )

    def test_the_three_atm_receipts_are_never_bunched(self):
        """Money arriving in the box is not a voucher anybody approves."""
        self.seed()
        receipts = [
            row for row in self.entries() if row.direction == CashDirection.IN
        ]
        self.assertEqual(len(receipts), 3)
        for receipt in receipts:
            self.assertIsNone(receipt.bunch_id)
            self.assertEqual(receipt.amount, Decimal("50000.00"))
            self.assertEqual(receipt.gl_account_code, "")
            self.assertIsNone(receipt.department)

    def test_a_back_dated_row_keeps_its_place_in_the_book(self):
        """Row 4 is dated the 3rd and sits between two entries dated later."""
        self.seed()
        rows = self.entries()
        self.assertEqual(str(rows[2].entry_date), "2026-06-05")
        self.assertEqual(str(rows[3].entry_date), "2026-06-03")
        self.assertEqual(rows[3].balance_after, Decimal("36000.00"))

    # -- the bunches -------------------------------------------------------

    def test_the_sheets_own_bunch_numbers_are_kept(self):
        self.seed()
        self.assertEqual(
            sorted(
                CashBunch.objects.filter(company=self.company).values_list(
                    "number", flat=True
                )
            ),
            [9620, 17570, 23342, 36972],
        )

    def test_each_bunch_holds_the_rows_the_sheet_puts_in_it(self):
        self.seed()
        sizes = {
            bunch.number: bunch.entries.count()
            for bunch in CashBunch.objects.filter(company=self.company)
        }
        self.assertEqual(sizes, {17570: 5, 36972: 11, 23342: 4, 9620: 1})

    def test_every_bunch_is_approved_and_dated_as_the_sheet_dates_it(self):
        """The sheet's Sign Date becomes the approval time."""
        self.seed()
        signed = {
            bunch.number: (
                str(bunch.sent_at.date()),
                str(bunch.decided_at.date()),
                bunch.status,
            )
            for bunch in CashBunch.objects.filter(company=self.company)
        }
        self.assertEqual(
            signed,
            {
                17570: ("2026-06-05", "2026-06-05", BunchStatus.APPROVED),
                36972: ("2026-06-11", "2026-06-11", BunchStatus.APPROVED),
                23342: ("2026-06-12", "2026-06-12", BunchStatus.APPROVED),
                9620: ("2026-06-12", "2026-06-12", BunchStatus.APPROVED),
            },
        )

    def test_a_bunch_is_never_signed_before_its_own_entries(self):
        """The check that settles the sheet's dates as mm/dd, not dd/mm."""
        self.seed()
        for bunch in CashBunch.objects.filter(company=self.company):
            for entry in bunch.entries.all():
                self.assertLessEqual(entry.entry_date, bunch.decided_at.date())

    # -- the masters -------------------------------------------------------

    def test_the_sheets_departments_are_created(self):
        self.seed()
        for name in ("Canola", "WG", "Common"):
            self.assertTrue(Department.objects.filter(name=name).exists())

    def test_an_existing_department_is_reused_not_duplicated(self):
        Department.objects.create(name="Canola")
        self.seed()
        self.assertEqual(Department.objects.filter(name="Canola").count(), 1)

    def test_the_two_spellings_of_wg_are_one_department(self):
        self.seed()
        wg_rows = CashEntry.objects.filter(
            company=self.company, department__name="WG"
        )
        self.assertEqual(wg_rows.count(), 4)

    # -- the guards --------------------------------------------------------

    def test_it_refuses_to_write_without_yes(self):
        with self.assertRaises(CommandError):
            call_command("seed_cash_book_sheet", stdout=StringIO())
        self.assertEqual(CashEntry.objects.count(), 0)

    def test_it_refuses_to_load_twice_over_an_existing_book(self):
        self.seed()
        with self.assertRaises(CommandError):
            self.seed()
        self.assertEqual(CashEntry.objects.filter(company=self.company).count(), 24)

    def test_reset_replaces_the_book_rather_than_doubling_it(self):
        self.seed()
        self.seed(reset=True)
        self.assertEqual(CashEntry.objects.filter(company=self.company).count(), 24)
        self.assertEqual(CashBunch.objects.filter(company=self.company).count(), 4)
        self.assertEqual(
            services.current_balance(self.company), Decimal("86743.00")
        )

    def test_an_unknown_company_is_named_rather_than_crashed_on(self):
        with self.assertRaises(CommandError):
            self.seed(company="NOPE")
