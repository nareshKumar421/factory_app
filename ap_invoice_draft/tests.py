"""A/P invoice drafts: the checklist rules, the service, and the API.

The fixtures are real: GRPOs 27481 (SSY Containers, three POs merged), 27465
(Frystal, IGST) and 27467 (Raj Technopack) as JIVO_OIL_HANADB had them on
2026-10-08, and the rows RapidOCR read off their scans on 2026-10-09. SAP, HANA
and the OCR engine are patched at the seams ``services`` imports them from.
"""

import copy
import importlib.util
from datetime import date
from decimal import Decimal
from unittest import mock, skipUnless

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase
from rest_framework.test import APIClient

from company.models import Company, UserCompany, UserRole
from sap_client.exceptions import SAPDataError, SAPOutcomeUnknown, SAPValidationError

from . import checks
from .invoice_reader import (
    InvoiceReadError,
    fill_rate_blanks,
    gate_stamp_date,
    group_rows,
    mend_slash_dashes,
    rate_check_marks,
    read_invoice,
)
from .models import (
    APInvoiceDraft,
    CheckStatus,
    InvoiceReadStatus,
    ReviewDecision,
    SapDraftStatus,
)
from .services import APInvoiceDraftService

D = Decimal


def _line(num, item, qty, price, po_num, po_entry, open_qty, po_price=None, tax="CG+SG@5", rate="5",
          whs="BH-PM"):
    return {
        "line_num": num, "item_code": item, "description": item,
        "quantity": D(qty), "price": D(price), "line_total": D(qty) * D(price),
        "warehouse": whs, "tax_code": tax, "tax_rate": D(rate), "tax_amount": D("0"),
        "is_open": True, "from_po": True, "po_doc_entry": po_entry, "po_line": 0,
        "po_num": po_num, "po_open_qty": D(open_qty),
        "po_price": D(po_price if po_price is not None else price),
        "po_quantity": D(open_qty), "po_tax_code": tax, "po_tax_rate": D(rate),
    }


SSY_GRPO = {
    "doc_entry": 27481, "doc_num": "2026106506",
    "doc_date": date(2026, 10, 3), "tax_date": date(2026, 10, 2), "created_on": date(2026, 10, 3),
    "reference": "26-27/1979", "vendor_code": "VENDA000936", "vendor_name": "SSY CONTAINERS PVT LTD",
    "total": D("284077"), "tax_total": D("13527.496"), "branch_id": 2,
    "comments": "App: FactoryApp v2 | User: shahrukh@jivo.in | PO: 220926152, 220726123, 220926064 "
                "| Gate Entry: GE-2026-6752 | Merged: 3 POs | GATE ENTRY NO 13",
    "is_open": True, "is_cancelled": False, "is_service": False, "gst_type": "GA",
    "lines": [
        _line(0, "PM0000920", "2000", "34.45", "220926152", 14125, "2000"),
        _line(1, "PM0000825", "2334", "37.88", "220726123", 12854, "5955"),
        _line(2, "PM0000914", "3240", "34.95", "220926064", 13802, "16696"),
    ],
}

def _bill(rows, *, lines=None, rate_check=None, gate_stamp_date="", po_numbers=()):
    """A bill as ``invoice_reader.read_invoice`` returns it: one box per row
    unless the test lays the boxes out itself."""
    return {
        "pages": 1,
        "rows": [{"page": 1, "y": 40 * i, "text": text} for i, text in enumerate(rows)],
        "lines": lines if lines is not None else [
            {"page": 1, "text": text, "score": 1.0, "box": [0, 40 * i, 1600, 40 * i + 20]}
            for i, text in enumerate(rows)
        ],
        "po_numbers": list(po_numbers),
        "gate_stamp_date": gate_stamp_date,
        "rate_check": rate_check or {"found": True, "ink": 0.0, "signed": False, "text": ""},
    }


SSY_ROWS = [
    "TAX INVOICE",
    "SSY CONTAINERS PRIVATE LIMITED Invoice No. e-Way Bill No. Dated",
    "KILLAN NO 17/21/1.1 26-27/1979 372347234423 2-Oct-26",
    "GSTIN/UIN: 06AAOCS0564L1ZY",
    "1 CARTON JIVO 1 LTR X 20 PCS NEW (435X340X295) 481910 2,334 PCS 37.88 PCS 88,411.92",
    "2 CARTON JIVO 1 LTR X 20 PCS 52GM PLAIN BOX 481910 2,000 PCS 34.45 PCS 68,900.00",
    "3 CARTON JIVO 1 LTR X 20 PCS 52GM OLIVE 481910 3,240 PCS 34.95 PCS 1,13,238.00",
    "OUTPUT SGST 2.5% 2.50 % 6,763.75",
    "OUTPUT CGST 2.5% 2.50 % 6,763.75",
    "Total 7,574 PCS 2,84,077.00",
    "HSN/SAC Taxable CGST SGST/UTGST Total",
    "481910 2,70,549.92 2.50% 6,763.75 2.50% 6,763.75 13,527.50",
    "2) Interest @ 18% PA shell be charged if bill is not paid in due date",
]
SSY_INVOICE = _bill(SSY_ROWS, gate_stamp_date="3/10/26", po_numbers=["220926152", "220926089"])

FRYSTAL_GRPO = {
    **SSY_GRPO,
    "doc_entry": 27465, "reference": "NINV/26-27/1113", "tax_total": D("183898.0454"),
    "lines": [
        _line(0, "PM0000817", "200448", "3.321", "220826039", 13140, "200000", po_price="3.3217",
              tax="IGST@18", rate="18"),
        _line(1, "PM0000594", "57600", "6.18", "220826039", 13140, "122000", tax="IGST@18", rate="18"),
    ],
}

FRYSTAL_INVOICE = _bill([
    "Invoice No. : NINV/26-27/1113 Transport Name : Kunal Cargo Movers",
    "1 Pet Preform 21 Gms CTC Box 39239090 174 BOX 4,209.4080 KGS 2,00,448 PCS 3.321 PCS 6,65,687.81 18% 7,85,511.62",
    "JIVO WELLNESS PVT. LTD. Pet Preform A0.0 Gms CTC (26/20mm) Clear 89239090 96 BOX 2,304.0000 KGS 57,600 PCS "
    "6.180 PCS 3,55,968.00 18% 4,20,042.24",
    "IGST CGST Total Amount",
    # Names SGST over a value column without charging it.
    "SGST Total Ass. Value 10,21,655.81",
    "39239090 10,21,655.81 1,83,898.05 0.00 0.00 12,05,553.86 OUTPUT-IGST 1,83,898.05",
    "(A) The credit period is 30 days from date of invoice any delay In payment shell be subject to 18% P.A. interest from the",
])

RAJ_GRPO = {
    **SSY_GRPO,
    "doc_entry": 27467, "reference": "SNP-0969/26-27", "tax_total": D("18322.2"),
    "lines": [_line(0, "PM0000916", "351", "290", "220926043", 13723, "350", tax="CG+SG@18", rate="18")],
}

GRAPHIC_GRPO = {
    **RAJ_GRPO,
    "reference": "368", "tax_total": D("151.2"),
    "lines": [
        _line(0, "PM0000001", "1500", "0.28", "221026001", 14000, "1500", tax="CG+SG@18", rate="18"),
        _line(1, "PM0000002", "1500", "0.28", "221026001", 14000, "1500", tax="CG+SG@18", rate="18"),
    ],
}

#: GRAPHIC-368's item rows, handwritten, as read: the first row's qty 1500 came
#: back "150", and "118h" is the HSN code 4811.
GRAPHIC_ITEM_ROWS = [
    "TiKki Bomude CSD 118h 150 0.28/- 420/-",
    "Tikki Borwade CSD 41 1500 0.28/- 420/-",
]


def _status(finding):
    return finding.status


# ---------------------------------------------------------------------------
# The rules
# ---------------------------------------------------------------------------

