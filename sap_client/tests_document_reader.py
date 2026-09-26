"""Tests for the SAP document reader ported from SAP Portal.

``HanaDocumentReader`` against a fake HANA connection that answers by SQL
pattern, and the two journal-preview builders on their own. Nothing here
reaches SAP.

    python manage.py test sap_client.tests_document_reader --settings=config.sqlite_test_settings

The builder cases are the ones SAP Portal's commits say were verified against
live drafts: RCM IGST, RCM CGST+SGST and normal GST + TDS (5e434d4), A/P and A/R
credit notes reversed (ca4a0ad), and a normal A/P invoice unchanged.
"""

import re
from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase
from hdbcli import dbapi

from .exceptions import SAPConnectionError, SAPDataError
from .hana.document_reader import (
    HanaDocumentReader,
    attachment_file_name,
    build_draft_journal,
    build_payment_journal,
    extract_tds_section,
    gst_account_kind,
    rate_text,
    resolve_gst_account,
)


def _context():
    context = MagicMock()
    context.hana = {"host": "h", "port": 1, "user": "u", "password": "p", "schema": "SCHEMA"}
    context.company_code = "JIVO_OIL"
    return context


class FakeHana:
    """A HANA connection that answers each statement from the first handler
    whose pattern matches its SQL. A handler's answer is a list of dicts (one
    per row) or an exception to raise."""

    def __init__(self, handlers):
        self.handlers = handlers
        self.calls = []

    def cursor(self):
        return _FakeCursor(self)

    def close(self):
        pass

    def sql(self, pattern):
        return [call for call in self.calls if re.search(pattern, call[0], re.S)]


class _FakeCursor:
    def __init__(self, hana):
        self.hana = hana
        self.description = None
        self._rows = []

    def execute(self, sql, params=()):
        self.hana.calls.append((sql, tuple(params)))
        for pattern, answer in self.hana.handlers:
            if re.search(pattern, sql, re.S):
                break
        else:
            answer = []
        if isinstance(answer, Exception):
            raise answer
        columns = []
        for row in answer:
            for key in row:
                if key not in columns:
                    columns.append(key)
        self.description = [(column,) for column in columns]
        self._rows = [tuple(row.get(column) for column in columns) for row in answer]

    def fetchall(self):
        return self._rows

    def close(self):
        pass


class ReaderTestCase(SimpleTestCase):
    def reader_with(self, handlers):
        reader = HanaDocumentReader(_context())
        hana = FakeHana(handlers)
        patcher = patch.object(reader.connection, "connect", return_value=hana)
        patcher.start()
        self.addCleanup(patcher.stop)
        return reader, hana


