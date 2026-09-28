"""Telling "SAP never saw it" from "SAP may have saved it".

On 2026-09-28 the Service Layer hung for hours, then refused connections while
it was restarted. Every posting failed, and every failure was the same
``SAPConnectionError`` whether or not the document had reached SAP. These pin
the split that a retry depends on, against real sockets rather than mocks: the
classification reads ``requests``' own exception chain, and a mock would only
test what the mock was told to raise.
"""

import socket
import threading
from unittest import mock

import requests
from django.test import SimpleTestCase
from rest_framework.test import APIRequestFactory
from rest_framework.views import APIView

from .drf import exception_handler
from .exceptions import (
    SAPConnectionError,
    SAPDataError,
    SAPOutcomeUnknown,
    SAPUnavailable,
    SAPValidationError,
)
from .service_layer.errors import never_sent, unanswered
from .service_layer.grpo_writer import GRPOWriter
from .service_layer.returns_writer import ReturnsWriter


def _closed_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _Server:
    """A local listener that accepts and then either stalls or hangs up."""

    def __init__(self, behaviour):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        self.held = []
        threading.Thread(target=self._serve, args=(behaviour,), daemon=True).start()

    def _serve(self, behaviour):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            if behaviour == "drop":
                conn.recv(65536)
                conn.close()
            else:  # stall: read nothing back, the way the hung SL behaved
                self.held.append(conn)

    def close(self):
        for conn in self.held:
            conn.close()
        self.sock.close()


def _raised(fn):
    try:
        fn()
    except Exception as exc:  # noqa: BLE001 -- the exception is the subject
        return exc
    raise AssertionError("expected the request to fail")


class NeverSentTests(SimpleTestCase):
    def test_a_refused_connection_was_never_sent(self):
        port = _closed_port()
        exc = _raised(lambda: requests.post(f"http://127.0.0.1:{port}/", timeout=2))
        self.assertTrue(never_sent(exc))
        self.assertIsInstance(unanswered(exc, "m"), SAPUnavailable)

    def test_a_server_that_accepts_and_never_answers_may_have_it(self):
        server = _Server("stall")
        self.addCleanup(server.close)
        exc = _raised(
            lambda: requests.post(f"http://127.0.0.1:{server.port}/", json={}, timeout=0.5)
        )
        self.assertIsInstance(exc, requests.exceptions.ReadTimeout)
        self.assertFalse(never_sent(exc))
        self.assertIsInstance(unanswered(exc, "m"), SAPOutcomeUnknown)

    def test_a_connection_dropped_after_sending_may_have_it(self):
        server = _Server("drop")
        self.addCleanup(server.close)
        exc = _raised(
            lambda: requests.post(f"http://127.0.0.1:{server.port}/", json={}, timeout=2)
        )
        self.assertIsInstance(exc, requests.exceptions.ConnectionError)
        self.assertFalse(never_sent(exc))
        self.assertIsInstance(unanswered(exc, "m"), SAPOutcomeUnknown)

    def test_a_connect_timeout_was_never_sent(self):
        exc = requests.exceptions.ConnectTimeout("connect timed out")
        self.assertTrue(never_sent(exc))

    def test_both_are_still_a_connection_error(self):
        # Every existing `except SAPConnectionError` keeps catching both.
        self.assertTrue(issubclass(SAPUnavailable, SAPConnectionError))
        self.assertTrue(issubclass(SAPOutcomeUnknown, SAPConnectionError))


_CONFIG = {
    "base_url": "https://sap.test:50000",
    "company_db": "TEST_DB",
    "username": "u",
    "password": "p",
}


class _Context:
    service_layer = _CONFIG


