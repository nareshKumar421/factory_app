"""
SAP Finance through the real permission stack.

    python manage.py test sap_finance --settings=config.sqlite_test_settings

Every request goes through IsAuthenticated + HasCompanyContext + the app's own
right, so these also pin a missing Company-Code header and each right. SAP is
mocked where each module looks ``SAPClient`` up (views and services).
"""

from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core.management import call_command
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from company.models import Company, UserCompany, UserRole
from sap_client.exceptions import SAPConnectionError, SAPValidationError

from . import permissions as guards
from .management.commands.setup_sap_finance_groups import SAP_FINANCE_GROUPS
from .models import SapBudgetChange

BASE = "/api/v1/sap-finance/"
ALL_PERMISSIONS = [
    "can_view_sap_ledgers", "can_view_sap_budgets", "can_manage_sap_budgets", "can_view_sap_outstanding",
]

BUDGET_BODY = {
    "budget": "BUD-ADMIN",
    "sub_budget": "",
    "lines": [
        {"month": "2026-04-01", "fixed_amount": "1000.00", "variable_amount": "250.50"},
        {"month": "2026-05-01", "fixed_amount": "1000.00", "variable_amount": "0", "sub_budget": "SUB-1"},
    ],
}

SAP_BUDGET = {
    "DocEntry": 12,
    "DocNum": 12,
    "U_BUDGET": "BUD-ADMIN",
    "U_SUB_BUDGET": None,
    "CreateDate": "2026-04-02T00:00:00Z",
    "BUDGET1Collection": [
        {"LineId": 1, "U_MONTH": "2026-04-01T00:00:00Z", "U_FIXED_AMOUNT": 1000.0, "U_V_AMOUNT": 250.5, "U_SUB_BUDGET": None},
    ],
}


class SapFinanceTestCase(APITestCase):
    """One company, one user, and whatever rights a test grants."""

    permissions = ALL_PERMISSIONS

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            email="sap_finance@example.com",
            password="testpass",
            full_name="SAP Finance User",
            employee_code="SCAF001",
        )
        self.company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        self.role = UserRole.objects.create(name="Staff")
        UserCompany.objects.create(
            user=self.user, company=self.company, role=self.role, is_default=True
        )
        self.headers = {"HTTP_COMPANY_CODE": self.company.code}
        self.grant(*self.permissions)

    def grant(self, *codenames):
        """Add rights, then re-fetch the user: has_perm() caches per instance."""
        if codenames:
            self.user.user_permissions.add(
                *Permission.objects.filter(
                    content_type__app_label="sap_finance", codename__in=codenames
                )
            )
        self.user = get_user_model().objects.get(pk=self.user.pk)
        self.client = APIClient()
        self.client.force_authenticate(self.user)