class InvoiceNumberCheckTests(SimpleTestCase):
    def test_the_grpo_reference_is_printed_on_the_bill(self):
        finding = checks.check_invoice_number(SSY_GRPO, SSY_INVOICE)
        self.assertEqual(finding.status, CheckStatus.PASS)
        self.assertIn("26-27/1979", finding.facts["found_in"])

    def test_a_dropped_slash_still_matches(self):
        bill = _bill(["Invoice No. : NINV 26-27 1113"])
        self.assertEqual(_status(checks.check_invoice_number(FRYSTAL_GRPO, bill)), CheckStatus.PASS)

    def test_another_number_by_the_label_fails_and_says_which(self):
        # SSY's layout: the number sits under its label, the vendor's address to the left.
        lines = [
            {"page": 1, "text": "Invoice No.", "score": 1, "box": [963, 361, 1080, 381]},
            {"page": 1, "text": "KILLAN NO 17/21/1.1", "score": 1, "box": [90, 390, 400, 410]},
            {"page": 1, "text": "26-27/1981", "score": 1, "box": [965, 390, 1080, 410]},
            {"page": 1, "text": "2-Oct-26", "score": 1, "box": [1242, 389, 1330, 409]},
        ]
        bill = _bill(["Invoice No.", "KILLAN NO 17/21/1.1 26-27/1981 2-Oct-26"], lines=lines)
        finding = checks.check_invoice_number(SSY_GRPO, bill)
        self.assertEqual(finding.status, CheckStatus.FAIL)
        self.assertIn("reads 26-27/1981", finding.detail)

    def test_the_number_beside_its_label(self):
        lines = [
            {"page": 1, "text": "Invoice No.", "score": 1, "box": [82, 343, 190, 363]},
            {"page": 1, "text": ": SNP-0970/26-27", "score": 1, "box": [180, 344, 400, 364]},
        ]
        bill = _bill(["Invoice No. : SNP-0970/26-27"], lines=lines)
        finding = checks.check_invoice_number(RAJ_GRPO, bill)
        self.assertEqual(finding.status, CheckStatus.FAIL)
        self.assertIn("reads SNP-0970/26-27", finding.detail)

    def test_nothing_found_needs_a_look(self):
        bill = _bill(["TAX INVOICE", "Total 7,574 PCS"])
        self.assertEqual(_status(checks.check_invoice_number(SSY_GRPO, bill)), CheckStatus.REVIEW)

    def test_waits_for_the_bill(self):
        self.assertEqual(_status(checks.check_invoice_number(SSY_GRPO, None)), CheckStatus.UNKNOWN)

    def test_dates_are_not_bill_numbers_but_26_27_is(self):
        self.assertTrue(checks.DATE_LIKE.match("2-Oct-26"))
        self.assertTrue(checks.DATE_LIKE.match("30.09.26"))
        self.assertFalse(checks.DATE_LIKE.match("26-27/1979"))


class WarehouseCheckTests(SimpleTestCase):
    def test_every_line_in_bh_pm(self):
        self.assertEqual(_status(checks.check_warehouse(SSY_GRPO)), CheckStatus.PASS)

    def test_one_line_elsewhere_fails(self):
        grpo = copy.deepcopy(SSY_GRPO)
        grpo["lines"][1]["warehouse"] = "BH-PC"
        finding = checks.check_warehouse(grpo)
        self.assertEqual(finding.status, CheckStatus.FAIL)
        self.assertIn("line 2 (PM0000825) into BH-PC", finding.detail)


class GSTCheckTests(SimpleTestCase):
    def test_cgst_sgst_at_the_po_rate(self):
        finding = checks.check_gst(SSY_GRPO, SSY_INVOICE)
        self.assertEqual(finding.status, CheckStatus.PASS)
        self.assertIn("CGST+SGST at 2.5%, ₹13,527.50", finding.detail)

    def test_igst_at_the_po_rate_past_an_sgst_heading_and_the_interest_clause(self):
        finding = checks.check_gst(FRYSTAL_GRPO, FRYSTAL_INVOICE)
        self.assertEqual(finding.status, CheckStatus.PASS)
        self.assertIn("IGST at 18%, ₹1,83,898.05", finding.detail)

    def test_rate_written_with_an_at_sign(self):
        bill = _bill(["Add : CGST @ 9.00 % 9,161.00", "Add : SGST @ 9.00 % 9,161.00", "18,322.00"])
        self.assertEqual(_status(checks.check_gst(RAJ_GRPO, bill)), CheckStatus.PASS)

    def test_igst_bill_on_a_cgst_po_fails(self):
        bill = _bill(["OUTPUT IGST 5% 13,527.50"])
        finding = checks.check_gst(SSY_GRPO, bill)
        self.assertEqual(finding.status, CheckStatus.FAIL)
        self.assertIn("charges IGST", finding.detail)

    def test_cgst_bill_on_an_igst_po_fails(self):
        bill = _bill(["CGST 9% 91,949.02", "SGST 9% 91,949.02"])
        self.assertEqual(_status(checks.check_gst(FRYSTAL_GRPO, bill)), CheckStatus.FAIL)

    def test_another_rate_fails(self):
        bill = _bill(["OUTPUT SGST 6% 6,763.75", "OUTPUT CGST 6% 6,763.75"])
        finding = checks.check_gst(SSY_GRPO, bill)
        self.assertEqual(finding.status, CheckStatus.FAIL)
        self.assertIn("GST at 6%", finding.detail)
        self.assertIn("CG+SG@5", finding.detail)

    def test_the_interest_clause_is_not_a_gst_rate(self):
        bill = _bill(["Interest @ 18% p.a. will be charged", "OUTPUT IGST 18% 1,83,898.05"])
        self.assertEqual(_status(checks.check_gst(FRYSTAL_GRPO, bill)), CheckStatus.PASS)

    def test_same_rate_but_the_grpos_tax_not_printed_needs_a_look(self):
        bill = _bill(["OUTPUT SGST 2.5% 7,000.00", "OUTPUT CGST 2.5% 7,000.00"])
        finding = checks.check_gst(SSY_GRPO, bill)
        self.assertEqual(finding.status, CheckStatus.REVIEW)
        self.assertIn("₹13,527.50 is not printed", finding.detail)

    def test_one_half_is_enough_where_the_bill_labels_cgst_and_sgst(self):
        # GRAPHIC-368 by hand: the SGST half read, the CGST half did not.
        bill = _bill(["+SGST@ 9 % 75.6/-", "+CGST@ 9 % K61"])
        finding = checks.check_gst(GRAPHIC_GRPO, bill)
        self.assertEqual(finding.status, CheckStatus.PASS)
        self.assertIn("CGST+SGST at 9%, ₹151.20", finding.detail)

    def test_one_half_alone_does_not_say_the_bill_is_cgst_sgst(self):
        bill = _bill(["Tax 9,161.00", "18%"])
        finding = checks.check_gst(RAJ_GRPO, bill)
        self.assertEqual(finding.status, CheckStatus.REVIEW)

    def test_nothing_readable_needs_a_look(self):
        bill = _bill(["TAX INVOICE", "Total 7,574 PCS"])
        self.assertEqual(_status(checks.check_gst(SSY_GRPO, bill)), CheckStatus.REVIEW)

    def test_waits_for_the_bill(self):
        self.assertEqual(_status(checks.check_gst(SSY_GRPO, None)), CheckStatus.UNKNOWN)


