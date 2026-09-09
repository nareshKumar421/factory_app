"""Reading the warehouse's shift-wise issue sheet into the register.

Run with:
    python manage.py test warehouse.tests_rm_stock_import \
        --settings=config.sqlite_test_settings

The fixture is the real sheet's layout — Date, Shift, RM SAP Code, SKU,
Requirement, and *two* "Issued to Production" columns headed with two people's
names — including the two things that make it awkward: the same item appearing
on two shifts, and a second issuer column that is blank on the later shift.
"""

import io
from datetime import date
from decimal import Decimal

from django.contrib.auth.models import Permission
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from accounts.models import User
from company.models import Company, UserCompany, UserRole
from warehouse.models_manager import UserWarehouse
from warehouse.models_rm_stock import RawMaterialStock, RawMaterialStockEntry
from warehouse.services import rm_stock_import

IMPORT_URL = "/api/v1/warehouse/rm-stock/import/"

HEADERS = [
    "Date", "Shift", "RM SAP Code", "SKU",
    "Requirement ( Raju Veer ji )",
    "Issued to Production ( Vicky Veer ji )",
    "Issued to Production ( Shunty Veer ji )",
]

# The rows as they appear on the sheet, including "45,000.00" as text and a
# blank second issuer on Shift 2.
SHEET_ROWS = [
    ["08.09.2026", "Shift 1", "RM0000011", "GROUNDNUT LOOSE OIL", 16000, 16000, 16000],
    ["08.09.2026", "Shift 1", "RM0000003", "MUSTARD LOOSE OIL", "45,000.00", 50000, 50000],
    ["08.09.2026", "Shift 1", "RM0000002", "CANOLA COLD PRESS LOOSE OIL OLD", 9000, 8500, 8500],
    ["08.09.2026", "Shift 1", "RM0000009", "REFINED SUNFLOWER OIL", 40000, 36000, 36000],
    ["08.09.2026", "Shift 2", "RM0000003", "MUSTARD LOOSE OIL", 3200, "3,100.00", None],
    ["08.09.2026", "Shift 2", "RM0000002", "CANOLA COLD PRESS LOOSE OIL OLD", 3000, 1200, None],
    ["08.09.2026", "Shift 2", "RM0000009", "REFINED SUNFLOWER OIL", 12000, "11,250", None],
]


def workbook(rows=None, headers=None, lead_blank_rows=0) -> io.BytesIO:
    from openpyxl import Workbook

    book = Workbook()
    sheet = book.active
    for _ in range(lead_blank_rows):
        sheet.append([])
    sheet.append(headers if headers is not None else HEADERS)
    for row in rows if rows is not None else SHEET_ROWS:
        sheet.append(row)
    buffer = io.BytesIO()
    book.save(buffer)
    buffer.seek(0)
    buffer.name = "issue-sheet.xlsx"
    return buffer


