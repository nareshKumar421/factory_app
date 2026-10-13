"""The purchasing writers: URLs, timeouts, approval drafts and the error split.

The Service Layer login (``_get_session_cookies``) and ``requests.post`` are
mocked; ``CompanyContext`` is patched where ``service_layer`` looks it up.
Nothing here reaches SAP.

Run: ``manage.py test sap_purchasing.tests_service_layer --settings=config.sqlite_test_settings``
"""

from decimal import Decimal
from datetime import date
from unittest import mock

import requests
from django.test import SimpleTestCase

from sap_client.exceptions import (
    SAPDataError,
    SAPOutcomeUnknown,
    SAPUnavailable,
    SAPValidationError,
)
from sap_purchasing import service_layer as sl

BASE = "https://sl.test:50000"


def _ctx():
    ctx = mock.Mock()
    ctx.service_layer = {"base_url": BASE, "company_db": "TEST_JIVO_OIL_HANADB",
                         "username": "u", "password": "p"}
    ctx.hana = {"schema": "TEST_JIVO_OIL_HANADB"}
    return ctx


def _resp(status, body=None, headers=None, text=""):
    response = mock.Mock()
    response.status_code = status
    response.headers = headers or {}
    response.text = text
    if body is None:
        response.json.side_effect = ValueError("no JSON")
    else:
        response.json.return_value = body
    return response


PO_PAYLOAD = {
    "CardCode": "ORGV000465", "BPL_IDAssignedToInvoice": 2, "Series": 2466,
    "DocDate": "2026-09-01", "TaxDate": "2026-09-01", "DocDueDate": "2026-09-24",
    "Comments": "Imprest bunch 17 [JI-CB PO 42] by Vishal Tyagi",
    "DocumentLines": [{"ItemCode": "CG0000015", "Quantity": 1, "UnitPrice": Decimal("350.00"),
                       "WarehouseCode": "BH-FA", "FreeText": "JI V12345"}],
}