@patch("sap_finance.views.SAPClient")
class LedgerApiTests(SapFinanceTestCase):
    def test_journal_entries_pass_the_validated_filters_to_sap(self, sap):
        sap.return_value.journal_entries.return_value = [{"trans_id": 9, "lines": []}]
        response = self.client.get(
            f"{BASE}journal-entries/?reference=INV&date_from=2026-04-01&limit=5", **self.headers
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)
        sap.assert_called_once_with(company_code="JIVO_OIL")
        kwargs = sap.return_value.journal_entries.call_args.kwargs
        self.assertEqual(kwargs["reference"], "INV")
        self.assertEqual(kwargs["limit"], 5)
        self.assertEqual(str(kwargs["date_from"]), "2026-04-01")

    def test_an_inverted_date_range_is_refused_before_sap(self, sap):
        response = self.client.get(
            f"{BASE}journal-entries/?date_from=2026-05-01&date_to=2026-04-01", **self.headers
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        sap.return_value.journal_entries.assert_not_called()

    def test_the_ledger_needs_an_account(self, sap):
        response = self.client.get(f"{BASE}general-ledger/", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_the_ledger_reads_one_account(self, sap):
        sap.return_value.general_ledger.return_value = {"account": "1101001", "lines": []}
        response = self.client.get(f"{BASE}general-ledger/?account=1101001&date_to=2026-06-30", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        args, kwargs = sap.return_value.general_ledger.call_args
        self.assertEqual(args, ("1101001",))
        self.assertEqual(str(kwargs["date_to"]), "2026-06-30")

    def test_account_search_needs_two_characters(self, sap):
        response = self.client.get(f"{BASE}ledger-accounts/?search=a", **self.headers)
        self.assertEqual(response.data, [])
        sap.return_value.ledger_account_search.assert_not_called()

    def test_sap_outage_is_503_and_refusal_is_400(self, sap):
        sap.return_value.chart_of_accounts.side_effect = SAPConnectionError("down")
        self.assertEqual(
            self.client.get(f"{BASE}chart-of-accounts/", **self.headers).status_code,
            status.HTTP_503_SERVICE_UNAVAILABLE,
        )
        sap.return_value.chart_of_accounts.side_effect = SAPValidationError("drawer must be a number")
        self.assertEqual(
            self.client.get(f"{BASE}chart-of-accounts/?drawer=x", **self.headers).status_code,
            status.HTTP_400_BAD_REQUEST,
        )


@patch("sap_finance.services.SAPClient")
@patch("sap_finance.views.SAPClient")
class BudgetApiTests(SapFinanceTestCase):
    def test_create_sends_the_portal_payload_and_records_who(self, view_sap, service_sap):
        service_sap.return_value.create_budget.return_value = SAP_BUDGET
        response = self.client.post(f"{BASE}budgets/", BUDGET_BODY, format="json", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        payload = service_sap.return_value.create_budget.call_args[0][0]
        self.assertEqual(payload["U_BUDGET"], "BUD-ADMIN")
        self.assertIsNone(payload["U_SUB_BUDGET"])
        self.assertEqual(
            payload["BUDGET1Collection"][1],
            {"U_MONTH": "2026-05-01", "U_FIXED_AMOUNT": 1000.0, "U_V_AMOUNT": 0.0, "U_SUB_BUDGET": "SUB-1"},
        )
        change = SapBudgetChange.objects.get()
        self.assertEqual((change.action, change.doc_entry, change.line_count), ("CREATE", 12, 2))
        self.assertEqual(change.created_by, self.user)
        self.assertEqual(response.data["lines"][0]["month"], "2026-04-01")

    def test_a_budget_needs_lines_and_distinct_months(self, view_sap, service_sap):
        empty = dict(BUDGET_BODY, lines=[])
        self.assertEqual(
            self.client.post(f"{BASE}budgets/", empty, format="json", **self.headers).status_code,
            status.HTTP_400_BAD_REQUEST,
        )
        twice = dict(BUDGET_BODY, lines=[BUDGET_BODY["lines"][0], BUDGET_BODY["lines"][0]])
        self.assertEqual(
            self.client.post(f"{BASE}budgets/", twice, format="json", **self.headers).status_code,
            status.HTTP_400_BAD_REQUEST,
        )
        service_sap.return_value.create_budget.assert_not_called()

    def test_sap_refusing_the_create_leaves_no_audit_row(self, view_sap, service_sap):
        service_sap.return_value.create_budget.side_effect = SAPValidationError("Invalid budget code")
        response = self.client.post(f"{BASE}budgets/", BUDGET_BODY, format="json", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Invalid budget code", response.data["detail"])
        self.assertFalse(SapBudgetChange.objects.exists())

    def test_update_replaces_and_records(self, view_sap, service_sap):
        view_sap.return_value.get_budget.return_value = SAP_BUDGET
        response = self.client.put(f"{BASE}budgets/12/", BUDGET_BODY, format="json", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        service_sap.return_value.update_budget.assert_called_once()
        self.assertEqual(SapBudgetChange.objects.get().action, "UPDATE")

    def test_a_missing_budget_is_404(self, view_sap, service_sap):
        view_sap.return_value.get_budget.return_value = None
        response = self.client.get(f"{BASE}budgets/99/", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_delete_keeps_a_snapshot_of_what_went(self, view_sap, service_sap):
        view_sap.return_value.get_budget.return_value = SAP_BUDGET
        response = self.client.delete(f"{BASE}budgets/12/", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        service_sap.return_value.delete_budget.assert_called_once_with(12)
        change = SapBudgetChange.objects.get()
        self.assertEqual((change.action, change.budget_code, change.line_count), ("DELETE", "BUD-ADMIN", 1))

    def test_the_change_log_is_this_companys_only(self, view_sap, service_sap):
        other = Company.objects.create(name="Jivo Mart", code="JIVO_MART")
        SapBudgetChange.objects.create(company=other, action="CREATE", doc_entry=1)
        mine = SapBudgetChange.objects.create(company=self.company, action="CREATE", doc_entry=2)
        response = self.client.get(f"{BASE}budget-changes/", **self.headers)
        self.assertEqual([row["id"] for row in response.data], [mine.id])


@patch("sap_finance.services.SAPClient")
@patch("sap_finance.views.SAPClient")
class PermissionTests(SapFinanceTestCase):
    permissions = []

    def test_missing_company_header_is_refused(self, view_sap, service_sap):
        self.grant(*ALL_PERMISSIONS)
        self.assertEqual(self.client.get(f"{BASE}journal-entries/").status_code, status.HTTP_403_FORBIDDEN)

    def test_no_rights_is_refused_everywhere(self, view_sap, service_sap):
        for path in ("journal-entries/", "chart-of-accounts/", "general-ledger/?account=1", "budgets/", "budget-changes/"):
            with self.subTest(path=path):
                self.assertEqual(
                    self.client.get(f"{BASE}{path}", **self.headers).status_code, status.HTTP_403_FORBIDDEN
                )

    def test_ledger_viewers_do_not_see_budgets(self, view_sap, service_sap):
        view_sap.return_value.journal_entries.return_value = []
        self.grant("can_view_sap_ledgers")
        self.assertEqual(self.client.get(f"{BASE}journal-entries/", **self.headers).status_code, status.HTTP_200_OK)
        self.assertEqual(self.client.get(f"{BASE}budgets/", **self.headers).status_code, status.HTTP_403_FORBIDDEN)

    def test_budget_viewers_can_read_but_not_write(self, view_sap, service_sap):
        view_sap.return_value.list_budgets.return_value = []
        self.grant("can_view_sap_budgets")
        self.assertEqual(self.client.get(f"{BASE}budgets/", **self.headers).status_code, status.HTTP_200_OK)
        self.assertEqual(
            self.client.post(f"{BASE}budgets/", BUDGET_BODY, format="json", **self.headers).status_code,
            status.HTTP_403_FORBIDDEN,
        )
        self.assertEqual(self.client.delete(f"{BASE}budgets/12/", **self.headers).status_code, status.HTTP_403_FORBIDDEN)

    def test_managing_budgets_implies_viewing_them(self, view_sap, service_sap):
        view_sap.return_value.list_budgets.return_value = []
        service_sap.return_value.create_budget.return_value = SAP_BUDGET
        self.grant("can_manage_sap_budgets")
        self.assertEqual(self.client.get(f"{BASE}budgets/", **self.headers).status_code, status.HTTP_200_OK)
        self.assertEqual(
            self.client.post(f"{BASE}budgets/", BUDGET_BODY, format="json", **self.headers).status_code,
            status.HTTP_201_CREATED,
        )


class PermissionSurfaceTests(TestCase):
    def test_only_the_declared_rights_exist(self):
        codenames = set(
            Permission.objects.filter(content_type__app_label="sap_finance").values_list("codename", flat=True)
        )
        self.assertEqual(codenames, set(ALL_PERMISSIONS))


class GroupCommandTests(TestCase):
    """Every group right exists, and every right the API checks is handed out."""

    def setUp(self):
        call_command("setup_sap_finance_groups", stdout=StringIO())

    def test_every_group_is_created_with_its_rights(self):
        for name, codes in SAP_FINANCE_GROUPS.items():
            with self.subTest(group=name):
                held = {
                    f"sap_finance.{codename}"
                    for codename in Group.objects.get(name=name).permissions.values_list(
                        "codename", flat=True
                    )
                }
                self.assertEqual(held, set(codes))

    def test_every_right_the_api_checks_is_granted_by_some_group(self):
        granted = {code for codes in SAP_FINANCE_GROUPS.values() for code in codes}
        checked = {
            guards.VIEW_LEDGERS_PERMISSION,
            guards.VIEW_BUDGETS_PERMISSION,
            guards.MANAGE_BUDGETS_PERMISSION,
        }
        self.assertEqual(checked - granted, set())