class ParseSheetTests(TestCase):

    def parse(self, **kwargs):
        return rm_stock_import.parse_sheet(workbook(**kwargs))

    def items(self, parsed):
        return {i["item_code"]: i for i in parsed["items"]}

    def test_the_issued_figure_is_what_is_registered_not_the_requirement(self):
        item = self.items(self.parse())["RM0000011"]

        self.assertEqual(item["qty"], "16000")
        self.assertEqual(item["requirement"], "16000")

    def test_a_requirement_and_an_issue_that_differ_keep_the_issue(self):
        """Mustard was wanted 45,000 and 50,000 went out. The register takes what went."""
        item = self.items(self.parse())["RM0000003"]

        # 50,000 on Shift 1 plus 3,100 on Shift 2.
        self.assertEqual(item["qty"], "53100.00")

    def test_shifts_are_summed_into_one_figure_per_item(self):
        items = self.items(self.parse())

        self.assertEqual(items["RM0000002"]["qty"], "9700")     # 8,500 + 1,200
        self.assertEqual(items["RM0000009"]["qty"], "47250")    # 36,000 + 11,250
        self.assertEqual(items["RM0000002"]["shifts"], ["Shift 1", "Shift 2"])
        self.assertEqual(items["RM0000002"]["row_count"], 2)

    def test_an_item_on_one_shift_only_is_not_doubled(self):
        item = self.items(self.parse())["RM0000011"]

        self.assertEqual(item["qty"], "16000")
        self.assertEqual(item["row_count"], 1)

    def test_the_two_issuer_columns_are_never_added_together(self):
        """They are two people counting one issue, not two issues.

        Adding them would double every quantity on the sheet.
        """
        item = self.items(self.parse())["RM0000011"]

        self.assertEqual(item["qty"], "16000")
        self.assertEqual(self.parse()["issuer_columns"], 2)

    def test_a_blank_second_issuer_falls_back_to_the_one_that_is_filled(self):
        item = self.items(self.parse())["RM0000009"]

        self.assertEqual(item["qty"], "47250")
        self.assertFalse(item["has_mismatch"])

    def test_quantities_typed_with_commas_are_read_as_numbers(self):
        rows = [["08.09.2026", "Shift 1", "RM0000003", "MUSTARD", "45,000.00", "3,100.00", None]]
        item = self.items(self.parse(rows=rows))["RM0000003"]

        self.assertEqual(item["qty"], "3100.00")
        self.assertEqual(item["requirement"], "45000.00")

    def test_a_day_first_date_is_read_day_first(self):
        """08.09.2026 is 8 September, not 9 August."""
        item = self.items(self.parse())["RM0000011"]

        self.assertEqual(item["as_of_date"], "2026-09-08")

    def test_the_figure_is_dated_by_its_most_recent_line(self):
        rows = [
            ["01.09.2026", "Shift 1", "RM0000003", "MUSTARD", 100, 100, None],
            ["05.09.2026", "Shift 2", "RM0000003", "MUSTARD", 100, 100, None],
        ]
        item = self.items(self.parse(rows=rows))["RM0000003"]

        self.assertEqual(item["as_of_date"], "2026-09-05")

    def test_the_two_issuers_disagreeing_is_reported_not_hidden(self):
        rows = [["08.09.2026", "Shift 1", "RM0000003", "MUSTARD", 100, 900, 800]]
        parsed = self.parse(rows=rows)

        self.assertEqual(len(parsed["mismatches"]), 1)
        self.assertEqual(parsed["mismatches"][0]["values"], ["900", "800"])
        # The later column is the one used, and the row says so.
        self.assertEqual(parsed["mismatches"][0]["using"], "800")
        self.assertTrue(self.items(parsed)["RM0000003"]["has_mismatch"])

    def test_a_row_with_no_issued_quantity_is_skipped_and_named(self):
        rows = [
            ["08.09.2026", "Shift 1", "RM0000003", "MUSTARD", 100, None, None],
            ["08.09.2026", "Shift 1", "RM0000011", "GROUNDNUT", 100, 100, None],
        ]
        parsed = self.parse(rows=rows)

        self.assertEqual([s["item_code"] for s in parsed["skipped"]], ["RM0000003"])
        self.assertEqual(list(self.items(parsed)), ["RM0000011"])

    def test_a_negative_issue_is_refused_rather_than_registered(self):
        rows = [
            ["08.09.2026", "Shift 1", "RM0000003", "MUSTARD", 100, -5, None],
            ["08.09.2026", "Shift 1", "RM0000011", "GROUNDNUT", 100, 100, None],
        ]
        parsed = self.parse(rows=rows)

        self.assertEqual(len(parsed["skipped"]), 1)
        self.assertIn("negative", parsed["skipped"][0]["reason"].lower())

    def test_blank_and_spacer_rows_are_ignored_without_complaint(self):
        rows = [
            [None, None, None, None, None, None, None],
            ["08.09.2026", "Shift 1", "RM0000011", "GROUNDNUT", 100, 100, None],
            [None, None, None, "TOTAL", None, 100, None],
        ]
        parsed = self.parse(rows=rows)

        self.assertEqual(list(self.items(parsed)), ["RM0000011"])
        self.assertEqual(parsed["skipped"], [])

    def test_the_header_row_is_found_below_a_title_row(self):
        """Sheets grow title rows; insisting on row 1 breaks the first tidy-up."""
        parsed = self.parse(lead_blank_rows=3)

        self.assertEqual(parsed["header_row"], 4)
        self.assertEqual(len(parsed["items"]), 4)

    def test_the_issuer_names_in_the_headers_do_not_have_to_match(self):
        headers = list(HEADERS)
        headers[5] = "Issued to Production ( Somebody Else )"
        headers[6] = "Issued to Production ( Another Person )"
        parsed = self.parse(headers=headers)

        self.assertEqual(parsed["issuer_columns"], 2)

    def test_a_lower_case_item_code_still_matches_the_register(self):
        rows = [["08.09.2026", "Shift 1", "rm0000011", "GROUNDNUT", 100, 100, None]]

        self.assertEqual(list(self.items(self.parse(rows=rows))), ["RM0000011"])

    def test_a_sheet_with_no_issued_column_is_refused(self):
        headers = ["Date", "Shift", "RM SAP Code", "SKU", "Requirement"]
        with self.assertRaises(rm_stock_import.SheetError):
            rm_stock_import.parse_sheet(
                workbook(headers=headers, rows=[["08.09.2026", "S1", "RM1", "X", 1]])
            )

    def test_a_sheet_with_nothing_usable_is_refused(self):
        with self.assertRaises(rm_stock_import.SheetError):
            self.parse(rows=[[None, None, None, None, None, None, None]])

    def test_a_file_that_is_not_a_spreadsheet_is_refused(self):
        with self.assertRaises(rm_stock_import.SheetError):
            rm_stock_import.parse_sheet(io.BytesIO(b"this is not a workbook"))