@mock.patch.object(sl._PurchasingDocWriter, "_get_session_cookies", return_value={"B1SESSION": "s"})
@mock.patch("sap_purchasing.service_layer.CompanyContext", side_effect=lambda code: _ctx())
@mock.patch("sap_purchasing.service_layer.requests.post")
class CreateTests(SimpleTestCase):
    def test_po_201_returns_the_json_on_v2_with_120s(self, post, _ctx_cls, _login):
        post.return_value = _resp(201, {"DocEntry": 14221, "DocNum": 220926157})
        result = sl.create_purchase_order("JIVO_OIL", PO_PAYLOAD)
        self.assertEqual(result["DocEntry"], 14221)
        self.assertFalse(sl.is_pending_approval(result))
        args, kwargs = post.call_args
        self.assertEqual(args[0], f"{BASE}/b1s/v2/PurchaseOrders")
        self.assertEqual(kwargs["timeout"], 120)
        # Decimals become floats before sending.
        self.assertEqual(kwargs["json"]["DocumentLines"][0]["UnitPrice"], 350.0)

    def test_grpo_uses_purchase_delivery_notes_with_180s(self, post, _ctx_cls, _login):
        post.return_value = _resp(201, {"DocEntry": 27585, "DocNum": 2026096948})
        sl.create_purchase_delivery_note("JIVO_OIL", {"CardCode": "V", "DocDate": date(2026, 9, 24)})
        args, kwargs = post.call_args
        self.assertEqual(args[0], f"{BASE}/b1s/v2/PurchaseDeliveryNotes")
        self.assertEqual(kwargs["timeout"], 180)
        self.assertEqual(kwargs["json"]["DocDate"], "2026-09-24")

    def test_ap_draft_injects_doc_object_code_with_120s(self, post, _ctx_cls, _login):
        post.return_value = _resp(201, {"DocEntry": 881})
        payload = {"CardCode": "V", "DocType": "dDocument_Service"}
        result = sl.create_purchase_invoice_draft("JIVO_OIL", payload)
        self.assertEqual(result["DocEntry"], 881)
        args, kwargs = post.call_args
        self.assertEqual(args[0], f"{BASE}/b1s/v2/Drafts")
        self.assertEqual(kwargs["timeout"], 120)
        self.assertEqual(kwargs["json"]["DocObjectCode"], "oPurchaseInvoices")
        self.assertNotIn("DocObjectCode", payload)  # the caller's dict is untouched

    def test_ap_draft_refuses_another_object(self, post, _ctx_cls, _login):
        with self.assertRaises(SAPValidationError):
            sl.create_purchase_invoice_draft("JIVO_OIL", {"DocObjectCode": "oPurchaseOrders"})
        post.assert_not_called()

    def test_po_and_grpo_detect_approval_drafts(self, post, _ctx_cls, _login):
        post.return_value = _resp(404, {"error": {"code": -2028, "message": "No matching records"}},
                                  headers={"Location": f"{BASE}/b1s/v2/Drafts(77)"})
        for writer in (sl.create_purchase_order, sl.create_purchase_delivery_note):
            with self.subTest(writer=writer.__name__):
                result = writer("JIVO_OIL", PO_PAYLOAD)
                self.assertTrue(sl.is_pending_approval(result))
                self.assertEqual(result["draft_entry"], 77)
                self.assertIsNone(result["DocEntry"])

    def test_ap_draft_does_no_approval_detection(self, post, _ctx_cls, _login):
        post.return_value = _resp(404, {"error": {"message": "x"}},
                                  headers={"Location": f"{BASE}/b1s/v2/Drafts(77)"})
        with self.assertRaises(SAPDataError):
            sl.create_purchase_invoice_draft("JIVO_OIL", {"CardCode": "V"})

    def test_400_is_a_validation_error_with_sap_text(self, post, _ctx_cls, _login):
        post.return_value = _resp(400, {"error": {"code": -5002, "message": {"value": "Bad line"}}})
        with self.assertRaises(SAPValidationError) as caught:
            sl.create_purchase_order("JIVO_OIL", PO_PAYLOAD)
        self.assertIn("Bad line", str(caught.exception))
        self.assertIn("-5002", str(caught.exception))

    def test_unavailable_statuses(self, post, _ctx_cls, _login):
        for status in (401, 403, 502, 503):
            with self.subTest(status=status):
                post.return_value = _resp(status, {"error": {"message": "down"}})
                with self.assertRaises(SAPUnavailable):
                    sl.create_purchase_order("JIVO_OIL", PO_PAYLOAD)

    def test_504_means_the_outcome_is_unknown(self, post, _ctx_cls, _login):
        post.return_value = _resp(504, text="Gateway Timeout")
        with self.assertRaises(SAPOutcomeUnknown):
            sl.create_purchase_delivery_note("JIVO_OIL", PO_PAYLOAD)

    def test_500_is_a_data_error(self, post, _ctx_cls, _login):
        post.return_value = _resp(500, {"error": {"message": "boom"}})
        with self.assertRaises(SAPDataError):
            sl.create_purchase_order("JIVO_OIL", PO_PAYLOAD)

    def test_unreadable_201_is_outcome_unknown(self, post, _ctx_cls, _login):
        post.return_value = _resp(201, None)
        with self.assertRaises(SAPOutcomeUnknown):
            sl.create_purchase_order("JIVO_OIL", PO_PAYLOAD)

    def test_connect_timeout_never_sent(self, post, _ctx_cls, _login):
        post.side_effect = requests.exceptions.ConnectTimeout("connect timed out")
        with self.assertRaises(SAPUnavailable):
            sl.create_purchase_order("JIVO_OIL", PO_PAYLOAD)

    def test_connect_timeout_message_does_not_say_it_may_have_been_done(self, post, _ctx_cls, _login):
        post.side_effect = requests.exceptions.ConnectTimeout("connect timed out")
        with self.assertRaises(SAPUnavailable) as caught:
            sl.create_purchase_order("JIVO_OIL", PO_PAYLOAD)
        self.assertNotIn("may still", str(caught.exception))
        self.assertIn("Unable to connect", str(caught.exception))

    def test_read_timeout_may_have_committed(self, post, _ctx_cls, _login):
        post.side_effect = requests.exceptions.ReadTimeout("read timed out")
        with self.assertRaises(SAPOutcomeUnknown) as caught:
            sl.create_purchase_delivery_note("JIVO_OIL", PO_PAYLOAD)
        self.assertIn("may still", str(caught.exception))

    def test_answer_cut_off_may_have_committed(self, post, _ctx_cls, _login):
        post.side_effect = requests.exceptions.ChunkedEncodingError("connection broken")
        with self.assertRaises(SAPOutcomeUnknown):
            sl.create_purchase_delivery_note("JIVO_OIL", PO_PAYLOAD)

    def test_failed_login_sends_nothing(self, post, _ctx_cls, _login):
        with mock.patch.object(sl._PurchasingDocWriter, "_get_session_cookies",
                               side_effect=SAPUnavailable("login failed")):
            with self.assertRaises(SAPUnavailable):
                sl.create_purchase_order("JIVO_OIL", PO_PAYLOAD)
        post.assert_not_called()

    def test_company_code_selects_the_context(self, post, ctx_cls, _login):
        post.return_value = _resp(201, {"DocEntry": 1})
        sl.create_purchase_order("JIVO_BEVERAGES", PO_PAYLOAD)
        ctx_cls.assert_called_with("JIVO_BEVERAGES")


