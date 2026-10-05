"""A bought-in finished-goods GRPO through the SAP posting queue.

The FG screen posts in one call (``/grpo/fg/post/``). That call now saves the
GRPO first and sends it the way a saved material GRPO is sent: SAP not
answering leaves it waiting -- not failed -- and the worker posts it once SAP
is back, under finished-goods rules (no QC slip). A second press while it
waits is refused, so SAP never gets the truck's GRPO twice.
"""

import json
from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import Permission
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from company.models import UserCompany, UserRole
from grpo import tests as grpo_tests
from grpo.models import GRPOPosting, GRPOStatus
from grpo.tests_sap_queue import _hana
from notifications.models import Notification
from sap_client.exceptions import SAPUnavailable, SAPValidationError
from sap_postings import services as sap_postings
from sap_postings.models import SapPosting, SapPostingStatus

URL = "/api/v1/grpo/fg/post/"


class FGGRPOQueueTests(TestCase):
    # The existing GRPO fixtures, with the truck a finished-goods gate entry.
    _draft_payload = grpo_tests.GRPOServiceTests._draft_payload

    @classmethod
    def setUpTestData(cls):
        grpo_tests.GRPOServiceTests.setUpTestData.__func__(cls)
        cls.vehicle_entry.entry_type = "FINISHED_GOODS"
        cls.vehicle_entry.save(update_fields=["entry_type"])

    def setUp(self):
        self.sap = MagicMock()
        self.sap.upload_attachment.return_value = {"AbsoluteEntry": 789}
        self.sap.create_grpo.return_value = {"DocEntry": 123, "DocNum": 456, "DocTotal": 4750.00}
        grpo_tests._stub_sap_open_qtys(self.sap)
        self.hana_down = False
        for target, value in (
            ("grpo.services.SAPClient", MagicMock(return_value=self.sap)),
            ("grpo.services.CompanyContext", MagicMock()),
            ("grpo.services.HanaConnection", MagicMock(side_effect=lambda *a, **k: _hana([], self.hana_down))),
        ):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        UserCompany.objects.get_or_create(
            user=self.user, company=self.company,
            defaults={"role": UserRole.objects.create(name="Stores"), "is_active": True},
        )
        self.user.user_permissions.add(Permission.objects.get(codename="add_grpoposting"))
        self.api = APIClient()
        self.api.force_authenticate(self.user)
        self.addCleanup(self._delete_files)

    def _delete_files(self):
        for posting in GRPOPosting.objects.all():
            for att in posting.attachments.all():
                att.file.delete(save=False)

    def press_post(self):
        return self.api.post(
            URL,
            {
                "data": json.dumps(self._draft_payload()),
                "attachments": SimpleUploadedFile("invoice.pdf", b"pdf", content_type="application/pdf"),
            },
            format="multipart",
            HTTP_COMPANY_CODE="TC001",
        )

    def test_sap_up_posts_it_as_before(self):
        response = self.press_post()

        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["sap_doc_num"], 456)
        self.assertEqual(GRPOPosting.objects.get().status, GRPOStatus.POSTED)
        posting = SapPosting.objects.get()
        self.assertEqual((posting.kind, posting.status), ("grpo.material", SapPostingStatus.POSTED))

    def test_sap_down_saves_it_waiting_and_tells_nobody_it_failed(self):
        self.sap.create_grpo.side_effect = SAPUnavailable("SAP Service Layer connection timeout")

        response = self.press_post()

        self.assertEqual(response.status_code, 202, response.content)
        self.assertEqual(response.json()["code"], "SAP_QUEUED")
        draft = GRPOPosting.objects.get()
        self.assertEqual(draft.status, GRPOStatus.QUEUED)
        self.assertEqual(draft.attachments.count(), 1)
        self.assertTrue(draft.request_payload)
        self.assertEqual(SapPosting.objects.get().link, f"/warehouse/grpo/fg/preview/{self.vehicle_entry.id}")
        self.assertFalse(Notification.objects.exists())

    def test_hana_down_waits_too(self):
        self.hana_down = True

        response = self.press_post()

        self.assertEqual(response.status_code, 202, response.content)
        self.sap.create_grpo.assert_not_called()

    def test_a_second_press_while_it_waits_is_refused(self):
        self.sap.create_grpo.side_effect = SAPUnavailable("down")
        self.press_post()

        response = self.press_post()

        self.assertEqual(response.status_code, 409, response.content)
        self.assertEqual(response.json()["code"], "SAP_QUEUED")
        self.assertEqual(GRPOPosting.objects.count(), 1)
        self.assertEqual(SapPosting.objects.count(), 1)

    def test_the_worker_posts_it_once_sap_is_back_under_finished_goods_rules(self):
        self.sap.create_grpo.side_effect = SAPUnavailable("down")
        self.press_post()
        SapPosting.objects.update(next_attempt_at=timezone.now() - timedelta(seconds=1))
        self.sap.create_grpo.side_effect = None

        with patch("sap_client.health.failing_fast", return_value=False), \
             patch("notifications.services.NotificationService.send_notification_to_user"), \
             patch("grpo.services.GRPOService.__init__", autospec=True,
                   side_effect=grpo_tests.GRPOService.__init__) as built:
            self.assertEqual(sap_postings.run_due(), 1)

        self.assertEqual(GRPOPosting.objects.get().status, GRPOStatus.POSTED)
        self.assertEqual(SapPosting.objects.get().status, SapPostingStatus.POSTED)
        self.assertEqual(built.call_args.kwargs.get("entry_type"), "FINISHED_GOODS")

    def test_a_refusal_is_a_400_and_kept_for_history(self):
        self.sap.create_grpo.side_effect = SAPValidationError("200032 - Gross weight is mandatory")

        response = self.press_post()

        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn("200032", response.json()["detail"])
        self.assertEqual(GRPOPosting.objects.get().status, GRPOStatus.FAILED)
        self.assertEqual(SapPosting.objects.get().status, SapPostingStatus.REJECTED)

    def test_a_retry_after_a_refusal_is_a_new_try(self):
        self.sap.create_grpo.side_effect = SAPValidationError("200032 - Gross weight is mandatory")
        self.press_post()
        self.sap.create_grpo.side_effect = None

        response = self.press_post()

        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(
            sorted(GRPOPosting.objects.values_list("status", flat=True)),
            sorted([GRPOStatus.FAILED, GRPOStatus.POSTED]),
        )
