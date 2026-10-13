"""Sending and the escape hatches (spec §2.3.8): send-next, stop, re-check,
leave out, re-date, reset, and accepting a bunch that changed.

Run: ``python manage.py test cash_sap.tests_services --settings=config.sqlite_test_settings``
"""

from datetime import date
from unittest.mock import MagicMock, patch

from django.test import override_settings
from rest_framework.exceptions import ValidationError

from cash_book.models import CashEntry
from cash_sap import services
from cash_sap.models import (
    BunchSapState,
    CashBranchSapRouting,
    ImprestAPDraft,
    ImprestBatch,
    ImprestGRPO,
    ImprestVoucher,
    SapDocStatus,
)
from cash_sap.support_for_tests import REAL_FLAGS, SIMULATE_FLAGS, CashSapFixture, patch_guard
from sap_postings import services as sap_postings
from sap_postings.models import SapPosting, SapPostingStatus


@override_settings(**SIMULATE_FLAGS)
class SimulatedPostingTests(CashSapFixture):
    def test_post_then_send_next_until_everything_is_posted(self):
        a, b = self.voucher("350.00"), self.voucher("150.00")
        bunch = self.bunch(a, b)
        result = services.post_bunch(user=self.poster, bunch=bunch)
        self.assertEqual(result["sent"]["kind"], "cash_sap.po")
        for _ in range(5):
            if not result["remaining"]:
                break
            result = services.send_next(user=self.poster, bunch=bunch)
        batch = ImprestBatch.objects.get(bunch=bunch)
        self.assertEqual(batch.po_status, SapDocStatus.POSTED)
        self.assertTrue(all(g.status == SapDocStatus.POSTED for g in ImprestGRPO.objects.filter(batch=batch)))
        self.assertEqual(services.bunch_status_summary(bunch)["state"], "SIMULATED")

    def test_disabled_is_refused_with_its_code(self):
        bunch = self.bunch(self.voucher())
        with override_settings(CASH_SAP_ENABLED=False):
            with self.assertRaises(services.CashSapDisabled) as caught:
                services.post_bunch(user=self.poster, bunch=bunch)
        self.assertEqual(caught.exception.detail["code"], "CASH_SAP_DISABLED")

    def test_send_next_ignores_another_bunchs_posting_with_the_same_source_id(self):
        bunch = self.bunch(self.voucher())
        [batch] = services.freeze_bunch(user=self.poster, bunch=bunch)
        # A GRPO elsewhere whose id equals this batch's id: different kind, same number.
        SapPosting.objects.create(company=self.company, kind="cash_sap.grpo", source_id=batch.id,
                                  title="other", status=SapPostingStatus.SENDING)
        result = services.send_next(user=self.poster, bunch=bunch)
        self.assertEqual(result["sent"]["kind"], "cash_sap.po")
        self.assertEqual(result["sent"]["source_id"], batch.id)

    def test_a_refused_unit_is_retried_on_the_same_posting(self):
        bunch = self.bunch(self.voucher())
        [batch] = services.freeze_bunch(user=self.poster, bunch=bunch)
        refused = SapPosting.objects.create(company=self.company, kind="cash_sap.po", source_id=batch.id,
                                            title="PO", status=SapPostingStatus.REJECTED)
        result = services.send_next(user=self.poster, bunch=bunch)
        self.assertEqual(result["sent"]["sap_posting_id"], refused.pk)

    def test_a_cancelled_unit_gets_a_fresh_posting(self):
        bunch = self.bunch(self.voucher())
        [batch] = services.freeze_bunch(user=self.poster, bunch=bunch)
        cancelled = SapPosting.objects.create(company=self.company, kind="cash_sap.po", source_id=batch.id,
                                              title="PO", status=SapPostingStatus.CANCELLED)
        result = services.send_next(user=self.poster, bunch=bunch)
        self.assertNotEqual(result["sent"]["sap_posting_id"], cancelled.pk)


