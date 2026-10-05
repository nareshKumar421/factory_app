"""A bill summary's invoice stamp through the SAP posting queue.

The warehouse's approval stands whatever SAP does. SAP answering is stamped at
once; SAP not answering leaves the sheet "Waiting for SAP" for the worker -- and
the rest of that approval are queued without being tried, so a hung Service
Layer costs the approver one timeout, not one per bill.
"""

from datetime import timedelta
from unittest.mock import MagicMock, patch

import requests
from django.utils import timezone
from hdbcli import dbapi

from dispatch_plans import tests_bill_summary as base
from dispatch_plans.bill_summary_service import BillSummaryService
from dispatch_plans.models_bill_summary import (
    BillSummary,
    BillSummarySapStatus,
    BillSummaryStatus,
)
from sap_client.exceptions import SAPDataError, SAPUnavailable
from sap_postings import services as sap_postings
from sap_postings.models import SapPosting, SapPostingStatus

SAP_DOWN = SAPUnavailable("SAP Service Layer did not answer the login: timed out")


class QueueTestCase(base.BillSummaryTestBase):
    def raise_sheets(self, *entries):
        with self.stub():
            return [
                self.generate(sap_invoice_doc_entry=entry, sap_invoice_doc_num=f"6260{entry}")
                for entry in entries
            ]

    def approve_all(self, sheets, *, sap):
        """Approve as the warehouse does, with the on-commit stamping run."""
        with self.stub(), patch.object(BillSummaryService, "_patch_invoice", **sap) as stamp:
            with self.captureOnCommitCallbacks(execute=True):
                approved, refused = self.service.approve(
                    [sheet.id for sheet in sheets], base.DISPATCH_DATE
                )
        return approved, refused, stamp

    def run_worker(self, **sap):
        SapPosting.objects.filter(status=SapPostingStatus.QUEUED).update(
            next_attempt_at=timezone.now() - timedelta(seconds=1)
        )
        with self.stub(), patch.object(BillSummaryService, "_patch_invoice", **sap), \
             patch("sap_client.health.failing_fast", return_value=False), \
             patch("notifications.services.NotificationService.send_notification_to_user") as told:
            sent = sap_postings.run_due()
        return sent, told


class ApprovalStampTests(QueueTestCase):
    def test_sap_answering_is_stamped_at_once_and_logged(self):
        sheet, = self.raise_sheets(5101)

        self.approve_all([sheet], sap={"return_value": ([], [])})

        sheet.refresh_from_db()
        self.assertEqual(sheet.sap_status, BillSummarySapStatus.POSTED)
        posting = SapPosting.objects.get()
        self.assertEqual((posting.kind, posting.status), ("bill_summary.stamp", SapPostingStatus.POSTED))
        self.assertEqual(posting.link, f"/warehouse/bill-summaries/{sheet.pk}")

    def test_sap_down_still_approves_and_waits(self):
        sheet, = self.raise_sheets(5101)

        _, refused, _ = self.approve_all([sheet], sap={"side_effect": SAP_DOWN})

        self.assertEqual(refused, [])
        sheet.refresh_from_db()
        self.assertEqual(sheet.status, BillSummaryStatus.APPROVED)
        self.assertEqual(sheet.sap_status, BillSummarySapStatus.WAITING)
        self.assertIn("did not answer", sheet.sap_error)
        self.assertEqual(SapPosting.objects.get().status, SapPostingStatus.QUEUED)

    def test_after_one_wait_the_rest_of_the_approval_is_not_tried(self):
        sheets = self.raise_sheets(5101, 5102, 5103)

        _, _, stamp = self.approve_all(sheets, sap={"side_effect": SAP_DOWN})

        self.assertEqual(stamp.call_count, 1)
        self.assertEqual(
            set(BillSummary.objects.values_list("sap_status", flat=True)),
            {BillSummarySapStatus.WAITING},
        )
        self.assertEqual(
            list(SapPosting.objects.values_list("status", flat=True)),
            [SapPostingStatus.QUEUED] * 3,
        )

    def test_the_worker_stamps_them_once_sap_is_back_and_says_so(self):
        sheets = self.raise_sheets(5101, 5102)
        self.approve_all(sheets, sap={"side_effect": SAP_DOWN})

        sent, told = self.run_worker(return_value=([], []))

        self.assertEqual(sent, 2)
        self.assertEqual(
            set(BillSummary.objects.values_list("sap_status", flat=True)),
            {BillSummarySapStatus.POSTED},
        )
        self.assertEqual(told.call_count, 2)
        self.assertTrue(told.call_args.kwargs["title"].startswith("Posted to SAP: Bill summary"))

    def test_a_refusal_is_kept_for_a_person(self):
        sheet, = self.raise_sheets(5101)

        self.approve_all([sheet], sap={"side_effect": SAPDataError("(1300012) Please update the dispatch qty")})

        sheet.refresh_from_db()
        self.assertEqual(sheet.sap_status, BillSummarySapStatus.FAILED)
        self.assertIn("1300012", sheet.sap_error)
        self.assertEqual(SapPosting.objects.get().status, SapPostingStatus.REJECTED)

    def test_hana_down_for_the_read_back_is_a_wait(self):
        sheet, = self.raise_sheets(5101)
        hana_down = SAPDataError("Failed to retrieve dispatch bills from SAP. Please try again.")
        hana_down.__cause__ = dbapi.OperationalError(-10807, "Connection lost")

        self.approve_all([sheet], sap={"side_effect": hana_down})

        sheet.refresh_from_db()
        self.assertEqual(sheet.sap_status, BillSummarySapStatus.WAITING)

    def test_stamp_again_from_the_sheet_uses_the_queue(self):
        sheet, = self.raise_sheets(5101)
        self.approve_all([sheet], sap={"side_effect": SAP_DOWN})

        with self.stub(), patch.object(BillSummaryService, "_patch_invoice", return_value=([], [])):
            updated = self.service.post_to_sap(sheet.id)

        self.assertEqual(updated.sap_status, BillSummarySapStatus.POSTED)
        posting = SapPosting.objects.get()
        self.assertEqual((posting.status, posting.attempts), (SapPostingStatus.POSTED, 2))

    def test_cancelling_a_stamped_sheet_clears_it_through_the_queue(self):
        sheet, = self.raise_sheets(5101)
        self.approve_all([sheet], sap={"return_value": ([], [])})

        with self.stub(), patch.object(BillSummaryService, "_patch_invoice", return_value=([], [])) as stamp:
            with self.captureOnCommitCallbacks(execute=True):
                self.service.cancel(sheet.id, "Truck changed")

        self.assertEqual(stamp.call_args.kwargs, {"clear": True})
        sheet.refresh_from_db()
        self.assertEqual(sheet.sap_status, BillSummarySapStatus.NOT_POSTED)


