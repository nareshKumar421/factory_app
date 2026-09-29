"""The shared "is SAP answering" state: who sets it, and what it changes.

Probes are patched; the state itself runs on a real (per-process) cache, since
the point is what one call leaves behind for the next.
"""

import socket
import time
from unittest import mock

import requests
from django.test import SimpleTestCase, TestCase, override_settings
from hdbcli import dbapi

from . import health
from .exceptions import SAPUnavailable
from .hana.connection import HanaConnection
from .service_layer.auth import ServiceLayerSession

_LOCMEM = {
    "default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"},
    "shared": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "sap-health-tests",
    },
}

_SL = {"base_url": "https://sap.test:50000", "company_db": "DB", "username": "u", "password": "p"}
_HANA = {"host": "hana.test", "port": 30015, "user": "u", "password": "p", "schema": "DB"}


def _fine():
    return None


def _hung():
    raise requests.exceptions.ReadTimeout("no answer")


@override_settings(CACHES=_LOCMEM, SAP_HEALTH_ALERT_GROUP="SAP Health Alerts")
class HealthStateTests(SimpleTestCase):
    def setUp(self):
        health._cache().clear()
        self.now = 1_790_000_000.0
        clock = mock.patch("sap_client.health.time.time", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        self.alerts = []
        alert = mock.patch(
            "sap_client.health._alert",
            side_effect=lambda component, recovered, state: self.alerts.append(
                (component, recovered)
            ),
        )
        alert.start()
        self.addCleanup(alert.stop)

    def probe(self, sl=_fine, hana=_fine, *, alert=True):
        """The worker's probe unless ``alert=False`` (a web process's)."""
        with mock.patch.dict(health._PROBES, {health.SERVICE_LAYER: sl, health.HANA: hana}):
            return health.probe(alert=alert)

    # -- probing ---------------------------------------------------------

    def test_a_probe_tells_the_two_apart(self):
        snap = self.probe(sl=_hung)
        self.assertFalse(snap["ok"])
        self.assertEqual(snap["components"]["service_layer"]["status"], "down")
        self.assertEqual(
            snap["components"]["service_layer"]["error"],
            "accepted the connection but did not answer",
        )
        self.assertEqual(snap["components"]["hana"]["status"], "up")

    def test_a_fresh_snapshot_does_not_ask_sap_again(self):
        self.probe()
        self.now += 10
        with mock.patch("sap_client.health.probe") as probe:
            health.snapshot()
        probe.assert_not_called()

    def test_a_stale_snapshot_asks_sap(self):
        self.probe()
        self.now += health.PROBE_INTERVAL + 1
        with mock.patch.dict(health._PROBES, {health.SERVICE_LAYER: _hung, health.HANA: _fine}):
            snap = health.snapshot()
        self.assertEqual(snap["components"]["service_layer"]["status"], "down")

    def test_only_one_worker_probes_at_a_time(self):
        self.probe()
        self.now += health.PROBE_INTERVAL + 1
        health._cache().add(health._PROBE_LOCK, 1, 20)  # another worker is probing
        with mock.patch("sap_client.health.probe") as probe:
            snap = health.snapshot()
        probe.assert_not_called()
        self.assertTrue(snap["ok"])  # what is stored, not a queue behind the probe

    # -- failing fast ----------------------------------------------------

    def test_a_login_right_after_a_failed_probe_is_not_even_tried(self):
        self.probe(sl=_hung)
        with mock.patch("sap_client.service_layer.auth.requests.post") as post:
            with self.assertRaisesMessage(SAPUnavailable, "was not sent"):
                ServiceLayerSession(_SL).login()
        post.assert_not_called()

    def test_past_the_window_the_login_is_tried_again(self):
        self.probe(sl=_hung)
        self.now += health.RETRY_AFTER + 1
        answered = mock.MagicMock()
        with mock.patch(
            "sap_client.service_layer.auth.requests.post", return_value=answered
        ) as post:
            ServiceLayerSession(_SL).login()
        post.assert_called_once()
        # ...and getting through is itself the news that SAP is back.
        self.assertEqual(health.current(health.SERVICE_LAYER)["status"], "up")

    def test_hana_known_down_fails_as_a_refused_connection_would(self):
        self.probe(hana=_hung)
        with mock.patch("sap_client.hana.connection.dbapi.connect") as connect:
            with self.assertRaises(dbapi.Error) as caught:
                HanaConnection(_HANA).connect()
        connect.assert_not_called()
        self.assertIsInstance(caught.exception, dbapi.OperationalError)
        self.assertEqual(caught.exception.errorcode, -10709)

    def test_the_service_layer_being_down_does_not_stop_reads(self):
        self.probe(sl=_hung)
        with mock.patch("sap_client.hana.connection.dbapi.connect") as connect:
            HanaConnection(_HANA).connect()
        connect.assert_called_once()

    def test_a_real_call_that_fails_does_not_declare_sap_down(self):
        # A document can fail for its own reasons; only a probe says SAP is down.
        with mock.patch(
            "sap_client.service_layer.auth.requests.post",
            side_effect=requests.exceptions.ReadTimeout("slow"),
        ):
            with self.assertRaises(requests.exceptions.ReadTimeout):
                ServiceLayerSession(_SL).login()
        self.assertNotEqual(health.current(health.SERVICE_LAYER)["status"], "down")

    # -- alerts ----------------------------------------------------------

    def test_the_group_hears_after_five_minutes_and_only_once(self):
        self.probe(sl=_hung)
        self.assertEqual(self.alerts, [])
        self.now += health.ALERT_AFTER + 1
        self.probe(sl=_hung)
        self.now += 60
        self.probe(sl=_hung)
        self.assertEqual(self.alerts, [("service_layer", False)])

    def test_a_web_process_never_alerts_however_long_it_is_down(self):
        # Several web processes each notice an outage; told once per process,
        # everyone would hear it several times over.
        self.probe(sl=_hung, alert=False)
        self.now += health.ALERT_AFTER + 1
        self.probe(sl=_hung, alert=False)
        self.assertEqual(self.alerts, [])
        # ...and the worker's next probe is the one that tells them.
        self.probe(sl=_hung)
        self.assertEqual(self.alerts, [("service_layer", False)])

    def test_a_web_process_seeing_it_back_leaves_the_news_to_the_worker(self):
        self.probe(sl=_hung)
        self.now += health.ALERT_AFTER + 1
        self.probe(sl=_hung)
        self.probe(alert=False)  # a browser's poll finds it back first
        self.assertEqual(self.alerts, [("service_layer", False)])
        self.assertEqual(health.current(health.SERVICE_LAYER)["status"], "up")
        self.probe()
        self.assertEqual(self.alerts, [("service_layer", False), ("service_layer", True)])

    def test_the_endpoints_snapshot_does_not_alert(self):
        self.probe(sl=_hung)
        self.now += health.ALERT_AFTER + 1
        with mock.patch.dict(health._PROBES, {health.SERVICE_LAYER: _hung, health.HANA: _fine}):
            health.snapshot()
        self.assertEqual(self.alerts, [])

    def test_a_blip_under_five_minutes_tells_nobody(self):
        self.probe(sl=_hung)
        self.now += 60
        self.probe()
        self.assertEqual(self.alerts, [])

    def test_they_hear_it_is_back_from_the_next_probe(self):
        self.probe(sl=_hung)
        self.now += health.ALERT_AFTER + 1
        self.probe(sl=_hung)
        # A posting gets through first: recorded up, but it sends nothing itself.
        self.now += health.RETRY_AFTER + 1
        with mock.patch("sap_client.service_layer.auth.requests.post"):
            ServiceLayerSession(_SL).login()
        self.assertEqual(self.alerts, [("service_layer", False)])
        self.probe()
        self.assertEqual(self.alerts, [("service_layer", False), ("service_layer", True)])
        self.probe()
        self.assertEqual(len(self.alerts), 2)


def _closed_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class RedisDownTests(SimpleTestCase):
    """Health must never become the outage it reports on."""

    def test_a_dead_redis_leaves_the_login_to_go_ahead_at_once(self):
        caches = {
            "default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"},
            "shared": {
                "BACKEND": "django.core.cache.backends.redis.RedisCache",
                "LOCATION": f"redis://127.0.0.1:{_closed_port()}/0",
                "OPTIONS": {"socket_connect_timeout": 0.5, "socket_timeout": 0.5},
            },
        }
        with override_settings(CACHES=caches):
            started = time.monotonic()
            with mock.patch("sap_client.service_layer.auth.requests.post") as post:
                ServiceLayerSession(_SL).login()
            post.assert_called_once()
            self.assertLess(time.monotonic() - started, 3)


