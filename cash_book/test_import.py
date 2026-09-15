"""
Tests for reading the cash sheet workbook.

Built on workbooks made here rather than on the real file, so the suite does
not depend on a spreadsheet sitting in somebody's Downloads folder. The
fixtures reproduce the three cell shapes the real one contains: text dates
Excel refused, transposed datetimes it mis-parsed, and literal datetimes from
after the locale changed.
"""

from datetime import date, datetime
from decimal import Decimal
from io import BytesIO, StringIO

import openpyxl
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from company.models import Company

from . import services, sheet_gl_map, sheet_import
from .models import BunchStatus, CashBranch, CashBunch, CashEntry
from .sheet_import import SheetError

User = get_user_model()

HEADER = [
    "Sr.no.", "Bunch", "Date", "Department", "G/L", "Item", "Detail",
    "Out", "In", "Balance", "Sign. Date", "Send Date",
]


def workbook(rows, sheet_name="Cash details 04-06-2026"):
    """A one-sheet workbook, returned as a file-like object."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet_name
    ws.append(HEADER)
    for row in rows:
        ws.append(list(row) + [None] * (len(HEADER) - len(row)))
    stream = BytesIO()
    wb.save(stream)
    stream.seek(0)
    return stream


def sheet_of(rows):
    return openpyxl.load_workbook(workbook(rows), data_only=True)["Cash details 04-06-2026"]


class DateReadingTests(TestCase):
    """The transposition rule, which is the whole risk in this import."""

    def test_a_text_date_is_literal_day_first(self):
        self.assertEqual(
            sheet_import.parse_sheet_date("30/05/2026", transposed=True),
            date(2026, 5, 30),
        )
        self.assertEqual(
            sheet_import.parse_sheet_date("30/05/2026", transposed=False),
            date(2026, 5, 30),
        )

    def test_a_transposed_datetime_is_swapped_back(self):
        """2026-12-06 was typed as 12 June, not 6 December."""
        self.assertEqual(
            sheet_import.parse_sheet_date(datetime(2026, 12, 6), transposed=True),
            date(2026, 6, 12),
        )

    def test_a_literal_datetime_is_left_alone(self):
        self.assertEqual(
            sheet_import.parse_sheet_date(datetime(2026, 7, 18), transposed=False),
            date(2026, 7, 18),
        )

    def test_an_unswappable_datetime_in_the_transposed_era_is_refused(self):
        """Day 18 cannot have come from the swap, so it is not guessed at."""
        with self.assertRaises(SheetError):
            sheet_import.parse_sheet_date(datetime(2026, 7, 18), transposed=True)

    def test_the_boundary_is_found_from_the_evidence(self):
        cells = [
            (5, "30/05/2026"),          # text -> mm/dd era
            (6, datetime(2026, 6, 4)),  # ambiguous
            (7, datetime(2026, 7, 18)), # day 18 -> dd/mm era
            (8, datetime(2026, 6, 4)),  # ambiguous
        ]
        self.assertEqual(sheet_import.detect_transposition_boundary(cells), 7)

    def test_a_column_with_no_literal_evidence_is_transposed_throughout(self):
        cells = [(2, "30/05/2026"), (3, datetime(2026, 6, 4))]
        self.assertIsNone(sheet_import.detect_transposition_boundary(cells))

    def test_a_column_with_no_text_evidence_is_literal_throughout(self):
        cells = [(2, datetime(2026, 7, 18)), (3, datetime(2026, 6, 4))]
        self.assertEqual(sheet_import.detect_transposition_boundary(cells), 2)

    def test_interleaved_eras_are_refused_rather_than_guessed(self):
        """No single boundary explains this, so every ambiguous date is a coin toss."""
        cells = [
            (2, "30/05/2026"),
            (3, datetime(2026, 7, 18)),
            (4, "31/05/2026"),
        ]
        with self.assertRaises(SheetError):
            sheet_import.detect_transposition_boundary(cells)

    def test_each_column_is_calibrated_on_its_own(self):
        """The Date and Sign columns really do switch at different rows."""
        rows = [
            # Date text (mm/dd era) but Sign already literal (dd/mm era).
            [1, 100, "30/05/2026", "Canola", "Refreshment", "Tea", "d", 10, None,
             None, datetime(2026, 7, 18), datetime(2026, 7, 18)],
            # Excel stored Apr 6 -- the real sheet's very first row. It was
            # typed "04/06/2026" (4 June) and read as mm/dd.
            [2, 100, datetime(2026, 4, 6), "Canola", "Refreshment", "Tea", "d", 10,
             None, None, datetime(2026, 7, 18), datetime(2026, 7, 18)],
        ]
        parsed = sheet_import.read_rows(sheet_of(rows))
        self.assertEqual(parsed[0]["date"], date(2026, 5, 30))
        self.assertEqual(parsed[1]["date"], date(2026, 6, 4))
        # ...while both Sign dates were written after the switch, so literal.
        self.assertEqual(parsed[0]["sign_date"], date(2026, 7, 18))


class RowReadingTests(TestCase):
    def test_rows_without_a_date_are_skipped(self):
        rows = [
            [1, None, datetime(2026, 6, 4), "Canola", "Cash", "Cash", "ATM",
             None, 50000, 50000],
            [2, None, None, None, None, None, None, None, None, None],
            [3, None, None, None, None, None, None, None, None, None],
        ]
        self.assertEqual(len(sheet_import.read_rows(sheet_of(rows))), 1)

    def test_a_row_with_both_an_in_and_an_out_is_refused(self):
        rows = [[1, None, datetime(2026, 6, 4), "Canola", "Cash", "Cash", "d", 10, 20]]
        with self.assertRaises(SheetError):
            sheet_import.read_rows(sheet_of(rows))

    def test_a_row_with_neither_amount_is_refused(self):
        rows = [[1, None, datetime(2026, 6, 4), "Canola", "Cash", "Cash", "d", None, None]]
        with self.assertRaises(SheetError):
            sheet_import.read_rows(sheet_of(rows))

    def test_the_sheets_departments_become_the_four_branches(self):
        rows = [
            [1, 1, datetime(2026, 6, 4), "Canola", "R&M", "B", "d", 10, None],
            [2, 1, datetime(2026, 6, 4), "Wg", "R&M", "B", "d", 10, None],
            [3, 1, datetime(2026, 6, 4), "wg", "R&M", "B", "d", 10, None],
            [4, 1, datetime(2026, 6, 4), "Water", "R&M", "B", "d", 10, None],
            [5, 1, datetime(2026, 6, 4), "Mart", "R&M", "B", "d", 10, None],
            [6, 1, datetime(2026, 6, 4), "Nowhere", "R&M", "B", "d", 10, None],
            [7, 1, datetime(2026, 6, 4), "", "R&M", "B", "d", 10, None],
        ]
        parsed = sheet_import.read_rows(sheet_of(rows))
        self.assertEqual(
            [row["branch"] for row in parsed],
            ["Oil", "Beverage", "Beverage", "Water", "Common", "Common",
             "Common"],
        )

    def test_the_side_list_past_the_register_is_ignored(self):
        """Columns 13+ are somebody's informal IOU list in the same tab."""
        rows = [
            [1, None, datetime(2026, 6, 4), "Canola", "Cash", "Cash", "ATM", None,
             50000, 50000, None, None, datetime(2026, 6, 4), "Jameet ji ko deye", 1638],
        ]
        parsed = sheet_import.read_rows(sheet_of(rows))
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["in"], 50000)


