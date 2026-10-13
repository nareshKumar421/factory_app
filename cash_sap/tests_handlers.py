"""The four posting handlers (spec §2.3.9), with SAP mocked.

The SAP client is patched at ``cash_sap.sap_posting.PurchasingClient`` and the
live guard at ``sap_purchasing.live_guard.CompanyContext``. Handlers are called
directly with a stand-in posting (``source_id`` is all they read), except the
worker test, which drives ``sap_postings.run_due``.

Run: ``python manage.py test cash_sap.tests_handlers --settings=config.sqlite_test_settings``
"""

from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from cash_book.models import CashEntryAttachment
from cash_sap import services
from cash_sap.models import (
    CashBranchSapRouting,
    ImprestAPDraft,
    ImprestBatch,
    ImprestGRPO,
    ImprestVoucher,
    SapDocStatus,
)
from cash_sap.sap_posting import (
    ImprestAPDraftHandler,
    ImprestGRPOHandler,
    ImprestPOCloseHandler,
    ImprestPOHandler,
)
from cash_sap.support_for_tests import REAL_FLAGS, SIMULATE_FLAGS, CashSapFixture, patch_guard
from sap_client.exceptions import SAPConnectionError, SAPDataError, SAPValidationError
from sap_postings.models import SapPosting, SapPostingOutcome, SapPostingStatus


def _posting(source_id):
    return SimpleNamespace(source_id=source_id, pk=None)


def _client():
    client = MagicMock()
    client.find_document_by_marker.return_value = None
    client.find_document_by_draft_key.return_value = None
    client.find_draft_by_marker.return_value = None
    client.resolve_series_for_branch.return_value = {"series": 2466}
    client.create_purchase_order_document.return_value = {"DocEntry": 14221, "DocNum": 220926157, "DocTotal": 500.0}
    client.create_purchase_delivery_note_document.return_value = {"DocEntry": 27585, "DocNum": 2026096948, "DocTotal": 350.0}
    client.create_purchase_invoice_draft.return_value = {"DocEntry": 777}
    client.document_lines.return_value = []
    client.document_state.return_value = {"doc_num": 220926157, "canceled": "N", "doc_status": "O"}
    client.num_at_card_taken.return_value = False
    client.upload_attachment.return_value = {"AbsoluteEntry": 98765}
    client.add_line_to_existing_attachment.return_value = {}
    return client


class HandlerFixture(CashSapFixture):
    def setUp(self):
        super().setUp()
        self.client_mock = _client()
        patcher = patch("cash_sap.sap_posting.PurchasingClient", MagicMock(return_value=self.client_mock))
        self.client_class = patcher.start()
        self.addCleanup(patcher.stop)

    def frozen(self, *entries, real=True):
        patch_guard(self, test=True)
        flags = REAL_FLAGS if real else SIMULATE_FLAGS
        with override_settings(**flags):
            bunch = self.bunch(*entries)
            [batch] = services.freeze_bunch(user=self.poster, bunch=bunch)
        return bunch, batch

    def po_in_sap(self, batch, entry=14221, num=220926157):
        ImprestBatch.objects.filter(pk=batch.pk).update(
            po_status=SapDocStatus.POSTED, po_doc_entry=entry, po_doc_num=num)
        batch.refresh_from_db()