@override_settings(**SIMULATE_FLAGS)
class StopAndResetTests(CashSapFixture):
    def _posted_po(self, *entries):
        bunch = self.bunch(*entries)
        [batch] = services.freeze_bunch(user=self.poster, bunch=bunch)
        sap_postings.queue(kind="cash_sap.po", company=self.company, source_id=batch.id, title="PO",
                           user=self.poster)
        return bunch, batch

    def test_stop_cancels_and_marks_failed(self):
        bunch, batch = self._posted_po(self.voucher())
        result = services.stop_bunch(user=self.poster, bunch=bunch, reason="Keyed by hand")
        self.assertEqual(result["stopped"], 1)
        batch.refresh_from_db()
        self.assertEqual(batch.po_status, SapDocStatus.FAILED)
        self.assertIn("Keyed by hand", batch.po_error)
        self.assertEqual(SapPosting.objects.get(kind="cash_sap.po").status, SapPostingStatus.CANCELLED)

    def test_a_cancel_from_the_posting_log_shows_as_stopped(self):
        bunch, batch = self._posted_po(self.voucher())
        sap_postings.cancel(SapPosting.objects.get(kind="cash_sap.po").pk, self.poster, "not wanted")
        plan = services.build_plan(bunch)
        self.assertTrue(plan.group("JIVO_OIL").po.stopped)
        self.assertEqual(plan.group("JIVO_OIL").po.label, "Stopped")

    def test_reset_cancels_first_then_deletes(self):
        bunch, batch = self._posted_po(self.voucher())
        services.reset_bunch(user=self.poster, bunch=bunch)
        self.assertFalse(ImprestBatch.objects.filter(bunch=bunch).exists())
        self.assertEqual(SapPosting.objects.get(kind="cash_sap.po").status, SapPostingStatus.CANCELLED)
        self.assertFalse(BunchSapState.objects.get(bunch=bunch).is_frozen)
        self.assertIsNone(ImprestVoucher.objects.get(entry__bunch=bunch).batch_id)

    def test_reset_is_refused_once_sap_has_the_po(self):
        bunch, batch = self._posted_po(self.voucher())
        ImprestBatch.objects.filter(pk=batch.pk).update(po_doc_entry=1, po_status=SapDocStatus.POSTED)
        with self.assertRaises(ValidationError):
            services.reset_bunch(user=self.poster, bunch=bunch)


@override_settings(**SIMULATE_FLAGS)
class LeaveOutAndRedateTests(CashSapFixture):
    def _po_in_sap(self, *entries, grouping="NONE"):
        CashBranchSapRouting.objects.filter(pk=self.oil_routing.pk).update(grpo_grouping=grouping)
        bunch = self.bunch(*entries)
        [batch] = services.freeze_bunch(user=self.poster, bunch=bunch)
        ImprestBatch.objects.filter(pk=batch.pk).update(po_status=SapDocStatus.POSTED, po_doc_entry=14221,
                                                        po_doc_num=220926157, simulated=False)
        batch.refresh_from_db()
        return bunch, batch

    def test_leave_out_recomputes_amount_and_reference(self):
        a = self.voucher("350.00", bill_date=date(2026, 9, 24), gate_entry_no="773")
        b = self.voucher("150.00", bill_date=date(2026, 9, 24), gate_entry_no="773")
        bunch, batch = self._po_in_sap(a, b, grouping="GATE_NO")
        grpo = ImprestGRPO.objects.get(batch=batch)
        self.assertEqual(grpo.num_at_card, "500/24-09-26")
        result = services.leave_out_after_po(user=self.poster, bunch=bunch, entry=b, reason="Bill lost")
        grpo.refresh_from_db()
        self.assertEqual(grpo.amount, 350)
        self.assertEqual(grpo.num_at_card, "350/24-09-26")
        self.assertTrue(ImprestVoucher.objects.get(entry=b).left_out_after_po)
        self.assertIn("stays open", result["notes"][0])

    def test_redate_refuses_a_date_before_the_po_and_keeps_the_reference(self):
        e = self.voucher("500.00", bill_date=date(2026, 9, 24))
        bunch, batch = self._po_in_sap(e)
        grpo = ImprestGRPO.objects.get(batch=batch)
        with self.assertRaises(ValidationError):
            services.redate_grpo(user=self.poster, bunch=bunch, grpo=grpo, posting_date=date(2026, 8, 31))
        services.redate_grpo(user=self.poster, bunch=bunch, grpo=grpo, posting_date=date(2026, 10, 1))
        grpo.refresh_from_db()
        self.assertEqual(grpo.posting_date, date(2026, 10, 1))
        self.assertEqual(grpo.num_at_card, "500/24-09-26")