class GRPOTimingCheckTests(SimpleTestCase):
    def test_same_day_as_the_gate(self):
        arrival = {"at": date(2026, 10, 3), "source": "gate entry GE-2026-6752"}
        finding = checks.check_grpo_timing(SSY_GRPO, None, arrival)
        self.assertEqual(finding.status, CheckStatus.PASS)
        self.assertEqual(finding.facts["days"], 0)

    def test_three_days_is_allowed_four_is_not(self):
        self.assertEqual(
            _status(checks.check_grpo_timing(SSY_GRPO, None, {"at": date(2026, 9, 30)})), CheckStatus.PASS,
        )
        self.assertEqual(
            _status(checks.check_grpo_timing(SSY_GRPO, None, {"at": date(2026, 9, 29)})), CheckStatus.FAIL,
        )

    def test_measured_from_when_the_grpo_was_made_not_its_typed_date(self):
        grpo = {**SSY_GRPO, "doc_date": date(2026, 10, 1), "created_on": date(2026, 10, 8)}
        self.assertEqual(
            _status(checks.check_grpo_timing(grpo, None, {"at": date(2026, 10, 1)})), CheckStatus.FAIL,
        )

    def test_arrival_after_the_grpo_needs_a_look(self):
        finding = checks.check_grpo_timing(SSY_GRPO, None, {"at": date(2026, 10, 5)})
        self.assertEqual(finding.status, CheckStatus.REVIEW)
        self.assertIn("One of the dates is wrong", finding.detail)

    def test_falls_back_to_the_gate_stamp_on_the_bill(self):
        finding = checks.check_grpo_timing(SSY_GRPO, SSY_INVOICE, None)
        self.assertEqual(finding.status, CheckStatus.PASS)
        self.assertEqual(finding.facts["arrived_on"], "2026-10-03")
        self.assertIn("gate stamp", finding.facts["arrival_source"])

    def test_nothing_to_go_on(self):
        self.assertEqual(_status(checks.check_grpo_timing(SSY_GRPO, None, None)), CheckStatus.UNKNOWN)
        invoice = {**SSY_INVOICE, "gate_stamp_date": ""}
        self.assertEqual(_status(checks.check_grpo_timing(SSY_GRPO, invoice, None)), CheckStatus.REVIEW)

    def test_stamp_dates_are_day_first(self):
        self.assertEqual(checks.parse_stamp_date("3/10/26"), date(2026, 10, 3))
        self.assertEqual(checks.parse_stamp_date("01-10-2026"), date(2026, 10, 1))
        self.assertEqual(checks.parse_stamp_date("1 . 10 . 26"), date(2026, 10, 1))
        self.assertIsNone(checks.parse_stamp_date("31/02/26"))
        self.assertIsNone(checks.parse_stamp_date(None))


class RateCheckSignatureTests(SimpleTestCase):
    def _with(self, **marks):
        return {**SSY_INVOICE, "rate_check": {"found": True, "ink": 0.0, "signed": False, "text": "", **marks}}

    def test_a_blank_rate_check_line_fails(self):
        # All four bills downloaded on 2026-10-08 were like this.
        self.assertEqual(_status(checks.check_rate_check_signature(SSY_INVOICE)), CheckStatus.FAIL)

    def test_signed_and_named_kulbeer_passes(self):
        invoice = self._with(ink=1.1, signed=True, text="Kulbeer")
        self.assertEqual(_status(checks.check_rate_check_signature(invoice)), CheckStatus.PASS)

    def test_a_signature_needs_a_look_to_say_whose(self):
        invoice = self._with(ink=1.1, signed=True)
        finding = checks.check_rate_check_signature(invoice)
        self.assertEqual(finding.status, CheckStatus.REVIEW)
        self.assertIn("Confirm it is Kulbeer's", finding.detail)

    def test_faint_marks_need_a_look(self):
        invoice = self._with(ink=0.4, signed=None)
        self.assertEqual(_status(checks.check_rate_check_signature(invoice)), CheckStatus.REVIEW)

    def test_no_stamp_found_needs_a_look(self):
        invoice = {**SSY_INVOICE, "rate_check": {"found": False}}
        self.assertEqual(_status(checks.check_rate_check_signature(invoice)), CheckStatus.REVIEW)


class POApproverCheckTests(SimpleTestCase):
    def test_every_po_printed_for_gagandeep(self):
        approvals = [
            {"po_num": "220926152", "is_approved": True, "approver": "GAGANDEEP SINGH"},
            {"po_num": "220726123", "is_approved": True, "approver": "Gagandeep Singh"},
        ]
        self.assertEqual(_status(checks.check_po_approver(approvals)), CheckStatus.PASS)

    def test_oils_shared_signatory_passes(self):
        # Live JIVO_OIL PO print setting since 2026-10-06: typed, "Vishal/Gagandeep Singh".
        approvals = [{"po_num": "220926043", "is_approved": True, "approver": "Vishal/Gagandeep Singh"}]
        finding = checks.check_po_approver(approvals)
        self.assertEqual(finding.status, CheckStatus.PASS)
        self.assertIn("the print names Vishal/Gagandeep Singh", finding.detail)

    def test_someone_else_fails(self):
        approvals = [{"po_num": "220926152", "is_approved": True, "approver": "VISHAL TYAGI"}]
        finding = checks.check_po_approver(approvals)
        self.assertEqual(finding.status, CheckStatus.FAIL)
        self.assertIn("VISHAL TYAGI", finding.detail)

    def test_an_unapproved_po_fails(self):
        approvals = [{"po_num": "220926152", "is_approved": False, "approver": "Gagandeep Singh"}]
        self.assertEqual(_status(checks.check_po_approver(approvals)), CheckStatus.FAIL)

    def test_unread_is_unknown(self):
        self.assertEqual(_status(checks.check_po_approver(None)), CheckStatus.UNKNOWN)


class PORateCheckTests(SimpleTestCase):
    def test_equal_to_the_paisa_passes(self):
        # PO 3.3217, bill and GRPO 3.321.
        self.assertEqual(_status(checks.check_po_rate(FRYSTAL_GRPO)), CheckStatus.PASS)

    def test_a_different_rate_fails(self):
        grpo = copy.deepcopy(SSY_GRPO)
        grpo["lines"][0]["po_price"] = D("33.95")
        finding = checks.check_po_rate(grpo)
        self.assertEqual(finding.status, CheckStatus.FAIL)
        self.assertIn("GRPO 34.45, PO 33.95", finding.detail)