@override_settings(RM_STOCK_WAREHOUSE="BH-LO")
class ImportAPITests(TestCase):

    def setUp(self):
        self.company = Company.objects.create(code="JIVO_OIL", name="Jivo Oil")
        role = UserRole.objects.create(name="Store")
        self.keeper = User.objects.create_user(
            email="pm@example.com", full_name="PM Keeper",
            employee_code="E-PM", password="x",
        )
        UserCompany.objects.create(user=self.keeper, company=self.company, role=role)
        for codename in ("can_view_rm_stock", "can_set_rm_stock"):
            self.keeper.user_permissions.add(
                Permission.objects.get(
                    content_type__app_label="warehouse", codename=codename
                )
            )
        UserWarehouse.objects.create(
            user=self.keeper, company=self.company, warehouse_code="BH-LO"
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.keeper)
        self.client.credentials(HTTP_COMPANY_CODE=self.company.code)

    def post(self, **data):
        payload = {"file": workbook(**data.pop("sheet", {}))}
        payload.update(data)
        return self.client.post(IMPORT_URL, payload, format="multipart")

    def test_a_preview_writes_nothing(self):
        response = self.post()

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["committed"])
        self.assertEqual(len(response.data["items"]), 4)
        self.assertFalse(RawMaterialStock.objects.exists())

    def test_committing_writes_one_register_row_per_item(self):
        response = self.post(commit="true")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["committed"])
        self.assertEqual(RawMaterialStock.objects.count(), 4)

        mustard = RawMaterialStock.objects.get(item_code="RM0000003")
        self.assertEqual(mustard.qty, Decimal("53100.000"))
        self.assertEqual(mustard.warehouse_code, "BH-LO")
        self.assertEqual(mustard.as_of_date, date(2026, 9, 8))

    def test_an_imported_figure_says_where_it_came_from(self):
        """A register entry nobody can trace is a number nobody can defend."""
        self.post(commit="true")
        row = RawMaterialStock.objects.get(item_code="RM0000002")

        self.assertIn("issue sheet", row.remarks)
        self.assertIn("Shift 1", row.remarks)
        self.assertIn("Shift 2", row.remarks)
        # Both issuers' readings survive on the permanent record.
        self.assertIn("8500", row.remarks)

    def test_an_import_leaves_the_same_history_a_typed_figure_would(self):
        self.post(commit="true")
        self.post(commit="true")

        entries = RawMaterialStockEntry.objects.filter(item_code="RM0000011")
        self.assertEqual(entries.count(), 2)
        self.assertEqual(
            entries.order_by("id").first().action, RawMaterialStockEntry.Action.CREATED
        )
        self.assertEqual(
            entries.order_by("id").last().action, RawMaterialStockEntry.Action.UPDATED
        )

    def test_a_disagreement_between_issuers_stops_the_commit(self):
        rows = [["08.09.2026", "Shift 1", "RM0000003", "MUSTARD", 100, 900, 800]]
        response = self.post(sheet={"rows": rows}, commit="true")

        self.assertEqual(response.status_code, 409)
        self.assertFalse(RawMaterialStock.objects.exists())
        self.assertEqual(len(response.data["mismatches"]), 1)

    def test_a_disagreement_can_be_accepted_deliberately(self):
        rows = [["08.09.2026", "Shift 1", "RM0000003", "MUSTARD", 100, 900, 800]]
        response = self.post(
            sheet={"rows": rows}, commit="true", accept_mismatches="true"
        )

        self.assertEqual(response.status_code, 200)
        row = RawMaterialStock.objects.get()
        self.assertEqual(row.qty, Decimal("800.000"))
        self.assertIn("disagreed", row.remarks)

    def test_a_keeper_who_does_not_run_the_register_warehouse_writes_nothing(self):
        UserWarehouse.objects.filter(user=self.keeper).delete()
        response = self.post(commit="true")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["written"], [])
        self.assertEqual(len(response.data["failed"]), 4)
        self.assertFalse(RawMaterialStock.objects.exists())

    def test_a_bad_file_is_a_400_not_a_500(self):
        response = self.client.post(
            IMPORT_URL, {"file": io.BytesIO(b"nope")}, format="multipart"
        )
        self.assertEqual(response.status_code, 400)

    def test_the_file_is_required(self):
        response = self.client.post(IMPORT_URL, {}, format="multipart")
        self.assertEqual(response.status_code, 400)