class BalanceCheckTests(TestCase):
    def test_a_disagreeing_balance_cell_is_reported(self):
        rows = [
            [1, None, datetime(2026, 6, 4), "", "Cash", "Cash", "ATM", None, 50000, 50000],
            [2, 1, datetime(2026, 6, 4), "Canola", "Refreshment", "Tea", "d", 6000, None, 99999],
        ]
        parsed = sheet_import.read_rows(sheet_of(rows))
        problems = sheet_import.check_balances(parsed)
        self.assertEqual(len(problems), 1)
        self.assertEqual(problems[0][1], 99999)
        self.assertEqual(problems[0][2], 44000)

    def test_an_agreeing_column_reports_nothing(self):
        rows = [
            [1, None, datetime(2026, 6, 4), "", "Cash", "Cash", "ATM", None, 50000, 50000],
            [2, 1, datetime(2026, 6, 4), "Canola", "Refreshment", "Tea", "d", 6000, None, 44000],
        ]
        parsed = sheet_import.read_rows(sheet_of(rows))
        self.assertEqual(sheet_import.check_balances(parsed), [])


class GLMapTests(TestCase):
    def test_the_sheets_spelling_variants_reach_one_account(self):
        for pair in (("Freight", "Fright"), ("Housekeeping", "Hosekeeping"),
                     ("Conveyance", "Conveyace")):
            first, second = (sheet_gl_map.resolve(word) for word in pair)
            self.assertIsNotNone(first, pair[0])
            self.assertEqual(first, second, pair)

    def test_lookup_ignores_case_and_padding(self):
        self.assertEqual(
            sheet_gl_map.resolve("  REFRESHMENT  "), sheet_gl_map.resolve("refreshment")
        )

    def test_an_unknown_head_resolves_to_nothing_rather_than_a_default(self):
        self.assertIsNone(sheet_gl_map.resolve("Spaceship"))

    def test_the_judgement_calls_are_declared_as_such(self):
        self.assertIn("advacne", sheet_gl_map.uncertain_words())
        self.assertNotIn("refreshment", sheet_gl_map.uncertain_words())


