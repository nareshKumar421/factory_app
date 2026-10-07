"""
Expense claims, end to end through the API.

SAP is patched out throughout: ``ExpenseSapReader`` is the only thing that
talks to HANA, so patching it keeps the suite offline without weakening what
is tested.
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

from .constants import APPROVER_GROUP, SUBMITTER_GROUP
from .models import ExpenseClaim, ExpenseClaimStatus

User = get_user_model()

BASE = "/api/v1/expense-claims"
READER = "expense_claims.services.ExpenseSapReader"
VIEW_READER = "expense_claims.views.ExpenseSapReader"

SUBMIT = "expense_claims.can_submit_expense_claim"
APPROVE = "expense_claims.can_approve_expense_claims"

BUDGET = {"budget_code": "Factory", "budget_name": "Factory"}
ACCOUNT = {"account_code": "5630004", "account_name": "REFRESHMENT"}


def _sap_ok(reader):
    reader.return_value.budget.return_value = BUDGET
    reader.return_value.expense_account.return_value = ACCOUNT


class ExpenseClaimTestCase(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.oil = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        cls.mart = Company.objects.create(name="Jivo Mart", code="JIVO_MART")
        cls.bev = Company.objects.create(name="Jivo Beverages", code="JIVO_BEVERAGES")
        cls.role = UserRole.objects.create(name="Staff")

        cls.worker = cls._user("worker@example.com", [SUBMIT])
        cls.approver = cls._user("approver@example.com", [SUBMIT, APPROVE])
        cls.other_approver = cls._user(
            "other.approver@example.com", [SUBMIT, APPROVE], companies=(cls.mart,)
        )
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
        self.client.credentials(
            HTTP_COMPANY_CODE=(company or user.usercompany_set.first().company).code
        )

    def payload(self, **changes):
        data = {
            "company": "JIVO_MART",
            "budget_code": "Factory",
            "gl_account_code": "5630004",
            "gl_description": "",
            "comment": "  Tea for the night shift ",
            "amount": "450",
        }
        data.update(changes)
        return data

    @patch(READER)
    def submit(self, reader, user=None, **changes):
        _sap_ok(reader)
        self.as_user(user or self.worker)
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(f"{BASE}/claims/", self.payload(**changes), format="json")

    @patch(READER)
    def edit(self, claim, reader, user=None, **changes):
        _sap_ok(reader)
        self.as_user(user or self.worker)
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.put(
                f"{BASE}/claims/{claim.id}/", self.payload(**changes), format="json"
            )

    def claim(self, by=None, **fields):
        return ExpenseClaim.objects.create(
            company=self.oil,
            budget_code="Factory",
            budget_name="Factory",
            gl_account_code="5630004",
            gl_account_name="REFRESHMENT",
            comment="Tea for the night shift",
            amount=Decimal("450.00"),
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
    def test_an_expense_is_put_in_and_the_approvers_are_told(self):
        with patch("expense_claims.notifications.waiting") as notify:
            response = self.submit()
        self.assertEqual(response.status_code, 201, response.data)
        claim = ExpenseClaim.objects.get(pk=response.data["id"])
        self.assertEqual(claim.company, self.mart)
        self.assertEqual(response.data["company_name"], "Mart")
        self.assertEqual((claim.budget_code, claim.budget_name), ("Factory", "Factory"))
        self.assertEqual((claim.gl_account_code, claim.gl_account_name), ("5630004", "REFRESHMENT"))
        self.assertEqual(claim.comment, "Tea for the night shift")
        self.assertEqual(claim.amount, Decimal("450.00"))
        self.assertEqual(claim.status, ExpenseClaimStatus.PENDING_APPROVAL)
        self.assertEqual(claim.created_by, self.worker)
        notify.assert_called_once()

    @patch(READER)
    def test_budget_and_account_are_read_from_the_chosen_companys_sap(self, reader):
        _sap_ok(reader)
        self.as_user(self.worker)
        self.client.post(f"{BASE}/claims/", self.payload(company="JIVO_BEVERAGES"), format="json")
        reader.assert_called_once_with("JIVO_BEVERAGES")
        reader.return_value.budget.assert_called_once_with("Factory")
        reader.return_value.expense_account.assert_called_once_with("5630004")

    @patch(READER)
    def test_the_gl_account_can_be_skipped_for_what_it_is_for(self, reader):
        _sap_ok(reader)
        self.as_user(self.worker)
        response = self.client.post(
            f"{BASE}/claims/",
            self.payload(gl_account_code="", gl_description="  pump repair  "),
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        claim = ExpenseClaim.objects.get(pk=response.data["id"])
        self.assertEqual((claim.gl_account_code, claim.gl_description), ("", "pump repair"))
        reader.return_value.expense_account.assert_not_called()

    def test_one_of_the_account_or_what_it_is_for_is_needed(self):
        response = self.submit(gl_account_code="", gl_description=" ")
        self.assertEqual(response.status_code, 400)
        self.assertIn("gl_account_code", response.data)

    def test_a_picked_account_wins_over_a_description(self):
        response = self.submit(gl_description="pump repair")
        self.assertEqual(response.data["gl_description"], "")

    def test_only_oil_mart_or_beverages(self):
        Company.objects.create(name="Test", code="TEST_MU")
        response = self.submit(company="TEST_MU")
        self.assertEqual(response.status_code, 400)
        self.assertIn("company", response.data)

    def test_a_blank_comment_or_a_zero_amount_is_refused(self):
        self.assertEqual(self.submit(comment="   ").status_code, 400)
        self.assertEqual(self.submit(amount="0").status_code, 400)
        self.assertFalse(ExpenseClaim.objects.exists())

    @patch(READER)
    def test_an_account_that_is_not_an_expense_account_is_refused(self, reader):
        reader.return_value.budget.return_value = BUDGET
        reader.return_value.expense_account.side_effect = SAPDataError("not an expense account")
        self.as_user(self.worker)
        response = self.client.post(f"{BASE}/claims/", self.payload(), format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("gl_account_code", response.data)

    @patch(READER)
    def test_sap_down_answers_503(self, reader):
        reader.return_value.budget.side_effect = SAPConnectionError("down")
        self.as_user(self.worker)
        response = self.client.post(f"{BASE}/claims/", self.payload(), format="json")
        self.assertEqual(response.status_code, 503)

    def test_somebody_without_the_right_cannot_submit(self):
        self.assertEqual(self.submit(user=self.outsider).status_code, 403)

    def test_anybody_sees_the_expenses_they_put_in(self):
        mine = self.claim()
        self.claim(by=self.approver)
        self.as_user(self.worker)
        response = self.client.get(f"{BASE}/claims/?by_me=1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([row["id"] for row in response.data["results"]], [mine.id])

    def test_companies_are_oil_mart_and_beverages(self):
        Company.objects.create(name="Test", code="TEST_MU")
        self.as_user(self.worker)
        response = self.client.get(f"{BASE}/companies/")
        self.assertEqual(
            [row["name"] for row in response.data], ["Oil", "Mart", "Beverages"]
        )

    @patch(VIEW_READER)
    def test_budgets_are_dimension_3_with_factory_the_default(self, reader):
        reader.return_value.budgets.return_value = [
            {"budget_code": "BackOff", "budget_name": "Back Office"},
            {"budget_code": "Factory", "budget_name": "Factory"},
        ]
        self.as_user(self.worker)
        response = self.client.get(f"{BASE}/budgets/?company=JIVO_BEVERAGES")
        self.assertEqual(response.status_code, 200)
        reader.assert_called_once_with("JIVO_BEVERAGES")
        self.assertEqual(
            [(row["budget_code"], row["is_default"]) for row in response.data],
            [("BackOff", False), ("Factory", True)],
        )

    @patch(VIEW_READER)
    def test_budgets_answer_503_when_sap_is_down(self, reader):
        reader.return_value.budgets.side_effect = SAPConnectionError("down")
        self.as_user(self.worker)
        self.assertEqual(self.client.get(f"{BASE}/budgets/?company=JIVO_OIL").status_code, 503)

    @patch(VIEW_READER)
    def test_gl_accounts_are_the_named_companys_expense_accounts(self, reader):
        reader.return_value.expense_accounts.return_value = [ACCOUNT]
        self.as_user(self.worker)
        response = self.client.get(f"{BASE}/gl-accounts/?company=JIVO_MART&search=refresh")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, [ACCOUNT])
        reader.assert_called_once_with("JIVO_MART")
        self.assertEqual(reader.return_value.expense_accounts.call_args.args[0], "refresh")


class EditTests(ExpenseClaimTestCase):
    def test_the_submitter_changes_a_waiting_expense_quietly(self):
        claim = self.claim()
        with patch("expense_claims.notifications.waiting") as notify:
            response = self.edit(claim, comment="Courier", amount="99.50")
        self.assertEqual(response.status_code, 200, response.data)
        claim.refresh_from_db()
        self.assertEqual(claim.company, self.mart)
        self.assertEqual((claim.comment, claim.amount), ("Courier", Decimal("99.50")))
        self.assertEqual(claim.status, ExpenseClaimStatus.PENDING_APPROVAL)
        notify.assert_not_called()

    def test_changing_a_rejected_expense_sends_it_again(self):
        claim = self.claim()
        self.decide(claim, self.approver, False, "Wrong account")
        with patch("expense_claims.notifications.waiting") as notify:
            self.assertEqual(self.edit(claim).status_code, 200)
        claim.refresh_from_db()
        self.assertEqual(claim.status, ExpenseClaimStatus.PENDING_APPROVAL)
        self.assertEqual(claim.decision_note, "")
        self.assertIsNone(claim.decided_by)
        notify.assert_called_once()

    def test_an_approved_expense_cannot_be_changed(self):
        claim = self.claim()
        self.decide(claim, self.approver, True)
        self.assertEqual(self.edit(claim).status_code, 400)

    def test_only_the_submitter_can_change_it(self):
        self.assertEqual(self.edit(self.claim(), user=self.approver).status_code, 403)


class ApprovalTests(ExpenseClaimTestCase):
    def test_approvers_see_every_expense_whichever_company_is_selected(self):
        first = self.claim()
        second = self.claim(by=self.outsider)
        self.as_user(self.other_approver, company=self.mart)
        response = self.client.get(f"{BASE}/claims/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            sorted(row["id"] for row in response.data["results"]), [first.id, second.id]
        )
        self.assertEqual(response.data["counts"]["PENDING_APPROVAL"], 2)

    def test_status_narrows_the_list(self):
        waiting = self.claim()
        self.decide(self.claim(), self.approver, True)
        self.as_user(self.approver)
        response = self.client.get(f"{BASE}/claims/?status=PENDING_APPROVAL")
        self.assertEqual([row["id"] for row in response.data["results"]], [waiting.id])

    def test_somebody_who_is_not_an_approver_cannot_see_every_expense(self):
        self.as_user(self.worker)
        self.assertEqual(self.client.get(f"{BASE}/claims/").status_code, 403)

    def test_any_approver_approves(self):
        claim = self.claim()
        with patch("expense_claims.notifications.decided") as notify:
            response = self.decide(claim, self.other_approver, True)
        self.assertEqual(response.status_code, 200, response.data)
        claim.refresh_from_db()
        self.assertEqual(claim.status, ExpenseClaimStatus.APPROVED)
        self.assertEqual(claim.decided_by, self.other_approver)
        notify.assert_called_once()

    def test_a_rejection_must_say_why(self):
        claim = self.claim()
        self.assertEqual(self.decide(claim, self.approver, False).status_code, 400)
        self.assertEqual(self.decide(claim, self.approver, False, "Bill missing").status_code, 200)
        claim.refresh_from_db()
        self.assertEqual(claim.status, ExpenseClaimStatus.REJECTED)
        self.assertEqual(claim.decision_note, "Bill missing")

    def test_nobody_approves_their_own_expense(self):
        claim = self.claim(by=self.approver)
        self.assertEqual(self.decide(claim, self.approver, True).status_code, 403)

    def test_somebody_who_is_not_an_approver_cannot_decide(self):
        self.assertEqual(self.decide(self.claim(), self.outsider, True).status_code, 403)

    def test_an_expense_is_decided_once(self):
        claim = self.claim()
        self.decide(claim, self.approver, True)
        self.assertEqual(self.decide(claim, self.other_approver, False, "No").status_code, 400)


class GroupTests(ExpenseClaimTestCase):
    def test_setup_creates_both_groups_and_assigns_everyone_to_submitter(self):
        call_command("setup_expense_claim_groups", "--assign-everyone", stdout=StringIO())
        submitters = Group.objects.get(name=SUBMITTER_GROUP)
        self.assertEqual(
            list(submitters.permissions.values_list("codename", flat=True)),
            ["can_submit_expense_claim"],
        )
        self.assertTrue(submitters.user_set.filter(pk=self.outsider.pk).exists())
        approvers = Group.objects.get(name=APPROVER_GROUP)
        self.assertEqual(
            sorted(approvers.permissions.values_list("codename", flat=True)),
            ["can_approve_expense_claims", "can_submit_expense_claim"],
        )
        # Approving is somebody's choice, never everybody's.
        self.assertFalse(approvers.user_set.exists())

    def test_a_new_account_joins_the_submitter_group(self):
        call_command("setup_expense_claim_groups", stdout=StringIO())
        newcomer = User.objects.create(email="new@example.com")
        self.assertTrue(newcomer.groups.filter(name=SUBMITTER_GROUP).exists())
