"""Tests for the OMS invoice-approval proxy.

Endpoint tests run against ``OMS_SIMULATE=True`` (fixtures, no network) and double
as the offline smoke test. Client tests mock ``requests`` to check exception
translation on the live (non-simulate) path.
"""
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.test import TestCase, override_settings
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from company.models import Company, UserCompany, UserRole

from .client import OmsClient
from .exceptions import OMSConnectionError, OMSDataError, OMSValidationError
from .models import InvoiceApprovalAudit

User = get_user_model()
COMPANY_CODE = "TC001"


@override_settings(OMS_ENABLED=True, OMS_SIMULATE=True, OMS_AUTH_ENABLED=False)
class OmsInvoiceEndpointTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Test Co", code=COMPANY_CODE)
        cls.role = UserRole.objects.create(name="Approver")

        cls.approver = User.objects.create_user(
            email="approver@example.com", password="pass12345",
            full_name="Approver User", employee_code="OMS-APP",
        )
        cls.viewer = User.objects.create_user(
            email="viewer@example.com", password="pass12345",
            full_name="Viewer User", employee_code="OMS-VIEW",
        )
        cls.outsider = User.objects.create_user(
            email="outsider@example.com", password="pass12345",
            full_name="Outsider User", employee_code="OMS-OUT",
        )
        for user in (cls.approver, cls.viewer, cls.outsider):
            UserCompany.objects.create(
                user=user, company=cls.company, role=cls.role, is_active=True
            )

        # Approver gets both perms via the group the data migration seeded.
        cls.approver.groups.add(Group.objects.get(name="Invoice Approval"))
        # Viewer gets read-only.
        cls.viewer.user_permissions.add(
            Permission.objects.get(content_type__app_label="oms", codename="view_invoice")
        )

    def client_for(self, user):
        client = APIClient()
        client.force_authenticate(user=user)
        return client

    def setUp(self):
        self.client = self.client_for(self.approver)

    # ── list ──────────────────────────────────────────────────────────────────
    def test_list_all(self):
        resp = self.client.get("/api/v1/oms/invoices/", HTTP_COMPANY_CODE=COMPANY_CODE)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.json()), 5)

    def test_list_pending_only(self):
        resp = self.client.get(
            "/api/v1/oms/invoices/?status=PENDING", HTTP_COMPANY_CODE=COMPANY_CODE
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        statuses = {row["status"] for row in resp.json()}
        self.assertEqual(statuses, {"PENDING"})

    def test_list_rejects_bad_status(self):
        resp = self.client.get(
            "/api/v1/oms/invoices/?status=BOGUS", HTTP_COMPANY_CODE=COMPANY_CODE
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    # ── approve / reject + audit ──────────────────────────────────────────────
    def test_approve_writes_audit(self):
        resp = self.client.patch(
            "/api/v1/oms/invoices/74/status/",
            {"status": "APPROVED", "so_number": "1726056787",
             "party_name": "G PURE INDIA", "total_amount": "104000.00"},
            format="json", HTTP_COMPANY_CODE=COMPANY_CODE,
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        audit = InvoiceApprovalAudit.objects.get(invoice_log_id=74)
        self.assertEqual(audit.decision, "APPROVED")
        self.assertEqual(audit.created_by, self.approver)
        self.assertEqual(audit.company, self.company)
        self.assertEqual(audit.so_number, "1726056787")

    def test_reject_requires_reason(self):
        resp = self.client.patch(
            "/api/v1/oms/invoices/74/status/",
            {"status": "REJECTED"}, format="json", HTTP_COMPANY_CODE=COMPANY_CODE,
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(InvoiceApprovalAudit.objects.filter(invoice_log_id=74).exists())

    def test_reject_with_reason_writes_audit(self):
        resp = self.client.patch(
            "/api/v1/oms/invoices/75/status/",
            {"status": "REJECTED", "rejection_reason": "Stock mismatch"},
            format="json", HTTP_COMPANY_CODE=COMPANY_CODE,
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        audit = InvoiceApprovalAudit.objects.get(invoice_log_id=75)
        self.assertEqual(audit.decision, "REJECTED")
        self.assertEqual(audit.rejection_reason, "Stock mismatch")

    # ── history / pending-count / audit ───────────────────────────────────────
    def test_history(self):
        resp = self.client.get(
            "/api/v1/oms/invoices/75/history/", HTTP_COMPANY_CODE=COMPANY_CODE
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertIsInstance(resp.json(), list)

    def test_pending_count(self):
        resp = self.client.get(
            "/api/v1/oms/invoices/pending-count/", HTTP_COMPANY_CODE=COMPANY_CODE
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        body = resp.json()
        self.assertEqual(body["pending"], 2)
        self.assertEqual(body["edited"], 1)
        self.assertEqual(body["total"], 3)

    def test_local_audit_endpoint(self):
        self.client.patch(
            "/api/v1/oms/invoices/74/status/", {"status": "APPROVED"},
            format="json", HTTP_COMPANY_CODE=COMPANY_CODE,
        )
        resp = self.client.get(
            "/api/v1/oms/invoices/74/audit/", HTTP_COMPANY_CODE=COMPANY_CODE
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.json()), 1)
        self.assertEqual(resp.json()[0]["decision"], "APPROVED")

    # ── auth / permission gating ──────────────────────────────────────────────
    def test_requires_company_header(self):
        resp = self.client.get("/api/v1/oms/invoices/")
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_viewer_cannot_approve(self):
        client = self.client_for(self.viewer)
        resp = client.patch(
            "/api/v1/oms/invoices/74/status/", {"status": "APPROVED"},
            format="json", HTTP_COMPANY_CODE=COMPANY_CODE,
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_outsider_cannot_view(self):
        client = self.client_for(self.outsider)
        resp = client.get("/api/v1/oms/invoices/", HTTP_COMPANY_CODE=COMPANY_CODE)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)


def _resp(status_code, json_data=None, text=""):
    m = mock.Mock()
    m.status_code = status_code
    m.text = text
    if json_data is None:
        m.json.side_effect = ValueError("no json")
    else:
        m.json.return_value = json_data
    return m


@override_settings(
    OMS_ENABLED=True, OMS_SIMULATE=False, OMS_AUTH_ENABLED=False,
    OMS_BASE_URL="http://oms.test",
)
class OmsClientTests(TestCase):
    """Exception translation on the live (non-simulate) path."""

    def test_list_maps_connection_error(self):
        import requests
        with mock.patch("oms.client.requests.request",
                        side_effect=requests.exceptions.ConnectionError()):
            with self.assertRaises(OMSConnectionError):
                OmsClient().list_invoices()

    def test_list_maps_timeout(self):
        import requests
        with mock.patch("oms.client.requests.request",
                        side_effect=requests.exceptions.Timeout()):
            with self.assertRaises(OMSConnectionError):
                OmsClient().list_invoices()

    def test_list_non_array_is_data_error(self):
        with mock.patch("oms.client.requests.request", return_value=_resp(200, {"oops": 1})):
            with self.assertRaises(OMSDataError):
                OmsClient().list_invoices()

    def test_update_status_400_is_validation_error(self):
        with mock.patch("oms.client.requests.request",
                        return_value=_resp(400, {"detail": "bad"})):
            with self.assertRaises(OMSValidationError):
                OmsClient().update_status(74, "APPROVED")

    def test_update_status_local_validation(self):
        # Reject without a reason is caught before any network call.
        with self.assertRaises(OMSValidationError):
            OmsClient().update_status(74, "REJECTED")

    def test_list_success_returns_array(self):
        with mock.patch("oms.client.requests.request",
                        return_value=_resp(200, [{"id": 1, "status": "PENDING"}])):
            self.assertEqual(OmsClient().list_invoices()[0]["id"], 1)