# A/P invoice 5001: two lines copied from GRPO 7001, a freight line with a
# negative SAC key and its received quantity in a UDF.
AP_LINES = [
    {"LineNum": 0, "AcctCode": "5010101", "WhsCode": "BH-FG", "OcrCode": "DL", "OcrCode2": "", "LocCode": 5,
     "SACEntry": None, "BaseType": 20, "BaseEntry": 7001, "BaseRef": "7001", "LineTotal": 1000, "LineVat": 180,
     "TaxCode": "IGST18"},
    {"LineNum": 1, "AcctCode": "5010102", "WhsCode": "BH-FG", "OcrCode": "HR", "LocCode": 5,
     "SACEntry": -482, "BaseType": -1, "BaseEntry": None, "U_Recvd_Qty": 12, "LineTotal": 500, "LineVat": 0,
     "TaxCode": ""},
]
AP_HANDLERS = [
    (r'"PCH1" WHERE', AP_LINES),
    (r'"OPCH" H', [{"paid_to_date": 400, "gross_profit": 0, "trans_id": 90001, "obj_type": "18",
                    "card_code": "VENDA000010", "card_name": "SHIV SHAMBHU TRADERS", "doc_total": 1680}]),
    (r'"PCH5" X', [{"code": "C2", "rate": 2, "amount": 20, "taxable": 1000, "name": "Contractor 194C",
                    "section": "", "ap_account": "2104001", "ar_account": ""}]),
    (r'"OACT" WHERE "AcctCode" IN', [{"code": "5010101", "name": "Purchase - Oil"},
                                      {"code": "5010102", "name": "Freight Inward"}]),
    (r'"OSAC"', [{"entry": -482, "code": "00996519", "name": "Goods transport"}]),
    (r'"OPRC"', [{"code": "DL", "name": "Delhi"}, {"code": "HR", "name": "Haryana"}]),
    (r'"OLCT"', [{"code": 5, "name": "DELHI ISD"}]),
    (r'"OBPL" WHERE', [{"id": 5, "name": "Branch Five"}, {"id": 1, "name": "Jivo Wellness - HR"}]),
    (r'"OSLP"', [{"kind": "sales_person", "name": "KAMALDEEP SINGH"}, {"kind": "payment_terms", "name": "30 DAYS"}]),
    (r'"OWHS" W', [{"code": "BH-FG", "name": "Bahadurgarh FG", "street": "Plot 1", "city": "Bahadurgarh",
                    "state": "HR", "gstin": "06AAAAA0000A1Z5", "branch": "Jivo Wellness - HR"}]),
    (r'"OCRD" C', [
        {"card_code": "VENDA000010", "card_name": "SHIV SHAMBHU TRADERS", "card_type": "S", "valid_for": "Y",
         "lic_trad_num": "", "address": "MP OFFICE", "gstin": "23BBBBB1111B1Z1", "state": "MP"},
        {"card_code": "VENDA000010", "card_name": "SHIV SHAMBHU TRADERS", "card_type": "S", "valid_for": "Y",
         "lic_trad_num": "", "address": "DELHI", "gstin": "07CCCCC2222C1Z2", "state": "DL"},
    ]),
    (r'"OPDN" WHERE', [{"doc_entry": 7001, "doc_num": 626070001, "atc_entry": 165165,
                        "doc_date": date(2026, 7, 1), "trans_id": 80001}]),
    (r'"OJDT" H', [
        {"trans_id": 90001, "number": 1234, "ref_date": date(2026, 7, 3), "trans_type": "18", "memo": "AP Inv",
         "line_id": 0, "account": "5010101", "account_name": "Purchase - Oil", "debit": 1000, "credit": 0},
        {"trans_id": 90001, "number": 1234, "ref_date": date(2026, 7, 3), "trans_type": "18", "memo": "AP Inv",
         "line_id": 1, "account": "2101001", "account_name": "Creditors", "short_name": "VENDA000010",
         "debit": 0, "credit": 1000},
        {"trans_id": 80001, "number": 1200, "ref_date": date(2026, 7, 1), "trans_type": "20", "memo": "GRPO",
         "line_id": 0, "account": "1301001", "debit": 1000, "credit": 0},
    ]),
]


def _ap_request(**overrides):
    request = {
        "doc_entry": 5001,
        "line_table": "PCH1",
        "header_table": "OPCH",
        "tds_table": "PCH5",
        "account_codes": ["5010101"],
        "warehouse_codes": ["BH-FG"],
        "sales_person": 2,
        "payment_group": 3,
        "transport": -1,
        "card_code": "VENDA000010",
        "branch_ids": [1],
        "base_refs": [("20", 7001, "")],
        "in_transit_base_type": "20",
    }
    request.update(overrides)
    return request


