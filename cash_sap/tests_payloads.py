"""The exact JSON of every imprest document (spec §2.6.1-2.6.4).

SAP takes what it is given, so the payload IS the posting. These tests pin
each builder to the spec's shape key for key, including what must be left
out: a blank dimension, ``CostingCode3`` on a service draft, ``Series`` when
series are off, and the branch on a company without branches.

Pure: no database, no SAP. ``SimpleTestCase``.
"""

import json
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

from django.test import SimpleTestCase
from rest_framework.exceptions import ValidationError

from cash_sap import payloads
from cash_sap.constants import marker


def _batch(**kw):
    base = dict(
        id=42, vendor_code="ORGV000465", bpl_id=2, po_doc_date=date(2026, 9, 1),
        po_due_date=date(2026, 9, 24), bunch=SimpleNamespace(number=17), sap_company_code="JIVO_OIL",
        marker=marker("PO ", 42), po_doc_num=220926157, po_doc_entry=14221,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _voucher(entry_id, amount, item="CG0000015", account="5630004", line=0, **kw):
    base = dict(
        entry_id=entry_id, amount=Decimal(amount), item_code=item, warehouse_code="BH-FA",
        account_code=account, tax_code="IGST@0", ocr_code="CANOLA", ocr_code2="09-2026",
        ocr_code3="Factory", ocr_code5="HR", bill_date=date(2026, 9, 3), po_line_num=line,
    )
    base.update(kw)
    return SimpleNamespace(**base)


class FormatterTests(SimpleTestCase):
    def test_amounts_drop_zero_paise_only(self):
        self.assertEqual(payloads.format_amount(Decimal("350.00")), "350")
        self.assertEqual(payloads.format_amount(Decimal("350.50")), "350.50")
        self.assertEqual(payloads.format_amount(Decimal("4050")), "4050")

    def test_grpo_num_at_card_is_amount_slash_bill_date(self):
        self.assertEqual(payloads.grpo_num_at_card(Decimal("500.00"), date(2026, 9, 24)), "500/24-09-26")

    def test_ap_num_at_card_is_month_total_amount(self):
        self.assertEqual(
            payloads.ap_num_at_card(date(2026, 9, 1), Decimal("4050.00"), Decimal("800.00")),
            "SEP 26/4050/800",
        )

    def test_month_code_is_dim2(self):
        self.assertEqual(payloads.month_code(date(2026, 9, 3)), "09-2026")


class CommentsTests(SimpleTestCase):
    def test_marker_survives_a_300_character_text(self):
        out = payloads.compose_comments("x" * 300, "[JI-CB G77]", "Vishal Tyagi")
        self.assertLessEqual(len(out), 254)
        self.assertTrue(out.endswith("[JI-CB G77] by Vishal Tyagi"))

    def test_a_very_long_name_is_dropped_before_the_marker(self):
        out = payloads.compose_comments("text", "[JI-CB G77]", "n" * 200)
        self.assertEqual(out, "text [JI-CB G77]")

    def test_upper_case_keeps_the_marker_findable(self):
        out = payloads.compose_comments("Based on", "[JI-CB PO 42-2]", "Vishal", upper=True)
        self.assertEqual(out, "BASED ON [JI-CB PO 42-2] BY VISHAL")

    def test_markers_and_rounds(self):
        self.assertEqual(marker("PO ", 42), "[JI-CB PO 42]")
        self.assertEqual(marker("PO ", 42, 2), "[JI-CB PO 42-2]")
        self.assertEqual(marker("G", 77), "[JI-CB G77]")
        self.assertEqual(marker("APS ", 10), "[JI-CB APS 10]")


class NextFreeNumAtCardTests(SimpleTestCase):
    def test_first_free_with_dots(self):
        taken = {"500/24-09-26", "500/24-09-26."}
        self.assertEqual(payloads.next_free_num_at_card("500/24-09-26", taken.__contains__), "500/24-09-26..")

    def test_free_base_is_kept(self):
        self.assertEqual(payloads.next_free_num_at_card("500/24-09-26", lambda c: False), "500/24-09-26")

    def test_too_many_dots_is_refused(self):
        with self.assertRaises(ValidationError):
            payloads.next_free_num_at_card("500/24-09-26", lambda c: True)


class POPayloadTests(SimpleTestCase):
    """§2.6.1"""

    def test_exact_po_payload(self):
        batch = _batch()
        v = _voucher(12345, "350.00")
        got = payloads.build_po_payload(batch, [v], series=2466, by="Vishal Tyagi", upper=False)
        self.assertEqual(got, {
            "CardCode": "ORGV000465", "BPL_IDAssignedToInvoice": 2, "Series": 2466,
            "DocDate": "2026-09-01", "TaxDate": "2026-09-01", "DocDueDate": "2026-09-24",
            # Upper case even with upper=False: SAP refuses a PO otherwise (2200012).
            "Comments": "IMPREST BUNCH 17 · 1 VOUCHER · JIVO_OIL [JI-CB PO 42] BY VISHAL TYAGI",
            "DocumentLines": [{
                "ItemCode": "CG0000015", "Quantity": 1, "UnitPrice": 350.0, "WarehouseCode": "BH-FA",
                "AccountCode": "5630004", "TaxCode": "IGST@0", "CostingCode": "CANOLA",
                "CostingCode2": "09-2026", "CostingCode3": "Factory", "CostingCode5": "HR",
                "ShipDate": "2026-09-03", "FreeText": "JI V12345",
            }],
        })
        json.dumps(got)  # JSON-native: no encoder needed

    def test_series_omitted_and_blank_dims_left_out(self):
        v = _voucher(1, "10.00", ocr_code3="", ocr_code5="")
        got = payloads.build_po_payload(_batch(), [v, _voucher(2, "5.50", line=1)], series=None, by="", upper=False)
        self.assertNotIn("Series", got)
        self.assertNotIn("NumAtCard", got)
        self.assertNotIn("CostingCode3", got["DocumentLines"][0])
        self.assertNotIn("CostingCode4", got["DocumentLines"][1])
        self.assertIn("2 VOUCHERS", got["Comments"])

    def test_po_comments_pass_sap_upper_case_rule_and_keep_the_marker(self):
        got = payloads.build_po_payload(_batch(), [_voucher(1, "10.00")], series=None,
                                        by="vishal tyagi", upper=False)
        self.assertEqual(got["Comments"], got["Comments"].upper())
        self.assertIn("[JI-CB PO 42]", got["Comments"])


class GRPOPayloadTests(SimpleTestCase):
    """§2.6.2"""

    def test_exact_grouped_grpo_payload(self):
        batch = _batch()
        grpo = SimpleNamespace(
            posting_date=date(2026, 9, 24), bill_date=date(2026, 9, 24), num_at_card="500/24-09-26",
            gate_entry_no="773", supplier_bill_no="1234", marker=marker("G", 77),
        )
        members = [_voucher(1, "350.00", line=3), _voucher(2, "150.00", item="CG0000005", account="5680015", line=4)]
        got = payloads.build_grpo_payload(grpo, members, batch, series=2478, attachment_entry=98765,
                                          by="Vishal Tyagi", upper=False)
        line = {"Quantity": 1, "BaseType": 22, "BaseEntry": 14221, "WarehouseCode": "BH-FA",
                "TaxCode": "IGST@0", "CostingCode": "CANOLA", "CostingCode2": "09-2026",
                "CostingCode3": "Factory", "CostingCode5": "HR"}
        self.assertEqual(got, {
            "CardCode": "ORGV000465", "BPL_IDAssignedToInvoice": 2, "Series": 2478,
            "DocDate": "2026-09-24", "DocDueDate": "2026-09-24", "TaxDate": "2026-09-24",
            "NumAtCard": "500/24-09-26",
            "Comments": "Based On Purchase Orders 220926157.\rG.no.773 Bill 1234 [JI-CB G77] by Vishal Tyagi",
            "AttachmentEntry": 98765,
            "DocumentLines": [
                dict(line, ItemCode="CG0000015", BaseLine=3, UnitPrice=350.0, AccountCode="5630004"),
                dict(line, ItemCode="CG0000005", BaseLine=4, UnitPrice=150.0, AccountCode="5680015"),
            ],
        })
        json.dumps(got)

    def test_re_dated_grpo_keeps_the_bill_date_as_tax_date(self):
        grpo = SimpleNamespace(posting_date=date(2026, 10, 1), bill_date=date(2026, 9, 24),
                               num_at_card="500/24-09-26", gate_entry_no="", supplier_bill_no="",
                               marker="[JI-CB G5]")
        got = payloads.build_grpo_payload(grpo, [_voucher(1, "500")], _batch(), series=None,
                                          attachment_entry=None, by="", upper=False)
        self.assertEqual(got["DocDate"], "2026-10-01")
        self.assertEqual(got["TaxDate"], "2026-09-24")
        self.assertNotIn("AttachmentEntry", got)
        self.assertNotIn("Series", got)
        self.assertEqual(got["Comments"], "Based On Purchase Orders 220926157. [JI-CB G5]")


class APDraftPayloadTests(SimpleTestCase):
    """§2.6.3 and §2.6.4"""

    def test_exact_item_draft(self):
        draft = SimpleNamespace(batch=_batch(), vendor_code="ORGV000465", doc_date=date(2026, 9, 24),
                                num_at_card="SEP 26/4050/3250", marker=marker("API ", 9))
        g1 = SimpleNamespace(id=1, grpo_doc_num=2026096948, grpo_doc_entry=27585, line_map={"11": 0, "12": 1})
        g2 = SimpleNamespace(id=2, grpo_doc_num=2026096949, grpo_doc_entry=27586, line_map={})
        got = payloads.build_ap_item_draft_payload(draft, [g1, g2], series=3325, by="Vishal Tyagi",
                                                   upper=False, member_counts={2: 1})
        self.assertEqual(got, {
            "DocObjectCode": "oPurchaseInvoices", "DocType": "dDocument_Items", "CardCode": "ORGV000465",
            "BPL_IDAssignedToInvoice": 2, "Series": 3325, "DocDate": "2026-09-24", "TaxDate": "2026-09-24",
            "NumAtCard": "SEP 26/4050/3250",
            "Comments": "Based On Goods Receipt PO 2026096948. 2026096949. [JI-CB API 9] by Vishal Tyagi",
            "DocumentLines": [
                {"BaseType": 20, "BaseEntry": 27585, "BaseLine": 0},
                {"BaseType": 20, "BaseEntry": 27585, "BaseLine": 1},
                {"BaseType": 20, "BaseEntry": 27586, "BaseLine": 0},
            ],
        })

    def _service_voucher(self, **kw):
        base = dict(
            entry=SimpleNamespace(serial_number=188, id=5, item="Courier to Delhi", detail="x"),
            gl_account_name="Postage & Courier", gl_account_code="5680023", account_code="5680023",
            amount=Decimal("250.00"), tax_code="Exampt", ocr_code="CANOLA", ocr_code2="09-2026",
            ocr_code3="Factory", ocr_code5="HR",
        )
        base.update(kw)
        return SimpleNamespace(**base)

    def test_exact_service_draft_with_every_dimension(self):
        draft = SimpleNamespace(batch=_batch(), vendor_code="ORGV000465", doc_date=date(2026, 9, 28),
                                num_at_card="SEP 26/4050/800", marker=marker("APS ", 10),
                                amount=Decimal("800.00"))
        v1 = self._service_voucher(gl_account_name="CONVEYANCE", account_code="5690002",
                                   entry=SimpleNamespace(serial_number=None, id=7, item="", detail="Auto to station"),
                                   amount=Decimal("550.00"))
        v2 = self._service_voucher()
        got = payloads.build_ap_service_draft_payload(draft, [v1, v2], series=3325, by="", upper=False)
        self.assertEqual(got, {
            "DocObjectCode": "oPurchaseInvoices", "DocType": "dDocument_Service", "CardCode": "ORGV000465",
            "BPL_IDAssignedToInvoice": 2, "Series": 3325, "DocDate": "2026-09-28", "TaxDate": "2026-09-28",
            "NumAtCard": "SEP 26/4050/800",
            "Comments": "BEING EXPENSE BOOKED AGAINST CONVEYANCE, POSTAGE & COURIER AMOUNT 800/-, "
                        "INVOICE NO. SEP 26/4050/800, DATED 28-09-2026 [JI-CB APS 10]",
            # Dim3 too: SAP refuses a draft line without a budget head (1120009).
            "DocumentLines": [
                {"ItemDescription": "Sr 7 Auto to station", "AccountCode": "5690002", "LineTotal": 550.0,
                 "TaxCode": "Exampt", "CostingCode": "CANOLA", "CostingCode2": "09-2026",
                 "CostingCode3": "Factory", "CostingCode5": "HR"},
                {"ItemDescription": "Sr 188 Courier to Delhi", "AccountCode": "5680023", "LineTotal": 250.0,
                 "TaxCode": "Exampt", "CostingCode": "CANOLA", "CostingCode2": "09-2026",
                 "CostingCode3": "Factory", "CostingCode5": "HR"},
            ],
        })
        self.assertNotIn("Rounding", got)  # whole rupees: nothing to round
        json.dumps(got)

    def test_a_total_with_paise_asks_sap_to_round(self):
        draft = SimpleNamespace(batch=_batch(), vendor_code="ORGV000465", doc_date=date(2026, 9, 28),
                                num_at_card="SEP 26/4050/250.50", marker=marker("APS ", 11),
                                amount=Decimal("250.50"))
        got = payloads.build_ap_service_draft_payload(
            draft, [self._service_voucher(amount=Decimal("250.50"))], series=None, by="", upper=False)
        self.assertEqual(got["Rounding"], "tYES")

    def test_item_draft_with_paise_asks_sap_to_round(self):
        draft = SimpleNamespace(batch=_batch(), vendor_code="ORGV000465", doc_date=date(2026, 9, 24),
                                num_at_card="SEP 26/4050/350.50", marker=marker("API ", 12),
                                amount=Decimal("350.50"))
        g = SimpleNamespace(id=1, grpo_doc_num=2026096948, grpo_doc_entry=27585, line_map={})
        got = payloads.build_ap_item_draft_payload(draft, [g], series=None, by="", upper=False)
        self.assertEqual(got["Rounding"], "tYES")

    def test_no_branch_means_no_branch_and_no_series(self):
        draft = SimpleNamespace(batch=_batch(bpl_id=None), vendor_code="ORGV000245",
                                doc_date=date(2026, 9, 28), num_at_card="SEP 26/1/1",
                                marker="[JI-CB APS 3]", amount=Decimal("1"))
        got = payloads.build_ap_service_draft_payload(draft, [self._service_voucher()], series=3325,
                                                      by="", upper=False)
        self.assertNotIn("BPL_IDAssignedToInvoice", got)
        self.assertNotIn("Series", got)

    def test_item_description_is_cut_to_100(self):
        v = self._service_voucher(entry=SimpleNamespace(serial_number=1, id=1, item="y" * 200, detail=""))
        self.assertEqual(len(payloads.service_line_description(v)), 100)
