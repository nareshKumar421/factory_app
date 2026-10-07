"""The API call log: what one call leaves behind, and what the commands make of it.

The calls go to small views of this module's own (``urlpatterns`` below), signed
in with a real JWT, so the user is read the way production reads it: from what
DRF hands back to the request.
"""

import json
from datetime import timedelta
from io import StringIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import path
from django.utils import timezone
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.test import APIClient
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken

from .middleware import BODY_LIMIT, HIDDEN
from .models import ApiCall


class EchoView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, pk=None):
        return Response({"pk": pk, "items": [1, 2, 3]})

    def post(self, request, pk=None):
        return Response({"received": request.data, "access": "a-new-token"}, status=201)


class FailView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        return Response({"detail": "No such bill."}, status=400)


class BoomView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        raise RuntimeError("boom")


class UploadView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        return Response({"note": request.data.get("note")})


urlpatterns = [
    path("api/v1/things/<int:pk>/", EchoView.as_view()),
    path("api/v1/fail/", FailView.as_view()),
    path("api/v1/boom/", BoomView.as_view()),
    path("api/v1/upload/", UploadView.as_view()),
    path("api/v1/health/", EchoView.as_view()),
    path("elsewhere/", EchoView.as_view()),
]


@override_settings(ROOT_URLCONF=__name__, API_LOG_ENABLED=True)
class ApiCallLogTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create(email="clerk@example.com", full_name="Clerk")
        self.client = APIClient()
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {RefreshToken.for_user(self.user).access_token}",
            HTTP_COMPANY_CODE="JIVO_OIL",
            HTTP_USER_AGENT="FactoryFlow test",
            HTTP_X_FORWARDED_FOR="10.0.0.9, 203.0.113.7",
        )

    def only_call(self):
        self.assertEqual(ApiCall.objects.count(), 1)
        return ApiCall.objects.get()

    def test_a_read_is_recorded_with_who_what_and_when_but_no_bodies(self):
        response = self.client.get("/api/v1/things/7/?status=open")

        self.assertEqual(response.status_code, 200)
        call = self.only_call()
        self.assertEqual(call.user, self.user)
        self.assertEqual(call.company_code, "JIVO_OIL")
        self.assertEqual(call.method, "GET")
        self.assertEqual(call.path, "/api/v1/things/7/")
        self.assertEqual(call.query_string, "status=open")
        self.assertEqual(call.route, "api/v1/things/<int:pk>/")
        self.assertEqual(call.view, "api_log.tests.EchoView")
        self.assertEqual(call.app_label, "api_log")
        self.assertEqual(call.status_code, 200)
        self.assertGreaterEqual(call.duration_ms, 0)
        self.assertEqual(call.ip_address, "203.0.113.7")
        self.assertEqual(call.user_agent, "FactoryFlow test")
        self.assertEqual(call.response_bytes, len(response.content))
        self.assertEqual(call.request_body, "")
        self.assertEqual(call.response_body, "")

    def test_a_write_keeps_what_was_sent_and_answered_with_secrets_hidden(self):
        sent = {"qty": 5, "password": "hunter2", "lines": [{"refresh_token": "r", "item": "OIL1"}]}
        response = self.client.post("/api/v1/things/7/", sent, format="json")

        # The view still read the body the log read first.
        self.assertEqual(response.data["received"]["qty"], 5)
        call = self.only_call()
        self.assertEqual(
            json.loads(call.request_body),
            {"qty": 5, "password": HIDDEN, "lines": [{"refresh_token": HIDDEN, "item": "OIL1"}]},
        )
        answered = json.loads(call.response_body)
        self.assertEqual(answered["access"], HIDDEN)
        self.assertEqual(answered["received"]["password"], HIDDEN)
        self.assertEqual(call.status_code, 201)

    def test_a_failed_read_keeps_its_answer(self):
        self.client.get("/api/v1/fail/")

        call = self.only_call()
        self.assertEqual(call.status_code, 400)
        self.assertEqual(json.loads(call.response_body), {"detail": "No such bill."})

    def test_an_unhandled_error_is_recorded_as_a_500(self):
        self.client.raise_request_exception = False
        self.client.get("/api/v1/boom/")

        call = self.only_call()
        self.assertEqual(call.status_code, 500)
        self.assertEqual(call.view, "api_log.tests.BoomView")

    def test_an_upload_keeps_its_fields_and_file_names_but_not_the_files(self):
        photo = SimpleUploadedFile("bilty.jpg", b"JPEGDATA" * 100, content_type="image/jpeg")
        response = self.client.post(
            "/api/v1/upload/", {"note": "truck 4", "photo": photo}, format="multipart",
        )

        self.assertEqual(response.data, {"note": "truck 4"})
        call = self.only_call()
        self.assertEqual(
            json.loads(call.request_body),
            {"note": "truck 4", "files": {"photo": ["bilty.jpg (800 bytes)"]}},
        )
        self.assertNotIn("JPEGDATA", call.request_body)

    def test_a_long_body_is_cut(self):
        self.client.post("/api/v1/things/7/", {"remarks": "x" * (BODY_LIMIT * 2)}, format="json")

        call = self.only_call()
        self.assertTrue(call.request_body.endswith("characters, cut]"))
        self.assertLess(len(call.request_body), BODY_LIMIT + 100)

    def test_secret_query_parameters_are_hidden(self):
        self.client.get("/api/v1/things/7/?token=abc&status=open")

        self.assertEqual(self.only_call().query_string, "token=%5Bhidden%5D&status=open")

    def test_a_call_without_a_token_has_no_user(self):
        APIClient().get("/api/v1/things/7/")

        self.assertIsNone(self.only_call().user)

    def test_a_call_no_url_answers_is_recorded_without_a_route(self):
        self.client.get("/api/v1/nowhere/")

        call = self.only_call()
        self.assertEqual(call.status_code, 404)
        self.assertEqual((call.route, call.view, call.app_label), ("", "", ""))

    def test_calls_outside_the_api_and_health_pings_are_not_recorded(self):
        self.client.get("/elsewhere/")
        self.client.get("/api/v1/health/")
        self.client.options("/api/v1/things/7/")

        self.assertFalse(ApiCall.objects.exists())

    @override_settings(API_LOG_ENABLED=False)
    def test_nothing_is_recorded_when_switched_off(self):
        self.client.post("/api/v1/things/7/", {"qty": 1}, format="json")

        self.assertFalse(ApiCall.objects.exists())

    def test_a_failure_to_record_never_breaks_the_call(self):
        with mock.patch.object(ApiCall.objects, "create", side_effect=RuntimeError("db gone")):
            with self.assertLogs("api_log.middleware", "ERROR"):
                response = self.client.post("/api/v1/things/7/", {"qty": 1}, format="json")

        self.assertEqual(response.status_code, 201)


class CommandTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create(email="clerk@example.com", full_name="Clerk")

    def call(self, days_ago, **fields):
        return ApiCall.objects.create(
            started_at=timezone.now() - timedelta(days=days_ago),
            method=fields.pop("method", "GET"),
            path="/api/v1/x/",
            status_code=fields.pop("status_code", 200),
            duration_ms=fields.pop("duration_ms", 10),
            **fields,
        )

    @override_settings(API_LOG_RETENTION_DAYS=90)
    def test_prune_deletes_only_what_is_past_the_retention(self):
        kept = self.call(10)
        self.call(100)
        self.call(200)

        out = StringIO()
        call_command("prune_api_log", stdout=out)

        self.assertEqual(list(ApiCall.objects.all()), [kept])
        self.assertIn("Deleted 2 API calls", out.getvalue())

    def test_usage_ranks_modules_by_calls_within_the_window(self):
        for _ in range(3):
            self.call(1, app_label="grpo", user=self.user)
        self.call(1, app_label="grpo", status_code=500)
        self.call(2, app_label="dispatch_plans", method="POST")
        self.call(30, app_label="weighment")  # outside the 7 days

        out = StringIO()
        call_command("api_usage", stdout=out)
        lines = out.getvalue().splitlines()

        self.assertEqual(lines[0], "5 calls by 1 users in the last 7 days")
        self.assertEqual(lines[2].split(), ["4", "1", "1", "10", "10", "grpo"])
        self.assertEqual(lines[3].split(), ["1", "0", "0", "10", "10", "dispatch_plans"])
        self.assertEqual(len(lines), 4)

    def test_usage_can_count_only_writes(self):
        self.call(1, app_label="grpo")
        self.call(1, app_label="dispatch_plans", method="POST")

        out = StringIO()
        call_command("api_usage", "--writes", stdout=out)

        self.assertTrue(out.getvalue().splitlines()[2].endswith("dispatch_plans"))
        self.assertEqual(len(out.getvalue().splitlines()), 3)