class DocumentLookupTests(ReaderTestCase):
    def test_everything_a_posted_invoice_needs_comes_back_named(self):
        reader, hana = self.reader_with(AP_HANDLERS)
        found = reader.document_lookups(**_ap_request())
        self.assertEqual(found["warnings"], [])
        self.assertEqual(found["accounts"], {"5010101": "Purchase - Oil", "5010102": "Freight Inward"})
        # OSAC.AbsEntry can be negative (imported SAC rows, 779594d).
        self.assertEqual(found["sac"], {"-482": {"code": "00996519", "name": "Goods transport"}})
        self.assertEqual(found["dimensions"], {"DL": "Delhi", "HR": "Haryana"})
        self.assertEqual(found["locations"], {"5": "DELHI ISD"})
        self.assertEqual(found["header_names"], {"sales_person": "KAMALDEEP SINGH", "payment_terms": "30 DAYS"})
        self.assertEqual(found["warehouses"]["BH-FG"]["address"], "Plot 1, Bahadurgarh, HR")
        self.assertEqual(len(found["partners"]), 1)
        self.assertEqual(len(found["partners"][0]["addresses"]), 2)
        self.assertEqual(found["header"]["paid_to_date"], 400)
        self.assertEqual(found["tds"][0]["name"], "Contractor 194C")

    def test_the_journal_is_the_headers_and_in_transit_is_the_grpos(self):
        reader, hana = self.reader_with(AP_HANDLERS)
        found = reader.document_lookups(**_ap_request())
        journal = found["journal_entry"]
        self.assertEqual((journal["trans_id"], journal["number"], journal["total_credit"]), (90001, 1234, 1000.0))
        self.assertEqual([line["account_name"] for line in journal["lines"]], ["Purchase - Oil", "Creditors"])
        self.assertEqual([j["trans_id"] for j in found["in_transit_journal_entries"]], [80001])
        # Both journals in one statement, keyed by TransId — never a text search.
        (je_call,) = hana.sql(r'"OJDT" H')
        self.assertEqual(sorted(je_call[1]), [80001, 90001])
        self.assertFalse(hana.sql(r"LIKE \?"))

    def test_base_documents_carry_their_own_attachment_entry(self):
        reader, _ = self.reader_with(AP_HANDLERS)
        found = reader.document_lookups(**_ap_request())
        self.assertEqual(
            found["base_documents"],
            [
                {
                    "base_type": "20",
                    "base_entry": 7001,
                    "type_label": "Goods Receipt PO",
                    "doc_num": 626070001,
                    "doc_date": "2026-07-01",
                    "base_ref": "7001",
                    "attachment_entry": 165165,
                    "trans_id": 80001,
                }
            ],
        )

    def test_statements_do_not_grow_with_the_lines(self):
        reader, few = self.reader_with(AP_HANDLERS)
        reader.document_lookups(**_ap_request())
        many_lines = [dict(AP_LINES[n % 2], LineNum=n, AcctCode=f"50101{n:02d}") for n in range(40)]
        reader, many = self.reader_with([(r'"PCH1" WHERE', many_lines), *AP_HANDLERS[1:]])
        reader.document_lookups(**_ap_request())
        self.assertEqual(len(few.calls), len(many.calls))
        (accounts,) = many.sql(r'"OACT" WHERE "AcctCode" IN')
        self.assertEqual(len(accounts[1]), 40)

    def test_values_are_bound_and_only_known_tables_are_named(self):
        reader, hana = self.reader_with(AP_HANDLERS)
        reader.document_lookups(**_ap_request(card_code="VENDA'10"))
        for sql, _params in hana.calls:
            self.assertNotIn("VENDA", sql)
            self.assertIn('"SCHEMA".', sql)
        with self.assertRaises(ValueError):
            reader.document_lookups(**_ap_request(line_table="OUSR"))

    def test_a_failing_lookup_is_named_and_the_rest_still_come_back(self):
        handlers = [(r'"OWHS" W', dbapi.Error("invalid column")), *AP_HANDLERS]
        reader, _ = self.reader_with(handlers)
        found = reader.document_lookups(**_ap_request())
        self.assertEqual(found["warnings"], ["warehouse names"])
        self.assertEqual(found["warehouses"], {})
        self.assertEqual(found["accounts"]["5010101"], "Purchase - Oil")

    def test_no_connection_is_an_outage(self):
        reader = HanaDocumentReader(_context())
        with patch.object(reader.connection, "connect", side_effect=dbapi.Error("down")):
            with self.assertRaises(SAPConnectionError):
                reader.document_lookups(**_ap_request())

    def test_a_transfer_finds_its_journal_by_type_and_source_entry(self):
        reader, hana = self.reader_with(
            [
                (r'"OJDT"\s+WHERE', [{"trans_id": 555}]),
                (r'"OJDT" H', [{"trans_id": 555, "number": 9, "trans_type": "67", "line_id": 0, "account": "1301"}]),
            ]
        )
        found = reader.document_lookups(doc_entry=77, line_table="WTR1", journal_created_by=("67", 77))
        self.assertEqual(found["journal_entry"]["trans_id"], 555)
        (lookup,) = hana.sql(r'"OJDT"\s+WHERE')
        self.assertEqual(lookup[1], ("67", 77))


