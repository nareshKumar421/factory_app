"""The customs exchange rates: read, cleaned, cached, and refused politely.

The outside service is never called from a test; ``requests.post`` is replaced.
"""

from unittest import mock

import requests
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.cache import cache
from django.test import SimpleTestCase
from rest_framework.test import APIClient, APITestCase

from . import customs_rates

PAYLOAD = {
    "data": [
        {
            "currency": "U.S.Dollar",
            "import": "88.90 ",
            "export": "87.20",
            "date": "2026-09-18T00:00:00.000Z",
            "notification_no": "27/2026",
        },
        {
            "currency": "Euro",
            "import": "103.70",
            "export": "100.10 ",
            "date": "2026-09-18T00:00:00.000Z",
            "notification_no": "27/2026",
        },
        {
            "currency": "Norwegian Krone",
            "import": "9.70",
            "export": "9.40",
            "date": "2026-07-03T00:00:00.000Z",
            "notification_no": "19/2026",
        },
        {"currency": "", "import": "1"},
    ]
}


def answer(payload=PAYLOAD):
    response = mock.Mock()
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    return response


class ParseTests(SimpleTestCase):
    def test_strips_the_stray_spaces_and_keeps_the_date(self):
        rows = customs_rates.parse(PAYLOAD)
        self.assertEqual(len(rows), 3)
        self.assertEqual(
            rows[0],
            {
                "currency": "U.S.Dollar",
                "import_rate": "88.90",
                "export_rate": "87.20",
                "notified_on": "2026-09-18",
                "notification_no": "27/2026",
            },
        )
        self.assertEqual(rows[1]["export_rate"], "100.10")

    def test_an_empty_answer_is_no_rows(self):
        self.assertEqual(customs_rates.parse({"data": []}), [])
        self.assertEqual(customs_rates.parse(None), [])


class CustomsRatesAPITests(APITestCase):
    url = "/api/v1/exim/customs-rates/"

    def setUp(self):
        cache.delete(customs_rates.CACHE_KEY)
        self.addCleanup(cache.delete, customs_rates.CACHE_KEY)
        self.user = get_user_model().objects.create_user(
            email="rates@example.com", password="x", full_name="Rates", employee_code="R1"
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def grant(self):
        self.user.user_permissions.add(
            Permission.objects.get(content_type__app_label="exim", codename="view_exim_rates")
        )

    def test_needs_the_rates_right(self):
        with mock.patch("exim.customs_rates.requests.post") as post:
            self.assertEqual(self.client.get(self.url).status_code, 403)
            post.assert_not_called()

    def test_returns_the_latest_notification(self):
        self.grant()
        with mock.patch("exim.customs_rates.requests.post", return_value=answer()) as post:
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["notified_on"], "2026-09-18")
        # The krone's older notification is not the latest one's number.
        self.assertEqual(response.data["notification_no"], "27/2026")
        self.assertEqual(
            [r["currency"] for r in response.data["rates"]],
            ["U.S.Dollar", "Euro", "Norwegian Krone"],
        )
        # No date is sent: the service answers only for an exact notification date.
        self.assertEqual(post.call_args.kwargs["json"], {})

    def test_a_second_read_comes_from_the_cache_unless_refreshed(self):
        self.grant()
        with mock.patch("exim.customs_rates.requests.post", return_value=answer()) as post:
            self.client.get(self.url)
            self.client.get(self.url)
            self.assertEqual(post.call_count, 1)
            self.client.get(self.url, {"refresh": "1"})
            self.assertEqual(post.call_count, 2)

    def test_an_empty_answer_is_not_cached(self):
        self.grant()
        with mock.patch("exim.customs_rates.requests.post", return_value=answer({"data": []})) as post:
            self.assertEqual(self.client.get(self.url).data["rates"], [])
            self.client.get(self.url)
            self.assertEqual(post.call_count, 2)

    def test_a_service_that_does_not_answer_is_a_502_with_a_code(self):
        self.grant()
        with mock.patch(
            "exim.customs_rates.requests.post", side_effect=requests.Timeout("slow")
        ):
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.data["code"], "customs_rates_unavailable")
