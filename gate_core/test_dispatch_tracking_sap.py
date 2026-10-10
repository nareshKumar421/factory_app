"""A delivery logged on Dispatch Tracking, written to each bill's SAP invoice."""
import shutil
import tempfile
from datetime import date
from decimal import Decimal
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from gate_core.models import (
    SalesDispatchDocumentType,
    SalesDispatchGateOutDocument,
    SalesDispatchGateOutItem,
    TruckDispatchSapReceipt,
    TruckDispatchSapReceiptStatus as Status,
)
from gate_core.services import dispatch_tracking_sap
from gate_core.services.dispatch_tracking_sap import SapReceiveHandler
# The module, not the class: a TestCase imported by name would run here twice.
from gate_core import test_dispatch_tracking as base
from sap_client.exceptions import SAPUnavailable, SAPValidationError
from sap_postings.models import SapPosting, SapPostingStatus

MEDIA = tempfile.mkdtemp(prefix="dt-sap-")


def sap_invoice(lines, *, doc_date=date(2026, 10, 1), received=None, attachment=None,
                cancelled=False, doc_num="INV"):
    """What ``invoice_receive_state`` answers: lines as (item_code, quantity)."""
    return {
        "doc_num": doc_num,
        "doc_date": doc_date,
        "cancelled": cancelled,
        "received_date": received,
        "attachment_entry": attachment,
        "lines": [
            {"line_num": n, "item_code": code, "quantity": Decimal(str(qty)), "received": None}
            for n, (code, qty) in enumerate(lines)
        ],
    }