DRAFT_HANDLERS = [
    (r'"DRF1" WHERE', [{"LineNum": 0, "AcctCode": "5010101", "LineTotal": 1000, "LineVat": 180, "TaxCode": "RIG18"}]),
    (r'"ODRF" H', [{"obj_type": "18", "card_code": "VENDA000010", "card_name": "SHIV SHAMBHU TRADERS",
                    "doc_total": 1000, "doc_date": date(2026, 7, 5), "control_account": "2101001",
                    "num_at_card": "TI-089/2026-27", "journal_memo": ""}]),
    (r'"DRF5" X', []),
    (r'"DRF3" X', []),
    (r'"OSTC" C', [{"code": "RIG18", "name": "IGST 18% RCM", "sta_code": "IGST", "rate": Decimal("18.000000")}]),
    (r'"OACT" WHERE "AcctCode" IN \(\?, \?\) OR', [
        {"code": "5010101", "name": "Purchase - Oil"},
        {"code": "2101001", "name": "Sundry Creditors"},
        {"code": "1201018", "name": "INPUT IGST @ 18 % RCM"},
        {"code": "1201008", "name": "INPUT IGST @ 18 %"},
        {"code": "2201018", "name": "OUTPUT IGST @ 18 % RCM"},
    ]),
    (r'"OACP" P', []),
]


class DraftJournalReaderTests(ReaderTestCase):
    def test_a_waiting_draft_gets_the_reconstruction(self):
        reader, hana = self.reader_with([(r'"OPCH" WHERE "draftKey"', []), *DRAFT_HANDLERS])
        found = reader.document_lookups(
            doc_entry=51165, line_table="DRF1", header_table="ODRF", tds_table="DRF5",
            card_code="VENDA000010", draft=True,
        )
        self.assertIsNone(found["journal_entry"])
        self.assertIsNone(found["posted_as"])
        preview = found["journal_preview"]
        self.assertTrue(preview["preview"])
        self.assertEqual(
            [(line["account"], line["debit"], line["credit"]) for line in preview["lines"]],
            [
                ("5010101", 1000.0, 0.0),
                ("1201018", 180.0, 0.0),
                ("2201018", 0.0, 180.0),
                ("2101001", 0.0, 1000.0),
            ],
        )
        self.assertEqual(preview["base_ref"], "TI-089/2026-27")
        # The draft's DRF1 rows were read once, for the screen and the preview.
        self.assertEqual(len(hana.sql(r'"DRF1" WHERE')), 1)

    def test_an_added_draft_shows_the_journal_of_the_document_it_became(self):
        reader, _ = self.reader_with(
            [
                (r'"OPCH" WHERE "draftKey"', [{"doc_entry": 5001, "doc_num": 626074136, "trans_id": 90001}]),
                (r'"OJDT" H', [{"trans_id": 90001, "number": 1234, "trans_type": "18", "line_id": 0, "account": "X"}]),
                *DRAFT_HANDLERS,
            ]
        )
        found = reader.document_lookups(
            doc_entry=51165, line_table="DRF1", header_table="ODRF", tds_table="DRF5", draft=True
        )
        self.assertEqual(found["posted_as"]["doc_num"], 626074136)
        self.assertEqual(found["posted_as"]["type_label"], "AP Invoice")
        self.assertEqual(found["journal_entry"]["trans_id"], 90001)
        self.assertIsNone(found["journal_preview"])


