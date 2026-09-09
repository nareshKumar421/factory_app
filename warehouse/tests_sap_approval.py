"""Tests for the SAP transfer-approval queue.

The load-bearing rule is that SAP accepts a decision from exactly one user —
the authorizer its approval template names on the request's *current* stage —
and refuses everyone else with ``-6006``. So the tests below pin four things:

* the decision is signed as the user HANA reports right now, never as one the
  browser supplied or as the single legacy ``SAP_APPROVAL_USER``;
* only the person whose own mapped SAP account IS that authorizer may decide it
  — otherwise the app is a shared-credential rubber stamp;
* a request whose authorizer has no password on file is refused up-front with
  an explanation, rather than sent to SAP to be rejected;
* SAP accepting a decision is never undone by local bookkeeping failing.

SAP itself is mocked throughout — these run with no HANA or Service Layer.
"""

from unittest.mock import patch

from django.contrib.auth.models import Permission
from django.test import SimpleTestCase, TestCase, override_settings
from rest_framework.test import APIClient

from accounts.models import User
from company.models import Company, UserCompany, UserRole
from sap_client.exceptions import SAPValidationError
from sap_client.models import SapApproverIdentity
from sap_client.service_layer.approval_writer import ApprovalRequestWriter
from warehouse.models_sap_approval import SapApprovalAudit

LIST_URL = "/api/v1/warehouse/sap-transfer-approvals/"


def status_url(wdd_code):
    return f"{LIST_URL}{wdd_code}/status/"


# One pending Beverages stock transfer waiting on USER37, as HANA reports it.
PENDING_ROW = {
    "id": 21600,
    "obj_type": "67",
    "doc_type_label": "Stock Transfer",
    "draft_entry": 16000,
    "doc_num": 926678040,
    "from_warehouse": "BH-PF",
    "to_warehouse": "BH-FG",
    "doc_date": "2026-09-08",
    "comments": None,
    "status": "PENDING",
    "rejection_reason": None,
    "current_step": 20,
    "approver_code": "USER37",
    "approver_name": "HONEY SINGH",
    "lines": [],
    "created_at": "2026-09-08T10:00:00",
    "created_by": "ATUL SHARMA",
}

# One waiting on a user whose password we do not hold.
BLOCKED_ROW = {**PENDING_ROW, "id": 66636, "approver_code": "USER32", "approver_name": "PANKAJ"}


class _Ctx:
    """The slice of SAPContext the writer touches."""

    def __init__(self, service_layer):
        self.service_layer = service_layer


class ApproverCredentialTests(SimpleTestCase):
    """Which SAP account ends up signing the decision."""

    def _writer(self, **overrides):
        config = {
            "base_url": "https://sap.example",
            "company_db": "TEST_DB",
            "username": "B1i",
            "password": "b1i-pw",
            "approval_username": "manager",
            "approval_password": "manager-pw",
            "approvers": {"USER37": "...."},
        }
        config.update(overrides)
        return ApprovalRequestWriter(_Ctx(config))

    def test_named_approver_uses_its_own_password(self):
        self.assertEqual(
            self._writer()._approver_credentials("USER37"), ("USER37", "....")
        )

    def test_approver_lookup_is_case_insensitive(self):
        """The page may send 'user37'; SAP's own user code is upper-case."""
        self.assertEqual(
            self._writer()._approver_credentials("user37"), ("user37", "....")
        )

    def test_unknown_approver_is_refused_before_sap_is_called(self):
        with self.assertRaises(SAPValidationError) as ctx:
            self._writer()._approver_credentials("USER24")
        message = str(ctx.exception)
        self.assertIn("USER24", message)
        self.assertIn("SAP_APPROVER_CREDENTIALS", message)

    def test_no_named_approver_falls_back_to_the_legacy_account(self):
        """The invoice page's existing behaviour must not change."""
        self.assertEqual(
            self._writer()._approver_credentials(), ("manager", "manager-pw")
        )

    def test_fallback_reaches_the_service_layer_user_when_unconfigured(self):
        writer = self._writer(approval_username="", approval_password="")
        self.assertEqual(writer._approver_credentials(), ("B1i", "b1i-pw"))


