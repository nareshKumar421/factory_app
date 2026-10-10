"""Posting a fixed-asset GRPO: the switch, simulate, the live guard, the queue.

What is guarded, in the order the handler applies it (spec D8/D9):

* switched off: the endpoint answers 400 ``FA_GRPO_DISABLED``; a queued row waits;
* simulate: every check runs (SAP is read), **no writer is called**, and the
  GRPO is recorded as a simulated post that blocks a real one until discarded;
* the live guard: a real send to a non-TEST_ company is refused, the draft is
  FAILED, and SAP is not called;
* a real send is the engine's, with one warehouse per document and the gate's
  invoice photos when nothing was uploaded; the document matches spec 2.6.5.

SAP is a fake (``grpo.services.SAPClient``); the read-back finds nothing
(``grpo.services.HanaConnection``); the live guard reads a patched
``sap_purchasing.live_guard.CompanyContext``.

    manage.py test fixed_asset_grpo.tests_grpo --settings=config.sqlite_test_settings
"""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, override_settings
from rest_framework.test import APIClient

from driver_management.models import VehicleEntry
from gate_core.enums import GateEntryStatus
from grpo.models import GRPOAttachment, GRPOPosting, GRPOStatus, SAPAttachmentStatus
from grpo.services import GRPOService
from grpo.tests_sap_queue import _hana
from sap_client.exceptions import SAPUnavailable, SAPValidationError
from sap_postings import services as sap_postings
from sap_postings.models import SapPosting, SapPostingOutcome, SapPostingStatus

from . import fa_test_support as fx
from .constants import FA_GRPO_GROUP, KIND
from .models import FixedAssetGRPOClosure, FixedAssetGRPORecord
from .sap_posting import FixedAssetGRPOHandler, draft_link, posted_link
from .services import FixedAssetGRPOService

URL = "/api/v1/fixed-asset-grpo/post/"
NOTIFY = "fixed_asset_grpo.notifications.NotificationService.send_notification_by_auth_group"