@override_settings(**REAL_FLAGS)
class PreludeTests(HandlerFixture):
    def test_disabled_waits_and_calls_nothing(self):
        _, batch = self.frozen(self.voucher())
        with override_settings(CASH_SAP_ENABLED=False):
            outcome = ImprestPOHandler().send(_posting(batch.id))
        self.assertEqual(outcome.kind, SapPostingOutcome.WAITING)
        self.assertEqual(ImprestBatch.objects.get(pk=batch.pk).po_status, SapDocStatus.QUEUED)
        self.client_class.assert_not_called()

    def test_simulate_runs_before_the_guard(self):
        _, batch = self.frozen(self.voucher(), real=False)
        patch_guard(self, test=False)  # live SAP
        with override_settings(**SIMULATE_FLAGS):
            outcome = ImprestPOHandler().send(_posting(batch.id))
        batch.refresh_from_db()
        self.assertEqual(outcome.kind, SapPostingOutcome.POSTED)
        self.assertEqual(batch.po_status, SapDocStatus.POSTED)
        self.assertTrue(batch.simulated)
        self.assertNotIn("Series", batch.po_payload)
        self.client_class.assert_not_called()

    def test_live_target_without_simulate_is_refused(self):
        _, batch = self.frozen(self.voucher())
        patch_guard(self, test=False)
        outcome = ImprestPOHandler().send(_posting(batch.id))
        batch.refresh_from_db()
        self.assertEqual(outcome.kind, SapPostingOutcome.REJECTED)
        self.assertEqual(batch.po_status, SapDocStatus.FAILED)
        self.assertIn("CASH_SAP_ALLOW_LIVE", batch.po_error)
        self.client_mock.create_purchase_order_document.assert_not_called()

    def test_a_changed_bunch_sends_nothing(self):
        a, b = self.voucher(), self.voucher("10.00")
        _, batch = self.frozen(a, b)
        type(b).objects.filter(pk=b.pk).update(bunch=None)
        outcome = ImprestPOHandler().send(_posting(batch.id))
        self.assertEqual(outcome.kind, SapPostingOutcome.REJECTED)
        self.assertIn("changed in the cash book", outcome.message)
        self.client_class.assert_not_called()


@override_settings(**REAL_FLAGS)
class POHandlerTests(HandlerFixture):
    def test_read_back_adopts_and_does_not_post_twice(self):
        _, batch = self.frozen(self.voucher())
        self.client_mock.find_document_by_marker.return_value = {
            "doc_entry": 14221, "doc_num": 220926157, "doc_total": 350.0}
        outcome = ImprestPOHandler().send(_posting(batch.id))
        batch.refresh_from_db()
        self.assertEqual(outcome.kind, SapPostingOutcome.POSTED)
        self.assertEqual((batch.po_status, batch.po_doc_num), (SapDocStatus.POSTED, 220926157))
        self.client_mock.create_purchase_order_document.assert_not_called()

    def test_approval_draft_waiting(self):
        _, batch = self.frozen(self.voucher())
        self.client_mock.find_draft_by_marker.return_value = {"draft_entry": 55, "wdd_status": "W",
                                                             "outcome": "IN_APPROVAL"}
        outcome = ImprestPOHandler().send(_posting(batch.id))
        batch.refresh_from_db()
        self.assertEqual(outcome.kind, SapPostingOutcome.WAITING)
        self.assertEqual((batch.po_status, batch.po_draft_entry), (SapDocStatus.IN_APPROVAL, 55))

    def test_approved_draft_is_added_then_adopted(self):
        _, batch = self.frozen(self.voucher())
        self.client_mock.find_draft_by_marker.return_value = {"draft_entry": 55, "wdd_status": "Y",
                                                             "outcome": "APPROVED"}
        self.client_mock.find_document_by_marker.side_effect = [
            None, {"doc_entry": 14221, "doc_num": 220926157, "doc_total": 350.0}]
        outcome = ImprestPOHandler().send(_posting(batch.id))
        self.client_mock.save_purchasing_draft_to_document.assert_called_once_with("22", 55)
        self.assertEqual(outcome.kind, SapPostingOutcome.POSTED)
        self.assertEqual(ImprestBatch.objects.get(pk=batch.pk).po_doc_entry, 14221)

    def test_rejected_draft(self):
        _, batch = self.frozen(self.voucher())
        self.client_mock.find_draft_by_marker.return_value = {"draft_entry": 55, "wdd_status": "N",
                                                             "outcome": "REJECTED"}
        outcome = ImprestPOHandler().send(_posting(batch.id))
        self.assertEqual(outcome.kind, SapPostingOutcome.REJECTED)
        self.assertEqual(ImprestBatch.objects.get(pk=batch.pk).po_status, SapDocStatus.FAILED)

    def test_create_maps_lines_by_free_text_and_queues_grpos(self):
        a, b = self.voucher("350.00"), self.voucher("150.00")
        _, batch = self.frozen(a, b)
        self.client_mock.document_lines.return_value = [
            {"line_num": 0, "free_text": f"JI V{b.id}"},
            {"line_num": 1, "free_text": f"JI V{a.id}"},
        ]
        outcome = ImprestPOHandler().send(_posting(batch.id))
        self.assertEqual(outcome.kind, SapPostingOutcome.POSTED)
        payload = self.client_mock.create_purchase_order_document.call_args[0][0]
        self.assertEqual(payload["Series"], 2466)
        self.assertEqual(ImprestVoucher.objects.get(entry=a).po_line_num, 1)
        self.assertEqual(ImprestVoucher.objects.get(entry=b).po_line_num, 0)
        postings = SapPosting.objects.filter(kind="cash_sap.grpo")
        self.assertEqual(postings.count(), 2)
        self.assertTrue(all(p.created_by_id is None for p in postings))
        self.assertTrue(all(g.status == SapDocStatus.QUEUED for g in ImprestGRPO.objects.filter(batch=batch)))

    def test_pending_approval_on_create(self):
        _, batch = self.frozen(self.voucher())
        self.client_mock.create_purchase_order_document.return_value = {
            "DocEntry": None, "DocNum": "", "pending_approval": True, "draft_entry": 77}
        outcome = ImprestPOHandler().send(_posting(batch.id))
        self.assertEqual(outcome.kind, SapPostingOutcome.WAITING)
        self.assertEqual(ImprestBatch.objects.get(pk=batch.pk).po_draft_entry, 77)

    def test_errors_map_to_wait_or_reject(self):
        _, batch = self.frozen(self.voucher())
        self.client_mock.create_purchase_order_document.side_effect = SAPConnectionError("down")
        self.assertEqual(ImprestPOHandler().send(_posting(batch.id)).kind, SapPostingOutcome.WAITING)
        self.assertEqual(ImprestBatch.objects.get(pk=batch.pk).po_status, SapDocStatus.QUEUED)

        self.client_mock.create_purchase_order_document.side_effect = SAPValidationError("bad item")
        self.assertEqual(ImprestPOHandler().send(_posting(batch.id)).kind, SapPostingOutcome.REJECTED)
        self.assertEqual(ImprestBatch.objects.get(pk=batch.pk).po_status, SapDocStatus.FAILED)

        self.client_mock.create_purchase_order_document.side_effect = ValidationError({"detail": "nope"})
        outcome = ImprestPOHandler().send(_posting(batch.id))
        self.assertEqual(outcome.kind, SapPostingOutcome.REJECTED)
        self.assertEqual(outcome.message, "nope")

    def test_series_error_is_a_rejection_with_its_message(self):
        _, batch = self.frozen(self.voucher())
        self.client_mock.resolve_series_for_branch.side_effect = SAPDataError("No open SAP series for object 22")
        outcome = ImprestPOHandler().send(_posting(batch.id))
        self.assertEqual(outcome.kind, SapPostingOutcome.REJECTED)
        self.assertIn("No open SAP series", outcome.message)


