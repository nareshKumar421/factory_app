"""A saved material GRPO through the SAP posting queue.

SAP not answering -- Service Layer or HANA -- leaves the GRPO waiting, not
failed, and nobody is told it failed; the worker posts it once SAP is back.
Before every try SAP is asked whether the app already posted this truck's GRPO,
so a retry after a timeout SAP committed anyway records that document instead.
"""

from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import Permission
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.utils import timezone
from hdbcli import dbapi
from rest_framework.test import APIClient

from company.models import UserCompany, UserRole
from grpo.models import GRPOPosting, GRPOStatus
from grpo.services import GRPOService
# The module, not the class: a TestCase named here would be collected and run again.
from grpo import tests as grpo_tests
from notifications.models import Notification
from raw_material_gatein.models import POReceipt
from sap_client.exceptions import SAPUnavailable, SAPValidationError
from sap_postings import services as sap_postings
from sap_postings.models import SapPosting, SapPostingStatus


class _Cursor:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, sql, params=None):
        self.params = params

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return None

    def close(self):
        pass


class _Connection:
    def __init__(self, rows):
        self.rows = rows

    def cursor(self):
        return _Cursor(self.rows)

    def close(self):
        pass


def _hana(rows=(), down=False):
    """What the already-posted check finds in SAP's OPDN/PDN1."""
    hana = MagicMock(schema="TEST_DB")
    if down:
        hana.connect.side_effect = dbapi.OperationalError(-10709, "Connection failed")
    else:
        hana.connect.return_value = _Connection(list(rows))
    return hana