@override_settings(**REAL_FLAGS)
class RecheckTests(CashSapFixture):
    def setUp(self):
        super().setUp()
        patch_guard(self, test=True)
        self.client_mock = MagicMock()
        patcher = patch("sap_purchasing.client.PurchasingClient", MagicMock(return_value=self.client_mock))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_cancelled_po_without_grpos_is_raised_again_with_round_two(self):
        bunch = self.bunch(self.voucher())
        [batch] = services.freeze_bunch(user=self.poster, bunch=bunch)
        ImprestBatch.objects.filter(pk=batch.pk).update(po_status=SapDocStatus.POSTED, po_doc_entry=5, po_doc_num=9)
        self.client_mock.document_state.return_value = {"doc_num": 9, "canceled": "Y", "doc_status": "C"}
        services.recheck_bunch(user=self.poster, bunch=bunch)
        batch.refresh_from_db()
        self.assertEqual((batch.po_status, batch.po_round, batch.po_doc_entry), (SapDocStatus.PLANNED, 2, None))
        self.assertTrue(batch.marker.endswith("-2]"))

    def test_cancelled_grpo_fails_with_round_two(self):
        bunch = self.bunch(self.voucher())
        [batch] = services.freeze_bunch(user=self.poster, bunch=bunch)
        ImprestBatch.objects.filter(pk=batch.pk).update(po_status=SapDocStatus.POSTED, po_doc_entry=5, po_doc_num=9)
        ImprestGRPO.objects.filter(batch=batch).update(status=SapDocStatus.POSTED, grpo_doc_entry=7, grpo_doc_num=8,
                                                       sap_attachment_entry=99)
        self.client_mock.document_state.side_effect = [
            {"doc_num": 9, "canceled": "N", "doc_status": "O"},
            {"doc_num": 8, "canceled": "Y", "doc_status": "C"},
        ]
        services.recheck_bunch(user=self.poster, bunch=bunch)
        grpo = ImprestGRPO.objects.get(batch=batch)
        self.assertEqual((grpo.status, grpo.round, grpo.sap_attachment_entry), (SapDocStatus.FAILED, 2, 99))


@override_settings(**SIMULATE_FLAGS)
class AcceptChangesTests(CashSapFixture):
    def test_a_voucher_taken_out_before_the_po_is_dropped(self):
        a, b = self.voucher("350.00"), self.voucher("150.00")
        bunch = self.bunch(a, b)
        services.freeze_bunch(user=self.poster, bunch=bunch)
        CashEntry.objects.filter(pk=b.pk).update(bunch=None)
        with self.assertRaises(services.BunchChanged):
            services.send_next(user=self.poster, bunch=bunch)
        services.accept_bunch_changes(user=self.poster, bunch=bunch, note="Taken out by mistake, kept out")
        self.assertIsNone(ImprestVoucher.objects.get(entry=b).batch_id)
        self.assertEqual(BunchSapState.objects.get(bunch=bunch).status, "FROZEN")
        services.send_next(user=self.poster, bunch=bunch)  # no longer refused

    def test_a_voucher_whose_grpo_is_in_sap_cannot_leave(self):
        a = self.voucher("350.00")
        bunch = self.bunch(a)
        [batch] = services.freeze_bunch(user=self.poster, bunch=bunch)
        ImprestBatch.objects.filter(pk=batch.pk).update(po_status=SapDocStatus.POSTED, po_doc_entry=1)
        ImprestGRPO.objects.filter(batch=batch).update(status=SapDocStatus.POSTED, grpo_doc_entry=2)
        CashEntry.objects.filter(pk=a.pk).update(bunch=None)
        with self.assertRaises(ValidationError):
            services.accept_bunch_changes(user=self.poster, bunch=bunch, note="x")