class AttachmentLineTests(ReaderTestCase):
    def test_lines_come_back_in_order_with_their_full_names(self):
        reader, hana = self.reader_with(
            [
                (r'"ATC1"', [
                    {"AbsEntry": 53121, "Line": 1, "FileName": "SN", "FileExt": "jpeg", "trgtPath": "\\\\share\\x",
                     "Date": date(2026, 7, 18)},
                    {"AbsEntry": 53121, "Line": 2, "FileName": "DocScanner 18 Jul 2026 5-58 pm", "FileExt": "pdf"},
                ])
            ]
        )
        lines = reader.attachment_lines(53121)
        self.assertEqual([line["line"] for line in lines], [1, 2])
        self.assertEqual(lines[0]["file_name"], "SN.jpeg")
        self.assertEqual(lines[0]["attached_on"], "2026-07-18")
        self.assertEqual(lines[1]["file_name"], "DocScanner 18 Jul 2026 5-58 pm.pdf")
        self.assertNotIn("trgtPath", lines[0])
        self.assertEqual(hana.calls[0][1], (53121,))

    def test_an_unreadable_attachment_table_is_a_data_error(self):
        reader, _ = self.reader_with([(r'"ATC1"', dbapi.Error("bad"))])
        with self.assertRaises(SAPDataError):
            reader.attachment_lines(1)

    def test_no_entry_reads_nothing(self):
        reader, hana = self.reader_with([])
        self.assertEqual(reader.attachment_lines(0), [])
        self.assertEqual(hana.calls, [])

    def test_file_name_keeps_a_stem_that_already_has_the_extension(self):
        self.assertEqual(attachment_file_name("1825.pdf", "pdf"), "1825.pdf")
        self.assertEqual(attachment_file_name("1825", ".PDF"), "1825.PDF")
        self.assertEqual(attachment_file_name("", "pdf"), "")


PAYMENT_HANDLERS = [
    (r'"OPDF" WHERE', [{"DocEntry": 812, "DocNum": 812, "DocDate": date(2026, 7, 9), "CardCode": "VENDA000010",
                        "CardName": "SHIV SHAMBHU TRADERS", "DocCurr": "INR", "TrsfrAcct": "1102003",
                        "TrsfrSum": 980, "CashSum": 0, "DocTotal": 980, "WtSum": 0, "WtAccount": "2104001",
                        "WddStatus": "W", "Status": "N", "AtcEntry": 170001, "Attachment": "legacy nclob",
                        "TransId": None, "U_Pymnt_Mode": "NEFT"}]),
    (r'"PDF4"', []),
    (r'"PDF2"', [{"invoice_id": 0, "doc_entry": 5001, "inv_type": "18", "sum_applied": 1000}]),
    (r'"PDF1"', []),
    (r'"PDF6" X', [{"code": "C2", "rate": 2, "amount": 20, "taxable": 1000, "name": "TDS 194C Contractor"}]),
    (r'"OPCH" WHERE "DocEntry" IN', [{"doc_entry": 5001, "doc_num": 626074136, "doc_date": date(2026, 7, 3),
                                        "doc_total": 1180}]),
    (r'"OACT" WHERE "AcctCode" IN', [{"code": "1102003", "name": "HDFC Bank"}, {"code": "2104001", "name": "TDS Payable 194C"}]),
]


