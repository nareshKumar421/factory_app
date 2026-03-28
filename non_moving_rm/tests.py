"""
non_moving_rm/tests.py

Unit tests for the Non-Moving Raw Material Dashboard app.

Tests cover:
  1. HanaNonMovingRMReader — row mapping
  2. NonMovingRMService    — aggregation, calculations
  3. Serializer validation
  4. API views             — response shape, auth, error handling
"""

from datetime import datetime
from unittest.mock import MagicMock, patch

from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient, APITestCase
from rest_framework_simplejwt.tokens import RefreshToken


# ---------------------------------------------------------------------------
# Helpers / Fixtures
# ---------------------------------------------------------------------------


def _make_report_row(
    *,
    branch="PM00000081",
    item_code="ITEM-001",
    item_name="LABEL 1 KG GOLD FULL",
    item_group_name="PACKAGING MATERIAL",
    quantity=26116.0,
    litres=22186.0,
    sub_group="LABEL",
    value=24203.0,
    last_movement_date=datetime(2020, 3, 19, 12, 0, 0),
    days_since_last_movement=2200,
    consumption_ratio=46.5,
):
    """Returns a tuple in the same column order as REPORT_BP_NON_MOVING_RM."""
    return (
        branch,
        item_code,
        item_name,
        item_group_name,
        quantity,
        litres,
        sub_group,
        value,
        last_movement_date,
        days_since_last_movement,
        consumption_ratio,
    )


def _make_item_group_row(*, item_group_code=105, item_group_name="PACKAGING MATERIAL"):
    return (item_group_code, item_group_name)


# ---------------------------------------------------------------------------
# 1. HanaNonMovingRMReader Tests
# ---------------------------------------------------------------------------


class TestHanaNonMovingRMReaderRowMapping(TestCase):
    """Tests for _map_report_row and _map_item_group_row."""

    def setUp(self):
        from non_moving_rm.hana_reader import HanaNonMovingRMReader

        context = MagicMock()
        context.hana = {
            "host": "localhost",
            "port": 30015,
            "user": "u",
            "password": "p",
            "schema": "TEST",
        }
        with patch("non_moving_rm.hana_reader.HanaConnection"):
            self.reader = HanaNonMovingRMReader(context)
            self.reader.connection.schema = "TEST"

    def test_map_report_row_basic_fields(self):
        row = _make_report_row()
        result = self.reader._map_report_row(row)

        self.assertEqual(result["branch"], "PM00000081")
        self.assertEqual(result["item_code"], "ITEM-001")
        self.assertEqual(result["item_name"], "LABEL 1 KG GOLD FULL")
        self.assertEqual(result["item_group_name"], "PACKAGING MATERIAL")
        self.assertEqual(result["sub_group"], "LABEL")

    def test_map_report_row_numeric_fields(self):
        row = _make_report_row(quantity=26116.0, litres=22186.0, value=24203.0)
        result = self.reader._map_report_row(row)

        self.assertEqual(result["quantity"], 26116.0)
        self.assertEqual(result["litres"], 22186.0)
        self.assertEqual(result["value"], 24203.0)

    def test_map_report_row_date_formatting(self):
        row = _make_report_row(last_movement_date=datetime(2020, 3, 19, 12, 0, 0))
        result = self.reader._map_report_row(row)
        self.assertEqual(result["last_movement_date"], "2020-03-19 12:00:00")

    def test_map_report_row_null_date(self):
        row = _make_report_row(last_movement_date=None)
        result = self.reader._map_report_row(row)
        self.assertIsNone(result["last_movement_date"])

    def test_map_report_row_days_and_ratio(self):
        row = _make_report_row(days_since_last_movement=2200, consumption_ratio=46.5)
        result = self.reader._map_report_row(row)
        self.assertEqual(result["days_since_last_movement"], 2200)
        self.assertEqual(result["consumption_ratio"], 46.5)

    def test_map_report_row_null_values_default(self):
        row = (None, None, None, None, None, None, None, None, None, None, None)
        result = self.reader._map_report_row(row)
        self.assertEqual(result["branch"], "")
        self.assertEqual(result["item_code"], "")
        self.assertEqual(result["quantity"], 0)
        self.assertEqual(result["value"], 0)

    def test_map_item_group_row(self):
        row = _make_item_group_row(item_group_code=105, item_group_name="PACKAGING MATERIAL")
        result = self.reader._map_item_group_row(row)
        self.assertEqual(result["item_group_code"], 105)
        self.assertEqual(result["item_group_name"], "PACKAGING MATERIAL")

    def test_map_item_group_row_null_name(self):
        row = (106, None)
        result = self.reader._map_item_group_row(row)
        self.assertEqual(result["item_group_code"], 106)
        self.assertEqual(result["item_group_name"], "")


