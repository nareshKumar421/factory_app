"""Tests for the Production-QC "running runs" selection endpoint."""

from datetime import date

from django.utils import timezone
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from company.models import Company, UserCompany, UserRole
from production_execution.models import ProductionLine, ProductionRun, RunStatus
from quality_control.models import ProductionQCSession
from quality_control.models.production_qc_session import ProductionQCSessionType

User = get_user_model()


class RunningRunsForQCTests(APITestCase):
    def setUp(self):
        self.company = Company.objects.create(code="TEST_CO", name="Test Co")
        self.user = User.objects.create_user(email="qc@test.com", password="x")
        role = UserRole.objects.create(name="QC")
        UserCompany.objects.create(
            user=self.user, company=self.company, role=role, is_active=True
        )
        # Grant QC + production permissions.
        self.user.user_permissions.set(
            Permission.objects.filter(
                content_type__app_label__in=["quality_control", "production_execution"]
            )
        )
        self.user = User.objects.get(pk=self.user.pk)

        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.client.credentials(HTTP_COMPANY_CODE="TEST_CO")

        self.line_a = ProductionLine.objects.create(company=self.company, name="Line-A")
        self.line_b = ProductionLine.objects.create(company=self.company, name="Line-B")

        self.running = ProductionRun.objects.create(
            company=self.company, run_number=1, date=date.today(),
            line=self.line_a, product="Oil 1L", item_code="FG0000001",
            status=RunStatus.IN_PROGRESS,
        )
        self.running_b = ProductionRun.objects.create(
            company=self.company, run_number=2, date=date.today(),
            line=self.line_b, product="Oil 5L", status=RunStatus.IN_PROGRESS,
        )
        # Should NOT appear: draft + completed.
        ProductionRun.objects.create(
            company=self.company, run_number=3, date=date.today(),
            line=self.line_a, status=RunStatus.DRAFT,
        )
        ProductionRun.objects.create(
            company=self.company, run_number=4, date=date.today(),
            line=self.line_a, status=RunStatus.COMPLETED,
        )

        self.url = reverse("production-qc-running-runs")

    def test_lists_only_in_progress_runs(self):
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        ids = {r["id"] for r in resp.data}
        self.assertEqual(ids, {self.running.id, self.running_b.id})
        row = next(r for r in resp.data if r["id"] == self.running.id)
        self.assertEqual(row["line_name"], "Line-A")
        self.assertEqual(row["product"], "Oil 1L")
        self.assertEqual(row["status"], RunStatus.IN_PROGRESS)
        # No active segment/breakdown => STOPPED live status.
        self.assertEqual(row["live_status"], "STOPPED")

    def test_line_filter(self):
        resp = self.client.get(self.url, {"line": self.line_b.id})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual([r["id"] for r in resp.data], [self.running_b.id])

    def test_qc_progress_hint(self):
        # No QC yet.
        row = next(r for r in self.client.get(self.url).data if r["id"] == self.running.id)
        self.assertEqual(row["inprocess_qc_count"], 0)
        self.assertIsNone(row["latest_inprocess_status"])
        self.assertFalse(row["has_pending_qc"])

        # Add an in-process DRAFT session -> count 1, pending true.
        ProductionQCSession.objects.create(
            production_run=self.running, session_number=1,
            session_type=ProductionQCSessionType.IN_PROCESS,
            checked_at=timezone.now(),
        )
        row = next(r for r in self.client.get(self.url).data if r["id"] == self.running.id)
        self.assertEqual(row["inprocess_qc_count"], 1)
        self.assertEqual(row["latest_inprocess_status"], "DRAFT")
        self.assertTrue(row["has_pending_qc"])

    def test_requires_authentication(self):
        self.client.force_authenticate(user=None)
        self.client.credentials()
        self.assertEqual(
            self.client.get(self.url).status_code, status.HTTP_401_UNAUTHORIZED
        )