@override_settings(SAP_APPROVER_CREDENTIALS={"JIVO_BEVERAGES": {"USER37": "...."}})
class SapTransferApprovalAPITests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(code="JIVO_BEVERAGES", name="Jivo Beverages")
        role = UserRole.objects.create(name="Store")
        self.user = User.objects.create_user(
            email="honey@example.com", full_name="Honey Singh",
            employee_code="E-37", password="x",
        )
        UserCompany.objects.create(user=self.user, company=self.company, role=role)
        for codename in ("can_view_transfer_request", "can_approve_transfer_request"):
            self.user.user_permissions.add(
                Permission.objects.get(
                    content_type__app_label="warehouse", codename=codename
                )
            )
        # Honey IS USER37 in SAP. Without this mapping nothing is decidable.
        self.identity = SapApproverIdentity.objects.create(
            user=self.user, company=self.company,
            sap_user_code="USER37", sap_user_name="HONEY SINGH",
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.client.credentials(HTTP_COMPANY_CODE=self.company.code)

    # ---- list -------------------------------------------------------------

    @patch("warehouse.views_sap_approval.SAPClient")
    def test_list_marks_only_the_callers_own_rows_actionable(self, sap):
        sap.return_value.list_transfer_approvals.return_value = [
            dict(PENDING_ROW), dict(BLOCKED_ROW),
        ]
        response = self.client.get(LIST_URL)
        self.assertEqual(response.status_code, 200)
        signable, blocked = response.data
        self.assertTrue(signable["credentials_configured"])
        self.assertTrue(signable["is_mine"])
        self.assertTrue(signable["can_decide"])
        # Still listed — a transfer stuck on somebody else is worth seeing.
        self.assertFalse(blocked["credentials_configured"])
        self.assertFalse(blocked["is_mine"])
        self.assertFalse(blocked["can_decide"])

    @patch("warehouse.views_sap_approval.SAPClient")
    def test_list_cannot_offer_a_decision_without_the_permission(self, sap):
        self.user.user_permissions.remove(
            Permission.objects.get(
                content_type__app_label="warehouse",
                codename="can_approve_transfer_request",
            )
        )
        self.user = User.objects.get(pk=self.user.pk)  # drop the perm cache
        self.client.force_authenticate(user=self.user)
        sap.return_value.list_transfer_approvals.return_value = [dict(PENDING_ROW)]
        response = self.client.get(LIST_URL)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data[0]["credentials_configured"])
        self.assertFalse(response.data[0]["can_decide"])

    @patch("warehouse.views_sap_approval.SAPClient")
    def test_all_drops_the_status_filter(self, sap):
        sap.return_value.list_transfer_approvals.return_value = []
        self.client.get(LIST_URL, {"status": "ALL"})
        self.assertIsNone(
            sap.return_value.list_transfer_approvals.call_args.kwargs["status"]
        )

    # ---- decide -----------------------------------------------------------

    @patch("warehouse.views_sap_approval.SAPClient")
    def test_approve_signs_as_the_stage_authorizer_sap_reports(self, sap):
        client = sap.return_value
        client.transfer_approval_stage.return_value = dict(PENDING_ROW)
        client.decide_transfer_approval.return_value = {
            "message": "Transfer approved in SAP.", "signed_as": "USER37",
        }
        response = self.client.patch(
            status_url(21600), {"status": "APPROVED"}, format="json"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["signed_as"], "USER37")

        kwargs = client.decide_transfer_approval.call_args.kwargs
        self.assertEqual(kwargs["approver"], "USER37")
        self.assertTrue(kwargs["approve"])
        # SAP stamps the authorizer, so the real actor rides in the remarks.
        self.assertIn("Honey Singh", kwargs["remarks"])

    @patch("warehouse.views_sap_approval.SAPClient")
    def test_the_body_cannot_choose_who_signs(self, sap):
        """A crafted request must not borrow another authorizer's credentials."""
        client = sap.return_value
        client.transfer_approval_stage.return_value = dict(PENDING_ROW)
        client.decide_transfer_approval.return_value = {"message": "ok", "signed_as": "USER37"}
        self.client.patch(
            status_url(21600),
            {"status": "APPROVED", "approver": "USER24", "approver_code": "USER24"},
            format="json",
        )
        self.assertEqual(
            client.decide_transfer_approval.call_args.kwargs["approver"], "USER37"
        )

    @patch("warehouse.views_sap_approval.SAPClient")
    def test_approve_records_who_actually_clicked(self, sap):
        client = sap.return_value
        client.transfer_approval_stage.return_value = dict(PENDING_ROW)
        client.decide_transfer_approval.return_value = {
            "message": "Transfer approved in SAP.", "signed_as": "USER37",
        }
        self.client.patch(status_url(21600), {"status": "APPROVED"}, format="json")

        audit = SapApprovalAudit.objects.get(approval_code=21600)
        self.assertEqual(audit.decision, SapApprovalAudit.DECISION_APPROVED)
        self.assertEqual(audit.sap_approver, "USER37")
        self.assertEqual(audit.created_by, self.user)
        self.assertEqual(audit.from_warehouse, "BH-PF")
        self.assertEqual(audit.to_warehouse, "BH-FG")
        self.assertEqual(audit.stage_code, 20)
        self.assertEqual(audit.company, self.company)

    @patch("warehouse.views_sap_approval.SAPClient")
    def test_someone_elses_approval_is_refused_without_calling_sap(self, sap):
        """The rubber-stamp guard: Honey must not decide Pankaj's request."""
        client = sap.return_value
        client.transfer_approval_stage.return_value = dict(BLOCKED_ROW)
        response = self.client.patch(
            status_url(66636), {"status": "APPROVED"}, format="json"
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn("USER32", response.data["error"])
        self.assertIn("USER37", response.data["error"])
        client.decide_transfer_approval.assert_not_called()

    @patch("warehouse.views_sap_approval.SAPClient")
    def test_an_unmapped_user_cannot_decide_anything(self, sap):
        """No mapping means the app cannot tell who the clicker is in SAP."""
        self.identity.delete()
        client = sap.return_value
        client.transfer_approval_stage.return_value = dict(PENDING_ROW)
        response = self.client.patch(
            status_url(21600), {"status": "APPROVED"}, format="json"
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn("not linked to a SAP user", response.data["error"])
        client.decide_transfer_approval.assert_not_called()

    @patch("warehouse.views_sap_approval.SAPClient")
    def test_a_deactivated_mapping_does_not_count(self, sap):
        self.identity.is_active = False
        self.identity.save()
        client = sap.return_value
        client.transfer_approval_stage.return_value = dict(PENDING_ROW)
        response = self.client.patch(
            status_url(21600), {"status": "APPROVED"}, format="json"
        )
        self.assertEqual(response.status_code, 403)
        client.decide_transfer_approval.assert_not_called()

    @patch("warehouse.views_sap_approval.SAPClient")
    def test_my_own_missing_password_gets_its_own_message(self, sap):
        """Mapped correctly, but the app cannot authenticate as them."""
        client = sap.return_value
        client.transfer_approval_stage.return_value = dict(PENDING_ROW)
        with override_settings(SAP_APPROVER_CREDENTIALS={"JIVO_BEVERAGES": {}}):
            response = self.client.patch(
                status_url(21600), {"status": "APPROVED"}, format="json"
            )
        self.assertEqual(response.status_code, 400)
        self.assertIn("password", response.data["error"].lower())
        client.decide_transfer_approval.assert_not_called()

    @patch("warehouse.views_sap_approval.SAPClient")
    def test_the_mapping_is_per_company(self, sap):
        """The same person is a different SAP account in each company."""
        other = Company.objects.create(code="JIVO_OIL", name="Jivo Oil")
        self.identity.delete()
        SapApproverIdentity.objects.create(
            user=self.user, company=other, sap_user_code="USER37"
        )
        client = sap.return_value
        client.transfer_approval_stage.return_value = dict(PENDING_ROW)
        response = self.client.patch(
            status_url(21600), {"status": "APPROVED"}, format="json"
        )
        self.assertEqual(response.status_code, 403)
        client.decide_transfer_approval.assert_not_called()

    @patch("warehouse.views_sap_approval.SAPClient")
    def test_an_already_decided_request_is_refused(self, sap):
        """The page can easily be a stage behind what SAP now holds."""
        client = sap.return_value
        client.transfer_approval_stage.return_value = {
            **PENDING_ROW, "status": "APPROVED",
        }
        response = self.client.patch(
            status_url(21600), {"status": "APPROVED"}, format="json"
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("already approved", response.data["error"])
        client.decide_transfer_approval.assert_not_called()

    @patch("warehouse.views_sap_approval.SAPClient")
    def test_a_stage_with_no_authorizer_is_refused(self, sap):
        client = sap.return_value
        client.transfer_approval_stage.return_value = {
            **PENDING_ROW, "approver_code": None, "approver_name": None,
        }
        response = self.client.patch(
            status_url(21600), {"status": "APPROVED"}, format="json"
        )
        self.assertEqual(response.status_code, 400)
        client.decide_transfer_approval.assert_not_called()

    @patch("warehouse.views_sap_approval.SAPClient")
    def test_reject_requires_a_reason(self, sap):
        sap.return_value.transfer_approval_stage.return_value = dict(PENDING_ROW)
        response = self.client.patch(
            status_url(21600), {"status": "REJECTED"}, format="json"
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("rejection_reason", response.data)
        sap.return_value.decide_transfer_approval.assert_not_called()

    @patch("warehouse.views_sap_approval.SAPClient")
    def test_reject_carries_the_reason_into_sap(self, sap):
        client = sap.return_value
        client.transfer_approval_stage.return_value = dict(PENDING_ROW)
        client.decide_transfer_approval.return_value = {
            "message": "Transfer rejected in SAP.", "signed_as": "USER37",
        }
        response = self.client.patch(
            status_url(21600),
            {"status": "REJECTED", "rejection_reason": "BH-PF is short"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        kwargs = client.decide_transfer_approval.call_args.kwargs
        self.assertFalse(kwargs["approve"])
        self.assertIn("BH-PF is short", kwargs["remarks"])
        audit = SapApprovalAudit.objects.get(approval_code=21600)
        self.assertEqual(audit.decision, SapApprovalAudit.DECISION_REJECTED)
        self.assertEqual(audit.rejection_reason, "BH-PF is short")

    @patch("warehouse.views_sap_approval.SAPClient")
    def test_a_failed_audit_write_does_not_undo_the_sap_decision(self, sap):
        """SAP has already accepted it; a 500 here would be a lie."""
        client = sap.return_value
        client.transfer_approval_stage.return_value = dict(PENDING_ROW)
        client.decide_transfer_approval.return_value = {
            "message": "Transfer approved in SAP.", "signed_as": "USER37",
        }
        with patch.object(
            SapApprovalAudit.objects, "create", side_effect=RuntimeError("db down")
        ):
            response = self.client.patch(
                status_url(21600), {"status": "APPROVED"}, format="json"
            )
        self.assertEqual(response.status_code, 200)

    @patch("warehouse.views_sap_approval.SAPClient")
    def test_sap_refusing_the_approver_surfaces_as_a_validation_error(self, sap):
        client = sap.return_value
        client.transfer_approval_stage.return_value = dict(PENDING_ROW)
        client.decide_transfer_approval.side_effect = SAPValidationError(
            "SAP refused the decision signed as 'USER37': (-6006) not permitted"
        )
        response = self.client.patch(
            status_url(21600), {"status": "APPROVED"}, format="json"
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("-6006", response.data["error"])
        self.assertFalse(SapApprovalAudit.objects.exists())

    def test_a_company_with_no_credentials_configured_blocks_every_row(self):
        """The default state: nothing signable until a password is added."""
        with override_settings(SAP_APPROVER_CREDENTIALS={}):
            with patch("warehouse.views_sap_approval.SAPClient") as sap:
                sap.return_value.list_transfer_approvals.return_value = [dict(PENDING_ROW)]
                response = self.client.get(LIST_URL)
        self.assertTrue(response.data[0]["is_mine"])
        self.assertFalse(response.data[0]["credentials_configured"])
        self.assertFalse(response.data[0]["can_decide"])