@override_settings(**REAL_FLAGS)
class GRPOHandlerTests(HandlerFixture):
    def test_waits_for_the_po_and_is_refused_when_the_po_was(self):
        _, batch = self.frozen(self.voucher())
        grpo = ImprestGRPO.objects.get(batch=batch)
        ImprestBatch.objects.filter(pk=batch.pk).update(po_status=SapDocStatus.QUEUED)
        self.assertEqual(ImprestGRPOHandler().send(_posting(grpo.id)).kind, SapPostingOutcome.WAITING)
        ImprestBatch.objects.filter(pk=batch.pk).update(po_status=SapDocStatus.FAILED)
        self.assertEqual(ImprestGRPOHandler().send(_posting(grpo.id)).kind, SapPostingOutcome.REJECTED)
        self.client_mock.create_purchase_delivery_note_document.assert_not_called()

    def test_num_at_card_gets_a_dot_when_sap_has_it(self):
        _, batch = self.frozen(self.voucher("500.00", bill_date=date(2026, 9, 24)))
        self.po_in_sap(batch)
        grpo = ImprestGRPO.objects.get(batch=batch)
        self.client_mock.num_at_card_taken.side_effect = lambda obj, vendor, c: c == "500/24-09-26"
        outcome = ImprestGRPOHandler().send(_posting(grpo.id))
        self.assertEqual(outcome.kind, SapPostingOutcome.POSTED)
        payload = self.client_mock.create_purchase_delivery_note_document.call_args[0][0]
        self.assertEqual(payload["NumAtCard"], "500/24-09-26.")
        self.assertEqual(ImprestGRPO.objects.get(pk=grpo.pk).num_at_card, "500/24-09-26.")

    def test_attachment_entry_is_kept_after_the_first_upload(self):
        e = self.voucher()
        CashEntryAttachment.objects.create(
            entry=e, file=SimpleUploadedFile("second.jpg", b"jpeg"), original_filename="second.jpg", size_bytes=4)
        _, batch = self.frozen(e)
        self.po_in_sap(batch)
        grpo = ImprestGRPO.objects.get(batch=batch)
        self.client_mock.add_line_to_existing_attachment.side_effect = SAPConnectionError("dropped")
        self.assertEqual(ImprestGRPOHandler().send(_posting(grpo.id)).kind, SapPostingOutcome.WAITING)
        grpo.refresh_from_db()
        self.assertEqual(grpo.sap_attachment_entry, 98765)
        self.assertEqual(len(grpo.uploaded_files), 1)

        self.client_mock.add_line_to_existing_attachment.side_effect = None
        self.assertEqual(ImprestGRPOHandler().send(_posting(grpo.id)).kind, SapPostingOutcome.POSTED)
        self.assertEqual(self.client_mock.upload_attachment.call_count, 1)
        payload = self.client_mock.create_purchase_delivery_note_document.call_args[0][0]
        self.assertEqual(payload["AttachmentEntry"], 98765)

    def test_grouped_lines_are_based_on_each_po_line(self):
        CashBranchSapRouting.objects.filter(pk=self.oil_routing.pk).update(grpo_grouping="GATE_NO")
        a = self.voucher("350.00", bill_date=date(2026, 9, 24), gate_entry_no="773")
        b = self.voucher("150.00", bill_date=date(2026, 9, 24), gate_entry_no="773")
        _, batch = self.frozen(a, b)
        self.po_in_sap(batch)
        ImprestVoucher.objects.filter(entry=a).update(po_line_num=3)
        ImprestVoucher.objects.filter(entry=b).update(po_line_num=4)
        grpo = ImprestGRPO.objects.get(batch=batch)
        ImprestGRPOHandler().send(_posting(grpo.id))
        payload = self.client_mock.create_purchase_delivery_note_document.call_args[0][0]
        self.assertEqual([line["BaseLine"] for line in payload["DocumentLines"]], [3, 4])
        self.assertIn("\rG.no.773", payload["Comments"])

    def test_item_draft_waits_for_every_grpo_and_one_notification(self):
        CashBranchSapRouting.objects.filter(pk=self.oil_routing.pk).update(ap_drafts_enabled=True)
        a, b = self.voucher("350.00"), self.voucher("150.00")
        _, batch = self.frozen(a, b)
        self.po_in_sap(batch)
        first, second = ImprestGRPO.objects.filter(batch=batch).order_by("id")
        with patch("notifications.services.NotificationService.send_notification_to_user") as notify:
            ImprestGRPOHandler().send(_posting(first.id))
            self.assertFalse(SapPosting.objects.filter(kind="cash_sap.ap_draft").exists())
            self.client_mock.create_purchase_delivery_note_document.return_value = {
                "DocEntry": 27586, "DocNum": 2026096949, "DocTotal": 150.0}
            ImprestGRPOHandler().send(_posting(second.id))
        item_draft = ImprestAPDraft.objects.get(batch=batch, kind="ITEM")
        self.assertEqual(item_draft.status, SapDocStatus.QUEUED)
        self.assertTrue(SapPosting.objects.filter(kind="cash_sap.ap_draft", source_id=item_draft.id,
                                                  created_by__isnull=True).exists())
        self.assertEqual(notify.call_count, 1)