# ---------------------------------------------------------------------------
# 2. NonMovingRMService Tests
# ---------------------------------------------------------------------------


class TestNonMovingRMService(TestCase):
    """Tests for service-level aggregation logic."""

    def _make_service(self):
        from non_moving_rm.services import NonMovingRMService

        with patch("non_moving_rm.services.CompanyContext"), \
             patch("non_moving_rm.services.HanaNonMovingRMReader"):
            service = NonMovingRMService.__new__(NonMovingRMService)
            service.company_code = "JIVO_OIL"
            service.reader = MagicMock()
            return service

    def test_get_report_meta(self):
        service = self._make_service()
        service.reader.get_non_moving_report.return_value = [
            {"branch": "B1", "value": 100.0, "quantity": 50.0},
            {"branch": "B1", "value": 200.0, "quantity": 30.0},
            {"branch": "B2", "value": 150.0, "quantity": 70.0},
        ]
        result = service.get_report(age=45, item_group=105)

        self.assertEqual(result["meta"]["age_days"], 45)
        self.assertEqual(result["meta"]["item_group"], 105)
        self.assertIn("fetched_at", result["meta"])

    def test_get_report_summary_totals(self):
        service = self._make_service()
        service.reader.get_non_moving_report.return_value = [
            {"branch": "B1", "value": 100.50, "quantity": 50.0},
            {"branch": "B1", "value": 200.25, "quantity": 30.0},
            {"branch": "B2", "value": 150.75, "quantity": 70.0},
        ]
        result = service.get_report(age=45, item_group=105)

        self.assertEqual(result["summary"]["total_items"], 3)
        self.assertEqual(result["summary"]["total_value"], 451.50)
        self.assertEqual(result["summary"]["total_quantity"], 150.0)

    def test_get_report_branch_summary(self):
        service = self._make_service()
        service.reader.get_non_moving_report.return_value = [
            {"branch": "B1", "value": 100.0, "quantity": 50.0},
            {"branch": "B1", "value": 200.0, "quantity": 30.0},
            {"branch": "B2", "value": 150.0, "quantity": 70.0},
        ]
        result = service.get_report(age=45, item_group=105)

        by_branch = {b["branch"]: b for b in result["summary"]["by_branch"]}
        self.assertEqual(by_branch["B1"]["item_count"], 2)
        self.assertEqual(by_branch["B1"]["total_value"], 300.0)
        self.assertEqual(by_branch["B2"]["item_count"], 1)
        self.assertEqual(by_branch["B2"]["total_value"], 150.0)

    def test_get_report_empty_result(self):
        service = self._make_service()
        service.reader.get_non_moving_report.return_value = []
        result = service.get_report(age=90, item_group=106)

        self.assertEqual(result["summary"]["total_items"], 0)
        self.assertEqual(result["summary"]["total_value"], 0)
        self.assertEqual(result["summary"]["by_branch"], [])

    def test_get_item_groups(self):
        service = self._make_service()
        service.reader.get_item_groups.return_value = [
            {"item_group_code": 105, "item_group_name": "PACKAGING MATERIAL"},
            {"item_group_code": 106, "item_group_name": "RAW MATERIAL"},
        ]
        result = service.get_item_groups()

        self.assertEqual(result["meta"]["total_groups"], 2)
        self.assertEqual(len(result["data"]), 2)
        self.assertIn("fetched_at", result["meta"])


