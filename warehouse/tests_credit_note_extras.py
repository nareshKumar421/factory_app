"""The two extras the credit-note queue took from SAP Portal's credit-note screen.

* **Without Qty Posting** on the existing decision endpoint: written to the
  draft's item lines BEFORE the approval, only lines whose flag differs, only
  while it is still a draft, with the signer proved first — and absent, the
  endpoint behaves exactly as before (``tests_credit_note_approval`` pins that).
* **Withdraw** for the person who raised the request, and ``actions/``, which
  tells the page when either is on offer.

SAP is mocked where the views look ``SAPClient`` up.

    python manage.py test warehouse.tests_credit_note_extras --settings=config.sqlite_test_settings
"""

from unittest.mock import call, patch

from django.contrib.auth.models import Permission
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from accounts.models import User
from company.models import Company, UserCompany, UserRole
from sap_client.exceptions import SAPValidationError
from sap_client.models import SapApproverIdentity
from warehouse.models_credit_note_approval import CreditNoteApprovalAudit

BASE = "/api/v1/warehouse/credit-note-approvals/"

# The credit-note queue's own stage read (credit_note_approval_reader).
CN_STAGE = {
    "id": 75424, "obj_type": "14", "doc_type_label": "A/R Credit Note", "status": "PENDING",
    "current_step": 20, "draft_entry": 57198, "doc_num": 626092648,
    "card_code": "CUSTA000844", "party_name": "ILAHI CO.", "total_amount": "17455.00",
    "approver_code": "USER37", "approver_name": "HONEY SINGH",
}


def inbox_state(**overrides):
    """The same request through the general approvals reader."""
    state = {
        "wdd_code": 75424, "object_type": "14", "object_type_label": "A/R Credit Note",
        "draft_entry": 57198, "is_draft": True, "status": "PENDING", "stale_pending": False,
        "superseded": False, "originator_code": "USER12", "approver_code": "USER37",
        "authorizer_codes": ["USER37"],
        "item_lines": [
            {"line_num": 0, "item_code": "FG1", "without_qty_posting": False},
            {"line_num": 1, "item_code": "FG2", "without_qty_posting": True},
        ],
    }
    state.update(overrides)
    return state


@override_settings(SAP_APPROVER_CREDENTIALS={"JIVO_OIL": {"USER37": "stored"}})
class _CreditNoteExtrasTestCase(TestCase):
    rights = (
        "can_view_ar_credit_note_approval", "can_approve_ar_credit_note",
        "can_view_ap_credit_note_approval", "can_approve_ap_credit_note",
    )

    def setUp(self):
        self.company = Company.objects.create(code="JIVO_OIL", name="Jivo Oil")
        role = UserRole.objects.create(name="Finance")
        self.user = User.objects.create_user(
            email="honey@example.com", full_name="Honey Singh", employee_code="E-37", password="x",
        )
        UserCompany.objects.create(user=self.user, company=self.company, role=role)
        for codename in self.rights:
            self.user.user_permissions.add(
                Permission.objects.get(content_type__app_label="warehouse", codename=codename)
            )
        SapApproverIdentity.objects.create(
            user=self.user, company=self.company, sap_user_code="USER37", sap_user_name="HONEY SINGH",
        )
        self.user = User.objects.get(pk=self.user.pk)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.client.credentials(HTTP_COMPANY_CODE=self.company.code)