@mock.patch.object(sl._PurchasingDocWriter, "_get_session_cookies", return_value={"B1SESSION": "s"})
@mock.patch("sap_purchasing.service_layer.CompanyContext", side_effect=lambda code: _ctx())
@mock.patch("sap_purchasing.service_layer.requests.post")
class ActionTests(SimpleTestCase):
    def test_close_hits_the_po_close_action(self, post, _ctx_cls, _login):
        post.return_value = _resp(204)
        self.assertIsNone(sl.close_purchase_order("JIVO_OIL", 5))
        args, kwargs = post.call_args
        self.assertEqual(args[0], f"{BASE}/b1s/v2/PurchaseOrders(5)/Close")
        self.assertEqual(kwargs["timeout"], 60)

    def test_close_refused(self, post, _ctx_cls, _login):
        post.return_value = _resp(400, {"error": {"message": "Document is already closed"}})
        with self.assertRaises(SAPValidationError):
            sl.close_purchase_order("JIVO_OIL", 5)

    def test_save_draft_to_document_per_object(self, post, _ctx_cls, _login):
        post.return_value = _resp(204)
        for code, doc_object in (("22", "oPurchaseOrders"), ("20", "oPurchaseDeliveryNotes")):
            with self.subTest(code=code):
                sl.save_draft_to_document("JIVO_OIL", code, 77)
                args, kwargs = post.call_args
                self.assertEqual(args[0], f"{BASE}/b1s/v2/DraftsService_SaveDraftToDocument")
                self.assertEqual(kwargs["json"],
                                 {"Document": {"DocEntry": 77, "DocObjectCode": doc_object}})

    def test_save_draft_to_document_refuses_ap(self, post, _ctx_cls, _login):
        with self.assertRaises(ValueError):
            sl.save_draft_to_document("JIVO_OIL", "18", 77)
        post.assert_not_called()

    def test_save_draft_to_document_gateway_timeout(self, post, _ctx_cls, _login):
        post.return_value = _resp(504)
        with self.assertRaises(SAPOutcomeUnknown):
            sl.save_draft_to_document("JIVO_OIL", "20", 77)


class JsonNativeTests(SimpleTestCase):
    def test_converts_decimals_dates_and_tuples(self):
        self.assertEqual(
            sl.json_native({"a": Decimal("1.50"), "d": date(2026, 9, 1), "t": (1, Decimal("2"))}),
            {"a": 1.5, "d": "2026-09-01", "t": [1, 2.0]},
        )

    def test_is_pending_approval(self):
        self.assertTrue(sl.is_pending_approval({"pending_approval": True, "draft_entry": 1}))
        self.assertFalse(sl.is_pending_approval({"DocEntry": 1}))
        self.assertFalse(sl.is_pending_approval(None))
