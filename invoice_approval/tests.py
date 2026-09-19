"""Tests for the SAP invoice-approval module.

Endpoint tests mock :class:`sap_client.client.SAPClient` at the view boundary
(no HANA / Service Layer network). Writer tests mock ``requests`` to check the
decision payload SAP receives and the exception translation.
"""
from unittest import mock

import requests
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from company.models import Company, UserCompany, UserRole
from sap_client.models import SapApproverIdentity
from sap_client.exceptions import SAPConnectionError, SAPValidationError
from sap_client.service_layer.approval_writer import ApprovalRequestWriter
from warehouse.models_manager import UserWarehouse

from .models import InvoiceApprovalAudit

User = get_user_model()
COMPANY_CODE = "TC001"
# The SAP account our test approver IS. SAP accepts a decision only from the
# authorizer named on the request's current stage.
SAP_APPROVER = "USER37"
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

        # SAP accepts a decision only from the authorizer named on the request's
        # current stage, so the approver has to be mapped to that SAP account.
        SapApproverIdentity.objects.create(
            user=cls.approver, company=cls.company,
            sap_user_code=SAP_APPROVER, sap_user_name="Approver User",
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


@override_settings(
    SAP_APPROVER_CREDENTIALS={COMPANY_CODE: {SAP_APPROVER: "stub-password"}}
)
class InvoiceApprovalEndpointTests(ApprovalEndpointTestData, APITestCase):
    def setUp(self):
        self.client = self.client_for(self.approver)
        patcher = mock.patch("invoice_approval.views.SAPClient")
        self.SAPClient = patcher.start()
        self.addCleanup(patcher.stop)
        self.sap = self.SAPClient.return_value
        # By default SAP is waiting on the account our approver is mapped to.
        self.sap.invoice_approval_stage.return_value = self._stage()

    @staticmethod
    def _stage(**overrides):
        stage = {
            "id": 73791,
            "status": "PENDING",
            "current_step": 23,
            "draft_entry": 56621,
            "doc_num": 826676551,
            "party_name": "PRIME SALES CORPORATION",
            "approver_code": SAP_APPROVER,
            "approver_name": "Approver User",
        }
        stage.update(overrides)
        return stage
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

    def test_the_decision_is_signed_as_the_authorizer_sap_named(self):
        """Not as the shared SL account, which SAP refuses with -6006."""
        self.sap.decide_invoice_approval.return_value = {"message": "ok"}
        resp = self.client.patch(
            f"{BASE}73791/status/", {"status": "APPROVED"},
            format="json", HTTP_COMPANY_CODE=COMPANY_CODE,
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(
            self.sap.decide_invoice_approval.call_args.kwargs["approver"], SAP_APPROVER
        )

    def test_someone_elses_invoice_is_refused_without_calling_sap(self):
        self.sap.invoice_approval_stage.return_value = self._stage(
            approver_code="USER26", approver_name="HARPREET SINGH"
        )
        resp = self.client.patch(
            f"{BASE}73791/status/", {"status": "APPROVED"},
            format="json", HTTP_COMPANY_CODE=COMPANY_CODE,
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("USER26", resp.json()["detail"])
        self.sap.decide_invoice_approval.assert_not_called()

    def test_an_unmapped_approver_is_refused(self):
        SapApproverIdentity.objects.filter(user=self.approver).delete()
        resp = self.client.patch(
            f"{BASE}73791/status/", {"status": "APPROVED"},
            format="json", HTTP_COMPANY_CODE=COMPANY_CODE,
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("not linked to a SAP user", resp.json()["detail"])
        self.sap.decide_invoice_approval.assert_not_called()

    @override_settings(SAP_APPROVER_CREDENTIALS={COMPANY_CODE: {}})
    def test_my_own_missing_password_is_refused(self):
        resp = self.client.patch(
            f"{BASE}73791/status/", {"status": "APPROVED"},
            format="json", HTTP_COMPANY_CODE=COMPANY_CODE,
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("password", resp.json()["detail"].lower())
        self.sap.decide_invoice_approval.assert_not_called()

    def test_an_already_decided_invoice_is_refused(self):
        self.sap.invoice_approval_stage.return_value = self._stage(status="APPROVED")
        resp = self.client.patch(
            f"{BASE}73791/status/", {"status": "APPROVED"},
            format="json", HTTP_COMPANY_CODE=COMPANY_CODE,
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.sap.decide_invoice_approval.assert_not_called()

    def test_list_says_which_rows_the_caller_may_decide(self):
        self.sap.list_invoice_approvals.return_value = [
            {**_row(73791, "SO-1", "Party A", "100.00", "PENDING"),
             "approver_code": SAP_APPROVER, "approver_name": "Approver User"},
            {**_row(73792, "SO-2", "Party B", "200.00", "PENDING"),
             "approver_code": "USER26", "approver_name": "HARPREET SINGH"},
        ]
        resp = self.client.get(f"{BASE}?whs={WH}", HTTP_COMPANY_CODE=COMPANY_CODE)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        mine, theirs = resp.json()
        self.assertTrue(mine["is_mine"])
        self.assertTrue(mine["can_decide"])
        self.assertFalse(theirs["is_mine"])
        self.assertFalse(theirs["can_decide"])

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

    def test_sap_403_is_a_refusal_not_an_outage(self):
        """-6006 is SAP answering "you may not decide this", not SAP being down.

        Mapping it to SAPConnectionError used to surface in the approver page as
        "SAP is currently unavailable", hiding the only useful fact: the signing
        user is not an authorizer on the request's stage.
        """
        pending = _resp(200, {"Code": 73791, "Status": "arsPending"})
        refused = _resp(
            403,
            {
                "error": {
                    "code": -6006,
                    "message": {"value": "You are not permitted to perform this action"},
                }
            },
        )
        with mock.patch(
            "sap_client.service_layer.approval_writer.requests.get", return_value=pending
        ), mock.patch(
            "sap_client.service_layer.approval_writer.requests.patch", return_value=refused
        ):
            with self.assertRaises(SAPValidationError) as ctx:
                self.writer.decide(73791, approve=True)
        message = str(ctx.exception)
        self.assertIn("-6006", message)
        self.assertIn("not permitted", message)
        self.assertIn("sl_user", message)
        self.assertIn("authorizer", message)

    def test_read_403_is_a_refusal_not_an_outage(self):
        with mock.patch(
            "sap_client.service_layer.approval_writer.requests.get",
            return_value=_resp(403, {"error": {"code": -6006, "message": "Not permitted"}}),
        ):
            with self.assertRaises(SAPValidationError) as ctx:
                self.writer.decide(73791, approve=True)
        self.assertIn("-6006", str(ctx.exception))

    def test_connection_error_maps(self):
        import requests as requests_lib
        with mock.patch(
            "sap_client.service_layer.approval_writer.requests.get",
            side_effect=requests_lib.exceptions.ConnectionError(),
        ):
            with self.assertRaises(SAPConnectionError):
                self.writer.decide(73791, approve=True)


class ApprovalLoginErrorTests(TestCase):
    """Service Layer login failures, with nothing about the session mocked away."""

    def test_login_refused_by_sap_is_not_an_outage(self):
        """A login SAP answered (bad password, no licence) names the refused user."""
        error = requests.exceptions.HTTPError()
        error.response = _resp(
            401, {"error": {"code": -304, "message": {"value": "Invalid user"}}}
        )
        with mock.patch(
            "sap_client.service_layer.approval_writer.ServiceLayerSession.login",
            side_effect=error,
        ):
            with self.assertRaises(SAPValidationError) as ctx:
                ApprovalRequestWriter(_FakeContext()).decide(73791, approve=True)
        message = str(ctx.exception)
        self.assertIn("sl_user", message)
        self.assertIn("Invalid user", message)

    def test_login_network_failure_is_still_an_outage(self):
        with mock.patch(
            "sap_client.service_layer.approval_writer.ServiceLayerSession.login",
            side_effect=requests.exceptions.ConnectionError(),
        ):
            with self.assertRaises(SAPConnectionError):
                ApprovalRequestWriter(_FakeContext()).decide(73791, approve=True)


# ──────────────────────────────────────────────────────────────────────────
# OMS rate limiting and call volume.
#
# OMS answers 429 with a Retry-After, and because we call it anonymously that
# quota is keyed on OUR source IP — so every approver shares one bucket and the
# app's own call volume is what spends it. These tests pin the three behaviours
# that keep that from reading to the user as "the page just times out".
# ──────────────────────────────────────────────────────────────────────────


def _oms_resp(status_code, json_data=None, headers=None):
    """A mock OMS response, with real headers (``_resp`` above has none)."""
    m = mock.Mock()
    m.status_code = status_code
    m.text = ""
    m.headers = headers or {}
    if json_data is None:
        m.json.side_effect = ValueError("no json")
    else:
        m.json.return_value = json_data
    return m


@override_settings(
    OMS_ENABLED=True,
    OMS_SIMULATE=False,
    OMS_AUTH_ENABLED=False,
    OMS_BASE_URL="http://oms.test",
)
class OmsThrottlingAndCallVolumeTests(ApprovalEndpointTestData, APITestCase):
    """Run against a mocked socket rather than the simulate fixtures, because
    what is under test is the HTTP layer itself: status codes, timeouts, and how
    many times we actually go out to OMS."""

    def setUp(self):
        self.client = self.client_for(self.approver)
        # The pending count is cached in the process-wide LocMemCache, which
        # Django does NOT reset between tests.
        cache.clear()

    def tearDown(self):
        cache.clear()

    def patch_oms(self, *responses):
        """Patch the shared session; returns the mock so callers can count calls.

        One response is given to every call (or raised, if it is an exception);
        several are handed out in order.
        """
        if len(responses) == 1:
            only = responses[0]
            kwargs = (
                {"side_effect": only}
                if isinstance(only, BaseException)
                else {"return_value": only}
            )
        else:
            kwargs = {"side_effect": list(responses)}
        patcher = mock.patch("invoice_approval.oms._SESSION.request", **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    # ── 429 ───────────────────────────────────────────────────────────────────
    def test_throttled_list_says_so_and_says_for_how_long(self):
        """A 429 must not surface as "unexpected response from OMS" (a 502).

        That was the old behaviour — ``status_code >= 400`` fell through to
        OMSDataError — and it threw away the one fact the approver needed.
        """
        self.patch_oms(_oms_resp(429, {"detail": "throttled"}, {"Retry-After": "13"}))
        resp = self.client.get(f"{OMS_BASE}?whs={WH}", HTTP_COMPANY_CODE=COMPANY_CODE)
        self.assertEqual(resp.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.assertIn("13 seconds", resp.json()["detail"])
        self.assertIn("limiting", resp.json()["detail"])
        self.assertEqual(resp["Retry-After"], "13")

    def test_throttled_without_a_retry_after_still_reads_as_throttling(self):
        self.patch_oms(_oms_resp(429, {"detail": "throttled"}))
        resp = self.client.get(f"{OMS_BASE}?whs={WH}", HTTP_COMPANY_CODE=COMPANY_CODE)
        self.assertEqual(resp.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.assertIn("limiting", resp.json()["detail"])
        self.assertNotIn("Retry-After", resp)

    def test_throttled_decision_is_not_reported_as_a_bad_request(self):
        """The decision PATCH branches on 400/404 itself — 429 must beat those."""
        self.patch_oms(_oms_resp(429, {"detail": "throttled"}, {"Retry-After": "7"}))
        resp = self.client.patch(
            f"{OMS_BASE}74/status/",
            {"status": "APPROVED", "warehouse": WH},
            format="json", HTTP_COMPANY_CODE=COMPANY_CODE,
        )
        self.assertEqual(resp.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.assertIn("7 seconds", resp.json()["detail"])
        # Nothing was decided, so nothing may be audited as decided.
        self.assertFalse(InvoiceApprovalAudit.objects.filter(approval_code=74).exists())

    # ── Call volume ───────────────────────────────────────────────────────────
    def test_pending_count_is_cached_across_polls(self):
        """The badge polls from every page, for every user. One OMS call, not N."""
        request = self.patch_oms(_oms_resp(200, [{"id": 1}, {"id": 2}]))
        for _ in range(3):
            resp = self.client.get(
                f"{OMS_BASE}pending-count/?whs={WH}", HTTP_COMPANY_CODE=COMPANY_CODE
            )
            self.assertEqual(resp.status_code, status.HTTP_200_OK)
            self.assertEqual(resp.json(), {"pending": 2, "total": 2})
        self.assertEqual(request.call_count, 1)

    def test_pending_count_cache_is_per_warehouse(self):
        """One key per warehouse — a shared key would show one site another's count."""
        UserWarehouse.objects.create(
            user=self.approver, company=self.company, warehouse_code=OTHER_WH
        )
        request = self.patch_oms(_oms_resp(200, [{"id": 1}]))
        self.client.get(f"{OMS_BASE}pending-count/?whs={WH}", HTTP_COMPANY_CODE=COMPANY_CODE)
        self.client.get(
            f"{OMS_BASE}pending-count/?whs={OTHER_WH}", HTTP_COMPANY_CODE=COMPANY_CODE
        )
        self.assertEqual(request.call_count, 2)

    def test_decision_clears_the_cached_count(self):
        """Otherwise the badge keeps the pre-decision number for a whole window."""
        request = self.patch_oms(
            _oms_resp(200, [{"id": 74}, {"id": 75}]),   # first count  -> 2
            _oms_resp(200, {"message": "Status updated successfully"}),  # the PATCH
            _oms_resp(200, [{"id": 75}]),               # recount after -> 1
        )
        resp = self.client.get(
            f"{OMS_BASE}pending-count/?whs={WH}", HTTP_COMPANY_CODE=COMPANY_CODE
        )
        self.assertEqual(resp.json()["pending"], 2)

        self.client.patch(
            f"{OMS_BASE}74/status/",
            {"status": "APPROVED", "warehouse": WH},
            format="json", HTTP_COMPANY_CODE=COMPANY_CODE,
        )

        resp = self.client.get(
            f"{OMS_BASE}pending-count/?whs={WH}", HTTP_COMPANY_CODE=COMPANY_CODE
        )
        self.assertEqual(resp.json()["pending"], 1)
        self.assertEqual(request.call_count, 3)

    # ── Timeouts ──────────────────────────────────────────────────────────────
    def test_timeouts_are_split_and_beat_the_frontend(self):
        """Connect and read are budgeted separately, and both under 30s.

        30s was the old single value and it exactly equalled the browser's axios
        timeout, so the browser always gave up first and the user never saw the
        backend's actual message.
        """
        request = self.patch_oms(_oms_resp(200, []))
        self.client.get(f"{OMS_BASE}?whs={WH}", HTTP_COMPANY_CODE=COMPANY_CODE)
        connect, read = request.call_args.kwargs["timeout"]
        self.assertLess(connect, read)
        self.assertLess(read, 30)

    def test_a_blackholed_connection_is_an_outage_not_a_data_error(self):
        """ConnectTimeout subclasses BOTH ConnectionError and Timeout."""
        self.patch_oms(requests.exceptions.ConnectTimeout())
        resp = self.client.get(f"{OMS_BASE}?whs={WH}", HTTP_COMPANY_CODE=COMPANY_CODE)
        self.assertEqual(resp.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.assertIn("unavailable", resp.json()["detail"])


# ─────────────────────────────────────────────────────────────────────────────
# Reading OMS from its own database (OMS_USE_DATABASE)
# ─────────────────────────────────────────────────────────────────────────────
class OmsDatabaseRouterTests(TestCase):
    """The one mistake this integration must make impossible."""

    def test_nothing_may_migrate_the_oms_alias(self):
        from .routers import OmsDatabaseRouter

        self.assertIs(OmsDatabaseRouter().allow_migrate("oms", "invoice_approval"), False)

    def test_other_aliases_are_left_to_django(self):
        """None, not True — this router has no opinion on the default alias."""
        from .routers import OmsDatabaseRouter

        router = OmsDatabaseRouter()
        self.assertIsNone(router.allow_migrate("default", "invoice_approval"))
        self.assertIsNone(router.db_for_read(InvoiceApprovalAudit))
        self.assertIsNone(router.db_for_write(InvoiceApprovalAudit))


class OmsBackendSelectionTests(TestCase):
    """OMS_USE_DATABASE picks the backend, and nothing else has to know."""

    def test_defaults_to_the_http_client(self):
        from .oms import OmsClient
        from .oms_db import get_oms_backend, get_oms_backend_class

        with override_settings(OMS_USE_DATABASE=False):
            self.assertIsInstance(get_oms_backend(), OmsClient)
            self.assertIs(get_oms_backend_class(), OmsClient)

    def test_switches_to_the_database_client(self):
        from .oms_db import OmsDbClient, get_oms_backend, get_oms_backend_class

        with override_settings(OMS_USE_DATABASE=True):
            self.assertIsInstance(get_oms_backend(), OmsDbClient)
            self.assertIs(get_oms_backend_class(), OmsDbClient)

    def test_a_missing_alias_says_which_key_is_missing(self):
        """The alias is absent under test by construction, so this is the real path."""
        from .oms import OMSValidationError
        from .oms_db import OmsDbClient

        with self.assertRaises(OMSValidationError) as caught:
            OmsDbClient().list_invoices(warehouse=WH)
        self.assertIn("OMS_DB_NAME", str(caught.exception))


class OmsDatabaseQueryTests(TestCase):
    """What the SQL actually asks for. The cursor is faked; the SQL is real."""

    def _cursor(self, rows):
        cursor = mock.MagicMock()
        cursor.__enter__ = mock.Mock(return_value=cursor)
        cursor.__exit__ = mock.Mock(return_value=False)
        cursor.fetchall.return_value = rows
        cursor.fetchone.return_value = (len(rows),)
        return cursor

    def _client(self, rows):
        from .oms_db import OmsDbClient

        client = OmsDbClient()
        cursor = self._cursor(rows)
        client._cursor = mock.Mock(return_value=cursor)
        return client, cursor

    def test_every_list_read_excludes_soft_deleted_rows(self):
        """On 2026-09-19 all 42 live PENDING rows were deleted ones."""
        client, cursor = self._client([])
        client.list_invoices(warehouse=WH, status="PENDING")
        sql, params = cursor.execute.call_args[0]
        self.assertIn("is_deleted = false", sql)
        self.assertIn(WH, params)
        self.assertIn("PENDING", params)

    def test_the_list_is_ordered(self):
        """The API had no ORDER BY, so its order varied between identical calls."""
        client, cursor = self._client([])
        client.list_invoices(warehouse=WH)
        sql = cursor.execute.call_args[0][0]
        self.assertIn("ORDER BY", sql)

    def test_a_blank_warehouse_is_refused_before_any_query(self):
        from .oms import OMSValidationError
        from .oms_db import OmsDbClient

        client = OmsDbClient()
        client._cursor = mock.Mock()
        with self.assertRaises(OMSValidationError):
            client.list_invoices(warehouse="  ")
        client._cursor.assert_not_called()

    def test_pending_count_counts_in_sql(self):
        """It polls from every page for every approver; it must not fetch a list."""
        cache.clear()
        client, cursor = self._client([])
        self.assertEqual(client.pending_count(WH), 0)
        sql = cursor.execute.call_args[0][0]
        self.assertIn("COUNT(*)", sql)
        self.assertIn("is_deleted = false", sql)
        self.assertNotIn("invoice_payload", sql)

    def test_total_amount_stays_a_string(self):
        """The frontend types it `string | null` because DRF rendered it so."""
        from decimal import Decimal

        from .oms_db import OmsDbClient

        row = {
            "id": 1, "so_number": "SO1", "party_name": "P",
            "total_amount": Decimal("17200.00"), "branch": "OIL", "warehouse": WH,
            "status": "PENDING", "rejection_reason": None, "error_message": None,
            "invoice_payload": {}, "created_at": None, "created_by_id": 8,
            "sap_doc_num": None, "sap_doc_entry": None, "supersedes_id": None,
        }
        self.assertEqual(OmsDbClient._serialize(row)["total_amount"], "17200.00")

    def test_history_survives_a_looping_revision_chain(self):
        """A bad backfill must not spin the query until the connection dies."""
        client, cursor = self._client([])
        cursor.fetchone.return_value = (1,)
        client.get_history(1)
        chain_sql = cursor.execute.call_args_list[1][0][0]
        self.assertIn("CYCLE", chain_sql)


class OmsDatabaseWriteTests(TestCase):
    """Approve/reject refuses the same things the API refused."""

    def _client(self):
        from .oms_db import OmsDbClient

        client = OmsDbClient()
        client._cursor = mock.Mock()
        return client

    def test_only_approved_or_rejected(self):
        from .oms import OMSValidationError

        client = self._client()
        with self.assertRaises(OMSValidationError):
            client.update_status(1, "POSTED_TO_SAP")
        client._cursor.assert_not_called()

    def test_a_rejection_needs_a_reason(self):
        from .oms import OMSValidationError

        client = self._client()
        with self.assertRaises(OMSValidationError):
            client.update_status(1, "REJECTED", rejection_reason="   ")
        client._cursor.assert_not_called()


class FgStockTests(TestCase):
    """The stock column, rebuilt here now that OMS's serializer no longer sends it."""

    def test_beverage_invoices_read_the_beverage_company(self):
        from .fg_stock import _company_for_branch

        self.assertEqual(_company_for_branch("BEVERAGE"), "JIVO_BEVERAGES")

    def test_oil_and_anything_unknown_read_the_oil_company(self):
        """OMS treats every non-BEVERAGE branch as oil; so does this."""
        from .fg_stock import _company_for_branch

        self.assertEqual(_company_for_branch("OIL"), "JIVO_OIL")
        self.assertEqual(_company_for_branch(None), "JIVO_OIL")
        self.assertEqual(_company_for_branch("SOMETHING_NEW"), "JIVO_OIL")

    def test_only_fg_lines_are_looked_up_and_only_once(self):
        from .fg_stock import extract_fg_item_codes

        payload = {"DocumentLines": [
            {"ItemCode": "FG0001"}, {"ItemCode": "CG0002"},
            {"ItemCode": "FG0001"}, {"ItemCode": None},
        ]}
        self.assertEqual(extract_fg_item_codes(payload), ["FG0001"])

    def test_a_hana_outage_costs_the_column_not_the_list(self):
        from .fg_stock import build_fg_stock_map

        invoices = [{"branch": "OIL", "warehouse": WH,
                     "invoice_payload": {"DocumentLines": [{"ItemCode": "FG0001"}]}}]
        with mock.patch("invoice_approval.fg_stock.SAPClient",
                        side_effect=SAPConnectionError("HANA down")):
            self.assertEqual(build_fg_stock_map(invoices), {})

    def test_stock_missing_for_a_warehouse_is_null_not_zero(self):
        """'Not stocked here' and 'stocked, empty' are different answers."""
        from .fg_stock import fg_stock_for_invoice

        invoice = {"branch": "OIL", "warehouse": WH,
                   "invoice_payload": {"DocumentLines": [
                       {"LineNum": 0, "ItemCode": "FG0001", "Quantity": 5}]}}
        rows = fg_stock_for_invoice(invoice, stock_map={})
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["warehouse_stock"])
        self.assertEqual(rows[0]["quantity"], 5.0)