@override_settings(**fx.FA_SETTINGS)
class FAPostTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        fx.build_fa_world(cls)
        fx.gate_photo(cls.entry, "invoice.jpg")
        fx.gate_photo(cls.entry, "asset.jpg")
        fx.gate_photo(cls.entry, "wrong.jpg", active=False)
        cls.poster = fx.make_user("poster@example.com", "can_post_fixed_asset_grpo", company=cls.company)
        cls.rm_poster = fx.make_user(
            "rm@example.com", "grpo.add_grpoposting", "grpo.can_preview_grpo", company=cls.company
        )

    def setUp(self):
        self.sap = fx.fake_sap()
        self.hana_down = False
        for target, value in (
            ("grpo.services.SAPClient", MagicMock(return_value=self.sap)),
            ("grpo.services.CompanyContext", MagicMock()),
            ("grpo.services.HanaConnection",
             MagicMock(side_effect=lambda *a, **k: _hana([], self.hana_down))),
            ("sap_purchasing.live_guard.CompanyContext", fx.company_context("TEST_JIVO_OIL")),
            ("grpo.signals.notify_material_grpo_posted", MagicMock()),
        ):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.api = APIClient()
        self.api.force_authenticate(self.poster)
        self.addCleanup(self._delete_files)

    def _delete_files(self):
        for att in GRPOAttachment.objects.all():
            att.file.delete(save=False)

    def press_post(self, payload=None, files=None, user=None):
        if user is not None:
            self.api.force_authenticate(user)
        data = {"data": json.dumps(payload or fx.grpo_payload(self))}
        if files:
            data["attachments"] = files
        return self.api.post(URL, data, format="multipart", HTTP_COMPANY_CODE=fx.COMPANY)

    @staticmethod
    def bill(name="bill.pdf"):
        return SimpleUploadedFile(name, b"pdf", content_type="application/pdf")

    # -- doors -------------------------------------------------------------------

    @override_settings(FIXED_ASSET_GRPO_ENABLED=False)
    def test_switched_off_is_a_400_with_its_code(self):
        response = self.press_post()

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "FA_GRPO_DISABLED")
        self.assertFalse(GRPOPosting.objects.filter(vehicle_entry=self.entry).exists())

    def test_grpo_s_own_post_right_does_not_post_a_fixed_asset(self):
        self.assertEqual(self.press_post(user=self.rm_poster).status_code, 403)
        self.sap.create_grpo.assert_not_called()

    def test_a_closed_entry_is_refused(self):
        FixedAssetGRPOClosure.objects.create(vehicle_entry=self.entry, note="keyed by HO")
        response = self.press_post()

        self.assertEqual(response.status_code, 400)
        self.assertIn("done in SAP by hand", response.json()["detail"])

    @override_settings(FIXED_ASSET_GRPO_CUTOVER="2999-01-01")
    def test_an_entry_before_the_cutover_is_refused(self):
        response = self.press_post()

        self.assertEqual(response.status_code, 400)
        self.assertIn("cut-over", response.json()["detail"])

    def test_a_future_posting_date_is_refused(self):
        response = self.press_post(fx.grpo_payload(self, doc_date="2999-01-01"))
        self.assertEqual(response.status_code, 400)

    def test_lines_in_two_warehouses_are_refused(self):
        fx.add_line(self.po_receipt, line=1, warehouse="DL-FA")
        response = self.press_post()

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "FA_GRPO_MIXED_WAREHOUSES")
        self.assertIn("one warehouse at a time", response.json()["detail"])

    def test_a_zero_quantity_line_in_another_warehouse_does_not_count(self):
        other = fx.add_line(self.po_receipt, line=1, warehouse="DL-FA")
        payload = fx.grpo_payload(self)
        payload["items"].append({"po_item_receipt_id": other.id, "accepted_qty": "0"})

        self.assertEqual(self.press_post(payload).status_code, 201)
        lines = self.sap.create_grpo.call_args[0][0]["DocumentLines"]
        self.assertEqual([line["WarehouseCode"] for line in lines], ["BH-FA"])

    # -- a real post ----------------------------------------------------------------

    def test_a_real_post_sends_the_spec_document(self):
        response = self.press_post(files=[self.bill()])

        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()
        self.assertFalse(body["simulated"])
        self.assertEqual(body["sap_doc_num"], 30001)
        document = self.sap.create_grpo.call_args[0][0]
        self.assertEqual(document["CardCode"], fx.SUPPLIER)
        self.assertEqual(document["BPL_IDAssignedToInvoice"], 2)
        self.assertIn("Gate Entry: GE-FA-0001", document["Comments"])
        self.assertIn(f"PO: {fx.PO_NUMBER}", document["Comments"])
        self.assertEqual(
            (document["DocDate"], document["DocDueDate"], document["TaxDate"]),
            ("2026-10-03", "2026-10-03", "2026-10-03"),
        )
        self.assertEqual(document["NumAtCard"], "ABD/26-27/814")
        self.assertEqual(document["AttachmentEntry"], 4567)
        self.assertNotIn("Series", document)
        self.assertEqual(document["DocumentLines"], [{
            "ItemCode": "CG0000073", "Quantity": "1.000",
            "BaseEntry": fx.PO_DOC_ENTRY, "BaseLine": 0, "BaseType": 22,
            "WarehouseCode": "BH-FA", "UnitPrice": 1270.0, "TaxCode": "GST18",
            "AccountCode": "1204023", "U_Variety": "CANOLA", "CostingCode": "CANOLA",
        }])
        posting = GRPOPosting.objects.get(status=GRPOStatus.POSTED)
        self.assertFalse(posting.fa_record.simulated)
        self.assertEqual(posting.fa_record.posted_by, self.poster)
        queue_row = SapPosting.objects.get()
        self.assertEqual((queue_row.kind, queue_row.status), (KIND, SapPostingStatus.POSTED))
        self.assertEqual(queue_row.link, f"/warehouse/grpo/fixed-asset/history/{posting.id}")

    def test_no_upload_sends_the_gate_s_active_photos(self):
        response = self.press_post()

        self.assertEqual(response.status_code, 201, response.content)
        posting = GRPOPosting.objects.get(status=GRPOStatus.POSTED)
        self.assertEqual(posting.attachments.count(), 2)
        # The engine uploads each file as its own Attachments2 entry and links
        # the last one (docs/README.md, "Attachments").
        self.assertEqual(self.sap.upload_attachment.call_count, 2)

    def test_no_file_anywhere_is_a_400(self):
        bare = fx.make_entry(self, "GE-FA-0003")
        receipt = fx.link_po(bare, po_number="221026777")
        payload = fx.grpo_payload(
            self, vehicle_entry_id=bare.id, po_receipt_ids=[receipt.id],
            items=[{"po_item_receipt_id": receipt.items.get().id, "accepted_qty": "1.000"}],
        )
        response = self.press_post(payload)

        self.assertEqual(response.status_code, 400)
        self.assertIn("no photo either", response.json()["detail"])

    def test_sap_down_waits_and_a_second_press_is_refused(self):
        self.sap.create_grpo.side_effect = SAPUnavailable("Service Layer timeout")
        first = self.press_post()

        self.assertEqual(first.status_code, 202, first.content)
        self.assertEqual(first.json()["code"], "SAP_QUEUED")
        self.assertEqual(GRPOPosting.objects.get().status, GRPOStatus.QUEUED)
        second = self.press_post()
        self.assertEqual(second.status_code, 409)
        self.assertEqual(GRPOPosting.objects.count(), 1)

    def test_a_wait_cancelled_in_sap_postings_can_be_sent_again(self):
        # Cancelling the queue row leaves the draft QUEUED; it must not stay
        # "on its way" for good.
        self.sap.create_grpo.side_effect = SAPUnavailable("Service Layer timeout")
        self.assertEqual(self.press_post().status_code, 202)
        draft = GRPOPosting.objects.get()
        row = SapPosting.objects.get()
        sap_postings.cancel(row.pk, self.poster, "checking it by hand first")
        draft.refresh_from_db()
        self.assertEqual(draft.status, GRPOStatus.QUEUED)

        self.sap.create_grpo.side_effect = None
        again = self.press_post(fx.grpo_payload(self, grpo_posting_id=draft.id))

        self.assertEqual(again.status_code, 201, again.content)
        self.assertEqual(GRPOPosting.objects.count(), 1)
        self.assertEqual(GRPOPosting.objects.get().status, GRPOStatus.POSTED)

    def test_a_draft_the_queue_still_holds_is_not_re_used(self):
        self.sap.create_grpo.side_effect = SAPUnavailable("Service Layer timeout")
        self.assertEqual(self.press_post().status_code, 202)
        draft = GRPOPosting.objects.get()
        # A request naming another PO of the entry, so grpo_on_its_way (which
        # looks by PO) does not see the waiting draft; the re-use check must.
        second = fx.link_po(self.entry, po_number="221026888")
        payload = fx.grpo_payload(
            self, po_receipt_ids=[second.id], grpo_posting_id=draft.id,
            items=[{"po_item_receipt_id": second.items.get().id, "accepted_qty": "1.000"}],
        )

        response = self.press_post(payload)

        self.assertEqual(response.status_code, 409, response.content)
        self.assertEqual(response.json()["code"], "SAP_QUEUED")
        draft.refresh_from_db()
        self.assertEqual(draft.status, GRPOStatus.QUEUED)

    def test_a_retry_reuses_the_failed_draft(self):
        self.sap.create_grpo.side_effect = SAPValidationError("200019 - attachment missing")
        refused = self.press_post()
        self.assertEqual(refused.status_code, 400, refused.content)
        draft = GRPOPosting.objects.get()
        self.assertEqual(draft.status, GRPOStatus.FAILED)

        self.sap.create_grpo.side_effect = None
        retried = self.press_post(fx.grpo_payload(self, grpo_posting_id=draft.id))

        self.assertEqual(retried.status_code, 201, retried.content)
        self.assertEqual(GRPOPosting.objects.count(), 1)
        self.assertEqual(GRPOPosting.objects.get().status, GRPOStatus.POSTED)

    # -- the live guard -----------------------------------------------------------

    def test_the_live_guard_refuses_live_sap_and_calls_nothing(self):
        with patch("sap_purchasing.live_guard.CompanyContext", fx.company_context("JIVO_OIL")):
            response = self.press_post()

        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("FIXED_ASSET_GRPO_ALLOW_LIVE", response.json()["detail"])
        draft = GRPOPosting.objects.get()
        self.assertEqual(draft.status, GRPOStatus.FAILED)
        self.sap.create_grpo.assert_not_called()
        self.sap.upload_attachment.assert_not_called()

    @override_settings(FIXED_ASSET_GRPO_ALLOW_LIVE=True)
    def test_allow_live_lets_a_live_post_through(self):
        with patch("sap_purchasing.live_guard.CompanyContext", fx.company_context("JIVO_OIL")):
            response = self.press_post()
        self.assertEqual(response.status_code, 201, response.content)

    # -- simulate ---------------------------------------------------------------

    @override_settings(FIXED_ASSET_GRPO_SIMULATE=True)
    def test_simulate_calls_no_writer_and_records_what_it_would_send(self):
        with patch("sap_purchasing.live_guard.CompanyContext", fx.company_context("JIVO_OIL")):
            response = self.press_post()

        self.assertEqual(response.status_code, 201, response.content)
        self.assertTrue(response.json()["simulated"])
        self.sap.create_grpo.assert_not_called()
        self.sap.upload_attachment.assert_not_called()
        posting = GRPOPosting.objects.get()
        self.assertEqual(posting.status, GRPOStatus.POSTED)
        self.assertIsNone(posting.sap_doc_entry)
        self.assertEqual(posting.lines.count(), 1)
        self.assertEqual(
            set(posting.attachments.values_list("sap_attachment_status", flat=True)),
            {SAPAttachmentStatus.PENDING},
        )
        record = FixedAssetGRPORecord.objects.get(grpo_posting=posting)
        self.assertTrue(record.simulated)
        line = record.sap_payload["document"]["DocumentLines"][0]
        self.assertEqual((line["WarehouseCode"], line["BaseEntry"]), ("BH-FA", fx.PO_DOC_ENTRY))
        # Saved with update(): grpo's signal never told the "grpo" group.
        from grpo import signals
        signals.notify_material_grpo_posted.assert_not_called()

    @override_settings(FIXED_ASSET_GRPO_SIMULATE=True)
    def test_simulate_still_refuses_what_sap_would(self):
        VehicleEntry.objects.filter(id=self.entry.id).update(status=GateEntryStatus.IN_PROGRESS)
        response = self.press_post()

        self.assertEqual(response.status_code, 400)
        self.assertIn("not completed", response.json()["detail"])
        self.assertEqual(GRPOPosting.objects.get().status, GRPOStatus.FAILED)

    @override_settings(FIXED_ASSET_GRPO_SIMULATE=True)
    def test_simulate_reads_sap_s_item_master(self):
        self.sap.batch_managed_flags.return_value = {"CG0000073": True}
        response = self.press_post()

        self.assertEqual(response.status_code, 400)
        self.assertIn("batch-managed", response.json()["detail"])
        self.sap.create_grpo.assert_not_called()

    def test_simulate_then_discard_then_a_real_post(self):
        with override_settings(FIXED_ASSET_GRPO_SIMULATE=True):
            simulated = self.press_post()
        self.assertEqual(simulated.status_code, 201, simulated.content)
        simulated_id = simulated.json()["grpo_posting_id"]

        refused = self.press_post()
        self.assertEqual(refused.status_code, 400)
        self.assertIn("A simulated GRPO exists for PO", refused.json()["detail"])
        self.sap.create_grpo.assert_not_called()
        failed_draft = GRPOPosting.objects.get(status=GRPOStatus.FAILED)
        # The simulated post does not supersede the real attempt it refused.
        summary = self.api.get("/api/v1/fixed-asset-grpo/summary/", HTTP_COMPANY_CODE=fx.COMPANY)
        self.assertEqual(summary.json()["failed_count"], 1)
        history = self.api.get(
            "/api/v1/fixed-asset-grpo/history/", {"vehicle_entry_id": self.entry.id},
            HTTP_COMPANY_CODE=fx.COMPANY,
        )
        row = next(r for r in history.json()["results"] if r["id"] == failed_draft.id)
        self.assertFalse(row["is_superseded"])

        discarded = self.api.post(
            f"/api/v1/fixed-asset-grpo/history/{simulated_id}/discard-simulated/",
            HTTP_COMPANY_CODE=fx.COMPANY,
        )
        self.assertEqual(discarded.status_code, 204, discarded.content)
        self.assertFalse(GRPOPosting.objects.filter(id=simulated_id).exists())
        self.po_item.refresh_from_db()
        self.assertEqual(self.po_item.accepted_qty, self.po_item.received_qty)

        real = self.press_post(fx.grpo_payload(self, grpo_posting_id=failed_draft.id))
        self.assertEqual(real.status_code, 201, real.content)
        self.assertFalse(real.json()["simulated"])
        self.sap.create_grpo.assert_called_once()

    def test_discard_restores_zero_lines_and_cancels_the_queue_row(self):
        other = fx.add_line(self.po_receipt, line=1, warehouse="DL-FA")
        payload = fx.grpo_payload(self)
        payload["items"].append({"po_item_receipt_id": other.id, "accepted_qty": "0"})
        with override_settings(FIXED_ASSET_GRPO_SIMULATE=True):
            simulated = self.press_post(payload)
        self.assertEqual(simulated.status_code, 201, simulated.content)
        other.refresh_from_db()
        self.assertEqual(other.accepted_qty, 0)
        queue_row = SapPosting.objects.get()
        self.assertEqual(queue_row.status, SapPostingStatus.POSTED)
        self.assertTrue(queue_row.result["simulated"])

        discarded = self.api.post(
            f"/api/v1/fixed-asset-grpo/history/{simulated.json()['grpo_posting_id']}"
            f"/discard-simulated/",
            HTTP_COMPANY_CODE=fx.COMPANY,
        )

        self.assertEqual(discarded.status_code, 204, discarded.content)
        other.refresh_from_db()
        self.assertEqual((other.accepted_qty, other.rejected_qty), (other.received_qty, 0))
        queue_row.refresh_from_db()
        self.assertEqual(queue_row.status, SapPostingStatus.CANCELLED)
        self.assertEqual(queue_row.link, "")
        self.assertTrue(queue_row.result["discarded"])
        self.assertIn("Simulated post discarded", queue_row.cancel_reason)

    def test_a_real_post_cannot_be_discarded_as_simulated(self):
        self.press_post()
        posting = GRPOPosting.objects.get(status=GRPOStatus.POSTED)
        response = self.api.post(
            f"/api/v1/fixed-asset-grpo/history/{posting.id}/discard-simulated/",
            HTTP_COMPANY_CODE=fx.COMPANY,
        )
        self.assertEqual(response.status_code, 400)

    # -- notifications --------------------------------------------------------------

    def test_a_real_post_tells_the_fixed_asset_group(self):
        with patch(NOTIFY) as send, self.captureOnCommitCallbacks(execute=True):
            self.press_post()

        posting = GRPOPosting.objects.get(status=GRPOStatus.POSTED)
        ours = [c for c in send.call_args_list if c.kwargs.get("group_name") == FA_GRPO_GROUP]
        self.assertEqual(len(ours), 1)
        self.assertEqual(
            ours[0].kwargs["click_action_url"], f"/warehouse/grpo/fixed-asset/history/{posting.id}"
        )

    @override_settings(FIXED_ASSET_GRPO_SIMULATE=True)
    def test_a_simulated_post_tells_nobody(self):
        with patch(NOTIFY) as send, self.captureOnCommitCallbacks(execute=True):
            self.press_post()
        self.assertFalse(
            [c for c in send.call_args_list if c.kwargs.get("group_name") == FA_GRPO_GROUP]
        )

    def test_a_refusal_tells_the_group_it_failed(self):
        self.sap.create_grpo.side_effect = SAPValidationError("200019 - attachment missing")
        with patch(NOTIFY) as send, self.captureOnCommitCallbacks(execute=True):
            self.press_post()
        ours = [c for c in send.call_args_list if c.kwargs.get("group_name") == FA_GRPO_GROUP]
        self.assertEqual(len(ours), 1)
        self.assertIn("200019", ours[0].kwargs["body"])


