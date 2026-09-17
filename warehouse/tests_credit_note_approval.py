"""Tests for the SAP credit-note approval queue.

Same load-bearing rule as the transfer queue: SAP accepts a decision from
exactly one user — the authorizer its approval template names on the request's
*current* stage — and refuses everyone else with ``-6006``. The guards live in
``views_sap_approval_base``, so what is pinned here is that this queue actually
wires them up, plus the two things that are specific to a credit note:

* a credit note comes in two families (A/R ``14`` and A/P ``19``) and two
  shapes (item and service), and the page can ask for one family;
* the audit row is party-and-money shaped, and a total SAP words oddly must
  never cost the app the record of a decision SAP has already taken.

SAP itself is mocked throughout — these run with no HANA or Service Layer.
"""

from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import Permission
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from accounts.models import User
from company.models import Company, UserCompany, UserRole
from sap_client.models import SapApproverIdentity
from warehouse.models_credit_note_approval import CreditNoteApprovalAudit

LIST_URL = "/api/v1/warehouse/credit-note-approvals/"
COUNT_URL = "/api/v1/warehouse/credit-note-approvals/pending-count/"


def status_url(wdd_code):
    return f"{LIST_URL}{wdd_code}/status/"


# One pending A/R item credit note waiting on USER37, as HANA reports it: goods
# a customer sent back, coming into BH-GR.
PENDING_ROW = {
    "id": 75424,
    "obj_type": "14",
    "doc_type_label": "A/R Credit Note",
    "family": "AR",
    "line_type": "I",
    "line_type_label": "Item",
    "moves_stock": True,
    "stock_direction": "IN",
    "draft_entry": 57198,
    "doc_num": 626092648,
    "posted_doc_entry": None,
    "posted_doc_num": None,
    "card_code": "CUSTA000844",
    "party_name": "ILAHI CO.",
    "total_amount": "17455.00",
    "tax_amount": "0.00",
    "currency": "INR",
    "branch": "FACTORY",
    "doc_date": "2026-09-16",
    "comments": "RN-1626096511",
    "reference": None,
    "base_documents": ["A/R Return 1626096511"],
    "warehouses": ["BH-GR"],
    "status": "PENDING",
    "rejection_reason": None,
    "current_step": 20,
    "approver_code": "USER37",
    "approver_name": "HONEY SINGH",
    "decided_by": None,
    "decided_by_name": None,
    "decided_at": None,
    "lines": [],
    "created_at": "2026-09-16T10:00:00",
    "created_by": "ATUL SHARMA",
}

# One waiting on a user whose password we do not hold.
BLOCKED_ROW = {**PENDING_ROW, "id": 66636, "approver_code": "USER32", "approver_name": "PANKAJ"}

# A service credit note: no items, no warehouse, no stock — just an amount
# against a G/L account. It must be listed and decidable all the same.
SERVICE_ROW = {
    **PENDING_ROW,
    "id": 74504,
    "line_type": "S",
    "line_type_label": "Service",
    "moves_stock": False,
    "stock_direction": None,
    "base_documents": [],
    "warehouses": [],
    "lines": [{
        "line_num": 0,
        "item_code": None,
        "description": "PROMOTIONAL DISCOUNT",
        "quantity": 0.0,
        "warehouse": None,
        "price": "10842.00",
        "line_total": "10842.00",
        "account_code": "5500004",
        "account_name": "PROMOTIONAL DISCOUNT",
        "warehouse_stock": None,
        "base_ref": None,
        "base_type_label": None,
    }],
}

# An A/P credit note — a vendor is debited, and the goods go back OUT. The
# family is what the permission check keys on.
AP_ROW = {
    **PENDING_ROW,
    "id": 71301,
    "obj_type": "19",
    "doc_type_label": "A/P Credit Note",
    "family": "AP",
    "stock_direction": "OUT",
    "card_code": "VENDA001182",
    "party_name": "BAJAJ ELECTRICAL",
}

# The same draft after it was approved and added. The number SAP gave the
# document is NOT the draft's — drafts carry the series' next number as at the
# save, and three open Oil credit notes share one.
DECIDED_ROW = {
    **PENDING_ROW,
    "id": 75425,
    "status": "APPROVED",
    "approver_code": None,
    "approver_name": None,
    "decided_by": "USER37",
    "decided_by_name": "HONEY SINGH",
    "decided_at": "2026-09-16T11:24:00",
    "posted_doc_entry": 41202,
    "posted_doc_num": 626092650,
}