class WriterClassificationTests(SimpleTestCase):
    """The writers raise the subclass that says which way it went."""

    def test_a_login_that_times_out_is_unavailable(self):
        with mock.patch(
            "sap_client.service_layer.auth.requests.post",
            side_effect=requests.exceptions.ReadTimeout("login hung"),
        ):
            with self.assertRaises(SAPUnavailable) as caught:
                ReturnsWriter(_Context()).create({"CardCode": "C", "DocumentLines": []})
        # The operator-facing text is what it always was.
        self.assertEqual(str(caught.exception), "SAP Service Layer connection timeout")

    def test_a_post_that_times_out_is_outcome_unknown(self):
        with mock.patch.object(GRPOWriter, "_get_session_cookies", return_value={}), \
             mock.patch(
                 "sap_client.service_layer.grpo_writer.requests.post",
                 side_effect=requests.exceptions.ReadTimeout("no answer"),
             ):
            with self.assertRaises(SAPOutcomeUnknown):
                GRPOWriter(_Context()).create({"CardCode": "V", "DocumentLines": []})

    def test_a_post_that_cannot_connect_is_unavailable(self):
        with mock.patch.object(GRPOWriter, "_get_session_cookies", return_value={}), \
             mock.patch(
                 "sap_client.service_layer.grpo_writer.requests.post",
                 side_effect=requests.exceptions.ConnectTimeout("no route"),
             ):
            with self.assertRaises(SAPUnavailable):
                GRPOWriter(_Context()).create({"CardCode": "V", "DocumentLines": []})


class _Raises(APIView):
    authentication_classes = []
    permission_classes = []
    exc = None

    def get(self, request):
        raise self.exc


class ExceptionHandlerTests(SimpleTestCase):
    """An SAP error no view caught: the status it should have had, not a 500."""

    def call(self, exc):
        view = _Raises.as_view(exc=exc)
        with mock.patch(
            "rest_framework.views.APIView.get_exception_handler",
            return_value=exception_handler,
        ):
            return view(APIRequestFactory().get("/"))

    def test_unavailable_is_a_503_with_a_code(self):
        response = self.call(SAPUnavailable("SAP Service Layer connection timeout"))
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.data["code"], "SAP_UNAVAILABLE")
        self.assertIn("Nothing was sent", response.data["detail"])

    def test_outcome_unknown_says_to_check_before_retrying(self):
        response = self.call(SAPOutcomeUnknown("SAP Service Layer request timeout"))
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.data["code"], "SAP_OUTCOME_UNKNOWN")
        self.assertIn("Check SAP before trying again", response.data["detail"])

    def test_an_unclassified_connection_error_is_still_a_503(self):
        response = self.call(SAPConnectionError("Unable to connect to SAP HANA."))
        self.assertEqual(response.status_code, 503)

    def test_validation_is_a_400_and_data_a_502(self):
        self.assertEqual(self.call(SAPValidationError("no such item")).status_code, 400)
        self.assertEqual(self.call(SAPDataError("odd reply")).status_code, 502)

    def test_anything_else_is_left_alone(self):
        with self.assertRaises(KeyError):
            self.call(KeyError("not ours"))


class CacheAnswersTests(SimpleTestCase):
    """A lookup kept per worker must not keep an outage."""

    def test_a_failed_lookup_is_asked_again_and_an_answer_is_kept(self):
        from .lookup_cache import NotCached, cache_answers

        calls = []
        sap_up = {"now": False}

        @cache_answers(maxsize=8)
        def tax_codes(company):
            calls.append(company)
            if not sap_up["now"]:
                raise NotCached({})
            return {"CG+SG@5": 5}

        self.assertEqual(tax_codes("OIL"), {})   # SAP down: the fallback...
        sap_up["now"] = True
        self.assertEqual(tax_codes("OIL"), {"CG+SG@5": 5})  # ...not remembered
        self.assertEqual(tax_codes("OIL"), {"CG+SG@5": 5})
        self.assertEqual(len(calls), 2)  # the answer was

    def test_the_grpo_tax_codes_recover_once_hana_does(self):
        from grpo.services import GRPOService

        GRPOService._get_sap_tax_codes.cache_clear()
        self.addCleanup(GRPOService._get_sap_tax_codes.cache_clear)

        cursor = mock.MagicMock()
        cursor.fetchall.return_value = [("CG+SG@5", "CGST 2.5 + SGST 2.5", 5)]
        working = mock.MagicMock()
        working.cursor.return_value = cursor
        hana = mock.MagicMock(schema="TEST_DB")
        hana.connect.side_effect = [RuntimeError("HANA down"), working]

        with mock.patch("grpo.services.CompanyContext"), \
             mock.patch("grpo.services.HanaConnection", return_value=hana):
            self.assertEqual(GRPOService._get_sap_tax_codes("JIVO_OIL"), {})
            codes = GRPOService._get_sap_tax_codes("JIVO_OIL")

        self.assertIn("CG+SG@5", codes)
