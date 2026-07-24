"""Tests for the Online Quality Monitoring module."""

from datetime import date

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from company.models import Company, UserCompany, UserRole
from production_execution.models import ProductionLine
from quality_control.models.online_monitoring import (
    OnlineQualityRecord,
    OnlineQualitySpec,
    OnlineRecordStatus,
    SpecValidationType,
)

User = get_user_model()


def _client(company, perms="all"):
    n = User.objects.count()
    user = User.objects.create_user(
        email=f"u{n}@t.com", password="x", full_name=f"User {n}", employee_code=f"E{n}"
    )
    role = UserRole.objects.create(name=f"R{UserRole.objects.count()}")
    UserCompany.objects.create(user=user, company=company, role=role, is_active=True)
    qs = Permission.objects.filter(content_type__app_label="quality_control")
    if perms != "all":
        qs = qs.filter(codename__in=perms)
    user.user_permissions.set(qs)
    user = User.objects.get(pk=user.pk)
    c = APIClient()
    c.force_authenticate(user=user)
    c.credentials(HTTP_COMPANY_CODE=company.code)
    return c


class OnlineMonitoringFlowTests(APITestCase):
    def setUp(self):
        self.company = Company.objects.create(code="TEST_CO", name="Test Co")
        self.line = ProductionLine.objects.create(company=self.company, name="Line-1")
        self.client = _client(self.company)

    def _create_record(self):
        resp = self.client.post(reverse("online-monitoring-list"), {
            "production_line_id": self.line.id,
            "date": str(date.today()),
            "sku": "FG0000001", "product_name": "Water 1L",
            "flavour": "Plain", "shift": "A", "batch_no": "B-100",
        }, format="json")
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        return resp.data["id"]

    def test_create_record_starts_draft(self):
        rid = self._create_record()
        record = OnlineQualityRecord.objects.get(id=rid)
        self.assertEqual(record.status, OnlineRecordStatus.DRAFT)
        self.assertEqual(record.company, self.company)
        self.assertEqual(record.batch_no, "B-100")

    def test_add_reading_with_torque_heads(self):
        rid = self._create_record()
        resp = self.client.post(
            reverse("online-monitoring-reading-create", args=[rid]),
            {
                "reading_time": "08:00",
                "filler_speed": "16000",
                "taste": "ACCEPTABLE", "aroma": "ACCEPTABLE", "appearance": "ACCEPTABLE",
                "ph": "7.24", "tds": "172",
                "package_attribute": "OK", "date_code": "OK",
                "rub_test": "PASS", "closure_jump_test": "PASS",
                "torque_heads": [
                    {"head_no": 1, "torque_value": "10"},
                    {"head_no": 2, "torque_value": "11"},
                    {"head_no": 3, "torque_value": "9"},
                ],
            },
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        self.assertEqual(len(resp.data["torque_heads"]), 3)

        detail = self.client.get(reverse("online-monitoring-detail", args=[rid]))
        self.assertEqual(len(detail.data["readings"]), 1)
        self.assertEqual(str(detail.data["readings"][0]["ph"]), "7.240")

    def test_full_lifecycle_submit_approve(self):
        rid = self._create_record()
        # Can't submit with no readings.
        empty = self.client.post(reverse("online-monitoring-submit", args=[rid]))
        self.assertEqual(empty.status_code, 400)

        self.client.post(reverse("online-monitoring-reading-create", args=[rid]),
                         {"reading_time": "09:00"}, format="json")
        sub = self.client.post(reverse("online-monitoring-submit", args=[rid]))
        self.assertEqual(sub.status_code, 200)
        self.assertEqual(sub.data["status"], "SUBMITTED")

        # No more edits once submitted.
        edit = self.client.post(reverse("online-monitoring-reading-create", args=[rid]),
                                {"reading_time": "10:00"}, format="json")
        self.assertEqual(edit.status_code, 400)

        appr = self.client.post(reverse("online-monitoring-approve", args=[rid]),
                                {"remarks": "ok"}, format="json")
        self.assertEqual(appr.status_code, 200)
        self.assertEqual(appr.data["status"], "APPROVED")

    def test_list_filters(self):
        rid = self._create_record()
        r = self.client.get(reverse("online-monitoring-list"), {"production_line": self.line.id})
        self.assertEqual([x["id"] for x in r.data], [rid])
        r2 = self.client.get(reverse("online-monitoring-list"), {"status": "APPROVED"})
        self.assertEqual(r2.data, [])

    def test_specs_endpoint_returns_global_and_company(self):
        # The seed data-migration is skipped under test syncdb, so create the
        # global defaults here to exercise the endpoint (company OR global).
        OnlineQualitySpec.objects.create(
            company=None, parameter_key="ph", parameter_name="pH",
            min_value=6.5, max_value=8.5, validation_type=SpecValidationType.RANGE,
        )
        OnlineQualitySpec.objects.create(
            company=self.company, parameter_key="torque", parameter_name="Torque",
            min_value=8, max_value=12, validation_type=SpecValidationType.RANGE,
        )
        r = self.client.get(reverse("online-monitoring-specs"))
        keys = {s["parameter_key"] for s in r.data}
        self.assertIn("ph", keys)  # global
        self.assertIn("torque", keys)  # company

    def test_company_scoping(self):
        rid = self._create_record()
        other = Company.objects.create(code="OTHER_CO", name="Other")
        other_client = _client(other)
        # Other company can't see this record.
        self.assertEqual(
            other_client.get(reverse("online-monitoring-detail", args=[rid])).status_code, 404
        )


class OnlineMonitoringPermissionTests(APITestCase):
    def setUp(self):
        self.company = Company.objects.create(code="TEST_CO", name="Test Co")
        self.line = ProductionLine.objects.create(company=self.company, name="Line-1")

    def test_operator_cannot_approve(self):
        # Operator: view + create only (no approve).
        operator = _client(self.company, perms=[
            "can_view_online_monitoring", "can_create_online_monitoring",
            "can_submit_online_monitoring",
        ])
        rid = operator.post(reverse("online-monitoring-list"), {
            "production_line_id": self.line.id, "date": str(date.today()),
        }, format="json").data["id"]
        operator.post(reverse("online-monitoring-reading-create", args=[rid]),
                      {"reading_time": "08:00"}, format="json")
        operator.post(reverse("online-monitoring-submit", args=[rid]))
        resp = operator.post(reverse("online-monitoring-approve", args=[rid]))
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)


class SpecValidationTests(APITestCase):
    def test_is_within_spec(self):
        ph = OnlineQualitySpec(
            parameter_key="ph", parameter_name="pH", min_value=6.5, max_value=8.5,
            validation_type=SpecValidationType.RANGE,
        )
        self.assertTrue(ph.is_within_spec(7.2))
        self.assertFalse(ph.is_within_spec(9.0))
        self.assertFalse(ph.is_within_spec(6.0))
        self.assertIsNone(ph.is_within_spec(None))

        turb = OnlineQualitySpec(
            parameter_key="turbidity", parameter_name="Turbidity", max_value=1,
            validation_type=SpecValidationType.MAX,
        )
        self.assertTrue(turb.is_within_spec(0.5))
        self.assertFalse(turb.is_within_spec(2))

        torque = OnlineQualitySpec(
            parameter_key="torque", parameter_name="Torque", min_value=8, max_value=12,
            validation_type=SpecValidationType.RANGE,
        )
        self.assertTrue(torque.is_within_spec(10))
        self.assertFalse(torque.is_within_spec(7))
        self.assertFalse(torque.is_within_spec(13))