@override_settings(**fx.FA_SETTINGS)
class FAQueueHandlerTests(TestCase):
    """The queue handler on its own: what the worker and Retry run."""

    @classmethod
    def setUpTestData(cls):
        fx.build_fa_world(cls)
        fx.gate_photo(cls.entry)
        cls.poster = fx.make_user("poster@example.com", "can_post_fixed_asset_grpo", company=cls.company)

    def setUp(self):
        self.sap = fx.fake_sap()
        for target, value in (
            ("grpo.services.SAPClient", MagicMock(return_value=self.sap)),
            ("grpo.services.CompanyContext", MagicMock()),
            ("grpo.services.HanaConnection", MagicMock(side_effect=lambda *a, **k: _hana([]))),
            ("sap_purchasing.live_guard.CompanyContext", fx.company_context("TEST_JIVO_OIL")),
        ):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(lambda: [a.file.delete(save=False) for a in GRPOAttachment.objects.all()])

    def queued(self, entry=None, receipt=None, item=None):
        entry, receipt, item = entry or self.entry, receipt or self.po_receipt, item or self.po_item
        service = GRPOService(fx.COMPANY, entry_type=entry.entry_type)
        payload = fx.grpo_payload(
            self, vehicle_entry_id=entry.id, po_receipt_ids=[receipt.id],
            items=[{"po_item_receipt_id": item.id, "accepted_qty": "1.000"}],
            warehouse_code="BH-FA",
        )
        draft = service.save_grpo_draft(
            vehicle_entry_id=entry.id, po_receipt_ids=[receipt.id], user=self.poster,
            request_payload=payload,
            attachments=[SimpleUploadedFile("bill.pdf", b"pdf", content_type="application/pdf")],
        )
        row = sap_postings.queue(
            kind=KIND, company=self.company, source_id=draft.id, title="t", user=None,
        )
        return draft, row

    def test_the_handler_is_registered_for_its_kind(self):
        self.assertEqual(
            sap_postings.HANDLERS[KIND], "fixed_asset_grpo.sap_posting.FixedAssetGRPOHandler"
        )

    @override_settings(FIXED_ASSET_GRPO_ENABLED=False)
    def test_switched_off_waits_and_sends_nothing(self):
        draft, row = self.queued()
        outcome = FixedAssetGRPOHandler().send(row)

        self.assertEqual(outcome.kind, SapPostingOutcome.WAITING)
        draft.refresh_from_db()
        self.assertEqual(draft.status, GRPOStatus.QUEUED)
        self.sap.create_grpo.assert_not_called()

    def test_the_worker_posts_as_the_person_who_saved_it(self):
        draft, row = self.queued()
        with patch.object(FixedAssetGRPOService, "__init__", autospec=True,
                          side_effect=FixedAssetGRPOService.__init__) as built:
            outcome = FixedAssetGRPOHandler().send(row)

        self.assertEqual(outcome.kind, SapPostingOutcome.POSTED, outcome.message)
        self.assertEqual(built.call_args.args[1], fx.COMPANY)
        posting = outcome.context["posting"]
        self.assertEqual(posting.posted_by, self.poster)
        self.assertIn(self.poster.email, self.sap.create_grpo.call_args[0][0]["Comments"])

    def test_a_raw_material_draft_is_refused(self):
        rm_entry = fx.make_entry(self, "GE-RM-0002", entry_type="RAW_MATERIAL")
        receipt = fx.link_po(rm_entry, po_number="221026555")
        draft, row = self.queued(rm_entry, receipt, receipt.items.get())

        outcome = FixedAssetGRPOHandler().send(row)
        self.assertEqual(outcome.kind, SapPostingOutcome.REJECTED)
        self.sap.create_grpo.assert_not_called()

    def test_a_grpo_sap_already_has_is_adopted_not_posted_twice(self):
        # The first try timed out after SAP had committed it: the read-back
        # finds the "Gate Entry" stamp on exactly this PO.
        draft, row = self.queued()
        found = [(9001, 30077, 1270.0, fx.PO_DOC_ENTRY)]
        with patch("grpo.services.HanaConnection",
                   MagicMock(side_effect=lambda *a, **k: _hana(found))),                 patch(NOTIFY) as send, self.captureOnCommitCallbacks(execute=True):
            outcome = FixedAssetGRPOHandler().send(row)

        self.assertEqual(outcome.kind, SapPostingOutcome.POSTED, outcome.message)
        self.assertIn("already in SAP", outcome.message)
        self.sap.create_grpo.assert_not_called()
        self.sap.upload_attachment.assert_not_called()
        draft.refresh_from_db()
        self.assertEqual((draft.status, draft.sap_doc_num), (GRPOStatus.POSTED, 30077))
        self.assertFalse(draft.fa_record.simulated)
        ours = [c for c in send.call_args_list if c.kwargs.get("group_name") == FA_GRPO_GROUP]
        self.assertEqual(len(ours), 1)

    def test_hana_down_on_the_read_back_waits(self):
        draft, row = self.queued()
        with patch("grpo.services.HanaConnection",
                   MagicMock(side_effect=lambda *a, **k: _hana([], down=True))):
            outcome = FixedAssetGRPOHandler().send(row)

        self.assertEqual(outcome.kind, SapPostingOutcome.WAITING)
        self.sap.create_grpo.assert_not_called()


class FALinkTests(SimpleTestCase):
    def test_links_point_at_the_fixed_asset_screens(self):
        draft = SimpleNamespace(id=7, vehicle_entry_id=3)
        self.assertEqual(draft_link(draft), "/warehouse/grpo/fixed-asset/preview/3?draft=7")
        self.assertEqual(posted_link(7), "/warehouse/grpo/fixed-asset/history/7")
