"""
Tests for the advance summary block -- columns M to P of the register's tab.

It went unread for a whole import because it sits past the grid's last header,
so the first thing tested here is that it is found at all. The rest is the one
risk it carries: the same money written down in two places. "Tiwari ji" in the
list and the ``bunty in out`` tab are one pot, and an import that believed both
would say the factory was owed 21,626.00 more than it is.
"""

from datetime import date
from decimal import Decimal
from io import BytesIO

import openpyxl
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase

from company.models import Company

from . import services, sheet_advances
from .models import AdvanceEntry

User = get_user_model()

HEADER = [
    "Sr.no.", "Bunch", "Date", "Department", "G/L", "Item", "Detail",
    "Out", "In", "Balance", "Sign. Date", "Send Date",
]


def sheet_with_block(block_rows, grid_rows=()):
    """A register tab carrying the block in columns M-P, as the real one does."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Cash details 04-06-2026"
    ws.append(HEADER)
    for row in grid_rows:
        ws.append(list(row) + [None] * (len(HEADER) - len(row)))

    # The block starts at row 1, beside the header -- which is exactly why a
    # reader that trusts the header row walks past it.
    for index, (when, detail, amount, note) in enumerate(block_rows, start=1):
        ws.cell(row=index, column=13, value=when)
        ws.cell(row=index, column=14, value=detail)
        ws.cell(row=index, column=15, value=amount)
        ws.cell(row=index, column=16, value=note)

    stream = BytesIO()
    wb.save(stream)
    stream.seek(0)
    return openpyxl.load_workbook(stream, data_only=True)["Cash details 04-06-2026"]


class NameReadingTests(TestCase):
    """Getting a person out of the custodian's shorthand."""

    def test_the_name_stops_where_the_reason_starts(self):
        self.assertEqual(
            sheet_advances.person_of("Kabal singh ko deye advance gaddi k leye"),
            "Kabal Singh",
        )

    def test_a_title_is_not_part_of_the_name(self):
        """Otherwise 'Jasmeet' and 'Jasmeet ji' become two people."""
        self.assertEqual(sheet_advances.person_of("Jasmeet ji ko deye manoj ne"), "Jasmeet")
        self.assertEqual(sheet_advances.person_of("Jasmeet ko deye"), "Jasmeet")

    def test_the_owed_direction_reads_the_same_way(self):
        self.assertEqual(
            sheet_advances.person_of("Rinkle vg ko dene hai mene pancher k leye"),
            "Rinkle",
        )

    def test_a_line_with_no_joining_word_is_kept_whole(self):
        self.assertEqual(sheet_advances.person_of("Manoj"), "Manoj")


class BlockReadingTests(TestCase):
    """Finding the block, and reading its two directions."""

    def test_it_is_found_beside_the_header_row(self):
        sheet = sheet_with_block(
            [(date(2026, 7, 21), "Vishal ko dene hai company k", 750, None)]
        )
        rows = sheet_advances.read_advance_block(sheet)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["person"], "Vishal")
        self.assertEqual(rows[0]["amount"], 750)

    def test_a_negative_row_is_money_the_factory_owes(self):
        sheet = sheet_with_block(
            [(date(2026, 8, 31), "Rinkle vg ko dene hai mene pancher", -400, None)]
        )
        row = sheet_advances.read_advance_block(sheet)[0]
        self.assertTrue(row["owed_to_them"])
        self.assertEqual(row["amount"], -400)

    def test_the_two_directions_net(self):
        sheet = sheet_with_block(
            [
                (date(2026, 7, 21), "Vishal ko deye", 750, None),
                (date(2026, 8, 31), "Rinkle ko dene hai", -400, None),
            ]
        )
        rows = sheet_advances.read_advance_block(sheet)
        self.assertEqual(sheet_advances.block_total(rows), 350)

    def test_a_spacer_row_does_not_end_it(self):
        """The real block has a gap at rows 8 and 9, and continues after it."""
        sheet = sheet_with_block(
            [
                (date(2026, 4, 6), "Jameet ji ko deye", 1638, None),
                (None, None, None, None),
                (None, None, None, None),
                (date(2026, 7, 21), "Vishal ko dene hai company k", 750, None),
            ]
        )
        self.assertEqual(len(sheet_advances.read_advance_block(sheet)), 2)

    def test_a_name_with_no_figure_is_not_a_holder(self):
        sheet = sheet_with_block(
            [
                (date(2026, 4, 6), "Somebody the custodian did not finish", None, None),
                (date(2026, 7, 21), "Vishal ko deye", 750, None),
            ]
        )
        rows = sheet_advances.read_advance_block(sheet)
        self.assertEqual([row["person"] for row in rows], ["Vishal"])

    def test_the_note_column_is_kept(self):
        sheet = sheet_with_block(
            [(date(2026, 7, 29), "Kulveer ji ko deye", 500, "500/- online by kamal ji")]
        )
        self.assertEqual(
            sheet_advances.read_advance_block(sheet)[0]["note"],
            "500/- online by kamal ji",
        )