class PaymentDraftReaderTests(ReaderTestCase):
    def test_a_payment_draft_is_assembled_with_its_real_attachment_entry(self):
        reader, _ = self.reader_with(PAYMENT_HANDLERS)
        draft = reader.payment_draft(812)
        # AtcEntry, not the legacy inline "Attachment" NCLOB (portal 2501-2505).
        self.assertEqual(draft["attachment_entry"], 170001)
        self.assertEqual(draft["header"]["approval_status"], "Pending approval")
        self.assertEqual(draft["payment"]["payment_mode"], "NEFT")
        self.assertEqual(draft["payment"]["invoices"][0]["doc_num"], 626074136)
        self.assertEqual(draft["payment"]["invoices"][0]["invoice_type"], "AP Invoice")
        self.assertEqual(draft["tds"][0]["section"], "194C")
        self.assertIsNone(draft["journal_entry"])

    def test_an_unposted_payment_draft_gets_a_balanced_preview(self):
        reader, _ = self.reader_with(PAYMENT_HANDLERS)
        preview = reader.payment_draft(812)["journal_preview"]
        self.assertEqual(
            [(line["account"], line["account_name"], line["debit"], line["credit"]) for line in preview["lines"]],
            [
                ("VENDA000010", "SHIV SHAMBHU TRADERS", 1000.0, 0.0),
                ("1102003", "HDFC Bank", 0.0, 980.0),
                ("2104001", "TDS Payable 194C", 0.0, 20.0),
            ],
        )
        self.assertEqual(preview["lines"][0]["line_memo"], "Settlement of 1 document(s)")
        self.assertEqual(preview["total_debit"], preview["total_credit"])

    def test_a_missing_draft_is_none(self):
        reader, _ = self.reader_with([(r'"OPDF" WHERE', [])])
        self.assertIsNone(reader.payment_draft(9))