@override_settings(**REAL_FLAGS)
class APDraftHandlerTests(HandlerFixture):
    def _service_draft(self):
        CashBranchSapRouting.objects.filter(pk=self.oil_routing.pk).update(ap_drafts_enabled=True)
        e = self.voucher("800.00", gl="5690002", gl_name="CONVEYANCE", bill_date=date(2026, 9, 28))
        bunch = self.bunch(e)
        patch_guard(self, test=True)
        [batch] = services.freeze_bunch(user=self.poster, bunch=bunch)
        return ImprestAPDraft.objects.get(batch=batch, kind="SERVICE")

    def test_creates_a_service_draft(self):
        draft = self._service_draft()
        outcome = ImprestAPDraftHandler().send(_posting(draft.id))
        self.assertEqual(outcome.kind, SapPostingOutcome.POSTED)
        self.assertEqual(ImprestAPDraft.objects.get(pk=draft.pk).sap_draft_entry, 777)
        payload = self.client_mock.create_purchase_invoice_draft.call_args[0][0]
        self.assertEqual(payload["DocType"], "dDocument_Service")
        # SAP refuses a draft line without a budget head (1120009).
        self.assertEqual(payload["DocumentLines"][0]["CostingCode3"], "Factory")

    def test_adopts_from_odrf(self):
        draft = self._service_draft()
        self.client_mock.find_draft_by_marker.return_value = {"draft_entry": 31, "wdd_status": "-", "outcome": "OTHER"}
        ImprestAPDraftHandler().send(_posting(draft.id))
        self.assertEqual(ImprestAPDraft.objects.get(pk=draft.pk).sap_draft_entry, 31)
        self.client_mock.create_purchase_invoice_draft.assert_not_called()

    def test_adopts_what_head_office_added(self):
        draft = self._service_draft()
        self.client_mock.find_document_by_marker.return_value = {"doc_entry": 49640, "doc_num": 626093114}
        outcome = ImprestAPDraftHandler().send(_posting(draft.id))
        self.assertEqual(outcome.message, "A/P already added by head office")
        self.assertEqual(ImprestAPDraft.objects.get(pk=draft.pk).sap_doc_num, 626093114)

    def test_switched_off_live_sends_nothing(self):
        draft = self._service_draft()
        CashBranchSapRouting.objects.filter(pk=self.oil_routing.pk).update(ap_drafts_enabled=False)
        outcome = ImprestAPDraftHandler().send(_posting(draft.id))
        self.assertEqual(outcome.kind, SapPostingOutcome.REJECTED)
        self.client_class.assert_not_called()