class BlockImportTests(TestCase):
    """What the block becomes once it is in the book.

    The workbook here is the real one in miniature: a register, a person tab
    that never clears, and a summary list naming that same pot under another
    name.
    """

    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(code="JIVO_OIL", name="Jivo Oil")
        cls.user = User.objects.create_superuser(
            email="custodian@example.com", password="x"
        )

    def build(self, block_rows, tab=None):
        """A workbook with a register, an optional person tab, and the block."""
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Cash details 04-06-2026"
        ws.append(HEADER)
        # One receipt and one payment, so the book has a balance to sit under.
        ws.append([1, None, date(2026, 6, 4), "Canola", None, None,
                   "Cash receive by Atm card", None, 50000, 50000, None, None])
        ws.append([2, None, date(2026, 6, 5), "Canola", "freight", None,
                   "Cash paid to Ravi kumar for freight", 6000, None, 44000,
                   None, None])

        for index, (when, detail, amount, note) in enumerate(block_rows, start=1):
            ws.cell(row=index, column=13, value=when)
            ws.cell(row=index, column=14, value=detail)
            ws.cell(row=index, column=15, value=amount)
            ws.cell(row=index, column=16, value=note)

        if tab:
            person_sheet = wb.create_sheet(tab["name"])
            person_sheet.append(["Sr.", "Date", "Detail", "Out", "In", "Total"])
            for row in tab["rows"]:
                person_sheet.append(row)

        stream = BytesIO()
        wb.save(stream)
        stream.seek(0)
        return stream

    def run_import(self, stream):
        import tempfile

        temp = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
        temp.write(stream.read())
        temp.close()
        call_command(
            "import_cash_sheet",
            file=temp.name,
            yes=True,
            reset=True,
            verbosity=0,
        )
        return temp.name

    def test_each_row_becomes_somebody_holding_cash(self):
        self.run_import(
            self.build(
                [
                    (date(2026, 7, 21), "Vishal ko dene hai company k", 750, None),
                    (date(2026, 7, 29), "Kulveer ji ko deye courier k leye", 500, None),
                ]
            )
        )
        holders = {
            row["person"].full_name: row["balance"]
            for row in services.advance_holders(self.company)
        }
        self.assertEqual(holders["Vishal"], Decimal("750.00"))
        self.assertEqual(holders["Kulveer"], Decimal("500.00"))

    def test_an_owed_row_lands_as_a_negative_balance(self):
        self.run_import(
            self.build(
                [(date(2026, 8, 31), "Rinkle vg ko dene hai mene pancher", -400, None)]
            )
        )
        holders = {
            row["person"].full_name: row["balance"]
            for row in services.advance_holders(self.company)
        }
        self.assertEqual(holders["Rinkle"], Decimal("-400.00"))

    def test_a_row_a_tab_already_details_is_not_counted_twice(self):
        """The bug this whole file guards: "Tiwari ji" IS the bunty tab."""
        self.run_import(
            self.build(
                [(date(2026, 7, 10), "Tiwari ji ko deye driver exp k leye", 900, None)],
                tab={
                    "name": "bunty in out",
                    "rows": [
                        [1, date(2026, 6, 10), "Bunty jo ko deye", 900, None, 900],
                    ],
                },
            )
        )
        total = sum(
            row["balance"] for row in services.advance_holders(self.company)
        )
        self.assertEqual(total, Decimal("900.00"), "the same pot was counted twice")

    def test_somebody_with_a_tab_is_brought_to_the_list_figure(self):
        """The list is the authority; the tab is detail, not a second sum."""
        self.run_import(
            self.build(
                [(date(2026, 9, 1), "Jasmeet ji ko deye manoj ne", 650, None)],
                tab={
                    "name": "Jasmeet in out",
                    "rows": [
                        [1, date(2026, 6, 10), "Jasmeet ji ko deye", 265, None, 265],
                    ],
                },
            )
        )
        holders = {
            row["person"].full_name: row["balance"]
            for row in services.advance_holders(self.company)
        }
        self.assertEqual(holders["Jasmeet Ji"], Decimal("650.00"))
        # And the adjustment says why, rather than appearing as a fresh float.
        adjustment = AdvanceEntry.objects.filter(
            company=self.company, detail__contains="advance list carries"
        ).first()
        self.assertIsNotNone(adjustment)
        self.assertEqual(adjustment.amount, Decimal("385.00"))

    def test_the_advances_come_out_of_the_box_not_on_top_of_it(self):
        """Handing a float out moves cash; it does not create any."""
        self.run_import(
            self.build([(date(2026, 7, 21), "Vishal ko dene hai company k", 750, None)])
        )
        recon = services.reconciliation(self.company)
        self.assertEqual(recon["advance_given"], Decimal("750.00"))
        self.assertEqual(recon["cash_in_hand"], Decimal("43250.00"))
        self.assertEqual(
            recon["cash_in_hand"] + recon["advance_given"],
            services.current_balance(self.company),
        )