# ---------------------------------------------------------------------------
# 3. Filter Serializer Tests
# ---------------------------------------------------------------------------


class TestNonMovingRMFilterSerializer(TestCase):

    def setUp(self):
        from non_moving_rm.serializers import NonMovingRMFilterSerializer
        self.Serializer = NonMovingRMFilterSerializer

    def test_valid_params(self):
        s = self.Serializer(data={"age": 45, "item_group": 105})
        self.assertTrue(s.is_valid(), s.errors)
        self.assertEqual(s.validated_data["age"], 45)
        self.assertEqual(s.validated_data["item_group"], 105)

    def test_missing_age_invalid(self):
        s = self.Serializer(data={"item_group": 105})
        self.assertFalse(s.is_valid())
        self.assertIn("age", s.errors)

    def test_missing_item_group_invalid(self):
        s = self.Serializer(data={"age": 45})
        self.assertFalse(s.is_valid())
        self.assertIn("item_group", s.errors)

    def test_age_zero_invalid(self):
        s = self.Serializer(data={"age": 0, "item_group": 105})
        self.assertFalse(s.is_valid())
        self.assertIn("age", s.errors)

    def test_negative_age_invalid(self):
        s = self.Serializer(data={"age": -10, "item_group": 105})
        self.assertFalse(s.is_valid())

    def test_empty_params_invalid(self):
        s = self.Serializer(data={})
        self.assertFalse(s.is_valid())


# ---------------------------------------------------------------------------
# 4. API View Tests (with mocked service)
# ---------------------------------------------------------------------------