@patch("warehouse.views_credit_note_approval.SAPClient")
class WithoutQtyPostingTests(_CreditNoteExtrasTestCase):
    def _approve(self, sap, **body):
        client = sap.return_value
        client.credit_note_approval_stage.return_value = dict(CN_STAGE)
        client.decide_credit_note_approval.return_value = {
            "message": "Credit note approved in SAP.", "signed_as": "USER37",
        }
        return self.client.patch(f"{BASE}75424/status/", {"status": "APPROVED", **body}, format="json")

    def test_the_flag_is_written_before_the_approval_on_the_lines_that_differ(self, sap):
        client = sap.return_value
        client.approval_inbox_stage.return_value = inbox_state()
        response = self._approve(sap, without_qty_posting=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["without_qty_posting_lines"], 1)
        client.set_draft_lines_without_qty_posting.assert_called_once_with(57198, [0], True)
        # Signer proved, then the lines, then the decision — in that order.
        names = [c[0] for c in client.method_calls]
        self.assertLess(names.index("verify_approval_signer"), names.index("set_draft_lines_without_qty_posting"))
        self.assertLess(names.index("set_draft_lines_without_qty_posting"), names.index("decide_credit_note_approval"))
        self.assertEqual(client.verify_approval_signer.call_args, call(75424, "USER37"))

    def test_moving_stock_again_clears_the_flag(self, sap):
        sap.return_value.approval_inbox_stage.return_value = inbox_state()
        self._approve(sap, without_qty_posting=False)
        sap.return_value.set_draft_lines_without_qty_posting.assert_called_once_with(57198, [1], False)

    def test_nothing_to_change_touches_nothing(self, sap):
        sap.return_value.approval_inbox_stage.return_value = inbox_state(item_lines=[
            {"line_num": 0, "item_code": "FG1", "without_qty_posting": True},
        ])
        response = self._approve(sap, without_qty_posting=True)
        self.assertEqual(response.data["without_qty_posting_lines"], 0)
        sap.return_value.verify_approval_signer.assert_not_called()
        sap.return_value.set_draft_lines_without_qty_posting.assert_not_called()
        sap.return_value.decide_credit_note_approval.assert_called_once()

    def test_leaving_it_out_is_the_queue_exactly_as_before(self, sap):
        response = self._approve(sap)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("without_qty_posting_lines", response.data)
        sap.return_value.approval_inbox_stage.assert_not_called()

    def test_a_rejection_ignores_it(self, sap):
        client = sap.return_value
        client.credit_note_approval_stage.return_value = dict(CN_STAGE)
        client.decide_credit_note_approval.return_value = {"message": "ok", "signed_as": "USER37"}
        response = self.client.patch(
            f"{BASE}75424/status/",
            {"status": "REJECTED", "rejection_reason": "wrong", "without_qty_posting": True},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        client.approval_inbox_stage.assert_not_called()

    def test_a_posted_credit_note_can_no_longer_change_it(self, sap):
        sap.return_value.approval_inbox_stage.return_value = inbox_state(is_draft=False)
        response = self._approve(sap, without_qty_posting=True)
        self.assertEqual(response.status_code, 409)
        self.assertIn("Correct it in SAP", response.data["error"])
        sap.return_value.decide_credit_note_approval.assert_not_called()

    def test_a_leftover_is_refused_before_anything_is_written(self, sap):
        sap.return_value.approval_inbox_stage.return_value = inbox_state(
            status="CANCELLED", stale_pending=True
        )
        response = self._approve(sap, without_qty_posting=True)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "STALE_REQUEST")
        sap.return_value.set_draft_lines_without_qty_posting.assert_not_called()

    def test_sap_refusing_the_change_approves_nothing(self, sap):
        sap.return_value.approval_inbox_stage.return_value = inbox_state()
        sap.return_value.set_draft_lines_without_qty_posting.side_effect = SAPValidationError("-5002 no")
        response = self._approve(sap, without_qty_posting=True)
        self.assertEqual(response.status_code, 400)
        self.assertIn("NOT approved", response.data["error"])
        sap.return_value.decide_credit_note_approval.assert_not_called()
        self.assertFalse(CreditNoteApprovalAudit.objects.exists())

    def test_a_refused_signer_leaves_the_draft_untouched(self, sap):
        sap.return_value.approval_inbox_stage.return_value = inbox_state()
        sap.return_value.verify_approval_signer.side_effect = SAPValidationError(
            "SAP refused the Service Layer login for user 'USER37'"
        )
        response = self._approve(sap, without_qty_posting=True)
        self.assertEqual(response.status_code, 400)
        sap.return_value.set_draft_lines_without_qty_posting.assert_not_called()
        sap.return_value.decide_credit_note_approval.assert_not_called()

    def test_the_identity_guards_run_before_the_draft_is_touched(self, sap):
        client = sap.return_value
        client.credit_note_approval_stage.return_value = {**CN_STAGE, "approver_code": "USER24"}
        response = self.client.patch(
            f"{BASE}75424/status/", {"status": "APPROVED", "without_qty_posting": True}, format="json"
        )
        self.assertEqual(response.status_code, 403)
        client.approval_inbox_stage.assert_not_called()
        client.set_draft_lines_without_qty_posting.assert_not_called()