class PersonMatchingTests(TestCase):
    """Who a name on the sheet turns out to be.

    The bug here cost a production clean-up. The sheet calls people what the
    custodian calls them -- a first name, a nickname, a spelling that went
    down as it sounded -- and the import matched on the full name alone. So it
    invented a second Bhupinder, a second Gurnam, a second Bunty, and hung
    their floats off logins nobody uses, while the real accounts sat empty.
    """

    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(code="JIVO_OIL", name="Jivo Oil")
        User.objects.create_superuser(email="custodian@example.com", password="x")
        cls.real = User.objects.create_user(
            email="sukhmeet@jivo.in", password="x", full_name="Sukhmeet Singh"
        )
        cls.gurnam = User.objects.create_user(
            email="gurnam@jivo.in", password="x", full_name="Gurnam Singh"
        )

    def build_and_import(self, block_rows):
        import tempfile

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Cash details 04-06-2026"
        ws.append(HEADER)
        ws.append([1, None, date(2026, 6, 4), "Canola", None, None,
                   "Cash receive by Atm card", None, 50000, 50000, None, None])
        for index, (when, detail, amount) in enumerate(block_rows, start=1):
            ws.cell(row=index, column=13, value=when)
            ws.cell(row=index, column=14, value=detail)
            ws.cell(row=index, column=15, value=amount)

        temp = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
        wb.save(temp.name)
        temp.close()
        call_command("import_cash_sheet", file=temp.name, yes=True, reset=True,
                     verbosity=0)

    def holders(self):
        return {
            row["person"].email: row["balance"]
            for row in services.advance_holders(self.company)
        }

    def test_a_confirmed_alias_finds_the_real_account(self):
        """"Sukhmit" is Sukhmeet Singh -- which no string comparison knows."""
        self.build_and_import(
            [(date(2026, 7, 28), "Sukhmit ji ko deye grass cutting machine", -15232)]
        )
        self.assertEqual(
            self.holders().get("sukhmeet@jivo.in"), Decimal("-15232.00")
        )
        self.assertFalse(
            User.objects.filter(email__endswith="@cash-book.local").exists(),
            "a second Sukhmeet was invented alongside the real one",
        )

    def test_a_first_name_finds_somebody_with_a_fuller_one(self):
        self.build_and_import([(date(2026, 7, 22), "Gurnam vg ko transfer keye", 1815)])
        self.assertEqual(self.holders().get("gurnam@jivo.in"), Decimal("1815.00"))

    def test_an_outsider_still_gets_a_login_less_person(self):
        """Not everybody is staff. The AC man is not meant to have an account."""
        self.build_and_import([(date(2026, 4, 6), "Amit Ac vale se lene hai", 230)])
        invented = User.objects.filter(email__endswith="@cash-book.local")
        self.assertEqual(invented.count(), 1)
        self.assertFalse(invented.first().is_active)

    def test_two_spellings_of_one_person_land_on_one_account(self):
        """"Jameet" and "Jasmeet ji" are the same man; his float is one float."""
        User.objects.create_user(
            email="jasmeet@jivo.in", password="x", full_name="Jasmeet Singh"
        )
        self.build_and_import(
            [
                (date(2026, 4, 6), "Jameet ji ko deye", 1638),
                (date(2026, 9, 1), "Jasmeet ji ko deye manoj ne", 650),
            ]
        )
        self.assertEqual(self.holders().get("jasmeet@jivo.in"), Decimal("2288.00"))