class ImportCommandTests(TestCase):
    """End to end, on a small workbook shaped like the real one."""

    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        cls.superuser = User.objects.create(
            email="boss@example.com", is_superuser=True, is_staff=True
        )

    def sheet_file(self, rows):
        path = self._make_path()
        openpyxl.load_workbook(workbook(rows)).save(path)
        return path

    def _make_path(self):
        import tempfile

        handle = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
        handle.close()
        return handle.name

    #: One receipt, three payments, two of them a bunch signed on the 11th.
    ROWS = [
        [1, None, datetime(2026, 6, 4), "Canola", "Cash", "Cash",
         "Cash receive by ATM card", None, 50000, 50000],
        [2, 17570, datetime(2026, 6, 4), "Canola", "Refreshment", "Refreshment",
         "Cash paid to Ravi kumar", 6000, None, 44000,
         datetime(2026, 11, 6), datetime(2026, 11, 6)],
        [3, 17570, datetime(2026, 3, 6), "Canola", "Installation", "Installation",
         "Cash paid to Amit kumar for A.c installation", 2770, None, 41230,
         datetime(2026, 11, 6), datetime(2026, 11, 6)],
        [4, None, datetime(2026, 6, 6), "Wg", "R&M", "Belt",
         "Cash paid to Jasmeet ji for belt", 350, None, 40880],
    ]

    def run_import(self, rows=None, **kwargs):
        out = StringIO()
        call_command(
            "import_cash_sheet",
            file=self.sheet_file(rows or self.ROWS),
            stdout=out,
            stderr=out,
            **kwargs,
        )
        return out.getvalue()

    def test_a_dry_run_writes_nothing(self):
        output = self.run_import(dry_run=True)
        self.assertIn("Dry run", output)
        self.assertEqual(CashEntry.objects.count(), 0)
        self.assertEqual(CashBranch.objects.count(), 0)

    def test_the_import_reproduces_the_sheets_balance_column(self):
        self.run_import(yes=True)
        balances = [
            str(entry.balance_after)
            for entry in CashEntry.objects.filter(company=self.company).order_by("id")
        ]
        self.assertEqual(
            balances, ["50000.00", "44000.00", "41230.00", "40880.00"]
        )
        self.assertEqual(
            services.current_balance(self.company), Decimal("40880.00")
        )

    def test_the_sheets_bunch_number_is_kept_and_approved_on_its_sign_date(self):
        self.run_import(yes=True)
        bunch = CashBunch.objects.get(company=self.company)
        self.assertEqual(bunch.number, 17570)
        self.assertEqual(bunch.status, BunchStatus.APPROVED)
        # 2026-11-06 in the transposed era is 6 November... but this column has
        # no literal evidence, so it is 11 June, which is after both entries.
        self.assertEqual(str(bunch.decided_at.date()), "2026-06-11")
        self.assertEqual(bunch.entries.count(), 2)

    def test_an_unbunched_payment_stays_unbunched(self):
        self.run_import(yes=True)
        loose = CashEntry.objects.filter(company=self.company, bunch__isnull=True)
        self.assertEqual(loose.count(), 2)  # the receipt and the belt

    def test_a_receipt_carries_no_branch_or_head(self):
        self.run_import(yes=True)
        receipt = CashEntry.objects.filter(company=self.company).order_by("id").first()
        self.assertEqual(receipt.gl_account_code, "")
        self.assertIsNone(receipt.branch)

    def test_a_payment_carries_the_mapped_sap_account(self):
        self.run_import(yes=True)
        entry = CashEntry.objects.filter(
            company=self.company, item="Refreshment"
        ).first()
        self.assertEqual(entry.gl_account_code, "5630004")
        self.assertEqual(entry.gl_account_name, "REFRESHMENT")

    def test_branches_are_created_from_the_sheet(self):
        self.run_import(yes=True)
        names = set(
            CashBranch.objects.filter(company=self.company).values_list(
                "name", flat=True
            )
        )
        # Canola becomes Oil and Wg becomes Beverage -- nothing called
        # "Canola" survives the import.
        self.assertEqual(names, {"Oil", "Beverage"})

    def test_an_unmapped_head_stops_the_import_and_is_named(self):
        rows = list(self.ROWS)
        rows.append(
            [5, None, datetime(2026, 6, 6), "Canola", "Spaceship", "Warp",
             "Cash paid for a warp core", 99, None, 40781]
        )
        with self.assertRaises(CommandError) as caught:
            self.run_import(rows=rows, yes=True)
        self.assertIn("Spaceship", str(caught.exception))
        self.assertEqual(CashEntry.objects.count(), 0)

    def test_it_refuses_to_write_without_yes(self):
        with self.assertRaises(CommandError):
            self.run_import()
        self.assertEqual(CashEntry.objects.count(), 0)

    def test_it_refuses_to_import_twice_without_reset(self):
        self.run_import(yes=True)
        with self.assertRaises(CommandError):
            self.run_import(yes=True)
        self.assertEqual(CashEntry.objects.filter(company=self.company).count(), 4)

    def test_reset_replaces_the_book_rather_than_doubling_it(self):
        self.run_import(yes=True)
        self.run_import(yes=True, reset=True)
        self.assertEqual(CashEntry.objects.filter(company=self.company).count(), 4)
        self.assertEqual(CashBunch.objects.filter(company=self.company).count(), 1)
        self.assertEqual(
            services.current_balance(self.company), Decimal("40880.00")
        )

    def test_a_missing_sheet_names_the_ones_that_are_there(self):
        with self.assertRaises(CommandError) as caught:
            self.run_import(yes=True, sheet="No Such Tab")
        self.assertIn("Cash details", str(caught.exception))

    def test_a_missing_file_is_reported_plainly(self):
        out = StringIO()
        with self.assertRaises(CommandError):
            call_command(
                "import_cash_sheet", file="nowhere.xlsx", dry_run=True, stdout=out
            )