@patch("warehouse.views_credit_note_approval.SAPClient")
class ActionsTests(_CreditNoteExtrasTestCase):
    def test_the_authorizer_may_set_it_and_sees_the_mixed_state(self, sap):
        sap.return_value.approval_inbox_stage.return_value = inbox_state()
        response = self.client.get(f"{BASE}75424/actions/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.data["without_qty_posting"], {"current": None, "item_lines": 2, "can_set": True}
        )
        self.assertFalse(response.data["can_withdraw"])
        self.assertTrue(sap.return_value.approval_inbox_stage.call_args.kwargs["with_item_lines"])

    def test_the_originator_may_withdraw(self, sap):
        sap.return_value.approval_inbox_stage.return_value = inbox_state(
            originator_code="USER37", approver_code="USER24"
        )
        data = self.client.get(f"{BASE}75424/actions/").data
        self.assertTrue(data["can_withdraw"])
        self.assertIsNone(data["withdraw_note"])
        self.assertFalse(data["without_qty_posting"]["can_set"])

    @override_settings(SAP_APPROVER_CREDENTIALS={})
    def test_without_a_stored_password_the_page_says_where_to_go(self, sap):
        sap.return_value.approval_inbox_stage.return_value = inbox_state(originator_code="USER37")
        data = self.client.get(f"{BASE}75424/actions/").data
        self.assertFalse(data["can_withdraw"])
        self.assertIn("SAP Approvals", data["withdraw_note"])

    def test_a_service_credit_note_has_nothing_to_set(self, sap):
        sap.return_value.approval_inbox_stage.return_value = inbox_state(item_lines=[])
        data = self.client.get(f"{BASE}75424/actions/").data
        self.assertEqual(data["without_qty_posting"], {"current": None, "item_lines": 0, "can_set": False})

    def test_viewing_without_approving_cannot_set_it(self, sap):
        self.user.user_permissions.remove(
            Permission.objects.get(content_type__app_label="warehouse", codename="can_approve_ar_credit_note")
        )
        self.client.force_authenticate(user=User.objects.get(pk=self.user.pk))
        sap.return_value.approval_inbox_stage.return_value = inbox_state()
        self.assertFalse(self.client.get(f"{BASE}75424/actions/").data["without_qty_posting"]["can_set"])

    def test_another_document_type_is_not_found_here(self, sap):
        sap.return_value.approval_inbox_stage.return_value = inbox_state(object_type="13")
        self.assertEqual(self.client.get(f"{BASE}75424/actions/").status_code, 404)

    def test_a_family_you_cannot_see_is_refused(self, sap):
        for codename in ("can_view_ap_credit_note_approval", "can_approve_ap_credit_note"):
            self.user.user_permissions.remove(
                Permission.objects.get(content_type__app_label="warehouse", codename=codename)
            )
        self.client.force_authenticate(user=User.objects.get(pk=self.user.pk))
        sap.return_value.approval_inbox_stage.return_value = inbox_state(object_type="19")
        self.assertEqual(self.client.get(f"{BASE}75424/actions/").status_code, 403)


@patch("warehouse.views_credit_note_approval.SAPClient")
class WithdrawTests(_CreditNoteExtrasTestCase):
    def _post(self):
        return self.client.post(f"{BASE}75424/withdraw/", {}, format="json")

    def test_the_originator_withdraws_signed_as_themselves(self, sap):
        sap.return_value.approval_inbox_stage.return_value = inbox_state(originator_code="USER37")
        sap.return_value.withdraw_approval_request.return_value = {
            "message": "Credit note withdrawn in SAP.", "signed_as": "USER37",
        }
        response = self._post()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["signed_as"], "USER37")
        sap.return_value.withdraw_approval_request.assert_called_once_with(
            75424, originator="USER37", subject="Credit note"
        )

    def test_only_the_originator_can(self, sap):
        sap.return_value.approval_inbox_stage.return_value = inbox_state()
        self.assertEqual(self._post().status_code, 403)
        sap.return_value.withdraw_approval_request.assert_not_called()

    def test_an_unmapped_user_cannot(self, sap):
        SapApproverIdentity.objects.all().delete()
        sap.return_value.approval_inbox_stage.return_value = inbox_state(originator_code="USER37")
        self.assertEqual(self._post().status_code, 403)

    def test_only_a_pending_request(self, sap):
        sap.return_value.approval_inbox_stage.return_value = inbox_state(
            originator_code="USER37", status="APPROVED"
        )
        response = self._post()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "STALE_REQUEST")

    @override_settings(SAP_APPROVER_CREDENTIALS={})
    def test_no_stored_password_points_to_the_inbox(self, sap):
        sap.return_value.approval_inbox_stage.return_value = inbox_state(originator_code="USER37")
        response = self._post()
        self.assertEqual(response.status_code, 400)
        self.assertIn("SAP Approvals", response.data["error"])
        sap.return_value.withdraw_approval_request.assert_not_called()

    def test_not_a_credit_note_is_404(self, sap):
        sap.return_value.approval_inbox_stage.return_value = inbox_state(object_type="22")
        self.assertEqual(self._post().status_code, 404)

    def test_the_view_right_is_needed(self, sap):
        for codename in self.rights:
            self.user.user_permissions.remove(
                Permission.objects.get(content_type__app_label="warehouse", codename=codename)
            )
        self.client.force_authenticate(user=User.objects.get(pk=self.user.pk))
        self.assertEqual(self._post().status_code, 403)
        self.assertEqual(self.client.get(f"{BASE}75424/actions/").status_code, 403)
