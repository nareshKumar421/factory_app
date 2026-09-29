"""A short dispatch through the SAP posting queue.

SAP refusing it while the operator is at the form still withdraws the entry.
SAP not answering keeps it, "Waiting for SAP", and the worker posts it once SAP
is back -- reading SAP back by the entry's reference first, so a Return an
earlier try did post is recorded, not posted twice.
"""

from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import Permission
from django.utils import timezone
from rest_framework.test import APIClient

from company.models import UserCompany, UserRole
from sap_client.exceptions import SAPUnavailable
from sap_postings import services as sap_postings
from sap_postings.models import SapPosting, SapPostingStatus

from .models import ShortDispatch, ShortDispatchStatus
from .tests import WAREHOUSE, FakeWriter, ShortDispatchTestCase


class UnreachableWriter:
    """A Service Layer that is not answering."""

    def __init__(self, context=None):
        self.posted = []

    def create(self, payload):
        raise SAPUnavailable("SAP Service Layer connection timeout")


class QueueTestCase(ShortDispatchTestCase):
    LINE = [{"source_line_num": 0, "short_quantity": 5}]

    def create_while_sap_is_down(self, lines=None):
        return self.create(lines or self.LINE, writer=UnreachableWriter())

    def run_worker(self, writer=None):
        SapPosting.objects.filter(status=SapPostingStatus.QUEUED).update(
            next_attempt_at=timezone.now() - timedelta(seconds=1)
        )
        with mock.patch("sap_client.health.failing_fast", return_value=False), \
             mock.patch("notifications.services.NotificationService.send_notification_to_user"):
            return self.run_service(sap_postings.run_due, writer)


class WaitingForSAPTests(QueueTestCase):
    def test_sap_not_answering_keeps_the_entry_waiting(self):
        entry = self.create_while_sap_is_down()

        self.assertEqual(entry.status, ShortDispatchStatus.QUEUED)
        self.assertIsNone(entry.sap_return_doc_entry)
        self.assertIn("could not be reached", entry.sap_error)
        posting = SapPosting.objects.get()
        self.assertEqual(posting.status, SapPostingStatus.QUEUED)
        self.assertEqual(posting.link, f"/warehouse/short-dispatch/{entry.pk}")

    def test_the_worker_posts_it_once_sap_is_back(self):
        entry = self.create_while_sap_is_down()

        self.assertEqual(self.run_worker(FakeWriter(start=9400)), 1)

        entry.refresh_from_db()
        self.assertEqual(entry.status, ShortDispatchStatus.POSTED)
        self.assertEqual(entry.sap_return_doc_num, "179401")
        self.assertEqual(entry.sap_error, "")
        self.assertEqual(SapPosting.objects.get().status, SapPostingStatus.POSTED)

    def test_a_return_sap_already_has_is_recorded_not_posted_twice(self):
        entry = self.create_while_sap_is_down()
        # The first try timed out after SAP had taken it.
        self.sap.existing_returns[f"{entry.entry_no} INV 1500"] = {
            "doc_entry": 8888, "doc_num": "178888",
        }
        writer = FakeWriter()

        self.run_worker(writer)

        self.assertEqual(writer.posted, [])
        entry.refresh_from_db()
        self.assertEqual(entry.status, ShortDispatchStatus.POSTED)
        self.assertEqual(entry.sap_return_doc_num, "178888")

    def test_a_waiting_entry_still_counts_against_the_invoice(self):
        self.create_while_sap_is_down()  # 5 of the 100 billed, waiting

        with self.assertRaisesMessage(ValueError, "at most 95"):
            self.create([{"source_line_num": 0, "short_quantity": 96}])

    def test_a_refusal_from_the_worker_is_kept_and_frees_the_quantity(self):
        entry = self.create_while_sap_is_down()

        self.run_worker(FakeWriter(refuse=True))

        entry.refresh_from_db()
        self.assertEqual(entry.status, ShortDispatchStatus.REFUSED)
        self.assertIn("160021", entry.sap_error)
        self.assertEqual(SapPosting.objects.get().status, SapPostingStatus.REJECTED)
        # Nothing went back into the warehouse for it, so the invoice can take
        # the whole shortfall again.
        again = self.create([{"source_line_num": 0, "short_quantity": 100}])
        self.assertEqual(again.status, ShortDispatchStatus.POSTED)


class OnTheSpotRefusalTests(QueueTestCase):
    def test_a_refusal_at_the_form_withdraws_the_entry_and_keeps_the_log(self):
        with self.assertRaisesMessage(ValueError, "SAP rejected the return note"):
            self.create(self.LINE, writer=FakeWriter(refuse=True))

        self.assertFalse(ShortDispatch.objects.exists())
        posting = SapPosting.objects.get()
        self.assertEqual(posting.status, SapPostingStatus.CANCELLED)
        self.assertIn("withdrawn", posting.cancel_reason)
        self.assertIn("160021", posting.attempt_log.get().message)

    def test_sap_answering_posts_it_as_before(self):
        entry = self.create(self.LINE)

        self.assertEqual(entry.status, ShortDispatchStatus.POSTED)
        self.assertEqual(SapPosting.objects.get().status, SapPostingStatus.POSTED)


class EndpointTests(QueueTestCase):
    def setUp(self):
        super().setUp()
        UserCompany.objects.create(
            user=self.user, company=self.company, role=UserRole.objects.create(name="Stores"),
            is_active=True,
        )
        self.user.user_permissions.add(
            Permission.objects.get(codename="can_create_short_dispatch")
        )
        self.api = APIClient()
        self.api.force_authenticate(self.user)

    def press_post(self, writer):
        return self.run_service(
            lambda: self.api.post(
                "/api/v1/short-dispatch/",
                {"invoice_number": "1500", "warehouse_code": WAREHOUSE, "lines": self.LINE},
                format="json",
                HTTP_COMPANY_CODE="OIL",
            ),
            writer,
        )

    def test_sap_down_is_accepted_and_waiting(self):
        response = self.press_post(UnreachableWriter())

        self.assertEqual(response.status_code, 202, response.content)
        body = response.json()
        self.assertEqual(body["code"], "SAP_QUEUED")
        self.assertEqual(body["status"], "QUEUED")
        self.assertEqual(body["status_label"], "Waiting for SAP")

    def test_sap_up_is_created(self):
        response = self.press_post(FakeWriter())

        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["status"], "POSTED")