class _StampReader(base._Reader):
    def dispatch_stamp_columns(self):
        return dict(base.OIL_COLUMNS)

    def invoice_dispatch_stamp(self, doc_entry):
        return dict(base.EMPTY_STAMP)

    def dispatch_stamp_sizes(self):
        return {}


class NotAnsweringIsToldFromRefusingTests(QueueTestCase):
    """``_patch_invoice`` itself: what counts as SAP not answering."""

    def setUp(self):
        super().setUp()
        with self.stub():
            self.sheet = self.approved()
        reader = _StampReader([base.sap_line()])
        patcher = patch.object(
            BillSummaryService, "reader", new_callable=lambda: property(lambda self: reader)
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def session(self, *, login=None, patch_response=None, patch_error=None):
        session = MagicMock()
        session.post.return_value = login or MagicMock(status_code=200)
        if patch_error:
            session.patch.side_effect = patch_error
        else:
            session.patch.return_value = patch_response
        return patch("dispatch_plans.bill_summary_service.requests.Session", return_value=session)

    def test_no_answer_to_the_login_is_sap_unavailable(self):
        with self.session() as factory:
            factory.return_value.post.side_effect = requests.ConnectTimeout("timed out")
            with self.assertRaises(SAPUnavailable):
                self.service._patch_invoice(self.sheet)

    def test_a_gateway_error_is_sap_unavailable(self):
        with self.session(patch_response=MagicMock(status_code=503)):
            with self.assertRaises(SAPUnavailable):
                self.service._patch_invoice(self.sheet)

    def test_a_timed_out_patch_is_a_wait_not_a_refusal(self):
        with self.session(patch_error=requests.ReadTimeout("read timed out")):
            with self.assertRaises(SAPUnavailable):
                self.service._patch_invoice(self.sheet)

    def test_sap_refusing_is_still_a_refusal(self):
        refused = MagicMock(status_code=400)
        refused.json.return_value = {"error": {"message": "(1300014) Dispatch date before bill date"}}
        with self.session(patch_response=refused):
            with self.assertRaises(SAPDataError):
                self.service._patch_invoice(self.sheet)

    def test_known_down_is_not_even_tried(self):
        import time

        down = {"status": "down", "since": time.time() - 60, "checked_at": time.time()}
        with self.session() as factory, \
             patch("sap_client.health.state_for_call", return_value=down), \
             patch("sap_client.health.failing_fast", return_value=True):
            with self.assertRaises(SAPUnavailable):
                self.service._patch_invoice(self.sheet)
        factory.return_value.post.assert_not_called()


class SheetDetailsWithSapDownTests(QueueTestCase):
    def test_the_last_sheets_gstin_and_legal_name_stand_in(self):
        with self.stub():
            self.generate()  # SAP answering: the real values are recorded
        reader = base._Reader([base.sap_line()])
        reader.branch_gstin = MagicMock(side_effect=SAPUnavailable("down"))
        reader.company_legal_name = MagicMock(side_effect=SAPUnavailable("down"))
        BillSummary.objects.update(status=BillSummaryStatus.CANCELLED)

        with patch.object(BillSummaryService, "reader",
                          new_callable=lambda: property(lambda self: reader)):
            sheet = self.generate()

        self.assertEqual(sheet.branch_gstin, "06AACCJ4223F1Z0")
        self.assertEqual(sheet.company_legal_name, "JIVO WELLNESS PVT LTD")