@override_settings(MEDIA_ROOT=MEDIA)
class DeliverySapReceiptTests(TestCase):
    setUp = base.PartialDeliveryTests.setUp

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA, ignore_errors=True)

    # SAP as the two bills stand in it: A = 600 + 400, B = 400.
    SAP = {
        2001: [("FG0000328", 600), ("FG0000422", 400)],
        2002: [("FG0000500", 400)],
    }

    def _proof(self):
        return SimpleUploadedFile("pod.pdf", b"%PDF-1.4 signed", content_type="application/pdf")

    def _sap(self, overrides=None):
        """Patch SAP: the read-back, the upload and the PATCH. Returns the mocks."""
        overrides = overrides or {}

        def read_state(document):
            if document.sap_doc_entry in overrides:
                answer = overrides[document.sap_doc_entry]
                if isinstance(answer, Exception):
                    raise answer
                return answer
            return sap_invoice(self.SAP[document.sap_doc_entry], doc_num=document.sap_doc_num)

        reader = mock.patch.object(SapReceiveHandler, "_read_state", side_effect=read_state)
        patcher = mock.patch.object(SapReceiveHandler, "_patch_invoice")
        client_cls = mock.patch("sap_client.client.SAPClient")
        self.read = reader.start()
        self.patch = patcher.start()
        self.client_cls = client_cls.start()
        self.sap_client = self.client_cls.return_value
        self.sap_client.upload_attachment.side_effect = [
            {"AbsoluteEntry": 900 + n} for n in range(10)
        ]
        for p in (reader, patcher, client_cls):
            self.addCleanup(p.stop)

    def _post(self, data, fmt="multipart"):
        with self.captureOnCommitCallbacks(execute=True):
            resp = self.client.post(self.updates_url, data, format=fmt, **self.hdr)
        self.assertEqual(resp.status_code, 201, resp.content)
        return resp

    def _receipts(self, update_id):
        """The update's bills as the timeline shows them, read after the send.

        Not the POST's own response: in a test the send runs when the captured
        commit callbacks do, after the response is built. Outside a test the
        view's transaction commits first, so the response already has them.
        """
        timeline = self.client.get(self.updates_url, **self.hdr)
        return next(u for u in timeline.data if u["id"] == update_id)["sap_receipts"]

    def _receipt(self, bill):
        return TruckDispatchSapReceipt.objects.get(document=bill)

    def _payload(self, doc_entry):
        for call in self.patch.call_args_list:
            company_code, entry, payload = call.args
            if entry == doc_entry:
                return payload
        self.fail(f"no PATCH for invoice {doc_entry}")

    # --- opening ---------------------------------------------------------

    def test_delivered_without_proof_waits_for_one_and_sends_nothing(self):
        self._sap()
        resp = self._post({"status": "DELIVERED", "delivered_date": "2026-10-05"})
        self.assertEqual(
            [(r["sap_doc_num"], r["status"]) for r in resp.data["sap_receipts"]],
            [("INV-2001", "NEEDS_PROOF"), ("INV-2002", "NEEDS_PROOF")],
        )
        self.assertFalse(SapPosting.objects.exists())
        self.read.assert_not_called()

    def test_other_statuses_open_no_receipts(self):
        self._sap()
        resp = self._post({"status": "IN_TRANSIT", "proof": self._proof()})
        self.assertEqual(resp.data["sap_receipts"], [])
        self.assertFalse(TruckDispatchSapReceipt.objects.exists())

    def test_stock_transfers_on_the_truck_are_left_out(self):
        self._sap()
        SalesDispatchGateOutDocument.objects.create(
            sales_dispatch=self.docking, company=self.company,
            document_type=SalesDispatchDocumentType.STOCK_TRANSFER,
            sap_doc_entry=3001, sap_doc_num="ST-3001",
            created_by=self.user, updated_by=self.user,
        )
        resp = self._post({"status": "DELIVERED"})
        self.assertEqual(
            [r["sap_doc_num"] for r in resp.data["sap_receipts"]], ["INV-2001", "INV-2002"]
        )

    # --- sending ---------------------------------------------------------

    def test_delivered_with_proof_marks_every_bill_received(self):
        self._sap()
        resp = self._post(
            {"status": "DELIVERED", "delivered_date": "2026-10-05", "proof": self._proof()}
        )
        self.assertEqual(
            [r["status"] for r in self._receipts(resp.data["id"])], ["POSTED", "POSTED"]
        )
        payload = self._payload(2001)
        self.assertEqual(payload["U_Recv_Date"], "2026-10-05")
        self.assertEqual(payload["AttachmentEntry"], 900)
        self.assertEqual(
            payload["DocumentLines"],
            [{"LineNum": 0, "U_Recvd_Qty": 600.0}, {"LineNum": 1, "U_Recvd_Qty": 400.0}],
        )
        self.assertEqual(self._payload(2002)["AttachmentEntry"], 901)
        self.assertEqual(self._receipt(self.bill_a).sap_attachment_entry, 900)
        self.assertEqual(
            list(SapPosting.objects.values_list("kind", "status")),
            [("dispatch_tracking.receive", SapPostingStatus.POSTED)] * 2,
        )

    def test_the_proof_goes_up_named_after_the_bill(self):
        self._sap()
        self._post({"status": "DELIVERED", "proof": self._proof()})
        filename = self.sap_client.upload_attachment.call_args_list[0].kwargs["filename"]
        receipt = self._receipt(self.bill_a)
        self.assertEqual(filename, f"INV-2001_delivery_{receipt.pk}.pdf")

    def test_partial_delivery_receives_what_was_delivered(self):
        self._sap()
        self._post({
            "status": "PARTIALLY_DELIVERED",
            "delivered_date": "2026-10-05",
            "proof": self._proof(),
            "partial_lines": (
                '[{"document": %d, "items": [{"item": %d, "qty_delivered": "500", '
                '"qty_returned": "100"}]}]' % (self.bill_a.id, self.item_a1.id)
            ),
        })
        # The short item at what was delivered, the untouched one in full; the
        # other bill was not short at all.
        self.assertEqual(
            self._payload(2001)["DocumentLines"],
            [{"LineNum": 0, "U_Recvd_Qty": 500.0}, {"LineNum": 1, "U_Recvd_Qty": 400.0}],
        )
        self.assertEqual(
            self._payload(2002)["DocumentLines"], [{"LineNum": 0, "U_Recvd_Qty": 400.0}]
        )

    def test_an_item_that_came_back_whole_leaves_the_bill_for_sap_by_hand(self):
        self._sap()
        resp = self._post({
            "status": "PARTIALLY_DELIVERED",
            "proof": self._proof(),
            "partial_lines": (
                '[{"document": %d, "items": [{"item": %d, "qty_delivered": "0", '
                '"qty_returned": "400"}]}]' % (self.bill_a.id, self.item_a2.id)
            ),
        })
        statuses = {r["sap_doc_num"]: r for r in self._receipts(resp.data["id"])}
        self.assertEqual(statuses["INV-2001"]["status"], "BY_HAND")
        self.assertIn("FG0000422", statuses["INV-2001"]["message"])
        self.assertEqual(statuses["INV-2002"]["status"], "POSTED")
        self.assertEqual([c.args[1] for c in self.patch.call_args_list], [2002])

    def test_a_short_dispatch_receives_what_left_the_gate(self):
        self._sap()
        SalesDispatchGateOutItem.objects.filter(pk=self.item_a1.pk).update(
            dispatched_quantity=300
        )
        self._post({"status": "DELIVERED", "proof": self._proof()})
        self.assertEqual(
            self._payload(2001)["DocumentLines"][0], {"LineNum": 0, "U_Recvd_Qty": 300.0}
        )

    def test_a_bill_sap_already_shows_received_is_left_alone(self):
        self._sap({2001: sap_invoice(self.SAP[2001], received=date(2026, 10, 3), attachment=55)})
        resp = self._post({"status": "DELIVERED", "proof": self._proof()})
        receipt = self._receipt(self.bill_a)
        self.assertEqual(receipt.status, Status.ALREADY_RECEIVED)
        self.assertIn("03 Oct 2026", receipt.message)
        self.assertEqual([c.args[1] for c in self.patch.call_args_list], [2002])
        self.assertEqual(self.sap_client.upload_attachment.call_count, 1)
        self.assertEqual(
            [r["status"] for r in self._receipts(resp.data["id"])],
            ["ALREADY_RECEIVED", "POSTED"],
        )

    def test_a_received_date_before_the_bill_is_refused_before_writing(self):
        self._sap({2001: sap_invoice(self.SAP[2001], doc_date=date(2026, 10, 6))})
        self._post({"status": "DELIVERED", "delivered_date": "2026-10-05", "proof": self._proof()})
        receipt = self._receipt(self.bill_a)
        self.assertEqual(receipt.status, Status.REFUSED)
        self.assertIn("earlier than the bill's own date", receipt.message)
        self.assertEqual(
            SapPosting.objects.get(source_id=receipt.pk).status, SapPostingStatus.REJECTED
        )

    def test_a_bill_changed_in_sap_is_refused_not_guessed(self):
        self._sap({2001: sap_invoice([("FG0000328", 600), ("FG0000999", 400)])})
        self._post({"status": "DELIVERED", "proof": self._proof()})
        receipt = self._receipt(self.bill_a)
        self.assertEqual(receipt.status, Status.REFUSED)
        self.assertIn("changed in SAP", receipt.message)

    def test_a_cancelled_bill_is_refused(self):
        self._sap({2001: sap_invoice(self.SAP[2001], cancelled=True)})
        self._post({"status": "DELIVERED", "proof": self._proof()})
        self.assertEqual(self._receipt(self.bill_a).status, Status.REFUSED)

    def test_sap_refusing_the_patch_is_kept_as_refused(self):
        self._sap()
        self.patch.side_effect = SAPValidationError("Please Attach its Receiving")
        self._post({"status": "DELIVERED", "proof": self._proof()})
        receipt = self._receipt(self.bill_a)
        self.assertEqual(receipt.status, Status.REFUSED)
        self.assertEqual(receipt.message, "Please Attach its Receiving")

    def test_a_proof_missing_from_the_server_is_refused_with_why(self):
        self._sap()
        self.sap_client.upload_attachment.side_effect = FileNotFoundError("pod.pdf")
        self._post({"status": "DELIVERED", "proof": self._proof()})
        receipt = self._receipt(self.bill_a)
        self.assertEqual(receipt.status, Status.REFUSED)
        self.assertIn("attach it again", receipt.message)

    def test_an_invoice_with_attachments_gets_the_proof_added_to_them(self):
        self._sap({2001: sap_invoice(self.SAP[2001], attachment=4321)})
        self._post({"status": "DELIVERED", "proof": self._proof()})
        add = self.sap_client.add_line_to_existing_attachment
        self.assertEqual(add.call_args.kwargs["absolute_entry"], 4321)
        self.assertNotIn("AttachmentEntry", self._payload(2001))
        self.assertEqual(self._receipt(self.bill_a).sap_attachment_entry, 4321)

    # --- SAP not answering, and retries ------------------------------------

    def test_sap_not_answering_queues_the_truck_after_one_try(self):
        self._sap({2001: SAPUnavailable("SL down"), 2002: SAPUnavailable("SL down")})
        resp = self._post({"status": "DELIVERED", "proof": self._proof()})
        self.assertEqual(
            [r["status"] for r in self._receipts(resp.data["id"])], ["WAITING", "WAITING"]
        )
        # The second bill was queued without being tried.
        self.assertEqual(self.read.call_count, 1)
        self.assertEqual(
            list(SapPosting.objects.order_by("id").values_list("status", "attempts")),
            [(SapPostingStatus.QUEUED, 1), (SapPostingStatus.QUEUED, 0)],
        )

    def test_a_retry_after_a_lost_answer_recognises_its_own_write(self):
        self._sap()
        self._post({"status": "DELIVERED", "delivered_date": "2026-10-05"})
        receipt = self._receipt(self.bill_a)
        receipt.sap_attachment_entry = 77
        receipt.status = Status.WAITING
        receipt.save()
        receipt.update.proof = self._proof()
        receipt.update.save()
        self.read.side_effect = None
        self.read.return_value = sap_invoice(
            self.SAP[2001], received=date(2026, 10, 5), attachment=77
        )
        outcome = SapReceiveHandler().send(mock.Mock(source_id=receipt.pk))
        receipt.refresh_from_db()
        self.assertEqual(receipt.status, Status.POSTED)
        self.assertEqual(outcome.kind, "POSTED")
        self.sap_client.upload_attachment.assert_not_called()
        self.patch.assert_not_called()

    def test_a_retry_reuses_the_proof_already_uploaded(self):
        self._sap()
        self._post({"status": "DELIVERED"})
        receipt = self._receipt(self.bill_a)
        receipt.sap_attachment_entry = 77
        receipt.save()
        update = receipt.update
        update.proof = self._proof()
        update.save()
        SapReceiveHandler().send(mock.Mock(source_id=receipt.pk))
        self.sap_client.upload_attachment.assert_not_called()
        self.assertEqual(self._payload(2001)["AttachmentEntry"], 77)

    # --- the proof arriving later, and later updates -------------------------

    def test_attaching_the_proof_later_sends_what_waited_for_it(self):
        self._sap()
        created = self._post({"status": "DELIVERED"})
        with self.captureOnCommitCallbacks(execute=True):
            resp = self.client.post(
                f"{self.updates_url}{created.data['id']}/proof/",
                {"proof": self._proof()}, format="multipart", **self.hdr,
            )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertTrue(resp.data["proof"])
        self.assertEqual(
            [r["status"] for r in self._receipts(created.data["id"])], ["POSTED", "POSTED"]
        )

    def test_send_again_retries_what_sap_refused(self):
        self._sap()
        self.patch.side_effect = [SAPValidationError("locked"), None, None]
        created = self._post({"status": "DELIVERED", "proof": self._proof()})
        self.assertEqual(self._receipt(self.bill_a).status, Status.REFUSED)
        resp = self.client.post(
            f"{self.updates_url}{created.data['id']}/sap-send/", {}, format="json", **self.hdr
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(self._receipt(self.bill_a).status, Status.POSTED)
        # The proof uploaded on the refused try is reused, not attached twice.
        self.assertEqual(self.sap_client.upload_attachment.call_count, 2)

    def test_send_again_needs_a_proof(self):
        self._sap()
        created = self._post({"status": "DELIVERED"})
        resp = self.client.post(
            f"{self.updates_url}{created.data['id']}/sap-send/", {}, format="json", **self.hdr
        )
        self.assertEqual(resp.status_code, 400, resp.content)

    def test_a_later_delivery_replaces_receipts_that_never_reached_sap(self):
        self._sap()
        first = self._post({"status": "DELIVERED"})
        self._post({"status": "DELIVERED", "proof": self._proof()})
        old = TruckDispatchSapReceipt.objects.filter(update_id=first.data["id"])
        self.assertEqual(set(old.values_list("status", flat=True)), {Status.SUPERSEDED})
        self.assertEqual(self.patch.call_count, 2)

    def test_a_later_delivery_does_not_reopen_a_bill_already_received(self):
        self._sap()
        self._post({"status": "DELIVERED", "proof": self._proof()})
        second = self._post({"status": "DELIVERED", "proof": self._proof()})
        self.assertEqual(second.data["sap_receipts"], [])
        self.assertEqual(self.patch.call_count, 2)

    def test_timeline_carries_each_bills_sap_status(self):
        self._sap()
        self._post({"status": "DELIVERED"})
        timeline = self.client.get(self.updates_url, **self.hdr)
        receipt = timeline.data[0]["sap_receipts"][0]
        self.assertEqual(receipt["sap_doc_num"], "INV-2001")
        self.assertEqual(receipt["status_display"], "Waiting for the proof of delivery")
        self.assertEqual(receipt["company"], "Jivo Oil")


class MatchLinesTests(TestCase):
    def test_position_code_and_quantity_must_agree(self):
        ours = [{"item": 1, "item_code": "A", "quantity": "10", "received": "8"}]
        theirs = [{"line_num": 3, "item_code": "A", "quantity": Decimal("10"), "received": None}]
        self.assertEqual(dispatch_tracking_sap.match_lines(ours, theirs), [(3, Decimal("8"))])
        with self.assertRaises(dispatch_tracking_sap.ReceiptError):
            dispatch_tracking_sap.match_lines(ours, [dict(theirs[0], quantity=Decimal("12"))])
        with self.assertRaises(dispatch_tracking_sap.ReceiptError):
            dispatch_tracking_sap.match_lines(ours, theirs * 2)


class InvoiceReceiveStateReaderTests(TestCase):
    """The read-back the write stands on, against a stand-in HANA cursor."""

    def _reader(self, header, lines, columns=("U_Recv_Date",), line_columns=("U_Recvd_Qty",)):
        from dispatch_plans.hana_reader import HanaDispatchBillReader

        reader = HanaDispatchBillReader.__new__(HanaDispatchBillReader)
        reader.connection = mock.Mock(schema="JIVO_OIL_HANADB")
        reader._table_columns = lambda table: set(columns if table == "OINV" else line_columns)
        answers = iter([header, lines])
        reader._execute = mock.Mock(side_effect=lambda query, params: next(answers))
        return reader

    def test_reads_header_and_lines(self):
        from datetime import datetime

        reader = self._reader(
            [(626090634, datetime(2026, 9, 29), "N", datetime(2026, 10, 8), 181907)],
            [(0, "FG0000461", 1800, 1800), (2, "FG0000143", 120, None)],
        )
        state = reader.invoice_receive_state(55)
        self.assertEqual(state["doc_date"], date(2026, 9, 29))
        self.assertEqual(state["received_date"], date(2026, 10, 8))
        self.assertEqual(state["attachment_entry"], 181907)
        self.assertFalse(state["cancelled"])
        self.assertEqual(
            [(l["line_num"], l["item_code"], l["quantity"], l["received"]) for l in state["lines"]],
            [(0, "FG0000461", Decimal("1800"), Decimal("1800")),
             (2, "FG0000143", Decimal("120"), None)],
        )

    def test_no_such_invoice_is_none(self):
        self.assertIsNone(self._reader([], []).invoice_receive_state(55))

    def test_a_company_without_the_fields_says_so(self):
        from sap_client.exceptions import SAPDataError

        with self.assertRaises(SAPDataError):
            self._reader([], [], columns=()).invoice_receive_state(55)