@override_settings(**SIMULATE_FLAGS)
class APDraftQueueTests(CashSapFixture):
    def test_item_draft_excludes_a_left_out_voucher(self):
        CashBranchSapRouting.objects.filter(pk=self.oil_routing.pk).update(ap_drafts_enabled=True)
        a, b = self.voucher("350.00"), self.voucher("150.00")
        bunch = self.bunch(a, b)
        [batch] = services.freeze_bunch(user=self.poster, bunch=bunch)
        ImprestBatch.objects.filter(pk=batch.pk).update(po_status=SapDocStatus.POSTED, po_doc_entry=1, po_doc_num=3)
        services.leave_out_after_po(user=self.poster, bunch=bunch, entry=b, reason="Lost")
        ImprestGRPO.objects.filter(batch=batch, status=SapDocStatus.PLANNED).update(
            status=SapDocStatus.POSTED, grpo_doc_entry=10, grpo_doc_num=11)
        batch.refresh_from_db()
        self.assertTrue(services.maybe_queue_item_ap_draft(batch))
        from cash_sap.sap_posting import ImprestAPDraftHandler
        from types import SimpleNamespace

        draft = ImprestAPDraft.objects.get(batch=batch, kind="ITEM")
        ImprestAPDraftHandler().send(SimpleNamespace(source_id=draft.id))
        draft.refresh_from_db()
        self.assertEqual(draft.amount, 350)


# ---------------------------------------------------------------------------
# Review fixes: a voucher moved between bunches, a GRPO being sent, re-queued
# GRPOs, stale snapshots, late service vouchers, cancel races.
# ---------------------------------------------------------------------------


def _codes(problems):
    return {p.code for p in problems}


@override_settings(**SIMULATE_FLAGS)
class MovedToAnotherBunchTests(CashSapFixture):
    def test_a_voucher_frozen_under_bunch_a_is_blocked_in_bunch_b(self):
        a, b = self.voucher("350.00"), self.voucher("150.00")
        bunch_a = self.bunch(a, b)
        [batch] = services.freeze_bunch(user=self.poster, bunch=bunch_a)
        ImprestBatch.objects.filter(pk=batch.pk).update(po_status=SapDocStatus.POSTED, po_doc_entry=1, po_doc_num=3)

        bunch_b = self.bunch(b)  # the cash book moves b to a new bunch
        b.refresh_from_db()
        plan = services.build_plan(bunch_b)
        self.assertIn("IN_OTHER_BUNCH", _codes(plan.problems))
        [vp] = plan.unrouted
        self.assertEqual(vp.entry_id, b.id)
        self.assertIn("IN_OTHER_BUNCH", _codes(vp.problems))
        self.assertFalse(plan.can_post)

        with self.assertRaises(ValidationError):
            services.freeze_bunch(user=self.poster, bunch=bunch_b)
        with self.assertRaises(ValidationError):
            services.update_voucher_override(user=self.poster, bunch=bunch_b, entry=b,
                                             data={"treatment": "SKIP"})
        iv = ImprestVoucher.objects.get(entry=b)
        self.assertEqual(iv.batch_id, batch.id)  # never re-homed
        self.assertEqual(iv.bunch_id, bunch_a.pk)

    def test_a_row_that_is_not_frozen_still_starts_clean_in_the_new_bunch(self):
        a, b = self.voucher("350.00"), self.voucher("150.00")
        bunch_a = self.bunch(a, b)
        services.freeze_bunch(user=self.poster, bunch=bunch_a)  # PO only PLANNED
        bunch_b = self.bunch(b)
        plan = services.build_plan(bunch_b)
        self.assertNotIn("IN_OTHER_BUNCH", _codes(plan.problems))
        [batch_b] = services.freeze_bunch(user=self.poster, bunch=bunch_b)
        self.assertEqual(ImprestVoucher.objects.get(entry=b).batch_id, batch_b.id)


