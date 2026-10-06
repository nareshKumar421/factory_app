"""Making the SAP copy visible: how old it is for the banner, and an alert when
it stops refreshing while HANA answers (once, and again when it recovers)."""

from datetime import datetime, timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import caches
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from company.models import Company
from sap_client import health

from . import monitor
from .models import MirrorDataset

NOW = timezone.make_aware(datetime(2026, 10, 6, 11, 0))
SHARED = {
    "default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "default"},
    "shared": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "monitor-tests"},
}


def snap(hana=health.UP):
    return {"components": {health.HANA: {"status": hana}, health.SERVICE_LAYER: {"status": health.UP}}}


class CopyTestCase(TestCase):
    def setUp(self):
        self.oil = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")

    def copy(self, name, minutes_old):
        return MirrorDataset.objects.create(
            company=self.oil, name=name, synced_at=NOW - timedelta(minutes=minutes_old), row_count=1
        )


class FreshnessTests(CopyTestCase):
    def test_the_banner_gets_the_older_of_each_kind(self):
        self.copy("bills", 5)
        self.copy("purchase_orders", 12)
        self.copy("fg_items", 600)
        self.copy("boms", 610)

        fresh = monitor.freshness("JIVO_OIL")

        self.assertEqual(datetime.fromisoformat(fresh["frequent_as_of"]), NOW - timedelta(minutes=12))
        self.assertEqual(datetime.fromisoformat(fresh["nightly_as_of"]), NOW - timedelta(minutes=610))

    def test_no_copy_says_nothing(self):
        self.assertIsNone(monitor.freshness("JIVO_OIL"))

    def test_what_counts_as_stopped(self):
        self.copy("bills", 44)            # fine: within three runs
        self.copy("purchase_orders", 50)  # three runs missed
        self.copy("vendors", 25 * 60)     # fine: last night's
        self.copy("boms", 27 * 60)        # a night missed

        stale = monitor.stale_copies(NOW)

        self.assertEqual(
            [(code, label) for code, label, _ in stale],
            [("JIVO_OIL", "Production BOMs"), ("JIVO_OIL", "Open purchase orders")],
        )


@override_settings(CACHES=SHARED)
class WatchTests(CopyTestCase):
    def setUp(self):
        super().setUp()
        caches["shared"].clear()
        patcher = patch("notifications.services.NotificationService.send_notification_to_group")
        self.sent = patcher.start()
        self.addCleanup(patcher.stop)
        self.manager = get_user_model().objects.create(
            email="manager@example.com", full_name="Plant Manager", is_superuser=True
        )

    def test_a_copy_that_stopped_is_told_once(self):
        self.copy("bills", 120)

        monitor.watch(snap(), NOW)
        monitor.watch(snap(), NOW + timedelta(minutes=5))

        self.assertEqual(self.sent.call_count, 1)
        call = self.sent.call_args.kwargs
        self.assertEqual(call["title"], "The app's copy of SAP has stopped refreshing")
        self.assertIn("JIVO_OIL A/R bills (last 30 days): last taken", call["body"])
        self.assertEqual(call["users"], [self.manager])

    def test_another_copy_stopping_is_told_again(self):
        self.copy("bills", 120)
        monitor.watch(snap(), NOW)
        self.copy("purchase_orders", 120)

        monitor.watch(snap(), NOW)

        self.assertEqual(self.sent.call_count, 2)

    def test_refreshing_again_is_told(self):
        bills = self.copy("bills", 120)
        monitor.watch(snap(), NOW)
        MirrorDataset.objects.filter(pk=bills.pk).update(synced_at=NOW)

        monitor.watch(snap(), NOW)

        self.assertEqual(self.sent.call_args.kwargs["title"], "The app's copy of SAP is refreshing again")

    def test_with_hana_down_an_old_copy_is_expected(self):
        self.copy("bills", 120)

        self.assertIsNone(monitor.watch(snap(hana=health.DOWN), NOW))
        self.sent.assert_not_called()


class HealthEndpointTests(CopyTestCase):
    def setUp(self):
        super().setUp()
        self.api = APIClient()
        self.api.force_authenticate(
            get_user_model().objects.create(email="gate@example.com", full_name="Gate")
        )

    def get(self, hana):
        with patch("sap_client.health.snapshot", return_value=snap(hana)):
            return self.api.get("/api/v1/sap-health/", HTTP_COMPANY_CODE="JIVO_OIL").json()

    def test_hana_down_says_how_old_the_copy_is(self):
        self.copy("bills", 10)

        body = self.get(health.DOWN)

        self.assertEqual(
            datetime.fromisoformat(body["copy"]["frequent_as_of"]), NOW - timedelta(minutes=10)
        )
        self.assertIn("GRPOs", body["waits_for_sap"])

    def test_hana_up_does_not_look(self):
        body = self.get(health.UP)

        self.assertNotIn("copy", body)
        self.assertIn("bill summary stamps", body["waits_for_sap"])


class WorkerHookTests(TestCase):
    def test_the_worker_watches_the_copies_every_few_minutes(self):
        from sap_postings.management.commands.run_sap_postings import Command

        command = Command()
        command.watch_copies = True
        command.copies_checked_at = 0.0
        with patch("sap_client.health.snapshot", return_value=snap()), \
             patch("sap_postings.services.run_due", return_value=0), \
             patch("sap_mirror.monitor.watch") as watched:
            command.one_pass(True)
            command.one_pass(True)

        self.assertEqual(watched.call_count, 1)

    def test_a_developers_worker_does_not(self):
        from sap_postings.management.commands.run_sap_postings import Command

        command = Command()
        command.watch_copies = False
        with patch("sap_client.health.snapshot", return_value=snap()), \
             patch("sap_postings.services.run_due", return_value=0), \
             patch("sap_mirror.monitor.watch") as watched:
            command.one_pass(True)

        watched.assert_not_called()