@override_settings(CACHES=_LOCMEM)
class HealthEndpointTests(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model
        from rest_framework.test import APIClient

        health._cache().clear()
        self.api = APIClient()
        self.api.force_authenticate(
            get_user_model().objects.create(email="gate@example.com", full_name="Gate")
        )

    def test_any_logged_in_user_can_ask(self):
        with mock.patch.dict(
            health._PROBES, {health.SERVICE_LAYER: _hung, health.HANA: _fine}
        ):
            response = self.api.get("/api/v1/sap-health/")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["components"]["service_layer"]["status"], "down")
        self.assertIsNotNone(body["components"]["service_layer"]["since"])

    def test_nobody_else_can(self):
        from rest_framework.test import APIClient

        self.assertEqual(APIClient().get("/api/v1/sap-health/").status_code, 401)


class AlertRecipientTests(TestCase):
    """Superusers hear without being added to anything; the group is for the rest."""

    def test_superusers_and_the_group_hear_and_nobody_else(self):
        from django.contrib.auth import get_user_model
        from django.contrib.auth.models import Group

        User = get_user_model()
        manager = User.objects.create(email="manager@example.com", full_name="M", is_superuser=True)
        User.objects.create(email="gone@example.com", full_name="G", is_superuser=True, is_active=False)
        storekeeper = User.objects.create(email="store@example.com", full_name="S")
        User.objects.create(email="clerk@example.com", full_name="C")
        Group.objects.create(name="SAP Health Alerts").user_set.add(storekeeper, manager)

        with override_settings(SAP_HEALTH_ALERT_GROUP="SAP Health Alerts"), mock.patch(
            "notifications.services.NotificationService.send_notification_to_group"
        ) as send:
            health._alert(health.SERVICE_LAYER, recovered=False, state={"since": 0, "error": "x"})

        told = sorted(u.email for u in send.call_args.kwargs["users"])
        # Once each, though the manager is in the group too; no inactive account.
        self.assertEqual(told, ["manager@example.com", "store@example.com"])
        self.assertEqual(send.call_args.kwargs["title"], "SAP Service Layer is down")