@override_settings(**SIMULATE_FLAGS)
class GrpoBeingSentTests(CashSapFixture):
    def _po_in_sap(self, *entries):
        bunch = self.bunch(*entries)
        [batch] = services.freeze_bunch(user=self.poster, bunch=bunch)
        ImprestBatch.objects.filter(pk=batch.pk).update(po_status=SapDocStatus.POSTED, po_doc_entry=14221,
                                                        po_doc_num=220926157, simulated=False)
        batch.refresh_from_db()
        return bunch, batch

    def test_leave_out_is_refused_while_the_grpo_is_being_sent(self):
        a = self.voucher("350.00")
        bunch, batch = self._po_in_sap(a)
        grpo = ImprestGRPO.objects.get(batch=batch)
        SapPosting.objects.create(company=self.company, kind="cash_sap.grpo", source_id=grpo.id,
                                  title="GRPO", status=SapPostingStatus.SENDING)
        with self.assertRaises(ValidationError):
            services.leave_out_after_po(user=self.poster, bunch=bunch, entry=a, reason="Bill lost")
        self.assertFalse(ImprestVoucher.objects.get(entry=a).left_out_after_po)

    def test_leave_out_answers_400_when_the_cancel_loses_the_race(self):
        a = self.voucher("350.00")
        bunch, batch = self._po_in_sap(a)
        grpo = ImprestGRPO.objects.get(batch=batch)
        SapPosting.objects.create(company=self.company, kind="cash_sap.grpo", source_id=grpo.id,
                                  title="GRPO", status=SapPostingStatus.QUEUED)
        with patch("cash_sap.services.sap_postings.cancel", side_effect=ValueError("claimed")):
            with self.assertRaises(ValidationError):
                services.leave_out_after_po(user=self.poster, bunch=bunch, entry=a, reason="Bill lost")
        self.assertFalse(ImprestVoucher.objects.get(entry=a).left_out_after_po)

    def test_accepting_a_removal_is_refused_while_its_grpo_is_being_sent(self):
        a, b = self.voucher("350.00"), self.voucher("150.00")
        bunch, batch = self._po_in_sap(a, b)
        grpo_b = ImprestVoucher.objects.get(entry=b).grpo
        SapPosting.objects.create(company=self.company, kind="cash_sap.grpo", source_id=grpo_b.id,
                                  title="GRPO", status=SapPostingStatus.SENDING)
        CashEntry.objects.filter(pk=b.pk).update(bunch=None)
        with self.assertRaises(ValidationError):
            services.accept_bunch_changes(user=self.poster, bunch=bunch, note="moved")
        self.assertFalse(ImprestVoucher.objects.get(entry=b).left_out_after_po)


@override_settings(**SIMULATE_FLAGS)
class RequeueGrpoTests(CashSapFixture):
    def _grpo_with_po_in_sap(self):
        bunch = self.bunch(self.voucher())
        [batch] = services.freeze_bunch(user=self.poster, bunch=bunch)
        ImprestBatch.objects.filter(pk=batch.pk).update(po_status=SapDocStatus.POSTED, po_doc_entry=1)
        batch.refresh_from_db()
        return batch, ImprestGRPO.objects.get(batch=batch)

    def test_a_grpo_refused_for_its_po_is_queued_again(self):
        batch, grpo = self._grpo_with_po_in_sap()
        refused = SapPosting.objects.create(company=self.company, kind="cash_sap.grpo", source_id=grpo.id,
                                            title="GRPO", status=SapPostingStatus.REJECTED)
        self.assertEqual(services.queue_grpo_units(batch), 1)
        refused.refresh_from_db()
        self.assertEqual(refused.status, SapPostingStatus.CANCELLED)
        self.assertEqual(
            SapPosting.objects.filter(kind="cash_sap.grpo", status=SapPostingStatus.QUEUED).count(), 1)
        self.assertEqual(services.queue_grpo_units(batch), 0)  # already waiting

    def test_a_grpo_a_person_cancelled_is_left_alone(self):
        batch, grpo = self._grpo_with_po_in_sap()
        SapPosting.objects.create(company=self.company, kind="cash_sap.grpo", source_id=grpo.id,
                                  title="GRPO", status=SapPostingStatus.CANCELLED)
        self.assertEqual(services.queue_grpo_units(batch), 0)