class InvoiceQtyCheckTests(SimpleTestCase):
    def test_each_line_found_on_the_bill_by_its_rate(self):
        finding = checks.check_invoice_qty(SSY_GRPO, SSY_INVOICE)
        self.assertEqual(finding.status, CheckStatus.PASS)
        self.assertEqual([r["invoice_qty"] for r in finding.facts["lines"]], ["2000", "2334", "3240"])

    def test_the_quantity_among_boxes_weight_and_hsn(self):
        # Frystal prints boxes, KGS and PCS on the row; only PCS x rate is the amount.
        finding = checks.check_invoice_qty(FRYSTAL_GRPO, FRYSTAL_INVOICE)
        self.assertEqual(finding.status, CheckStatus.PASS)
        self.assertEqual([r["invoice_qty"] for r in finding.facts["lines"]], ["200448", "57600"])

    def test_decimals_on_the_bill(self):
        bill = _bill(["1. 20LTR_MSR_NRB_IVORY_1300 39231090 19BAG X 18+9PCS 351.00 PCS. 290.00 1,01,790.00"])
        self.assertEqual(_status(checks.check_invoice_qty(RAJ_GRPO, bill)), CheckStatus.PASS)

    def test_a_bill_for_more_than_was_received_fails(self):
        grpo = copy.deepcopy(SSY_GRPO)
        grpo["lines"][1]["quantity"] = D("2300")
        finding = checks.check_invoice_qty(grpo, SSY_INVOICE)
        self.assertEqual(finding.status, CheckStatus.FAIL)
        self.assertIn("line 2 (PM0000825) at 37.88: GRPO 2300, bill 2334", finding.detail)

    def test_po_lines_merged_at_one_rate_are_held_to_the_bill_as_one(self):
        grpo = copy.deepcopy(SSY_GRPO)
        grpo["lines"] = [
            _line(0, "PM0000920", "1500", "34.45", "220926152", 14125, "1500"),
            _line(1, "PM0000920", "500", "34.45", "220926153", 14126, "500"),
        ]
        bill = _bill(["2 CARTON JIVO 1 LTR X 20 PCS 52GM PLAIN BOX 481910 2,000 PCS 34.45 PCS 68,900.00"])
        finding = checks.check_invoice_qty(grpo, bill)
        self.assertEqual(finding.status, CheckStatus.PASS)
        [row] = finding.facts["lines"]
        self.assertEqual((row["line"], row["grpo_qty"], row["invoice_qty"]), ("1, 2", "2000", "2000"))

    def test_a_rate_printed_to_the_paisa(self):
        # Worked at 3.3217 a piece, printed as 3.32.
        grpo = copy.deepcopy(FRYSTAL_GRPO)
        grpo["lines"] = [grpo["lines"][0] | {"price": D("3.3217")}]
        bill = _bill(["Pet Preform 21 Gms 2,00,448 PCS 3.32 PCS 6,65,828.12"])
        self.assertEqual(_status(checks.check_invoice_qty(grpo, bill)), CheckStatus.PASS)

    def test_a_misread_handwritten_qty_is_worked_back_from_the_amount(self):
        # 420 at 0.28 is the GRPO's 1500, though the qty read "150".
        finding = checks.check_invoice_qty(GRAPHIC_GRPO, _bill(GRAPHIC_ITEM_ROWS))
        self.assertEqual(finding.status, CheckStatus.PASS)
        self.assertEqual([r["invoice_qty"] for r in finding.facts["lines"]], ["1500", "1500"])

    def test_the_amount_holds_the_bill_to_what_it_charges(self):
        grpo = copy.deepcopy(GRAPHIC_GRPO)
        grpo["lines"][0]["quantity"] = D("1400")
        finding = checks.check_invoice_qty(grpo, _bill(GRAPHIC_ITEM_ROWS))
        # Both rows charge 420, the price of 1500, against 1400 received.
        self.assertEqual(finding.status, CheckStatus.FAIL)
        self.assertIn("GRPO 2900, bill 3000", finding.detail)

    def test_a_row_at_the_rate_that_did_not_read_needs_a_look_not_a_fail(self):
        bill = _bill(["TiKki Bomude CSD 118h 150 0.28/- —1166", GRAPHIC_ITEM_ROWS[1]])
        finding = checks.check_invoice_qty(GRAPHIC_GRPO, bill)
        self.assertEqual(finding.status, CheckStatus.REVIEW)
        self.assertIn("GRPO 3000, bill 1500", finding.detail)

    def test_no_line_at_the_rate_needs_a_look(self):
        bill = _bill(["TAX INVOICE", "Total 7,574 PCS 2,84,077.00"])
        finding = checks.check_invoice_qty(SSY_GRPO, bill)
        self.assertEqual(finding.status, CheckStatus.REVIEW)
        self.assertIn("line 1 (PM0000920) at 34.45", finding.detail)

    def test_waits_for_the_bill(self):
        self.assertEqual(_status(checks.check_invoice_qty(SSY_GRPO, None)), CheckStatus.UNKNOWN)


class OverReceiptCheckTests(SimpleTestCase):
    def test_one_over_on_a_350_line_is_within_ten_percent(self):
        self.assertEqual(_status(checks.check_over_receipt(RAJ_GRPO, False)), CheckStatus.PASS)

    def test_over_110_percent_of_what_was_open_fails(self):
        grpo = copy.deepcopy(RAJ_GRPO)
        grpo["lines"][0]["quantity"] = D("386")
        finding = checks.check_over_receipt(grpo, False)
        self.assertEqual(finding.status, CheckStatus.FAIL)
        self.assertIn("386 received, 385 allowed", finding.detail)

    def test_an_exempt_vendor_passes(self):
        grpo = copy.deepcopy(RAJ_GRPO)
        grpo["lines"][0]["quantity"] = D("500")
        self.assertEqual(_status(checks.check_over_receipt(grpo, True)), CheckStatus.PASS)


class QCCheckTests(SimpleTestCase):
    def test_accepted(self):
        items = [{"po_num": "1", "item_code": "PM1", "status": "ACCEPTED", "report_no": "QC-1"}]
        self.assertEqual(_status(checks.check_qc(items)), CheckStatus.PASS)

    def test_on_hold_fails(self):
        items = [
            {"po_num": "1", "item_code": "PM1", "status": "ACCEPTED", "report_no": "QC-1"},
            {"po_num": "1", "item_code": "PM2", "status": "HOLD", "report_no": "QC-2"},
        ]
        self.assertEqual(_status(checks.check_qc(items)), CheckStatus.FAIL)

    def test_no_inspection_on_record_needs_a_look(self):
        items = [{"po_num": "1", "item_code": "PM1", "status": "NO_ARRIVAL_SLIP", "report_no": ""}]
        self.assertEqual(_status(checks.check_qc(items)), CheckStatus.REVIEW)

    def test_not_received_through_the_app_needs_a_look(self):
        self.assertEqual(_status(checks.check_qc(None)), CheckStatus.REVIEW)


class RunChecksTests(SimpleTestCase):
    def test_ten_findings_in_order(self):
        findings = checks.run_checks(SSY_GRPO, SSY_INVOICE, {})
        self.assertEqual([f.key for f in findings], checks.ORDER)
        self.assertEqual(len(findings), 10)


# ---------------------------------------------------------------------------
# The service
# ---------------------------------------------------------------------------

def _pdf(name="SSY-1979.pdf"):
    return SimpleUploadedFile(name, b"%PDF-1.7 test bill", content_type="application/pdf")


class FakeReader:
    """GRPOReader with one GRPO and whatever open A/P drafts the test sets."""

    def __init__(self, grpo=SSY_GRPO, drafts=None):
        self.grpo_data = copy.deepcopy(grpo)
        self.drafts = drafts or {}

    def __call__(self, company_code):
        return self

    def grpo(self, doc_entry):
        return copy.deepcopy(self.grpo_data) if doc_entry == self.grpo_data["doc_entry"] else None

    def open_ap_drafts(self, entries):
        return {e: self.drafts[e] for e in entries if e in self.drafts}

    def open_grpos(self, search="", doc_entry=None):
        g = self.grpo_data
        if doc_entry is not None and doc_entry != g["doc_entry"]:
            return []
        return [{
            "doc_entry": g["doc_entry"], "doc_num": g["doc_num"], "doc_date": g["doc_date"],
            "reference": g["reference"], "vendor_code": g["vendor_code"], "vendor_name": g["vendor_name"],
            "total": g["total"], "comments": "", "warehouses": ["BH-PM"],
            "sap_draft_entries": self.drafts.get(g["doc_entry"], []),
        }]

    def ap_invoice_states(self, entries):
        states = getattr(self, "states", {
            27481: {"grpo_open": True, "grpo_cancelled": False, "invoices": [], "draft_entries": []},
        })
        return {e: copy.deepcopy(states[e]) for e in entries if e in states}

    def ap_invoice_series(self, posting_date, branch_id, gst_type):
        self.series_asked = (posting_date, branch_id, gst_type)
        return None if getattr(self, "no_series", False) else (3686, "HR_G1026")


class FakeSAP:
    def __init__(self):
        self.payloads = []
        self.create_error = None
        self.upload_error = None
        self.approver = "Gagandeep Singh"

    def __call__(self, company_code):
        return self

    def upload_attachment(self, file_path, filename):
        if self.upload_error:
            raise self.upload_error
        return {"AbsoluteEntry": 181900}

    def create_ap_invoice_draft(self, payload):
        self.payloads.append(payload)
        if self.create_error:
            raise self.create_error
        return {"DocEntry": 59001, "DocNum": 59001}

    def po_print(self, doc_entry):
        return {"doc_num": 0, "approval": {"is_approved": True, "approver": self.approver}}