@override_settings(**REAL_FLAGS)
class POCloseHandlerTests(HandlerFixture):
    def test_already_closed_or_closes(self):
        _, batch = self.frozen(self.voucher())
        self.po_in_sap(batch)
        ImprestBatch.objects.filter(pk=batch.pk).update(po_close_status=SapDocStatus.QUEUED)
        self.client_mock.document_state.return_value = {"doc_num": 1, "canceled": "N", "doc_status": "C"}
        self.assertEqual(ImprestPOCloseHandler().send(_posting(batch.id)).kind, SapPostingOutcome.POSTED)
        self.client_mock.close_purchase_order.assert_not_called()

        ImprestBatch.objects.filter(pk=batch.pk).update(po_close_status=SapDocStatus.QUEUED)
        self.client_mock.document_state.return_value = {"doc_num": 1, "canceled": "N", "doc_status": "O"}
        ImprestPOCloseHandler().send(_posting(batch.id))
        self.client_mock.close_purchase_order.assert_called_once_with(14221)


@override_settings(**REAL_FLAGS)
class WorkerTests(HandlerFixture):
    def test_run_due_sends_a_queued_grpo(self):
        from sap_postings import services as sap_postings

        _, batch = self.frozen(self.voucher())
        self.po_in_sap(batch)
        services.queue_grpo_units(batch)
        SapPosting.objects.filter(kind="cash_sap.grpo").update(next_attempt_at=timezone.now() - timedelta(minutes=1))
        with patch("sap_client.health.failing_fast", return_value=False), \
                patch("sap_client.health.state_for_call", return_value=None):
            sent = sap_postings.run_due()
        self.assertEqual(sent, 1)
        self.assertEqual(SapPosting.objects.get(kind="cash_sap.grpo").status, SapPostingStatus.POSTED)
        self.assertEqual(ImprestGRPO.objects.get(batch=batch).grpo_doc_num, 2026096948)