@override_settings(**SIMULATE_FLAGS)
class StaleSnapshotTests(CashSapFixture):
    def test_a_refused_po_is_frozen_again_before_it_is_resent(self):
        from types import SimpleNamespace

        from cash_sap.sap_posting import ImprestPOHandler

        a = self.voucher("350.00")
        bunch = self.bunch(a)
        a.refresh_from_db()
        [batch] = services.freeze_bunch(user=self.poster, bunch=bunch)
        ImprestBatch.objects.filter(pk=batch.pk).update(po_status=SapDocStatus.FAILED, po_error="Closed period")
        SapPosting.objects.create(company=self.company, kind="cash_sap.po", source_id=batch.id,
                                  title="PO", status=SapPostingStatus.REJECTED)
        services.update_voucher_override(user=self.poster, bunch=bunch, entry=a, data={"item_code": "CG0000099"})

        group = services.build_plan(bunch).group("JIVO_OIL")
        self.assertIn("CHANGED_SINCE_FREEZE", _codes(group.problems))
        [change] = services.stale_snapshot(batch, "ITEM")
        self.assertIn("item_code", change)

        # A Retry that bypasses send-next (the sap_postings log) is refused.
        outcome = ImprestPOHandler().send(SimpleNamespace(source_id=batch.id, pk=None))
        self.assertIn("Changed since it was frozen", outcome.message)
        batch.refresh_from_db()
        self.assertEqual(batch.po_status, SapDocStatus.FAILED)

        # A Retry through send-next freezes again first, then sends the new item.
        result = services.send_next(user=self.poster, bunch=bunch, include_failed=True)
        self.assertEqual(result["sent"]["kind"], "cash_sap.po")
        self.assertEqual(result["sent"]["outcome"], "posted")
        self.assertEqual(ImprestVoucher.objects.get(entry=a).item_code, "CG0000099")
        batch.refresh_from_db()
        self.assertIn("CG0000099", str(batch.po_payload))

    def test_a_current_snapshot_is_not_frozen_again(self):
        bunch = self.bunch(self.voucher())
        [batch] = services.freeze_bunch(user=self.poster, bunch=bunch)
        self.assertEqual(services.stale_snapshot(batch, "ITEM"), [])
        with patch("cash_sap.services.freeze_bunch") as refreeze:
            services.send_next(user=self.poster, bunch=bunch)
        refreeze.assert_not_called()


@override_settings(**SIMULATE_FLAGS)
class LateServiceVoucherTests(CashSapFixture):
    def test_a_voucher_turned_service_after_the_service_draft_is_blocked(self):
        CashBranchSapRouting.objects.filter(pk=self.oil_routing.pk).update(ap_drafts_enabled=True)
        a = self.voucher("90.00", gl="5690002", gl_name="CONVEYANCE")
        b = self.voucher("350.00")
        bunch = self.bunch(a, b)
        b.refresh_from_db()
        [batch] = services.freeze_bunch(user=self.poster, bunch=bunch)
        ImprestAPDraft.objects.filter(batch=batch, kind="SERVICE").update(
            status=SapDocStatus.POSTED, sap_draft_entry=55)
        services.update_voucher_override(user=self.poster, bunch=bunch, entry=b, data={"treatment": "SERVICE"})

        group = services.build_plan(bunch).group("JIVO_OIL")
        vp = next(v for v in group.vouchers if v.entry_id == b.id)
        self.assertIn("NOT_ON_AP_DRAFT", _codes(vp.problems))
        with self.assertRaises(ValidationError):
            services.freeze_bunch(user=self.poster, bunch=bunch)


@override_settings(**SIMULATE_FLAGS)
class CancelRaceTests(CashSapFixture):
    def _queued_po(self):
        bunch = self.bunch(self.voucher())
        [batch] = services.freeze_bunch(user=self.poster, bunch=bunch)
        sap_postings.queue(kind="cash_sap.po", company=self.company, source_id=batch.id, title="PO",
                           user=self.poster)
        return bunch, batch

    def test_stop_reports_a_posting_claimed_mid_cancel_as_sending(self):
        bunch, _ = self._queued_po()
        with patch("cash_sap.services.sap_postings.cancel", side_effect=ValueError("claimed")):
            result = services.stop_bunch(user=self.poster, bunch=bunch, reason="Keyed by hand")
        self.assertEqual(result["stopped"], 0)
        self.assertEqual(result["sending"], ["Imprest PO"])

    def test_reset_answers_400_when_a_cancel_loses_the_race(self):
        bunch, batch = self._queued_po()
        with patch("cash_sap.services.sap_postings.cancel", side_effect=ValueError("claimed")):
            with self.assertRaises(ValidationError):
                services.reset_bunch(user=self.poster, bunch=bunch)
        self.assertTrue(ImprestBatch.objects.filter(pk=batch.pk).exists())