@override_settings(SAP_APPROVER_CREDENTIALS={"JIVO_OIL": {"USER37": "...."}})
class CreditNoteApprovalAPITests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(code="JIVO_OIL", name="Jivo Oil")
        role = UserRole.objects.create(name="Finance")
        self.user = User.objects.create_user(
            email="honey@example.com", full_name="Honey Singh",
            employee_code="E-37", password="x",
        )
        UserCompany.objects.create(user=self.user, company=self.company, role=role)
        # Both families to start with; individual tests drop one to prove the
        # split is enforced rather than decorative.
        for codename in (
            "can_view_ar_credit_note_approval", "can_approve_ar_credit_note",
            "can_view_ap_credit_note_approval", "can_approve_ap_credit_note",
        ):
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

    def _drop_permission(self, codename):
        self.user.user_permissions.remove(
            Permission.objects.get(
                content_type__app_label="warehouse", codename=codename
            )
        )
        self.user = User.objects.get(pk=self.user.pk)  # drop the perm cache
        self.client.force_authenticate(user=self.user)

    # ---- list -------------------------------------------------------------

    @patch("warehouse.views_credit_note_approval.SAPClient")
    def test_list_marks_only_the_callers_own_rows_actionable(self, sap):
        sap.return_value.list_credit_note_approvals.return_value = [
            dict(PENDING_ROW), dict(BLOCKED_ROW),
        ]
        response = self.client.get(LIST_URL)
        self.assertEqual(response.status_code, 200)
        signable, blocked = response.data
        self.assertTrue(signable["credentials_configured"])
        self.assertTrue(signable["is_mine"])
        self.assertTrue(signable["can_decide"])
        # Still listed — a credit note stuck on somebody else is worth seeing.
        self.assertFalse(blocked["credentials_configured"])
        self.assertFalse(blocked["is_mine"])
        self.assertFalse(blocked["can_decide"])

    @patch("warehouse.views_credit_note_approval.SAPClient")
    def test_viewing_does_not_imply_deciding(self, sap):
        self._drop_permission("can_approve_ar_credit_note")
        sap.return_value.list_credit_note_approvals.return_value = [dict(PENDING_ROW)]
        response = self.client.get(LIST_URL)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data[0]["is_mine"])
        self.assertFalse(response.data[0]["can_decide"])

    def test_the_queue_is_closed_without_any_view_permission(self):
        self._drop_permission("can_view_ar_credit_note_approval")
        self._drop_permission("can_view_ap_credit_note_approval")
        self.assertEqual(self.client.get(LIST_URL).status_code, 403)

    def test_one_family_is_enough_to_open_the_page(self):
        """An A/P-only clerk still gets in; the queue is then narrowed for them."""
        self._drop_permission("can_view_ar_credit_note_approval")
        self._drop_permission("can_approve_ar_credit_note")
        with patch("warehouse.views_credit_note_approval.SAPClient") as sap:
            sap.return_value.list_credit_note_approvals.return_value = []
            self.assertEqual(self.client.get(LIST_URL).status_code, 200)

    @patch("warehouse.views_credit_note_approval.SAPClient")
    def test_the_family_filter_is_passed_through(self, sap):
        """A/P credit notes are a different document to a different person."""
        sap.return_value.list_credit_note_approvals.return_value = []
        self.client.get(LIST_URL, {"family": "AP"})
        self.assertEqual(
            sap.return_value.list_credit_note_approvals.call_args.kwargs["family"], "AP"
        )

    @patch("warehouse.views_credit_note_approval.SAPClient")
    def test_all_drops_the_status_filter(self, sap):
        sap.return_value.list_credit_note_approvals.return_value = []
        self.client.get(LIST_URL, {"status": "ALL"})
        self.assertIsNone(
            sap.return_value.list_credit_note_approvals.call_args.kwargs["status"]
        )

    @patch("warehouse.views_credit_note_approval.SAPClient")
    def test_a_service_credit_note_is_listed_and_decidable(self, sap):
        """37% of them have no items at all; hiding those strands them in SAP."""
        sap.return_value.list_credit_note_approvals.return_value = [dict(SERVICE_ROW)]
        row = self.client.get(LIST_URL).data[0]
        self.assertFalse(row["moves_stock"])
        self.assertEqual(row["lines"][0]["account_name"], "PROMOTIONAL DISCOUNT")
        self.assertTrue(row["can_decide"])

    @patch("warehouse.views_credit_note_approval.SAPClient")
    def test_a_decided_row_carries_who_decided_it_and_the_real_number(self, sap):
        sap.return_value.list_credit_note_approvals.return_value = [dict(DECIDED_ROW)]
        row = self.client.get(LIST_URL, {"status": "APPROVED"}).data[0]
        self.assertEqual(row["decided_by"], "USER37")
        self.assertEqual(row["posted_doc_num"], 626092650)
        self.assertNotEqual(row["posted_doc_num"], row["doc_num"])
        # History is read-only here: SAP will not take a second decision.
        self.assertFalse(row["can_decide"])

    @patch("warehouse.views_credit_note_approval.SAPClient")
    def test_pending_count_feeds_the_badge(self, sap):
        sap.return_value.count_pending_credit_note_approvals.return_value = 25
        response = self.client.get(COUNT_URL)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["total"], 25)

    # ---- decide -----------------------------------------------------------

    @patch("warehouse.views_credit_note_approval.SAPClient")
    def test_approve_signs_as_the_stage_authorizer_sap_reports(self, sap):
        client = sap.return_value
        client.credit_note_approval_stage.return_value = dict(PENDING_ROW)
        client.decide_credit_note_approval.return_value = {
            "message": "Credit note approved in SAP.", "signed_as": "USER37",
        }
        response = self.client.patch(
            status_url(75424), {"status": "APPROVED"}, format="json"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["signed_as"], "USER37")

        kwargs = client.decide_credit_note_approval.call_args.kwargs
        self.assertEqual(kwargs["approver"], "USER37")
        self.assertTrue(kwargs["approve"])
        # SAP stamps the authorizer, so the real actor rides in the remarks.
        self.assertIn("Honey Singh", kwargs["remarks"])

    @patch("warehouse.views_credit_note_approval.SAPClient")
    def test_the_body_cannot_choose_who_signs(self, sap):
        """A crafted request must not borrow another authorizer's credentials."""
        client = sap.return_value
        client.credit_note_approval_stage.return_value = dict(PENDING_ROW)
        client.decide_credit_note_approval.return_value = {
            "message": "ok", "signed_as": "USER37",
        }
        self.client.patch(
            status_url(75424),
            {"status": "APPROVED", "approver": "USER24", "approver_code": "USER24"},
            format="json",
        )
        self.assertEqual(
            client.decide_credit_note_approval.call_args.kwargs["approver"], "USER37"
        )

    @patch("warehouse.views_credit_note_approval.SAPClient")
    def test_approve_records_the_party_the_amount_and_who_clicked(self, sap):
        client = sap.return_value
        client.credit_note_approval_stage.return_value = dict(PENDING_ROW)
        client.decide_credit_note_approval.return_value = {
            "message": "Credit note approved in SAP.", "signed_as": "USER37",
        }
        self.client.patch(status_url(75424), {"status": "APPROVED"}, format="json")

        audit = CreditNoteApprovalAudit.objects.get(approval_code=75424)
        self.assertEqual(audit.decision, CreditNoteApprovalAudit.DECISION_APPROVED)
        self.assertEqual(audit.sap_approver, "USER37")
        self.assertEqual(audit.created_by, self.user)
        self.assertEqual(audit.obj_type, "14")
        self.assertEqual(audit.card_code, "CUSTA000844")
        self.assertEqual(audit.total_amount, Decimal("17455.00"))
        self.assertEqual(audit.stage_code, 20)
        self.assertEqual(audit.company, self.company)

    @patch("warehouse.views_credit_note_approval.SAPClient")
    def test_an_unparseable_total_still_records_the_decision(self, sap):
        """SAP has taken the decision; bookkeeping must not pretend it has not."""
        client = sap.return_value
        client.credit_note_approval_stage.return_value = {
            **PENDING_ROW, "total_amount": "n/a",
        }
        client.decide_credit_note_approval.return_value = {
            "message": "Credit note approved in SAP.", "signed_as": "USER37",
        }
        response = self.client.patch(
            status_url(75424), {"status": "APPROVED"}, format="json"
        )
        self.assertEqual(response.status_code, 200)
        audit = CreditNoteApprovalAudit.objects.get(approval_code=75424)
        self.assertIsNone(audit.total_amount)

    @patch("warehouse.views_credit_note_approval.SAPClient")
    def test_someone_elses_approval_is_refused_without_calling_sap(self, sap):
        """The rubber-stamp guard: Honey must not decide Pankaj's credit note."""
        client = sap.return_value
        client.credit_note_approval_stage.return_value = dict(BLOCKED_ROW)
        response = self.client.patch(
            status_url(66636), {"status": "APPROVED"}, format="json"
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn("USER32", response.data["error"])
        self.assertIn("USER37", response.data["error"])
        client.decide_credit_note_approval.assert_not_called()

    @patch("warehouse.views_credit_note_approval.SAPClient")
    def test_an_unmapped_user_cannot_decide_anything(self, sap):
        """No mapping means the app cannot tell who the clicker is in SAP."""
        self.identity.delete()
        client = sap.return_value
        client.credit_note_approval_stage.return_value = dict(PENDING_ROW)
        response = self.client.patch(
            status_url(75424), {"status": "APPROVED"}, format="json"
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn("not linked to a SAP user", response.data["error"])
        client.decide_credit_note_approval.assert_not_called()

    @patch("warehouse.views_credit_note_approval.SAPClient")
    def test_my_own_missing_password_gets_its_own_message(self, sap):
        """Mapped correctly, but the app cannot authenticate as them."""
        client = sap.return_value
        client.credit_note_approval_stage.return_value = dict(PENDING_ROW)
        with override_settings(SAP_APPROVER_CREDENTIALS={"JIVO_OIL": {}}):
            response = self.client.patch(
                status_url(75424), {"status": "APPROVED"}, format="json"
            )
        self.assertEqual(response.status_code, 400)
        self.assertIn("password", response.data["error"].lower())
        client.decide_credit_note_approval.assert_not_called()

    @patch("warehouse.views_credit_note_approval.SAPClient")
    def test_an_already_decided_request_is_refused(self, sap):
        """The page can easily be a stage behind what SAP now holds."""
        client = sap.return_value
        client.credit_note_approval_stage.return_value = {
            **PENDING_ROW, "status": "APPROVED",
        }
        response = self.client.patch(
            status_url(75424), {"status": "APPROVED"}, format="json"
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("already approved", response.data["error"])
        client.decide_credit_note_approval.assert_not_called()

    @patch("warehouse.views_credit_note_approval.SAPClient")
    def test_a_stage_with_no_authorizer_is_refused(self, sap):
        client = sap.return_value
        client.credit_note_approval_stage.return_value = {
            **PENDING_ROW, "approver_code": None, "approver_name": None,
        }
        response = self.client.patch(
            status_url(75424), {"status": "APPROVED"}, format="json"
        )
        self.assertEqual(response.status_code, 400)
        client.decide_credit_note_approval.assert_not_called()

    @patch("warehouse.views_credit_note_approval.SAPClient")
    def test_reject_requires_a_reason(self, sap):
        sap.return_value.credit_note_approval_stage.return_value = dict(PENDING_ROW)
        response = self.client.patch(
            status_url(75424), {"status": "REJECTED"}, format="json"
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("rejection_reason", response.data)
        sap.return_value.decide_credit_note_approval.assert_not_called()

    @patch("warehouse.views_credit_note_approval.SAPClient")
    def test_reject_carries_the_reason_into_sap_and_the_audit(self, sap):
        client = sap.return_value
        client.credit_note_approval_stage.return_value = dict(PENDING_ROW)
        client.decide_credit_note_approval.return_value = {
            "message": "Credit note rejected in SAP.", "signed_as": "USER37",
        }
        response = self.client.patch(
            status_url(75424),
            {"status": "REJECTED", "rejection_reason": "Return never came back"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        kwargs = client.decide_credit_note_approval.call_args.kwargs
        self.assertFalse(kwargs["approve"])
        self.assertIn("Return never came back", kwargs["remarks"])
        audit = CreditNoteApprovalAudit.objects.get(approval_code=75424)
        self.assertEqual(audit.decision, CreditNoteApprovalAudit.DECISION_REJECTED)
        self.assertEqual(audit.rejection_reason, "Return never came back")

    def test_deciding_needs_more_than_viewing(self):
        self._drop_permission("can_approve_ar_credit_note")
        self._drop_permission("can_approve_ap_credit_note")
        response = self.client.patch(
            status_url(75424), {"status": "APPROVED"}, format="json"
        )
        self.assertEqual(response.status_code, 403)

    # ---- the A/R / A/P split ----------------------------------------------

    @patch("warehouse.views_credit_note_approval.SAPClient")
    def test_an_ar_only_user_never_reads_an_ap_row(self, sap):
        """Narrowed server-side: asking for ALL must not widen past the grant."""
        self._drop_permission("can_view_ap_credit_note_approval")
        sap.return_value.list_credit_note_approvals.return_value = []
        self.client.get(LIST_URL, {"family": "ALL"})
        self.assertEqual(
            sap.return_value.list_credit_note_approvals.call_args.kwargs["family"], "AR"
        )

    @patch("warehouse.views_credit_note_approval.SAPClient")
    def test_asking_for_a_family_you_do_not_hold_returns_nothing(self, sap):
        """Not an error, and emphatically not everything: an empty queue."""
        self._drop_permission("can_view_ap_credit_note_approval")
        response = self.client.get(LIST_URL, {"family": "AP"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, [])
        sap.return_value.list_credit_note_approvals.assert_not_called()

    @patch("warehouse.views_credit_note_approval.SAPClient")
    def test_the_badge_counts_only_the_families_you_hold(self, sap):
        self._drop_permission("can_view_ar_credit_note_approval")
        sap.return_value.count_pending_credit_note_approvals.return_value = 5
        self.client.get(COUNT_URL)
        self.assertEqual(
            sap.return_value.count_pending_credit_note_approvals.call_args.kwargs["family"],
            "AP",
        )

    @patch("warehouse.views_credit_note_approval.SAPClient")
    def test_approving_one_family_does_not_offer_the_other(self, sap):
        """Listed, visible, and explicitly not actionable."""
        self._drop_permission("can_approve_ap_credit_note")
        sap.return_value.list_credit_note_approvals.return_value = [
            dict(PENDING_ROW), dict(AP_ROW),
        ]
        ar_row, ap_row = self.client.get(LIST_URL).data
        self.assertTrue(ar_row["can_decide"])
        # Same authorizer, same password, same pending status — only the family
        # differs, and that is enough.
        self.assertTrue(ap_row["is_mine"])
        self.assertTrue(ap_row["credentials_configured"])
        self.assertFalse(ap_row["can_decide"])

    @patch("warehouse.views_credit_note_approval.SAPClient")
    def test_deciding_the_wrong_family_is_refused_without_calling_sap(self, sap):
        """The endpoint gate only proves they may decide SOMETHING."""
        self._drop_permission("can_approve_ap_credit_note")
        client = sap.return_value
        client.credit_note_approval_stage.return_value = dict(AP_ROW)
        response = self.client.patch(
            status_url(71301), {"status": "APPROVED"}, format="json"
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn("A/P", response.data["error"])
        client.decide_credit_note_approval.assert_not_called()

    @patch("warehouse.views_credit_note_approval.SAPClient")
    def test_the_family_comes_from_sap_not_from_the_caller(self, sap):
        """A body claiming A/R must not unlock an A/P document."""
        self._drop_permission("can_approve_ap_credit_note")
        client = sap.return_value
        client.credit_note_approval_stage.return_value = dict(AP_ROW)
        response = self.client.patch(
            status_url(71301),
            {"status": "APPROVED", "family": "AR", "obj_type": "14"},
            format="json",
        )
        self.assertEqual(response.status_code, 403)
        client.decide_credit_note_approval.assert_not_called()

    @patch("warehouse.views_credit_note_approval.SAPClient")
    def test_holding_both_families_decides_both(self, sap):
        client = sap.return_value
        client.credit_note_approval_stage.return_value = dict(AP_ROW)
        client.decide_credit_note_approval.return_value = {
            "message": "Credit note approved in SAP.", "signed_as": "USER37",
        }
        response = self.client.patch(
            status_url(71301), {"status": "APPROVED"}, format="json"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            CreditNoteApprovalAudit.objects.get(approval_code=71301).obj_type, "19"
        )

    @patch("warehouse.views_credit_note_approval.SAPClient")
    def test_a_failed_audit_write_never_undoes_a_sap_decision(self, sap):
        client = sap.return_value
        client.credit_note_approval_stage.return_value = dict(PENDING_ROW)
        client.decide_credit_note_approval.return_value = {
            "message": "Credit note approved in SAP.", "signed_as": "USER37",
        }
        with patch(
            "warehouse.views_credit_note_approval.CreditNoteApprovalAudit.objects.create",
            side_effect=Exception("db down"),
        ):
            response = self.client.patch(
                status_url(75424), {"status": "APPROVED"}, format="json"
            )
        self.assertEqual(response.status_code, 200)