class DraftJournalBuilderTests(SimpleTestCase):
    ACCOUNTS = {
        "5010101": "Purchase - Oil",
        "4010101": "Sales - Oil",
        "2101001": "Sundry Creditors",
        "1101001": "Sundry Debtors",
        "1201008": "INPUT IGST @ 18 %",
        "1201018": "INPUT IGST @ 18 % RCM",
        "2201008": "OUTPUT IGST @ 18 %",
        "2201018": "OUTPUT IGST @ 18 % RCM",
        "1201009": "INPUT CGST @ 9 %",
        "1201010": "INPUT SGST @ 9 %",
        "1201019": "INPUT CGST @ 9 % RCM",
        "1201020": "INPUT SGST @ 9 % RCM",
        "2201019": "OUTPUT CGST @ 9 % RCM",
        "2201020": "OUTPUT SGST @ 9 % RCM",
        "2201005": "OUTPUT IGST @ 5 %",
        "2104001": "TDS Payable 194C",
        "5101001": "Expense Clearing",
        "6101001": "Short & Excess Stock",
        "6101002": "Short & Excess",
    }
    TAX = {
        "IG18": {"name": "IGST 18%", "components": [("IGST", 18)]},
        "RIG18": {"name": "IGST 18% RCM", "components": [("IGST", 18)]},
        "RCS18": {"name": "CGST+SGST 18% Reverse Charge", "components": [("CGST", 9), ("SGST", 9)]},
        "IG5": {"name": "IGST 5%", "components": [("IGST", 5)]},
    }

    def header(self, obj_type, doc_total, control="2101001"):
        return {"obj_type": obj_type, "card_code": "BP1", "card_name": "Partner", "doc_total": doc_total,
                "control_account": control, "doc_date": "2026-07-05"}

    def build(self, obj_type, doc_total, lines, **kwargs):
        return build_draft_journal(
            self.header(obj_type, doc_total, kwargs.pop("control", "2101001")),
            lines,
            kwargs.pop("expenses", []),
            kwargs.pop("withholding", []),
            self.TAX,
            self.ACCOUNTS,
            kwargs.pop("rounding", None),
        )

    @staticmethod
    def legs(journal):
        return [(line["account"], line["debit"], line["credit"]) for line in journal["lines"]]

    def test_a_normal_ap_invoice_debits_expense_and_input_tax_and_credits_the_vendor(self):
        journal = self.build("18", 1180, [{"account": "5010101", "line_total": 1000, "line_vat": 180, "tax_code": "IG18"}])
        self.assertEqual(
            self.legs(journal),
            [("5010101", 1000.0, 0.0), ("1201008", 180.0, 0.0), ("2101001", 0.0, 1180.0)],
        )
        self.assertEqual(journal["total_debit"], journal["total_credit"])
        self.assertEqual(journal["lines"][2]["short_name"], "BP1")

    def test_rcm_igst_posts_the_input_rcm_and_the_matching_output_rcm_liability(self):
        journal = self.build("18", 1000, [{"account": "5010101", "line_total": 1000, "line_vat": 180, "tax_code": "RIG18"}])
        self.assertEqual(
            self.legs(journal),
            [("5010101", 1000.0, 0.0), ("1201018", 180.0, 0.0), ("2201018", 0.0, 180.0), ("2101001", 0.0, 1000.0)],
        )

    def test_rcm_cgst_and_sgst_split_by_component_rate(self):
        journal = self.build("18", 1000, [{"account": "5010101", "line_total": 1000, "line_vat": 180, "tax_code": "RCS18"}])
        self.assertEqual(
            self.legs(journal),
            [
                ("5010101", 1000.0, 0.0),
                ("1201019", 90.0, 0.0),
                ("2201019", 0.0, 90.0),
                ("1201020", 90.0, 0.0),
                ("2201020", 0.0, 90.0),
                ("2101001", 0.0, 1000.0),
            ],
        )

    def test_tds_posts_to_the_withholding_account_on_the_vendors_side(self):
        journal = self.build(
            "18", 1160,
            [{"account": "5010101", "line_total": 1000, "line_vat": 180, "tax_code": "IG18"}],
            withholding=[{"amount": 20, "ap_account": "2104001", "ar_account": "", "name": "Contractor 194C"}],
        )
        self.assertEqual(self.legs(journal)[-1], ("2104001", 0.0, 20.0))
        self.assertEqual(journal["lines"][-1]["account_name"], "TDS Payable 194C")
        self.assertEqual(journal["total_debit"], journal["total_credit"])
        self.assertNotIn("6101002", [line["account"] for line in journal["lines"]])

    def test_an_ap_credit_note_reverses_every_leg(self):
        journal = self.build("19", 1180, [{"account": "5010101", "line_total": 1000, "line_vat": 180, "tax_code": "IG18"}])
        self.assertEqual(
            self.legs(journal),
            [("5010101", 0.0, 1000.0), ("1201008", 0.0, 180.0), ("2101001", 1180.0, 0.0)],
        )

    def test_an_ar_credit_note_credits_the_customer_and_keeps_output_tax(self):
        journal = self.build(
            "14", 1050, [{"account": "4010101", "line_total": 1000, "line_vat": 50, "tax_code": "IG5"}],
            control="1101001",
        )
        self.assertEqual(
            self.legs(journal),
            [("4010101", 1000.0, 0.0), ("2201005", 50.0, 0.0), ("1101001", 0.0, 1050.0)],
        )

    def test_an_ar_invoice_debits_the_customer(self):
        journal = self.build(
            "13", 1050, [{"account": "4010101", "line_total": 1000, "line_vat": 50, "tax_code": "IG5"}],
            control="1101001",
        )
        self.assertEqual(
            self.legs(journal),
            [("4010101", 0.0, 1000.0), ("2201005", 0.0, 50.0), ("1101001", 1050.0, 0.0)],
        )

    def test_a_residual_lands_on_short_and_excess_never_the_stock_one(self):
        journal = self.build("18", 1180.4, [{"account": "5010101", "line_total": 1000, "line_vat": 180, "tax_code": "IG18"}])
        self.assertEqual(self.legs(journal)[-1], ("6101002", 0.4, 0.0))

    def test_the_rounding_account_from_account_determination_wins(self):
        journal = self.build(
            "18", 1180.4, [{"account": "5010101", "line_total": 1000, "line_vat": 180, "tax_code": "IG18"}],
            rounding=("7101001", "Rounding Off"),
        )
        self.assertEqual(self.legs(journal)[-1], ("7101001", 0.4, 0.0))

    def test_freight_on_stock_clears_through_expense_clearing(self):
        journal = self.build(
            "18", 1100, [{"account": "5010101", "line_total": 1000, "line_vat": 0, "tax_code": ""}],
            expenses=[{"account": "9999", "line_total": 100, "line_vat": 0, "tax_code": "", "stock": "Y"}],
        )
        self.assertIn(("5101001", 100.0, 0.0), self.legs(journal))

    def test_documents_the_rules_were_not_verified_for_get_no_preview(self):
        for obj_type in ("20", "22", "17", "67", ""):
            with self.subTest(obj_type=obj_type):
                self.assertIsNone(self.build(obj_type, 100, [{"account": "5010101", "line_total": 100}]))

    def test_gst_helpers(self):
        self.assertEqual(gst_account_kind("RCGST"), "CGST")
        self.assertEqual(gst_account_kind("RSG"), "SGST")
        self.assertEqual(gst_account_kind("IG"), "IGST")
        self.assertEqual(gst_account_kind("CESS"), "CESS")
        self.assertIsNone(gst_account_kind("TCS"))
        self.assertEqual(rate_text(Decimal("18.000000")), "18")
        self.assertEqual(rate_text(Decimal("2.500000")), "2.5")
        self.assertEqual(resolve_gst_account(self.ACCOUNTS, "INPUT", "IGST", 18, False), ("1201008", "INPUT IGST @ 18 %"))
        self.assertEqual(resolve_gst_account(self.ACCOUNTS, "INPUT", "IGST", 18, True), ("1201018", "INPUT IGST @ 18 % RCM"))
        self.assertIsNone(resolve_gst_account(self.ACCOUNTS, "INPUT", "IGST", 28, False))
        self.assertEqual(extract_tds_section("TDS u/s 194C Contractor"), "194C")
        self.assertEqual(extract_tds_section("TCS 206C1H"), "")
        self.assertEqual(extract_tds_section("TCS 206CH"), "206CH")


class PaymentJournalBuilderTests(SimpleTestCase):
    def test_gl_lines_come_first_then_the_partner_for_the_rest(self):
        journal = build_payment_journal(
            {
                "card_code": "V1",
                "card_name": "Vendor",
                "invoices": [],
                "accounts": [{"account_code": "6101", "account_name": "Rent", "sum_paid": 300, "cost_centers": ["DL"]}],
                "transfer_account": "1102003",
                "transfer_sum": 1000,
            },
            {"1102003": "HDFC Bank"},
        )
        self.assertEqual(
            [(line["account"], line["debit"], line["credit"], line["line_memo"]) for line in journal["lines"]],
            [
                ("6101", 300.0, 0.0, "G/L payment"),
                ("V1", 700.0, 0.0, "Payment on account"),
                ("1102003", 0.0, 1000.0, "Bank transfer"),
            ],
        )
        self.assertEqual(journal["lines"][0]["cost_centers"][0], "DL")
        self.assertEqual(journal["lines"][2]["account_name"], "HDFC Bank")

    def test_nothing_paid_is_no_preview(self):
        self.assertIsNone(build_payment_journal({"accounts": []}, {}))
