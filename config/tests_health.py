"""The monitoring endpoint.

    python manage.py test config.tests_health --settings=config.sqlite_test_settings
"""

from unittest.mock import patch

from django.db import DatabaseError
from django.test import TestCase
from rest_framework.test import APIClient


class HealthTests(TestCase):
    def test_up_with_its_database_needs_no_login(self):
        response = APIClient().get("/api/v1/health/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok", "database": "ok"})

    def test_a_database_it_cannot_reach_is_a_503(self):
        with patch("django.db.backends.utils.CursorWrapper.execute", side_effect=DatabaseError("down")):
            response = APIClient().get("/api/v1/health/")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["database"], "unreachable")

    def test_a_stale_token_does_not_make_it_a_401(self):
        response = APIClient().get("/api/v1/health/", HTTP_AUTHORIZATION="Bearer expired")
        self.assertEqual(response.status_code, 200)
