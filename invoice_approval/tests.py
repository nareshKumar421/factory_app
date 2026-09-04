"""Tests for the SAP invoice-approval module.

Endpoint tests mock :class:`sap_client.client.SAPClient` at the view boundary
(no HANA / Service Layer network). Writer tests mock ``requests`` to check the
decision payload SAP receives and the exception translation.
"""
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.test import TestCase, override_settings
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from company.models import Company, UserCompany, UserRole
from sap_client.exceptions import SAPConnectionError, SAPValidationError
from sap_client.service_layer.approval_writer import ApprovalRequestWriter
from warehouse.models_manager import UserWarehouse

from .models import InvoiceApprovalAudit

User = get_user_model()
COMPANY_CODE = "TC001"
WH = "GP-FG"
OTHER_WH = "JB-FG"  # a warehouse the approver does NOT manage

BASE = "/api/v1/invoice-approvals/invoices/"


def _row(wdd_code, so, party, amount, row_status):
    return {
        "id": wdd_code,
        "doc_entry": 55000 + wdd_code,
        "doc_num": 626090000 + wdd_code,
        "so_number": so,
        "card_code": "CUSTA000001",
        "party_name": party,
        "total_amount": amount,
        "branch": "FACTORY",
        "warehouse": WH,
        "status": row_status,
        "rejection_reason": None,
        "error_message": None,
        "invoice_payload": {"DocObjectCode": "13", "DocumentLines": []},
        "fg_stock": [],
        "created_at": "2026-09-02T13:16:00",
        "created_by": "SUMIT",
    }


class ApprovalEndpointTestData:
    """Shared fixture for the endpoint tests: company, users + permissions, and
    the warehouse scope (approver/viewer manage GP-FG; nobody manages JB-FG)."""

    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Test Co", code=COMPANY_CODE)
        cls.role = UserRole.objects.create(name="Approver")

        cls.approver = User.objects.create_user(
            email="approver@example.com", password="pass12345",
            full_name="Approver User", employee_code="IA-APP",
        )
        cls.viewer = User.objects.create_user(
            email="viewer@example.com", password="pass12345",
            full_name="Viewer User", employee_code="IA-VIEW",
        )
        cls.outsider = User.objects.create_user(
            email="outsider@example.com", password="pass12345",
            full_name="Outsider User", employee_code="IA-OUT",
        )
        for user in (cls.approver, cls.viewer, cls.outsider):
            UserCompany.objects.create(
                user=user, company=cls.company, role=cls.role, is_active=True
            )

        # Mirror the 0002 data migration (which does not run when tests disable
        # migrations): the "Invoice Approval" group carries both permissions.
        group, _ = Group.objects.get_or_create(name="Invoice Approval")
        group.permissions.add(
            *Permission.objects.filter(
                content_type__app_label="invoice_approval",
                codename__in=["view_invoice", "approve_invoice"],
            )
        )
        cls.approver.groups.add(group)
        cls.viewer.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="invoice_approval", codename="view_invoice"
            )
        )

        # The page is scoped to the warehouses a user manages (warehouse.UserWarehouse).
        # Approver and viewer manage GP-FG; nobody manages OTHER_WH.
        for user in (cls.approver, cls.viewer):
            UserWarehouse.objects.create(
                user=user, company=cls.company, warehouse_code=WH
            )

        cls.super = User.objects.create_superuser(
            email="root@example.com", password="pass12345",
            full_name="Root User", employee_code="IA-ROOT",
        )
        UserCompany.objects.create(
            user=cls.super, company=cls.company, role=cls.role, is_active=True
        )

    def client_for(self, user):
        client = APIClient()
        client.force_authenticate(user=user)
        return client