def tsv(rows, headers=None) -> str:
    """The clipboard shape: tab-separated, one line per row, blanks as empties."""
    lines = []
    if headers is not None:
        lines.append("\t".join(headers))
    for row in rows:
        lines.append("\t".join("" if c is None else str(c) for c in row))
    return "\r\n".join(lines)


class PasteTests(TestCase):
    """Rows copied straight out of Excel, which arrive as tab-separated text."""

    def items(self, parsed):
        return {i["item_code"]: i for i in parsed["items"]}

    def test_a_paste_with_headers_reads_like_the_file(self):
        parsed = rm_stock_import.parse_pasted(tsv(SHEET_ROWS, HEADERS))

        items = self.items(parsed)
        self.assertEqual(len(items), 4)
        self.assertEqual(items["RM0000003"]["qty"], "53100.00")
        self.assertEqual(items["RM0000002"]["qty"], "9700")

    def test_a_paste_of_data_rows_only_falls_back_to_the_column_order(self):
        """Copying just the rows, without the header line, is the common case."""
        parsed = rm_stock_import.parse_pasted(tsv(SHEET_ROWS))

        items = self.items(parsed)
        self.assertEqual(len(items), 4)
        self.assertEqual(items["RM0000011"]["qty"], "16000")
        self.assertIsNone(parsed["header_row"])

    def test_a_paste_matches_the_file_exactly(self):
        """The two routes must not read the same grid differently."""
        from_file = rm_stock_import.parse_sheet(workbook())
        from_paste = rm_stock_import.parse_pasted(tsv(SHEET_ROWS, HEADERS))

        self.assertEqual(
            [(i["item_code"], i["qty"]) for i in from_file["items"]],
            [(i["item_code"], i["qty"]) for i in from_paste["items"]],
        )

    def test_excel_thousands_separators_survive_the_clipboard(self):
        parsed = rm_stock_import.parse_pasted(tsv(SHEET_ROWS, HEADERS))

        self.assertEqual(self.items(parsed)["RM0000009"]["qty"], "47250")

    def test_a_blank_trailing_line_is_ignored(self):
        parsed = rm_stock_import.parse_pasted(tsv(SHEET_ROWS, HEADERS) + "\r\n")

        self.assertEqual(len(parsed["items"]), 4)

    def test_a_disagreement_is_reported_from_a_paste_too(self):
        rows = [["08.09.2026", "Shift 1", "RM0000003", "MUSTARD", 100, 900, 800]]
        parsed = rm_stock_import.parse_pasted(tsv(rows, HEADERS))

        self.assertEqual(len(parsed["mismatches"]), 1)

    def test_a_single_column_paste_is_refused_with_an_explanation(self):
        """One column has no tabs, and guessing at spaces would split names."""
        with self.assertRaises(rm_stock_import.SheetError) as caught:
            rm_stock_import.parse_pasted("RM0000011\nRM0000003\n")

        self.assertIn("columns", str(caught.exception).lower())

    def test_an_item_name_with_spaces_is_not_split_into_columns(self):
        rows = [["08.09.2026", "Shift 1", "RM0000002", "CANOLA COLD PRESS LOOSE OIL OLD",
                 9000, 8500, None]]
        parsed = rm_stock_import.parse_pasted(tsv(rows, HEADERS))

        item = self.items(parsed)["RM0000002"]
        self.assertEqual(item["item_name"], "CANOLA COLD PRESS LOOSE OIL OLD")
        self.assertEqual(item["qty"], "8500")

    def test_empty_text_is_refused(self):
        with self.assertRaises(rm_stock_import.SheetError):
            rm_stock_import.parse_pasted("")


class PasteAPITests(ImportAPITests):
    """The same endpoint behaviour, driven by `text` instead of a file."""

    def post(self, **data):
        sheet = data.pop("sheet", {})
        payload = {
            "text": tsv(sheet.get("rows", SHEET_ROWS), sheet.get("headers", HEADERS))
        }
        payload.update(data)
        return self.client.post(IMPORT_URL, payload, format="multipart")

    def test_a_bad_file_is_a_400_not_a_500(self):
        response = self.client.post(
            IMPORT_URL, {"text": "no columns here"}, format="multipart"
        )
        self.assertEqual(response.status_code, 400)

    def test_the_file_is_required(self):
        response = self.client.post(IMPORT_URL, {}, format="multipart")
        self.assertEqual(response.status_code, 400)

    def test_a_pasted_import_says_it_came_from_a_paste(self):
        self.post(commit="true")
        row = RawMaterialStock.objects.get(item_code="RM0000011")

        self.assertIn("pasted rows", row.remarks)
