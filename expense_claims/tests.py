"""
Expense claims, end to end through the API.

SAP is patched out throughout: the two readers are the only things that talk
to HANA, so patching them keeps the suite offline without weakening what is
tested.
"""

from decimal import Decimal
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core.management import call_command
from rest_framework.test import APITestCase

from company.models import Company, UserCompany, UserRole
from sap_client.exceptions import SAPConnectionError, SAPDataError

from .constants import SUBMITTER_GROUP
from .models import ExpenseClaim, ExpenseClaimStatus

User = get_user_model()

BASE = "/api/v1/expense-claims"

#: The HOD right is the cash book's approve right.
CASH_APPROVER = "cash_book.can_approve_cash_entries"
SUBMIT = "expense_claims.can_submit_expense_claim"

BUDGET = {"branch_id": 2, "branch_name": "FACTORY"}
ACCOUNT = {"account_code": "5630004", "account_name": "REFRESHMENT"}


class ExpenseClaimTestCase(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.oil = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        cls.mart = Company.objects.create(name="Jivo Mart", code="JIVO_MART")
        cls.bev = Company.objects.create(name="Jivo Beverages", code="JIVO_BEVERAGES")
        cls.role = UserRole.objects.create(name="Staff")

        cls.worker = cls._user("worker@example.com", [SUBMIT])
        cls.hod = cls._user("hod@example.com", [SUBMIT, CASH_APPROVER])
        cls.other_hod = cls._user("other.hod@example.com", [SUBMIT, CASH_APPROVER])
        cls.outsider = cls._user("outsider@example.com", [])

    @classmethod
    def _user(cls, email, codes, companies=None):
        user = User.objects.create(email=email, full_name=email.split("@")[0].title())
        for company in companies or (cls.oil,):
            UserCompany.objects.create(user=user, company=company, role=cls.role)
        for code in codes:
            app_label, codename = code.split(".")
            user.user_permissions.add(
                Permission.objects.get(content_type__app_label=app_label, codename=codename)
            )
        return user

    def as_user(self, user, company=None):
        self.client.force_authenticate(user=user)
        self.client.credentials(HTTP_COMPANY_CODE=(company or self.oil).code)

    def payload(self, **changes):
        data = {
            "company": "JIVO_MART",
            "budget_id": 2,
            "gl_account_code": "5630004",
            "comment": "  Tea for the night shift ",
            "amount": "450",
            "approver": self.hod.id,
        }
        data.update(changes)
        return data

    @patch("expense_claims.services.GLAccountReader")
    @patch("expense_claims.services.BranchReader")
    def submit(self, branch_reader, gl_reader, user=None, **changes):
        branch_reader.return_value.resolve.return_value = BUDGET
        gl_reader.return_value.resolve.return_value = ACCOUNT
        self.as_user(user or self.worker)
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(f"{BASE}/claims/", self.payload(**changes), format="json")

    def claim(self, by=None, approver=None, **fields):
        return ExpenseClaim.objects.create(
            company=self.oil,
            budget_id=2,
            budget_name="FACTORY",
            gl_account_code="5630004",
            gl_account_name="REFRESHMENT",
            comment="Tea for the night shift",
            amount=Decimal("450.00"),
            approver=approver or self.hod,
            created_by=by or self.worker,
            **fields,
        )

    def decide(self, claim, user, approve, note=""):
        self.as_user(user)
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(
                f"{BASE}/claims/{claim.id}/decide/",
                {"approve": approve, "note": note},
                format="json",
            )


class EntryTests(ExpenseClaimTestCase):
    def test_the_whole_expense_is_put_in_and_sent_to_the_hod(self):
        with patch("expense_claims.notifications.sent_to_hod") as notify:
            response = self.submit()
        self.assertEqual(response.status_code, 201, response.data)
        claim = ExpenseClaim.objects.get(pk=response.data["id"])
        self.assertEqual(claim.company, self.mart)
        self.assertEqual(response.data["company_name"], "Mart")
        self.assertEqual((claim.budget_id, claim.budget_name), (2, "FACTORY"))
        self.assertEqual((claim.gl_account_code, claim.gl_account_name), ("5630004", "REFRESHMENT"))
        self.assertEqual(claim.comment, "Tea for the night shift")
        self.assertEqual(claim.amount, Decimal("450.00"))
        self.assertEqual(claim.approver, self.hod)
        self.assertEqual(claim.status, ExpenseClaimStatus.PENDING_APPROVAL)
        self.assertEqual(claim.created_by, self.worker)
        notify.assert_called_once()

    @patch("expense_claims.services.GLAccountReader")
    @patch("expense_claims.services.BranchReader")
    def test_budget_and_account_are_read_from_the_chosen_companys_sap(self, branch_reader, gl_reader):
        branch_reader.return_value.resolve.return_value = BUDGET
        gl_reader.return_value.resolve.return_value = ACCOUNT
        # Header says Oil; the page says Beverages. The page wins.
        self.as_user(self.worker, company=self.oil)
        self.client.post(f"{BASE}/claims/", self.payload(company="JIVO_BEVERAGES"), format="json")
        branch_reader.assert_called_once_with("JIVO_BEVERAGES")
        gl_reader.assert_called_once_with("JIVO_BEVERAGES")

    def test_only_oil_mart_or_beverages(self):
        Company.objects.create(name="Test", code="TEST_MU")
        response = self.submit(company="TEST_MU")
        self.assertEqual(response.status_code, 400)
        self.assertIn("company", response.data)

    def test_every_field_is_needed(self):
        for field in ("company", "budget_id", "gl_account_code", "comment", "amount", "approver"):
            data = self.payload()
            del data[field]
            self.as_user(self.worker)
            response = self.client.post(f"{BASE}/claims/", data, format="json")
            self.assertEqual(response.status_code, 400, field)
        self.assertFalse(ExpenseClaim.objects.exists())

    def test_a_blank_comment_or_a_zero_amount_is_refused(self):
        self.assertEqual(self.submit(comment="   ").status_code, 400)
        self.assertEqual(self.submit(amount="0").status_code, 400)

    def test_any_active_user_can_be_chosen(self):
        self.assertEqual(self.submit(approver=self.outsider.id).status_code, 201)

    def test_an_inactive_user_cannot_be_chosen(self):
        gone = User.objects.create(email="gone@example.com", is_active=False)
        response = self.submit(approver=gone.id)
        self.assertEqual(response.status_code, 400)
        self.assertIn("approver", response.data)

    def test_nobody_sends_their_own_expense_to_themselves(self):
        response = self.submit(user=self.hod, approver=self.hod.id)
        self.assertEqual(response.status_code, 400)

    @patch("expense_claims.services.GLAccountReader")
    @patch("expense_claims.services.BranchReader")
    def test_an_account_sap_will_not_post_to_is_refused(self, branch_reader, gl_reader):
        branch_reader.return_value.resolve.return_value = BUDGET
        gl_reader.return_value.resolve.side_effect = SAPDataError("not postable")
        self.as_user(self.worker)
        response = self.client.post(f"{BASE}/claims/", self.payload(), format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("gl_account_code", response.data)
        self.assertFalse(ExpenseClaim.objects.exists())

    @patch("expense_claims.services.GLAccountReader")
    @patch("expense_claims.services.BranchReader")
    def test_sap_down_answers_503(self, branch_reader, gl_reader):
        branch_reader.return_value.resolve.side_effect = SAPConnectionError("down")
        self.as_user(self.worker)
        response = self.client.post(f"{BASE}/claims/", self.payload(), format="json")
        self.assertEqual(response.status_code, 503)

    def test_somebody_without_the_right_cannot_submit(self):
        self.assertEqual(self.submit(user=self.outsider).status_code, 403)

    def test_companies_are_oil_mart_and_beverages(self):
        Company.objects.create(name="Test", code="TEST_MU")
        self.as_user(self.worker)
        response = self.client.get(f"{BASE}/companies/")
        self.assertEqual(
            response.data,
            [
                {"code": "JIVO_OIL", "name": "Oil"},
                {"code": "JIVO_MART", "name": "Mart"},
                {"code": "JIVO_BEVERAGES", "name": "Beverages"},
            ],
        )

    @patch("expense_claims.views.BranchReader")
    def test_budgets_come_from_the_named_companys_sap_with_factory_first_choice(self, reader):
        reader.return_value.list.return_value = [
            {"branch_id": 1, "branch_name": "DELHI"},
            {"branch_id": 2, "branch_name": "FACTORY"},
        ]
        self.as_user(self.worker)
        response = self.client.get(f"{BASE}/budgets/?company=JIVO_BEVERAGES")
        self.assertEqual(response.status_code, 200)
        reader.assert_called_once_with("JIVO_BEVERAGES")
        self.assertEqual(
            [(row["budget_name"], row["is_default"]) for row in response.data],
            [("DELHI", False), ("FACTORY", True)],
        )

    @patch("expense_claims.views.BranchReader")
    def test_budgets_answer_503_when_sap_is_down(self, reader):
        reader.return_value.list.side_effect = SAPConnectionError("down")
        self.as_user(self.worker)
        self.assertEqual(self.client.get(f"{BASE}/budgets/?company=JIVO_OIL").status_code, 503)

    @patch("expense_claims.views.GLAccountReader")
    def test_gl_accounts_are_searched_in_the_named_companys_sap(self, reader):
        reader.return_value.search.return_value = [ACCOUNT]
        self.as_user(self.worker)
        response = self.client.get(f"{BASE}/gl-accounts/?company=JIVO_MART&search=refresh")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, [ACCOUNT])
        reader.assert_called_once_with("JIVO_MART")
        self.assertEqual(reader.return_value.search.call_args.args[0], "refresh")

    def test_approvers_are_every_active_user_but_the_caller(self):
        User.objects.create(email="gone@example.com", is_active=False)
        self.as_user(self.worker)
        response = self.client.get(f"{BASE}/approvers/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            sorted(row["email"] for row in response.data),
            ["hod@example.com", "other.hod@example.com", "outsider@example.com"],
        )


class EditTests(ExpenseClaimTestCase):
    @patch("expense_claims.services.GLAccountReader")
    @patch("expense_claims.services.BranchReader")
    def edit(self, claim, branch_reader, gl_reader, user=None, **changes):
        branch_reader.return_value.resolve.return_value = {"branch_id": 1, "branch_name": "DELHI"}
        gl_reader.return_value.resolve.return_value = {
            "account_code": "5680023",
            "account_name": "POSTAGE & COURIER",
        }
        self.as_user(user or self.worker)
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.put(
                f"{BASE}/claims/{claim.id}/",
                self.payload(**{"budget_id": 1, "gl_account_code": "5680023", **changes}),
                format="json",
            )

    def test_the_submitter_changes_a_waiting_expense(self):
        claim = self.claim()
        with patch("expense_claims.notifications.sent_to_hod") as notify:
            response = self.edit(claim, comment="Courier", amount="99.50")
        self.assertEqual(response.status_code, 200, response.data)
        claim.refresh_from_db()
        self.assertEqual(claim.company, self.mart)
        self.assertEqual((claim.budget_id, claim.budget_name), (1, "DELHI"))
        self.assertEqual(claim.gl_account_name, "POSTAGE & COURIER")
        self.assertEqual((claim.comment, claim.amount), ("Courier", Decimal("99.50")))
        self.assertEqual(claim.status, ExpenseClaimStatus.PENDING_APPROVAL)
        # Still with the same approver: a correction is not news to them.
        notify.assert_not_called()

    def test_sending_it_to_somebody_else_tells_them(self):
        claim = self.claim()
        with patch("expense_claims.notifications.sent_to_hod") as notify:
            self.edit(claim, approver=self.other_hod.id)
        claim.refresh_from_db()
        self.assertEqual(claim.approver, self.other_hod)
        notify.assert_called_once()

    def test_changing_a_rejected_expense_sends_it_again(self):
        claim = self.claim()
        self.decide(claim, self.hod, False, "Wrong account")
        with patch("expense_claims.notifications.sent_to_hod") as notify:
            self.assertEqual(self.edit(claim).status_code, 200)
        claim.refresh_from_db()
        self.assertEqual(claim.status, ExpenseClaimStatus.PENDING_APPROVAL)
        self.assertEqual(claim.decision_note, "")
        self.assertIsNone(claim.decided_by)
        notify.assert_called_once()

    def test_an_approved_expense_cannot_be_changed(self):
        claim = self.claim()
        self.decide(claim, self.hod, True)
        self.assertEqual(self.edit(claim).status_code, 400)
        claim.refresh_from_db()
        self.assertEqual(claim.comment, "Tea for the night shift")

    def test_only_the_submitter_can_change_it(self):
        claim = self.claim()
        self.assertEqual(self.edit(claim, user=self.hod, approver=self.other_hod.id).status_code, 403)


class ApprovalTests(ExpenseClaimTestCase):
    def test_hods_see_every_expense_or_just_their_own(self):
        mine = self.claim()
        theirs = self.claim(approver=self.other_hod)
        self.as_user(self.hod)

        every = self.client.get(f"{BASE}/claims/")
        self.assertEqual(every.status_code, 200)
        self.assertEqual(sorted(row["id"] for row in every.data["results"]), [mine.id, theirs.id])

        for_me = self.client.get(f"{BASE}/claims/?for_me=1&status=PENDING_APPROVAL")
        self.assertEqual([row["id"] for row in for_me.data["results"]], [mine.id])
        self.assertEqual(for_me.data["counts"]["PENDING_APPROVAL"], 1)

    def test_the_list_is_the_same_whichever_company_is_selected(self):
        self.claim()
        self.hod.usercompany_set.create(company=self.mart, role=self.role)
        self.as_user(self.hod, company=self.mart)
        self.assertEqual(len(self.client.get(f"{BASE}/claims/").data["results"]), 1)

    def test_anybody_sees_what_was_sent_to_them_but_not_every_expense(self):
        to_outsider = self.claim(approver=self.outsider)
        self.claim()
        self.as_user(self.outsider)
        theirs = self.client.get(f"{BASE}/claims/?for_me=1")
        self.assertEqual(theirs.status_code, 200)
        self.assertEqual([row["id"] for row in theirs.data["results"]], [to_outsider.id])
        self.assertEqual(self.client.get(f"{BASE}/claims/").status_code, 403)

    def test_anybody_sees_the_expenses_they_put_in(self):
        mine = self.claim()
        self.claim(by=self.outsider)
        self.as_user(self.worker)
        response = self.client.get(f"{BASE}/claims/?by_me=1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([row["id"] for row in response.data["results"]], [mine.id])

    def test_anybody_it_was_sent_to_can_decide_it(self):
        claim = self.claim(approver=self.outsider)
        self.assertEqual(self.decide(claim, self.outsider, True).status_code, 200)

    def test_the_hod_approves(self):
        claim = self.claim()
        with patch("expense_claims.notifications.decided") as notify:
            response = self.decide(claim, self.hod, True)
        self.assertEqual(response.status_code, 200, response.data)
        claim.refresh_from_db()
        self.assertEqual(claim.status, ExpenseClaimStatus.APPROVED)
        self.assertEqual(claim.decided_by, self.hod)
        self.assertIsNotNone(claim.decided_at)
        notify.assert_called_once()

    def test_a_rejection_must_say_why(self):
        claim = self.claim()
        self.assertEqual(self.decide(claim, self.hod, False).status_code, 400)
        self.assertEqual(self.decide(claim, self.hod, False, "Bill missing").status_code, 200)
        claim.refresh_from_db()
        self.assertEqual(claim.status, ExpenseClaimStatus.REJECTED)
        self.assertEqual(claim.decision_note, "Bill missing")

    def test_another_hod_cannot_decide(self):
        self.assertEqual(self.decide(self.claim(), self.other_hod, True).status_code, 403)

    def test_an_expense_is_decided_once(self):
        claim = self.claim()
        self.decide(claim, self.hod, True)
        self.assertEqual(self.decide(claim, self.hod, False, "No").status_code, 400)


class GroupTests(ExpenseClaimTestCase):
    def test_setup_creates_the_submitter_group_and_assigns_everyone(self):
        call_command("setup_expense_claim_groups", "--assign-everyone", stdout=StringIO())
        submitters = Group.objects.get(name=SUBMITTER_GROUP)
        self.assertEqual(
            list(submitters.permissions.values_list("codename", flat=True)),
            ["can_submit_expense_claim"],
        )
        self.assertTrue(submitters.user_set.filter(pk=self.outsider.pk).exists())

    def test_a_new_account_joins_the_submitter_group(self):
        call_command("setup_expense_claim_groups", stdout=StringIO())
        newcomer = User.objects.create(email="new@example.com")
        self.assertTrue(newcomer.groups.filter(name=SUBMITTER_GROUP).exists())