class GRPOQueueTests(TestCase):
    # The fixtures of the existing GRPO tests, without running those tests again.
    setUpTestData = classmethod(grpo_tests.GRPOServiceTests.setUpTestData.__func__)
    _draft_payload = grpo_tests.GRPOServiceTests._draft_payload

    def setUp(self):
        self.sap = MagicMock()
        self.sap.upload_attachment.return_value = {"AbsoluteEntry": 789}
        self.sap.create_grpo.return_value = {"DocEntry": 123, "DocNum": 456, "DocTotal": 4750.00}
        grpo_tests._stub_sap_open_qtys(self.sap)
        self.found_in_sap = []
        self.hana_down = False
        for target, value in (
            ("grpo.services.SAPClient", MagicMock(return_value=self.sap)),
            ("grpo.services.CompanyContext", MagicMock()),
        ):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch(
            "grpo.services.HanaConnection",
            side_effect=lambda *a, **k: _hana(self.found_in_sap, self.hana_down),
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.draft = GRPOService(company_code="TC001").save_grpo_draft(
            vehicle_entry_id=self.vehicle_entry.id,
            po_receipt_ids=[self.po_receipt.id],
            user=self.user,
            request_payload=self._draft_payload(),
            attachments=[SimpleUploadedFile("invoice.pdf", b"pdf", content_type="application/pdf")],
        )
        self.addCleanup(self._delete_files)

    def _delete_files(self):
        for posting in GRPOPosting.objects.all():
            for att in posting.attachments.all():
                att.file.delete(save=False)

    def post(self):
        return sap_postings.post_now(
            kind="grpo.material", company=self.company, source_id=self.draft.id,
            title="GRPO VE-2024-001 (PO PO-001)", user=self.user,
        )

    # -- the outcomes -------------------------------------------------------

    def test_sap_up_posts_it_and_logs_the_posted_grpo(self):
        posting, outcome = self.post()

        posting.refresh_from_db()
        self.assertEqual(posting.status, SapPostingStatus.POSTED)
        posted = GRPOPosting.objects.get(status=GRPOStatus.POSTED)
        self.assertEqual(posting.result, {"doc_nums": ["456"], "grpo_posting_id": posted.id})
        self.assertEqual(posting.link, f"/warehouse/grpo/material/history/{posted.id}")
        self.assertFalse(GRPOPosting.objects.filter(id=self.draft.id).exists())

    def test_the_service_layer_down_leaves_it_waiting_and_tells_nobody_it_failed(self):
        self.sap.create_grpo.side_effect = SAPUnavailable("SAP Service Layer connection timeout")

        posting, outcome = self.post()

        posting.refresh_from_db()
        self.assertEqual(posting.status, SapPostingStatus.QUEUED)
        self.draft.refresh_from_db()
        self.assertEqual(self.draft.status, GRPOStatus.QUEUED)
        self.assertIn("could not be reached", self.draft.error_message)
        # The payload and the attachment wait with it.
        self.assertEqual(self.draft.attachments.count(), 1)
        self.assertTrue(self.draft.request_payload)
        self.assertFalse(Notification.objects.exists())

    def test_hana_down_waits_too_without_sending_anything(self):
        self.hana_down = True

        posting, outcome = self.post()

        posting.refresh_from_db()
        self.assertEqual(posting.status, SapPostingStatus.QUEUED)
        # Not knowing whether SAP has it already is not "it does not".
        self.sap.create_grpo.assert_not_called()
        self.draft.refresh_from_db()
        self.assertEqual(self.draft.status, GRPOStatus.QUEUED)

    def test_the_worker_posts_it_once_sap_is_back(self):
        self.sap.create_grpo.side_effect = SAPUnavailable("down")
        posting, _ = self.post()
        SapPosting.objects.filter(pk=posting.pk).update(
            next_attempt_at=timezone.now() - timedelta(seconds=1)
        )
        self.sap.create_grpo.side_effect = None

        with patch("sap_client.health.failing_fast", return_value=False), \
             patch("notifications.services.NotificationService.send_notification_to_user"):
            self.assertEqual(sap_postings.run_due(), 1)

        posting.refresh_from_db()
        self.assertEqual(posting.status, SapPostingStatus.POSTED)
        self.assertTrue(posting.attempt_log.get(number=2).by_worker)
        self.assertEqual(GRPOPosting.objects.get().status, GRPOStatus.POSTED)

    def test_a_grpo_sap_already_has_is_recorded_not_posted_twice(self):
        # The first try timed out after SAP had committed it.
        self.found_in_sap = [(9001, 2026096999, 4750.00, 12345)]

        posting, outcome = self.post()

        self.sap.create_grpo.assert_not_called()
        self.draft.refresh_from_db()
        self.assertEqual(self.draft.status, GRPOStatus.POSTED)
        self.assertEqual(self.draft.sap_doc_num, 2026096999)
        self.assertIn("rather than posted twice", self.draft.error_message)
        posting.refresh_from_db()
        self.assertEqual(posting.result["doc_nums"], ["2026096999"])

    def test_a_grpo_for_other_pos_of_the_same_truck_is_not_taken_for_this_one(self):
        self.found_in_sap = [(9002, 2026096998, 100.00, 99999)]

        self.post()

        self.sap.create_grpo.assert_called_once()

    def test_a_document_already_linked_to_another_posting_is_not_taken_again(self):
        # The same truck's other PO, posted and linked to that document already.
        other_po = POReceipt.objects.create(
            vehicle_entry=self.vehicle_entry, po_number="PO-002", supplier_code="SUP001",
            supplier_name="Test Supplier", sap_doc_entry=12346, branch_id=1,
        )
        GRPOPosting.objects.create(
            vehicle_entry=self.vehicle_entry, po_receipt=other_po,
            status=GRPOStatus.POSTED, sap_doc_entry=9001, sap_doc_num=2026096999,
        )
        self.found_in_sap = [(9001, 2026096999, 4750.00, 12345)]

        self.post()

        self.sap.create_grpo.assert_called_once()

    def test_a_refusal_stops_it_for_a_person(self):
        self.sap.create_grpo.side_effect = SAPValidationError("200032 - Gross weight is mandatory")

        posting, outcome = self.post()

        posting.refresh_from_db()
        self.assertEqual(posting.status, SapPostingStatus.REJECTED)
        self.draft.refresh_from_db()
        self.assertEqual(self.draft.status, GRPOStatus.FAILED)
        self.assertIn("200032", posting.last_error)


class GRPOQueueEndpointTests(GRPOQueueTests):
    """What the Post GRPO button gets back. Reuses the fixtures and patches above."""

    def setUp(self):
        super().setUp()
        UserCompany.objects.get_or_create(
            user=self.user, company=self.company,
            defaults={"role": UserRole.objects.create(name="Stores"), "is_active": True},
        )
        self.user.user_permissions.add(Permission.objects.get(codename="add_grpoposting"))
        self.api = APIClient()
        self.api.force_authenticate(self.user)

    def press_post(self):
        return self.api.post(
            f"/api/v1/grpo/draft/{self.draft.id}/post/", {}, format="json",
            HTTP_COMPANY_CODE="TC001",
        )

    def test_sap_down_is_accepted_and_waiting(self):
        self.sap.create_grpo.side_effect = SAPUnavailable("SAP Service Layer connection timeout")

        response = self.press_post()

        self.assertEqual(response.status_code, 202, response.content)
        body = response.json()
        self.assertEqual(body["code"], "SAP_QUEUED")
        self.assertEqual(body["grpo_posting_id"], self.draft.id)
        self.assertFalse(Notification.objects.exists())

    def test_sap_up_is_posted_as_before(self):
        response = self.press_post()

        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["sap_doc_num"], 456)

    def test_a_refusal_is_a_400_as_before(self):
        self.sap.create_grpo.side_effect = SAPValidationError("200032 - Gross weight is mandatory")

        response = self.press_post()

        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("200032", response.json()["detail"])

    # The queue-level tests are not repeated for the endpoint.
    test_sap_up_posts_it_and_logs_the_posted_grpo = None
    test_the_service_layer_down_leaves_it_waiting_and_tells_nobody_it_failed = None
    test_hana_down_waits_too_without_sending_anything = None
    test_the_worker_posts_it_once_sap_is_back = None
    test_a_grpo_sap_already_has_is_recorded_not_posted_twice = None
    test_a_grpo_for_other_pos_of_the_same_truck_is_not_taken_for_this_one = None
    test_a_document_already_linked_to_another_posting_is_not_taken_again = None
    test_a_refusal_stops_it_for_a_person = None