class InvoiceApprovalEndpointTests(ApprovalEndpointTestData, APITestCase):
    def setUp(self):
        self.client = self.client_for(self.approver)
        patcher = mock.patch("invoice_approval.views.SAPClient")
        self.SAPClient = patcher.start()
        self.addCleanup(patcher.stop)
        self.sap = self.SAPClient.return_value
        # Every pending request in these tests ships from the managed warehouse.
        self.sap.invoice_approval_warehouses.return_value = {WH}

    # ── list (warehouse-scoped) ─────────────────────────────────────────────
    def test_list_requires_warehouse(self):
        resp = self.client.get(BASE, HTTP_COMPANY_CODE=COMPANY_CODE)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.sap.list_invoice_approvals.assert_not_called()

    def test_list_passes_warehouse_and_status(self):
        self.sap.list_invoice_approvals.return_value = [
            _row(73791, "1726086801", "ONENESS TRADERS", "68880.00", "PENDING"),
        ]
        resp = self.client.get(
            f"{BASE}?whs={WH}&status=PENDING", HTTP_COMPANY_CODE=COMPANY_CODE
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.json()), 1)
        self.assertEqual(resp.json()[0]["status"], "PENDING")
        self.sap.list_invoice_approvals.assert_called_once_with(
            warehouse=WH, status="PENDING"
        )
        self.SAPClient.assert_called_with(company_code=COMPANY_CODE)

    def test_list_rejects_bad_status(self):
        resp = self.client.get(
            f"{BASE}?whs={WH}&status=EDITED", HTTP_COMPANY_CODE=COMPANY_CODE
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_sap_validation_error_maps_to_400(self):
        self.sap.list_invoice_approvals.side_effect = SAPValidationError("bad input")
        resp = self.client.get(f"{BASE}?whs={WH}", HTTP_COMPANY_CODE=COMPANY_CODE)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_sap_connection_error_maps_to_503(self):
        self.sap.list_invoice_approvals.side_effect = SAPConnectionError("down")
        resp = self.client.get(f"{BASE}?whs={WH}", HTTP_COMPANY_CODE=COMPANY_CODE)
        self.assertEqual(resp.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)

    # ── approve / reject + audit ──────────────────────────────────────────────
    def test_approve_decides_in_sap_and_writes_audit(self):
        self.sap.decide_invoice_approval.return_value = {"message": "Invoice approved in SAP."}
        resp = self.client.patch(
            f"{BASE}73791/status/",
            {"status": "APPROVED", "so_number": "1726086801",
             "party_name": "ONENESS TRADERS", "total_amount": "68880.00"},
            format="json", HTTP_COMPANY_CODE=COMPANY_CODE,
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        args, kwargs = self.sap.decide_invoice_approval.call_args
        self.assertEqual(args[0], 73791)
        self.assertTrue(kwargs["approve"])
        self.assertIn("Approver User", kwargs["remarks"])

        audit = InvoiceApprovalAudit.objects.get(approval_code=73791)
        self.assertEqual(audit.decision, "APPROVED")
        self.assertEqual(audit.created_by, self.approver)
        self.assertEqual(audit.company, self.company)
        self.assertEqual(audit.so_number, "1726086801")
        self.assertEqual(audit.sap_message, "Invoice approved in SAP.")

    def test_reject_requires_reason(self):
        resp = self.client.patch(
            f"{BASE}73791/status/",
            {"status": "REJECTED"}, format="json", HTTP_COMPANY_CODE=COMPANY_CODE,
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.sap.decide_invoice_approval.assert_not_called()
        self.assertFalse(InvoiceApprovalAudit.objects.filter(approval_code=73791).exists())

    def test_reject_with_reason_decides_and_writes_audit(self):
        self.sap.decide_invoice_approval.return_value = {"message": "Invoice rejected in SAP."}
        resp = self.client.patch(
            f"{BASE}73791/status/",
            {"status": "REJECTED", "rejection_reason": "Stock mismatch"},
            format="json", HTTP_COMPANY_CODE=COMPANY_CODE,
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        args, kwargs = self.sap.decide_invoice_approval.call_args
        self.assertFalse(kwargs["approve"])
        self.assertIn("Stock mismatch", kwargs["remarks"])

        audit = InvoiceApprovalAudit.objects.get(approval_code=73791)
        self.assertEqual(audit.decision, "REJECTED")
        self.assertEqual(audit.rejection_reason, "Stock mismatch")

    def test_sap_failure_writes_no_audit(self):
        self.sap.decide_invoice_approval.side_effect = SAPValidationError("already approved")
        resp = self.client.patch(
            f"{BASE}73791/status/",
            {"status": "APPROVED"}, format="json", HTTP_COMPANY_CODE=COMPANY_CODE,
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(InvoiceApprovalAudit.objects.filter(approval_code=73791).exists())

    # ── history / pending-count / audit ───────────────────────────────────────
    def test_history(self):
        self.sap.invoice_approval_history.return_value = [
            {"id": 7379100, "status": "PENDING", "created_by_name": "SUMIT",
             "remarks": None, "created_at": "2026-09-02T13:16:00"},
        ]
        resp = self.client.get(f"{BASE}73791/history/", HTTP_COMPANY_CODE=COMPANY_CODE)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.json()), 1)
        self.sap.invoice_approval_history.assert_called_once_with(73791)

    def test_pending_count_requires_whs(self):
        resp = self.client.get(f"{BASE}pending-count/", HTTP_COMPANY_CODE=COMPANY_CODE)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_pending_count(self):
        self.sap.count_pending_invoice_approvals.return_value = 3
        resp = self.client.get(
            f"{BASE}pending-count/?whs={WH}", HTTP_COMPANY_CODE=COMPANY_CODE
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.json(), {"pending": 3, "total": 3})

    def test_local_audit_endpoint(self):
        self.sap.decide_invoice_approval.return_value = {"message": "ok"}
        self.client.patch(
            f"{BASE}73791/status/", {"status": "APPROVED"},
            format="json", HTTP_COMPANY_CODE=COMPANY_CODE,
        )
        resp = self.client.get(f"{BASE}73791/audit/", HTTP_COMPANY_CODE=COMPANY_CODE)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.json()), 1)
        self.assertEqual(resp.json()[0]["decision"], "APPROVED")
        self.assertEqual(resp.json()[0]["acted_by_name"], "Approver User")

    # ── auth / permission gating ──────────────────────────────────────────────
    def test_requires_company_header(self):
        resp = self.client.get(f"{BASE}?whs={WH}")
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_viewer_cannot_approve(self):
        client = self.client_for(self.viewer)
        resp = client.patch(
            f"{BASE}73791/status/", {"status": "APPROVED"},
            format="json", HTTP_COMPANY_CODE=COMPANY_CODE,
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_outsider_cannot_view(self):
        client = self.client_for(self.outsider)
        resp = client.get(f"{BASE}?whs={WH}", HTTP_COMPANY_CODE=COMPANY_CODE)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    # ── warehouse-manager scoping ─────────────────────────────────────────────
    def test_list_rejects_unmanaged_warehouse(self):
        resp = self.client.get(f"{BASE}?whs={OTHER_WH}", HTTP_COMPANY_CODE=COMPANY_CODE)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.sap.list_invoice_approvals.assert_not_called()

    def test_pending_count_rejects_unmanaged_warehouse(self):
        resp = self.client.get(
            f"{BASE}pending-count/?whs={OTHER_WH}", HTTP_COMPANY_CODE=COMPANY_CODE
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.sap.count_pending_invoice_approvals.assert_not_called()

    def test_decide_rejected_for_unmanaged_warehouse(self):
        # The invoice behind this request ships from a warehouse the user doesn't manage.
        self.sap.invoice_approval_warehouses.return_value = {OTHER_WH}
        resp = self.client.patch(
            f"{BASE}73791/status/", {"status": "APPROVED"},
            format="json", HTTP_COMPANY_CODE=COMPANY_CODE,
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.sap.decide_invoice_approval.assert_not_called()
        self.assertFalse(InvoiceApprovalAudit.objects.filter(approval_code=73791).exists())

    def test_history_rejected_for_unmanaged_warehouse(self):
        self.sap.invoice_approval_warehouses.return_value = {OTHER_WH}
        resp = self.client.get(f"{BASE}73791/history/", HTTP_COMPANY_CODE=COMPANY_CODE)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.sap.invoice_approval_history.assert_not_called()

    def test_superuser_is_unrestricted(self):
        # A superuser manages no warehouse explicitly but may see any of them.
        self.sap.list_invoice_approvals.return_value = []
        client = self.client_for(self.super)
        resp = client.get(f"{BASE}?whs={OTHER_WH}", HTTP_COMPANY_CODE=COMPANY_CODE)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)


OMS_BASE = "/api/v1/invoice-approvals/oms-invoices/"


@override_settings(OMS_ENABLED=True, OMS_SIMULATE=True)
class OmsInvoiceEndpointTests(ApprovalEndpointTestData, APITestCase):
    """The OMS proxy endpoints, run against the simulate fixtures (no network).

    The fixtures in ``invoice_approval.oms`` carry GP-FG (= the managed WH)
    invoices: 2 PENDING (ids 74/75), 1 APPROVED, 1 REJECTED — plus one JB-FG
    EDITED entry that must never appear in a GP-FG list.
    """

    def setUp(self):
        self.client = self.client_for(self.approver)

    def test_disabled_module_maps_to_503(self):
        with override_settings(OMS_ENABLED=False):
            resp = self.client.get(f"{OMS_BASE}?whs={WH}", HTTP_COMPANY_CODE=COMPANY_CODE)
        self.assertEqual(resp.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)

    def test_list_requires_warehouse(self):
        resp = self.client.get(OMS_BASE, HTTP_COMPANY_CODE=COMPANY_CODE)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_list_filters_by_warehouse_and_status(self):
        resp = self.client.get(
            f"{OMS_BASE}?whs={WH}&status=PENDING", HTTP_COMPANY_CODE=COMPANY_CODE
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        rows = resp.json()
        self.assertEqual({r["id"] for r in rows}, {74, 75})
        self.assertTrue(all(r["warehouse"] == WH for r in rows))

    def test_list_rejects_unmanaged_warehouse(self):
        resp = self.client.get(f"{OMS_BASE}?whs={OTHER_WH}", HTTP_COMPANY_CODE=COMPANY_CODE)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_approve_writes_oms_audit(self):
        resp = self.client.patch(
            f"{OMS_BASE}74/status/",
            {"status": "APPROVED", "warehouse": WH, "so_number": "1726056787",
             "party_name": "G PURE INDIA", "total_amount": "104000.00"},
            format="json", HTTP_COMPANY_CODE=COMPANY_CODE,
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        audit = InvoiceApprovalAudit.objects.get(
            approval_code=74, source=InvoiceApprovalAudit.SOURCE_OMS
        )
        self.assertEqual(audit.decision, "APPROVED")
        self.assertEqual(audit.created_by, self.approver)
        self.assertEqual(audit.so_number, "1726056787")

    def test_reject_requires_reason(self):
        resp = self.client.patch(
            f"{OMS_BASE}74/status/",
            {"status": "REJECTED"}, format="json", HTTP_COMPANY_CODE=COMPANY_CODE,
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(InvoiceApprovalAudit.objects.filter(approval_code=74).exists())

    def test_decide_rejected_for_unmanaged_warehouse(self):
        resp = self.client.patch(
            f"{OMS_BASE}74/status/",
            {"status": "APPROVED", "warehouse": OTHER_WH},
            format="json", HTTP_COMPANY_CODE=COMPANY_CODE,
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(InvoiceApprovalAudit.objects.filter(approval_code=74).exists())

    def test_pending_count(self):
        resp = self.client.get(
            f"{OMS_BASE}pending-count/?whs={WH}", HTTP_COMPANY_CODE=COMPANY_CODE
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.json(), {"pending": 2, "total": 2})

    def test_local_audit_is_source_scoped(self):
        # A SAP audit row under the SAME numeric code must not leak into the OMS
        # audit trail — OMS ids and SAP WddCodes are different id-spaces.
        InvoiceApprovalAudit.objects.create(
            approval_code=74, source=InvoiceApprovalAudit.SOURCE_SAP,
            decision="REJECTED", company=self.company, created_by=self.approver,
        )
        self.client.patch(
            f"{OMS_BASE}74/status/", {"status": "APPROVED", "warehouse": WH},
            format="json", HTTP_COMPANY_CODE=COMPANY_CODE,
        )
        resp = self.client.get(f"{OMS_BASE}74/audit/", HTTP_COMPANY_CODE=COMPANY_CODE)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.json()), 1)
        self.assertEqual(resp.json()[0]["decision"], "APPROVED")
        self.assertEqual(resp.json()[0]["source"], "OMS")

    def test_sap_audit_endpoint_excludes_oms_rows(self):
        # The mirror of the above: the SAP audit route must not serve OMS rows.
        InvoiceApprovalAudit.objects.create(
            approval_code=74, source=InvoiceApprovalAudit.SOURCE_OMS,
            decision="APPROVED", company=self.company, created_by=self.approver,
        )
        resp = self.client.get(f"{BASE}74/audit/", HTTP_COMPANY_CODE=COMPANY_CODE)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.json(), [])

    def test_viewer_cannot_approve(self):
        client = self.client_for(self.viewer)
        resp = client.patch(
            f"{OMS_BASE}74/status/", {"status": "APPROVED"},
            format="json", HTTP_COMPANY_CODE=COMPANY_CODE,
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_history_passthrough(self):
        resp = self.client.get(f"{OMS_BASE}75/history/", HTTP_COMPANY_CODE=COMPANY_CODE)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.json()), 2)


def _resp(status_code, json_data=None, text=""):
    m = mock.Mock()
    m.status_code = status_code
    m.text = text
    if json_data is None:
        m.json.side_effect = ValueError("no json")
    else:
        m.json.return_value = json_data
    return m


class _FakeContext:
    service_layer = {
        "base_url": "https://sap.test:50000",
        "company_db": "TESTDB",
        "username": "sl_user",
        "password": "sl_pass",
    }


class ApprovalRequestWriterTests(TestCase):
    """Decision payload shaping + exception translation (requests fully mocked)."""

    def setUp(self):
        self.writer = ApprovalRequestWriter(_FakeContext())
        login = mock.patch.object(
            ApprovalRequestWriter, "_get_session_cookies", return_value={"B1SESSION": "x"}
        )
        login.start()
        self.addCleanup(login.stop)

    def test_approve_sends_decision_payload(self):
        pending = _resp(200, {"Code": 73791, "Status": "arsPending", "DraftEntry": 55890})
        with mock.patch(
            "sap_client.service_layer.approval_writer.requests.get", return_value=pending
        ), mock.patch(
            "sap_client.service_layer.approval_writer.requests.patch",
            return_value=_resp(204),
        ) as patched:
            result = self.writer.decide(73791, approve=True, remarks="ok by tester")

        self.assertEqual(result, {"message": "Invoice approved in SAP."})
        url = patched.call_args[0][0]
        self.assertIn("/b1s/v2/ApprovalRequests(73791)", url)
        body = patched.call_args[1]["json"]
        decision = body["ApprovalRequestDecisions"][0]
        self.assertEqual(decision["Status"], "ardApproved")
        self.assertEqual(decision["ApproverUserName"], "sl_user")
        self.assertEqual(decision["ApproverPassword"], "sl_pass")
        self.assertEqual(decision["Remarks"], "ok by tester")

    def test_configured_approver_signs_the_decision(self):
        ctx = _FakeContext()
        ctx.service_layer = dict(
            _FakeContext.service_layer,
            approval_username="manager",
            approval_password="secret",
        )
        writer = ApprovalRequestWriter(ctx)
        with mock.patch.object(
            ApprovalRequestWriter, "_get_session_cookies", return_value={"B1SESSION": "x"}
        ), mock.patch(
            "sap_client.service_layer.approval_writer.requests.get",
            return_value=_resp(200, {"Code": 73791, "Status": "arsPending"}),
        ), mock.patch(
            "sap_client.service_layer.approval_writer.requests.patch",
            return_value=_resp(204),
        ) as patched:
            writer.decide(73791, approve=True)
        decision = patched.call_args[1]["json"]["ApprovalRequestDecisions"][0]
        self.assertEqual(decision["ApproverUserName"], "manager")
        self.assertEqual(decision["ApproverPassword"], "secret")

    def test_reject_sends_not_approved(self):
        pending = _resp(200, {"Code": 73791, "Status": "arsPending"})
        with mock.patch(
            "sap_client.service_layer.approval_writer.requests.get", return_value=pending
        ), mock.patch(
            "sap_client.service_layer.approval_writer.requests.patch",
            return_value=_resp(204),
        ) as patched:
            self.writer.decide(73791, approve=False, remarks="stock short")
        decision = patched.call_args[1]["json"]["ApprovalRequestDecisions"][0]
        self.assertEqual(decision["Status"], "ardNotApproved")

    def test_already_decided_is_validation_error(self):
        decided = _resp(200, {"Code": 73791, "Status": "arsApproved"})
        with mock.patch(
            "sap_client.service_layer.approval_writer.requests.get", return_value=decided
        ):
            with self.assertRaises(SAPValidationError):
                self.writer.decide(73791, approve=True)

    def test_missing_request_is_validation_error(self):
        with mock.patch(
            "sap_client.service_layer.approval_writer.requests.get",
            return_value=_resp(404),
        ):
            with self.assertRaises(SAPValidationError):
                self.writer.decide(999999, approve=True)

    def test_sap_400_message_passes_through(self):
        pending = _resp(200, {"Code": 73791, "Status": "arsPending"})
        error = _resp(400, {"error": {"message": {"value": "User is not an approver"}}})
        with mock.patch(
            "sap_client.service_layer.approval_writer.requests.get", return_value=pending
        ), mock.patch(
            "sap_client.service_layer.approval_writer.requests.patch", return_value=error
        ):
            with self.assertRaises(SAPValidationError) as ctx:
                self.writer.decide(73791, approve=True)
        self.assertIn("not an approver", str(ctx.exception))

    def test_connection_error_maps(self):
        import requests as requests_lib
        with mock.patch(
            "sap_client.service_layer.approval_writer.requests.get",
            side_effect=requests_lib.exceptions.ConnectionError(),
        ):
            with self.assertRaises(SAPConnectionError):
                self.writer.decide(73791, approve=True)