class ServiceTestCase(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        self.user = get_user_model().objects.create(email="store@jivo.in", full_name="Store")
        self.reader = FakeReader()
        self.sap = FakeSAP()
        patches = [
            mock.patch("ap_invoice_draft.services.GRPOReader", self.reader),
            mock.patch("ap_invoice_draft.services.SAPClient", self.sap),
            mock.patch("ap_invoice_draft.services.is_over_receipt_exempt", return_value=False),
        ]
        self.read = mock.patch(
            "ap_invoice_draft.services.read_invoice", return_value=(SSY_INVOICE, "RapidOCR test"),
        ).start()
        self.addCleanup(mock.patch.stopall)
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.service = APInvoiceDraftService(self.company)

    def _checks(self, entry):
        return {c.key: c for c in entry.checks.all()}


class CreateTests(ServiceTestCase):
    def test_makes_the_sap_draft_as_accounts_copy_would(self):
        entry = self.service.create(27481, _pdf(), self.user)

        self.assertEqual(entry.sap_status, SapDraftStatus.CREATED)
        self.assertEqual(entry.sap_draft_entry, 59001)
        self.assertFalse(entry.sap_draft_adopted)
        self.assertEqual(entry.grpo_reference, "26-27/1979")
        self.assertTrue(entry.entry_no.startswith("APD-"))
        payload = self.sap.payloads[0]
        self.assertEqual(payload["CardCode"], "VENDA000936")
        self.assertEqual(payload["NumAtCard"], "26-27/1979")
        self.assertEqual(payload["DocDate"], "2026-10-03")
        self.assertEqual(payload["TaxDate"], "2026-10-02")
        self.assertEqual(payload["BPL_IDAssignedToInvoice"], 2)
        self.assertEqual(payload["Series"], 3686)
        self.assertEqual(payload["GSTTransactionType"], "gsttrantyp_GSTTaxInvoice")
        self.assertEqual(self.reader.series_asked, (date(2026, 10, 3), 2, "GA"))
        self.assertEqual(payload["AttachmentEntry"], 181900)
        self.assertEqual(
            payload["DocumentLines"],
            [{"BaseType": 20, "BaseEntry": 27481, "BaseLine": n} for n in range(3)],
        )
        self.assertIn("Based On Goods Receipt PO 2026106506.", payload["Comments"])

    def test_reads_the_bill_and_runs_every_check_in_the_same_go(self):
        entry = self.service.create(27481, _pdf(), self.user)
        self.assertEqual(entry.invoice_read_status, InvoiceReadStatus.READ)
        self.assertEqual(entry.invoice_read_model, "RapidOCR test")
        found = self._checks(entry)
        self.assertEqual(list(found), checks.ORDER)
        self.assertEqual(found["invoice_number"].status, CheckStatus.PASS)
        self.assertEqual(found["warehouse"].status, CheckStatus.PASS)
        self.assertEqual(found["gst"].status, CheckStatus.PASS)
        self.assertEqual(found["grpo_timing"].status, CheckStatus.PASS)
        self.assertEqual(found["rate_check_signature"].status, CheckStatus.FAIL)
        self.assertEqual(found["po_approver"].status, CheckStatus.PASS)
        self.assertEqual(found["po_rate"].status, CheckStatus.PASS)
        self.assertEqual(found["over_receipt"].status, CheckStatus.PASS)
        self.assertEqual(found["qc"].status, CheckStatus.REVIEW)

    def test_an_unreadable_bill_keeps_the_entry_and_its_draft(self):
        self.read.side_effect = InvoiceReadError("No text could be read off the bill.")
        entry = self.service.create(27481, _pdf(), self.user)
        self.assertEqual(entry.sap_status, SapDraftStatus.CREATED)
        self.assertEqual(entry.invoice_read_status, InvoiceReadStatus.FAILED)
        self.assertEqual(self._checks(entry)["invoice_number"].status, CheckStatus.UNKNOWN)

    def test_whatever_the_ocr_engine_throws_keeps_the_entry(self):
        self.read.side_effect = RuntimeError("onnxruntime: bad alloc")
        entry = self.service.create(27481, _pdf(), self.user)
        self.assertEqual(entry.invoice_read_status, InvoiceReadStatus.FAILED)
        self.assertIn("could not be read", entry.invoice_read_error)

    def test_links_the_draft_sap_already_has(self):
        # On 2026-10-08 accounts had already made draft 58620 for this GRPO by hand.
        self.reader.drafts = {27481: [58620]}
        entry = self.service.create(27481, _pdf(), self.user)
        self.assertEqual(entry.sap_status, SapDraftStatus.CREATED)
        self.assertEqual(entry.sap_draft_entry, 58620)
        self.assertTrue(entry.sap_draft_adopted)
        self.assertEqual(self.sap.payloads, [])

    def test_a_refusal_keeps_the_entry_with_sap_reason(self):
        self.sap.create_error = SAPValidationError("Period is locked")
        entry = self.service.create(27481, _pdf(), self.user)
        self.assertEqual(entry.sap_status, SapDraftStatus.FAILED)
        self.assertIn("Period is locked", entry.sap_error)
        self.assertIsNone(entry.sap_draft_entry)
        self.assertEqual(APInvoiceDraft.objects.count(), 1)

    def test_sap_v2_refusals_read_as_a_sentence(self):
        self.sap.create_error = SAPValidationError(
            '{\n "error" : {\n "code" : "-4002", "details" : [], "message" : '
            '"To generate this document, first define the numbering series in the Administration module"\n }\n}'
        )
        entry = self.service.create(27481, _pdf(), self.user)
        self.assertEqual(
            entry.sap_error,
            "SAP refused the draft: To generate this document, first define the numbering "
            "series in the Administration module",
        )

    def test_a_lost_answer_is_found_on_retry_not_made_twice(self):
        self.sap.create_error = SAPOutcomeUnknown("timeout")
        entry = self.service.create(27481, _pdf(), self.user)
        self.assertEqual(entry.sap_status, SapDraftStatus.FAILED)
        self.assertIn("looks for the draft", entry.sap_error)

        self.reader.drafts = {27481: [59001]}  # SAP had taken it
        self.service.send_to_sap(entry, self.user)
        entry.refresh_from_db()
        self.assertEqual(entry.sap_status, SapDraftStatus.CREATED)
        self.assertEqual(entry.sap_draft_entry, 59001)
        self.assertEqual(len(self.sap.payloads), 1)

    def test_no_open_series_is_said_before_sap_is_asked(self):
        self.reader.no_series = True
        entry = self.service.create(27481, _pdf(), self.user)
        self.assertEqual(entry.sap_status, SapDraftStatus.FAILED)
        self.assertIn("no open A/P invoice series for branch 2", entry.sap_error)
        self.assertEqual(self.sap.payloads, [])

    def test_an_attachment_failure_still_makes_the_draft(self):
        self.sap.upload_error = SAPDataError("Attachments folder not defined")
        entry = self.service.create(27481, _pdf(), self.user)
        self.assertEqual(entry.sap_status, SapDraftStatus.CREATED)
        self.assertNotIn("AttachmentEntry", self.sap.payloads[0])
        self.assertIn("Attachments folder", entry.sap_attachment_error)

    def test_one_entry_per_grpo(self):
        self.service.create(27481, _pdf(), self.user)
        with self.assertRaisesMessage(ValueError, "already has entry"):
            self.service.create(27481, _pdf(), self.user)

    def test_a_simultaneous_second_entry_is_refused_not_a_500(self):
        from django.db import IntegrityError

        with mock.patch.object(APInvoiceDraftService, "_new_entry", side_effect=IntegrityError("dup")):
            with self.assertRaisesMessage(ValueError, "already has an entry"):
                self.service.create(27481, _pdf(), self.user)

    def test_refuses_what_cannot_be_invoiced(self):
        with self.assertRaisesMessage(ValueError, "no GRPO"):
            self.service.create(1, _pdf(), self.user)
        self.reader.grpo_data["is_open"] = False
        with self.assertRaisesMessage(ValueError, "already invoiced"):
            self.service.create(27481, _pdf(), self.user)
        self.reader.grpo_data.update({"is_open": True, "is_service": True})
        with self.assertRaisesMessage(ValueError, "service GRPO"):
            self.service.create(27481, _pdf(), self.user)
        with self.assertRaisesMessage(ValueError, "PDF or a photo"):
            self.service.create(27481, _pdf("bill.docx"), self.user)
        self.assertEqual(APInvoiceDraft.objects.count(), 0)


class ReadInvoiceTests(ServiceTestCase):
    def test_reading_again_replaces_what_was_read_and_rechecks(self):
        entry = self.service.create(27481, _pdf(), self.user)
        signed = {**SSY_INVOICE, "rate_check": {"found": True, "ink": 1.2, "signed": True, "text": "Kulbeer"}}
        self.read.return_value = (signed, "RapidOCR test 2")
        self.service.read_invoice(entry)
        entry.refresh_from_db()
        self.assertEqual(entry.invoice_read_status, InvoiceReadStatus.READ)
        self.assertEqual(entry.invoice_read_model, "RapidOCR test 2")
        self.assertEqual(self._checks(entry)["rate_check_signature"].status, CheckStatus.PASS)

    def test_a_failed_read_says_why(self):
        entry = self.service.create(27481, _pdf(), self.user)
        self.read.side_effect = InvoiceReadError("The PDF could not be opened.")
        self.service.read_invoice(entry)
        entry.refresh_from_db()
        self.assertEqual(entry.invoice_read_status, InvoiceReadStatus.FAILED)
        self.assertEqual(entry.invoice_read_error, "The PDF could not be opened.")

    def test_a_read_under_way_is_not_started_twice(self):
        entry = self.service.create(27481, _pdf(), self.user)
        APInvoiceDraft.objects.filter(pk=entry.pk).update(invoice_read_status=InvoiceReadStatus.READING)
        with self.assertRaisesMessage(ValueError, "already being read"):
            self.service.read_invoice(entry)


class ReviewTests(ServiceTestCase):
    def test_a_decision_outranks_the_finding_and_outlives_a_rerun(self):
        entry = self.service.create(27481, _pdf(), self.user)
        check = self.service.review_check(entry, "qc", ReviewDecision.OK, "QC register p. 14", self.user)
        self.assertEqual(check.effective_status, CheckStatus.PASS)

        self.service.run_checks(entry)
        check.refresh_from_db()
        self.assertEqual(check.status, CheckStatus.REVIEW)
        self.assertEqual(check.review_decision, ReviewDecision.OK)
        self.assertEqual(check.effective_status, CheckStatus.PASS)

    def test_not_ok_needs_a_reason_and_blank_clears(self):
        entry = self.service.create(27481, _pdf(), self.user)
        with self.assertRaisesMessage(ValueError, "Say why"):
            self.service.review_check(entry, "qc", ReviewDecision.NOT_OK, "", self.user)
        self.service.review_check(entry, "qc", ReviewDecision.NOT_OK, "On hold", self.user)
        check = self.service.review_check(entry, "qc", "", "", self.user)
        self.assertEqual(check.review_decision, "")
        self.assertIsNone(check.reviewed_by)


class GRPOAPStatusTests(ServiceTestCase):
    def _state(self, open_=True, cancelled=False, invoices=(), drafts=()):
        return {"grpo_open": open_, "grpo_cancelled": cancelled,
                "invoices": list(invoices), "draft_entries": list(drafts)}

    def test_each_way_a_grpos_a_p_invoice_can_stand(self):
        invoice = {"doc_entry": 52342, "doc_num": "626094326", "doc_date": date(2026, 10, 6)}
        self.reader.states = {
            1: self._state(),
            2: self._state(drafts=[58620]),
            3: self._state(open_=False, invoices=[invoice]),
            4: self._state(open_=True, invoices=[invoice]),
            5: self._state(open_=False),
            6: self._state(cancelled=True),
        }
        status = self.service.grpo_ap_status([1, 2, 3, 4, 5, 6, 7])
        self.assertEqual(
            {k: v["status"] for k, v in status.items()},
            {1: "NONE", 2: "DRAFT", 3: "POSTED", 4: "PARTIAL", 5: "CLOSED", 6: "CLOSED"},
        )
        self.assertEqual(status[3]["invoices"][0]["doc_num"], "626094326")
        self.assertNotIn(7, status)  # SAP has no such GRPO

    def test_the_apps_own_entry_is_named(self):
        entry = self.service.create(27481, _pdf(), self.user)
        status = self.service.grpo_ap_status([27481])[27481]
        self.assertEqual(status["status"], "DRAFT")
        self.assertEqual(status["entry"]["entry_no"], entry.entry_no)
        self.assertEqual(status["entry"]["sap_draft_entry"], 59001)


# ---------------------------------------------------------------------------
# The API
# ---------------------------------------------------------------------------

API = "/api/v1/ap-invoice-drafts/"


class APITests(ServiceTestCase):
    def setUp(self):
        super().setUp()
        role = UserRole.objects.create(name="Warehouse")
        self.maker = get_user_model().objects.create(email="maker@jivo.in", full_name="Maker")
        self.auditor = get_user_model().objects.create(email="audit@jivo.in", full_name="Auditor")
        self.nobody = get_user_model().objects.create(email="nobody@jivo.in", full_name="Nobody")
        for user in (self.maker, self.auditor, self.nobody):
            UserCompany.objects.create(user=user, company=self.company, role=role, is_active=True)

        def grant(user, *codenames):
            user.user_permissions.add(*Permission.objects.filter(
                content_type__app_label="ap_invoice_draft", codename__in=codenames,
            ))

        grant(self.maker, "can_view_ap_invoice_draft", "can_create_ap_invoice_draft")
        grant(self.auditor, "can_view_ap_invoice_draft", "can_review_ap_invoice_draft")
        self.client = APIClient()

    def _as(self, user):
        self.client.force_authenticate(user)
        return self.client

    def _create(self):
        return self._as(self.maker).post(
            API, {"grpo_doc_entry": 27481, "invoice_file": _pdf()},
            format="multipart", HTTP_COMPANY_CODE="JIVO_OIL",
        )

    def test_maker_creates_and_gets_the_checklist(self):
        response = self._create()
        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()
        self.assertEqual(body["sap_status"], "CREATED")
        self.assertEqual(len(body["checks"]), 10)
        self.assertTrue(body["invoice_file_url"].startswith("http://testserver/"))

        listed = self._as(self.maker).get(API, HTTP_COMPANY_CODE="JIVO_OIL").json()
        self.assertEqual([row["entry_no"] for row in listed], [body["entry_no"]])

    def test_the_picker_lists_open_grpos(self):
        self.reader.drafts = {27481: [58620]}
        response = self._as(self.maker).get(f"{API}grpos/?search=SSY", HTTP_COMPANY_CODE="JIVO_OIL")
        self.assertEqual(response.status_code, 200, response.content)
        [row] = response.json()
        self.assertEqual((row["doc_num"], row["reference"]), ("2026106506", "26-27/1979"))
        self.assertEqual(row["sap_draft_entries"], [58620])
        self.assertEqual(row["entry_no"], "")

    def test_a_refused_draft_is_tried_again(self):
        self.sap.create_error = SAPValidationError("Period is locked")
        entry_id = self._create().json()["id"]
        self.sap.create_error = None
        response = self._as(self.maker).post(f"{API}{entry_id}/send-to-sap/", HTTP_COMPANY_CODE="JIVO_OIL")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["sap_status"], "CREATED")

    def test_the_form_can_ask_for_one_grpo(self):
        url = f"{API}grpos/?doc_entry=27481"
        [row] = self._as(self.maker).get(url, HTTP_COMPANY_CODE="JIVO_OIL").json()
        self.assertEqual(row["doc_entry"], 27481)
        self.assertEqual(self._as(self.maker).get(f"{API}grpos/?doc_entry=1", HTTP_COMPANY_CODE="JIVO_OIL").json(), [])
        self.assertEqual(self._as(self.maker).get(f"{API}grpos/?doc_entry=x", HTTP_COMPANY_CODE="JIVO_OIL").status_code, 400)

    def test_grpo_history_viewers_see_the_a_p_status(self):
        store = get_user_model().objects.create(email="grpo@jivo.in", full_name="GRPO viewer")
        UserCompany.objects.create(user=store, company=self.company, role=UserRole.objects.first(), is_active=True)
        store.user_permissions.add(Permission.objects.get(content_type__app_label="grpo", codename="can_view_grpo_history"))
        response = self._as(store).get(f"{API}grpo-status/?doc_entries=27481,1", HTTP_COMPANY_CODE="JIVO_OIL")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json(), {"27481": {
            "status": "NONE", "invoices": [], "sap_draft_entries": [], "entry": None,
        }})
        self.assertEqual(self._as(self.nobody).get(f"{API}grpo-status/?doc_entries=27481", HTTP_COMPANY_CODE="JIVO_OIL").status_code, 403)
        self.assertEqual(self._as(store).get(f"{API}grpo-status/?doc_entries=a,b", HTTP_COMPANY_CODE="JIVO_OIL").status_code, 400)

    def test_auditor_cannot_create_nobody_cannot_see(self):
        response = self._as(self.auditor).post(
            API, {"grpo_doc_entry": 27481, "invoice_file": _pdf()},
            format="multipart", HTTP_COMPANY_CODE="JIVO_OIL",
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self._as(self.nobody).get(API, HTTP_COMPANY_CODE="JIVO_OIL").status_code, 403)

    def test_only_the_auditor_reviews(self):
        entry_id = self._create().json()["id"]
        url = f"{API}{entry_id}/checks/qc/review/"
        body = {"decision": "OK", "remark": "QC register"}
        self.assertEqual(
            self._as(self.maker).post(url, body, format="json", HTTP_COMPANY_CODE="JIVO_OIL").status_code, 403,
        )
        response = self._as(self.auditor).post(url, body, format="json", HTTP_COMPANY_CODE="JIVO_OIL")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["effective_status"], "PASS")
        self.assertEqual(response.json()["reviewed_by_name"], "Auditor")

    def test_a_bad_grpo_is_a_400(self):
        response = self._as(self.maker).post(
            API, {"grpo_doc_entry": 1, "invoice_file": _pdf()},
            format="multipart", HTTP_COMPANY_CODE="JIVO_OIL",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("no GRPO", response.json()["detail"])


# ---------------------------------------------------------------------------
# Reading the bill
# ---------------------------------------------------------------------------

_HAS_OCR = all(importlib.util.find_spec(m) for m in ("cv2", "numpy", "rapidocr", "pypdfium2"))


def _page(width=1400, height=700):
    import numpy as np
    return np.full((height, width, 3), 255, np.uint8)


def _stamp(image, label_box=(100, 300, 280, 340)):
    """The check stamp's Rate Check line: the label (a box, as OCR finds it)
    and the long printed rule after it."""
    import cv2
    x0, y0, x1, y1 = label_box
    cv2.line(image, (x1 + 8, y1 - 6), (x1 + 290, y1 - 9), (150, 40, 30), 7)
    return {"page": 1, "text": "Rate Chock", "score": 0.9, "box": list(label_box)}


@skipUnless(_HAS_OCR, "the OCR packages are not installed")
class RateCheckMarksTests(SimpleTestCase):
    def test_the_printed_rule_alone_is_blank(self):
        image = _page()
        label = _stamp(image)
        marks = rate_check_marks(image, [label])
        self.assertEqual((marks["found"], marks["signed"]), (True, False))
        self.assertLess(marks["ink"], 0.25)

    def test_a_pen_signature_over_the_rule_is_signed(self):
        import cv2
        import numpy as np
        image = _page()
        label = _stamp(image)
        points = [(300 + i * 2, 330 - int(30 * np.sin(i / 8))) for i in range(130)]
        cv2.polylines(image, [np.array(points, np.int32)], False, (120, 40, 30), 3)
        marks = rate_check_marks(image, [label])
        self.assertTrue(marks["signed"])
        self.assertGreater(marks["ink"], 0.6)

    def test_ink_beyond_the_rule_is_not_counted(self):
        import cv2
        image = _page()
        label = _stamp(image)
        # The gate stamp's handwriting further right on the same line.
        cv2.putText(image, "HR69F", (800, 335), cv2.FONT_HERSHEY_SCRIPT_SIMPLEX, 1.5, (120, 40, 30), 3)
        self.assertFalse(rate_check_marks(image, [label])["signed"])

    def test_no_label_no_finding(self):
        self.assertEqual(rate_check_marks(_page(), []), {"found": False})


class RowsTests(SimpleTestCase):
    def test_boxes_on_one_printed_line_join_left_to_right(self):
        lines = [
            {"page": 1, "text": "9,161.00", "score": 1, "box": [1200, 1350, 1300, 1370]},
            {"page": 1, "text": "Add : CGST", "score": 1, "box": [750, 1349, 900, 1371]},
            {"page": 1, "text": "@ 9.00 %", "score": 1, "box": [1000, 1352, 1100, 1368]},
            {"page": 1, "text": "Add : SGST", "score": 1, "box": [750, 1378, 900, 1398]},
        ]
        self.assertEqual(
            [row["text"] for row in group_rows(lines)],
            ["Add : CGST @ 9.00 % 9,161.00", "Add : SGST"],
        )

    def test_the_gate_stamp_date_is_read_by_its_label_only(self):
        lines = [
            {"page": 1, "text": "Dated 2-10-26", "score": 1, "box": [900, 360, 1100, 380]},
            {"page": 1, "text": "3/10/26", "score": 1, "box": [1196, 1430, 1290, 1450]},
            {"page": 1, "text": "G. No....", "score": 1, "box": [923, 1450, 1010, 1470]},
        ]
        self.assertEqual(gate_stamp_date(lines), "3/10/26")
        self.assertEqual(gate_stamp_date(lines[:1]), "")



class SlashDashTests(SimpleTestCase):
    """Handwritten figures closed with "/-", its stroke read as a 1 (GRAPHIC-368)."""

    def _texts(self, *texts):
        lines = [{"page": 1, "text": text, "score": 0.9, "box": [0, 40 * i, 200, 40 * i + 30]}
                 for i, text in enumerate(texts)]
        return mend_slash_dashes(lines)

    def test_the_stroke_is_not_a_digit(self):
        lines = self._texts("4201-", "0.281-", "75.61-", "991|-", "840|—")
        self.assertEqual([line["text"] for line in lines], ["420/-", "0.28/-", "75.6/-", "991/-", "840/-"])
        self.assertEqual(lines[0]["ocr_text"], "4201-")

    def test_printed_figures_dates_and_numbers_are_left_alone(self):
        texts = [
            "Rs. 4,201/-", "Dated 21-10-26", "KILLAN NO 17/21/1.1 26-27/1979", "GE-2026-9786",
            "Add : CGST @ 9.00 % 9,161.00", "0.28-", "840", "SNP-0969/26-27",
        ]
        lines = self._texts(*texts)
        self.assertEqual([line["text"] for line in lines], texts)
        self.assertFalse(any("ocr_text" in line for line in lines))


def _rate_blank(rate=""):
    """A hand-filled bill book's GST line: "+SGST@", a dotted blank with the
    rate written over it, and "%", between the table's rules."""
    import cv2
    image = _page(900, 200)
    for y in (60, 140):
        cv2.line(image, (40, y), (860, y), (0, 0, 0), 3)
    cv2.line(image, (620, 60), (620, 140), (0, 0, 0), 3)
    cv2.putText(image, "+SGST@", (50, 120), cv2.FONT_HERSHEY_SIMPLEX, 1.1, (0, 0, 0), 3)
    for x in range(185, 545, 11):
        cv2.rectangle(image, (x, 114), (x + 5, 119), (0, 0, 0), -1)
    cv2.putText(image, rate, (330, 112), cv2.FONT_HERSHEY_SCRIPT_SIMPLEX, 1.6, (120, 40, 30), 3)
    cv2.putText(image, "%", (550, 120), cv2.FONT_HERSHEY_SIMPLEX, 1.1, (0, 0, 0), 3)
    return image


#: What OCR read off GRAPHIC-368's tax block (Graphics Elite, 2026-10-09): the
#: printed dots of each blank, without the 9 written in them.
GRAPHIC_TAX_LINES = [
    {"page": 1, "text": "Total", "score": 1.0, "box": [818, 1516, 942, 1582]},
    {"page": 1, "text": "840", "score": 0.998, "box": [1276, 1530, 1395, 1599]},
    {"page": 1, "text": "+SGST@.....%", "score": 0.774, "box": [814, 1584, 1206, 1643]},
    {"page": 1, "text": "75.61-", "score": 0.834, "box": [1271, 1593, 1451, 1663]},
    {"page": 1, "text": "+CGST@..", "score": 0.999, "box": [815, 1654, 1003, 1702]},
    {"page": 1, "text": "...%", "score": 0.518, "box": [1038, 1645, 1198, 1707]},
    {"page": 1, "text": "K61", "score": 0.66, "box": [1286, 1655, 1432, 1721]},
    {"page": 1, "text": "+IGST @..", "score": 0.942, "box": [817, 1717, 993, 1763]},
    {"page": 1, "text": "Grand Total", "score": 0.994, "box": [1053, 1780, 1223, 1828]},
]

@skipUnless(_HAS_OCR, "the OCR packages are not installed")
class RateBlankTests(SimpleTestCase):
    """GST rates written by hand into a printed "+SGST@.....%" blank."""

    BLANK = {"page": 1, "text": "+SGST@..........%", "score": 0.8, "box": [41, 70, 582, 133]}

    def test_the_rate_is_read_off_the_blank_with_its_dots_and_rules_erased(self):
        for rate in ("9", "2.5", "18"):
            with self.subTest(rate=rate):
                lines = fill_rate_blanks(_rate_blank(rate), [dict(self.BLANK)])
                self.assertEqual([line["text"] for line in lines], [f"+SGST@ {rate} %"])
                self.assertEqual(lines[0]["ocr_text"], "+SGST@..........%")

    def test_a_blank_nobody_wrote_in_stays_as_read(self):
        self.assertEqual(fill_rate_blanks(_rate_blank(), [dict(self.BLANK)]), [self.BLANK])

    def test_graphic_368_reads_cgst_sgst_at_9_percent(self):
        reads = [("+SGST@ 9 %", 0.968), ("+CGST@ 9.%", 0.946), ("+IGST@%", 0.996)]
        with mock.patch("ap_invoice_draft.invoice_reader._recognize", side_effect=reads) as recognize:
            lines = fill_rate_blanks(_page(1600, 1900), copy.deepcopy(GRAPHIC_TAX_LINES))
        # The CGST blank is cut out to the end of its "%" box, and that box joins the line.
        self.assertEqual(recognize.call_args_list[1].args[0].shape[1], 1198 - 815)
        self.assertEqual(
            [row["text"] for row in group_rows(lines)],
            ["Total 840", "+SGST@ 9 % 75.61-", "+CGST@ 9 % K61", "+IGST @..", "Grand Total"],
        )
        self.assertEqual(lines[4]["box"], [815, 1645, 1198, 1707])

        before = checks.check_gst(GRAPHIC_GRPO, {"rows": group_rows(GRAPHIC_TAX_LINES)})
        self.assertIn("could not be read clearly", before.detail)
        after = checks.check_gst(GRAPHIC_GRPO, {"rows": group_rows(lines)})
        # The CGST amount still reads "K61"; the SGST half is enough.
        self.assertEqual(after.status, CheckStatus.PASS)
        self.assertIn("CGST+SGST at 9%, ₹151.20", after.detail)

    def test_a_digit_read_among_the_dots_is_used_as_it_is(self):
        lines = [
            {"page": 1, "text": "+SGST@..", "score": 0.9, "box": [41, 84, 203, 133]},
            {"page": 1, "text": "9", "score": 0.9, "box": [323, 75, 364, 117]},
            {"page": 1, "text": "..%", "score": 0.9, "box": [529, 89, 581, 127]},
        ]
        with mock.patch("ap_invoice_draft.invoice_reader._recognize") as recognize:
            filled = fill_rate_blanks(_page(), lines)
        recognize.assert_not_called()
        self.assertEqual([line["text"] for line in filled], ["+SGST@ 9 %"])

    def test_a_printed_rate_is_left_alone(self):
        lines = [{"page": 1, "text": "Add : CGST @ 9.00 % 9,161.00", "score": 1.0, "box": [750, 349, 1300, 371]}]
        with mock.patch("ap_invoice_draft.invoice_reader._recognize") as recognize:
            self.assertEqual(fill_rate_blanks(_page(), copy.deepcopy(lines)), lines)
        recognize.assert_not_called()

    def test_an_unsure_second_reading_is_not_used(self):
        with mock.patch("ap_invoice_draft.invoice_reader._recognize", return_value=("+SGST@ 9 %", 0.3)):
            self.assertEqual(fill_rate_blanks(_rate_blank("9"), [dict(self.BLANK)]), [self.BLANK])

    def test_the_engine_still_finds_text_after_reading_a_blank(self):
        """RapidOCR keeps a call's use_det=False for the calls after it."""
        import cv2
        fill_rate_blanks(_rate_blank("9"), [dict(self.BLANK)])
        data, _ = read_invoice(cv2.imencode(".png", _rate_blank("18"))[1].tobytes(), "bill.png")
        self.assertIn("+SGST@ 18 %", [row["text"] for row in data["rows"]])


@skipUnless(_HAS_OCR, "the OCR packages are not installed")
class InvoiceReaderTests(SimpleTestCase):
    """The reader end to end, on a photo of a bill, with the engine mocked."""

    def _png(self, image):
        import cv2
        return cv2.imencode(".png", image)[1].tobytes()

    def test_a_photo_is_read_into_rows_po_numbers_and_the_rate_check(self):
        image = _page()
        label = _stamp(image)
        page_lines = [
            {"page": 1, "text": "Invoice No. : SNP-0969/26-27", "score": 1, "box": [80, 100, 500, 120]},
            {"page": 1, "text": "PO_NUMBER : 220926043", "score": 1, "box": [820, 101, 1200, 121]},
            label,
        ]
        with mock.patch("ap_invoice_draft.invoice_reader._ocr", return_value=page_lines):
            data, engine = read_invoice(self._png(image), "RAJ-969.jpg")
        self.assertIn("RapidOCR", engine)
        self.assertEqual(data["po_numbers"], ["220926043"])
        self.assertEqual(data["rows"][0]["text"], "Invoice No. : SNP-0969/26-27 PO_NUMBER : 220926043")
        self.assertEqual(data["rate_check"]["signed"], False)

    def test_nothing_read_is_an_error(self):
        with mock.patch("ap_invoice_draft.invoice_reader._ocr", return_value=[]):
            with self.assertRaisesMessage(InvoiceReadError, "No text could be read"):
                read_invoice(self._png(_page()), "blank.png")

    def test_not_a_pdf_or_photo(self):
        with self.assertRaisesMessage(InvoiceReadError, "PDF or a photo"):
            read_invoice(b"PK", "bill.docx")

    def test_a_broken_pdf_is_an_error(self):
        with self.assertRaisesMessage(InvoiceReadError, "could not be opened"):
            read_invoice(b"%PDF-1.7 not really", "bill.pdf")

    def test_the_real_engine_reads_printed_text(self):
        """Not mocked: catches a RapidOCR upgrade that changes what it returns."""
        import cv2
        image = _page(1200, 300)
        cv2.putText(image, "Invoice No. 26-27/1979", (60, 150), cv2.FONT_HERSHEY_SIMPLEX, 1.6, (0, 0, 0), 3)
        data, _ = read_invoice(self._png(image), "bill.png")
        self.assertIn("26-27/1979", " ".join(row["text"] for row in data["rows"]))