class TestNonMovingRMAPIViews(APITestCase):
    """
    Tests API views by mocking NonMovingRMService to avoid real SAP calls.
    """

    def setUp(self):
        from django.contrib.auth import get_user_model
        from company.models import Company, UserCompany, UserRole

        User = get_user_model()
        self.user = User.objects.create_user(
            email="analyst@test.com",
            password="testpass123",
            full_name="Test Analyst",
            employee_code="EMP002",
        )
        from django.contrib.auth.models import Permission
        from django.contrib.contenttypes.models import ContentType
        from non_moving_rm.models import NonMovingRMPermission

        ct = ContentType.objects.get_for_model(NonMovingRMPermission)
        perm, _ = Permission.objects.get_or_create(
            codename="can_view_non_moving_rm",
            content_type=ct,
            defaults={"name": "Can view Non-Moving RM Dashboard"},
        )
        self.user.user_permissions.add(perm)
        self.user.save()

        self.company = Company.objects.create(
            name="Jivo Oil", code="JIVO_OIL", is_active=True
        )
        role = UserRole.objects.create(name="Analyst")
        UserCompany.objects.create(
            user=self.user,
            company=self.company,
            role=role,
            is_default=True,
            is_active=True,
        )

        refresh = RefreshToken.for_user(self.user)
        self.client = APIClient()
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}",
            HTTP_COMPANY_CODE="JIVO_OIL",
        )

    def _mock_report_response(self):
        return {
            "data": [
                {
                    "branch": "PM00000081",
                    "item_code": "ITEM-001",
                    "item_name": "LABEL 1 KG GOLD FULL",
                    "item_group_name": "PACKAGING MATERIAL",
                    "quantity": 26116.0,
                    "litres": 22186.0,
                    "sub_group": "LABEL",
                    "value": 24203.0,
                    "last_movement_date": "2020-03-19 12:00:00",
                    "days_since_last_movement": 2200,
                    "consumption_ratio": 46.5,
                }
            ],
            "summary": {
                "total_items": 1,
                "total_value": 24203.0,
                "total_quantity": 26116.0,
                "by_branch": [
                    {
                        "branch": "PM00000081",
                        "item_count": 1,
                        "total_value": 24203.0,
                        "total_quantity": 26116.0,
                    }
                ],
            },
            "meta": {
                "age_days": 45,
                "item_group": 105,
                "fetched_at": "2026-03-28T10:30:00+00:00",
            },
        }

    def _mock_item_groups_response(self):
        return {
            "data": [
                {"item_group_code": 105, "item_group_name": "PACKAGING MATERIAL"},
                {"item_group_code": 106, "item_group_name": "RAW MATERIAL"},
            ],
            "meta": {
                "total_groups": 2,
                "fetched_at": "2026-03-28T10:30:00+00:00",
            },
        }

    # -- Report API --

    @patch("non_moving_rm.views.NonMovingRMService")
    def test_report_returns_200(self, MockService):
        MockService.return_value.get_report.return_value = self._mock_report_response()
        response = self.client.get(
            "/api/v1/non-moving-rm/report/",
            {"age": 45, "item_group": 105},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("data", response.data)
        self.assertIn("summary", response.data)
        self.assertIn("meta", response.data)

    @patch("non_moving_rm.views.NonMovingRMService")
    def test_report_passes_correct_params(self, MockService):
        MockService.return_value.get_report.return_value = self._mock_report_response()
        self.client.get(
            "/api/v1/non-moving-rm/report/",
            {"age": 90, "item_group": 106},
        )
        MockService.return_value.get_report.assert_called_once_with(age=90, item_group=106)

    def test_report_missing_params_returns_400(self):
        response = self.client.get("/api/v1/non-moving-rm/report/")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("errors", response.data)

    def test_report_invalid_age_returns_400(self):
        response = self.client.get(
            "/api/v1/non-moving-rm/report/",
            {"age": 0, "item_group": 105},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_report_requires_authentication(self):
        client = APIClient()
        response = client.get(
            "/api/v1/non-moving-rm/report/",
            {"age": 45, "item_group": 105},
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_report_requires_company_code(self):
        client = APIClient()
        refresh = RefreshToken.for_user(self.user)
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")
        response = client.get(
            "/api/v1/non-moving-rm/report/",
            {"age": 45, "item_group": 105},
        )
        self.assertIn(
            response.status_code,
            [status.HTTP_403_FORBIDDEN, status.HTTP_401_UNAUTHORIZED],
        )

    @patch("non_moving_rm.views.NonMovingRMService")
    def test_report_sap_connection_error_returns_503(self, MockService):
        from sap_client.exceptions import SAPConnectionError

        MockService.return_value.get_report.side_effect = SAPConnectionError("down")
        response = self.client.get(
            "/api/v1/non-moving-rm/report/",
            {"age": 45, "item_group": 105},
        )
        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)

    @patch("non_moving_rm.views.NonMovingRMService")
    def test_report_sap_data_error_returns_502(self, MockService):
        from sap_client.exceptions import SAPDataError

        MockService.return_value.get_report.side_effect = SAPDataError("bad data")
        response = self.client.get(
            "/api/v1/non-moving-rm/report/",
            {"age": 45, "item_group": 105},
        )
        self.assertEqual(response.status_code, status.HTTP_502_BAD_GATEWAY)

    # -- Item Groups API --

    @patch("non_moving_rm.views.NonMovingRMService")
    def test_item_groups_returns_200(self, MockService):
        MockService.return_value.get_item_groups.return_value = self._mock_item_groups_response()
        response = self.client.get("/api/v1/non-moving-rm/item-groups/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("data", response.data)
        self.assertIn("meta", response.data)

    @patch("non_moving_rm.views.NonMovingRMService")
    def test_item_groups_sap_error_returns_503(self, MockService):
        from sap_client.exceptions import SAPConnectionError

        MockService.return_value.get_item_groups.side_effect = SAPConnectionError("down")
        response = self.client.get("/api/v1/non-moving-rm/item-groups/")
        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)

    def test_item_groups_requires_authentication(self):
        client = APIClient()
        response = client.get("/api/v1/non-moving-rm/item-groups/")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
