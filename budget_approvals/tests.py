"""
budget_approvals/tests.py

Unit tests for the Budget Approvals Dashboard app.

Tests cover:
  1. HanaBudgetApprovalReader — row mapping
  2. BudgetApprovalService    — Factory filter, filters, pagination, summary
  3. API views                — response shape, auth, error handling
"""

from datetime import date
from unittest.mock import MagicMock, patch

from django.core.cache import cache
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient, APITestCase
from rest_framework_simplejwt.tokens import RefreshToken


READER_COLUMNS = [
    "Branch", "DocEntry", "ObjType", "LineNum", "AcctCode", "AcctName",
    "CardCode", "CardName", "EFFECTMONTH", "BUDGET", "SUB_BUDGET", "STATE",
    "DocDate", "AMOUNT", "CURRENTMONTH", "Status", "U_NAME", "ApproverName",
    "CreatedDate", "CreateTime", "LineRemarks", "Comments", "ProcesStat",
    "UpdateDate", "OcrCode",
]


def _make_reader_row(
    *,
    branch="OIL",
    doc_entry=1001,
    obj_type="18",
    line_num=0,
    acct_code="512001",
    acct_name="Diesel Expense",
    card_code="VEND001",
    card_name="Bharat Fuels",
    effect_month="09-2026",
    budget="Factory",
    sub_budget="DIESEL",
    state="PB",
    doc_date=date(2026, 9, 1),
    amount=15000.0,
    current_month="09-2026",
    wdd_status="W",
    owner="naresh",
    approver_name="rajeev",
    created_date=date(2026, 9, 1),
    create_time=933,
    line_remarks="September diesel",
    comments="Monthly fuel",
    process_status="W",
    update_date=date(2026, 9, 2),
    ocr_code="FCT",
):
    """Returns a tuple in the same column order as the reader's branch query."""
    return (
        branch, doc_entry, obj_type, line_num, acct_code, acct_name,
        card_code, card_name, effect_month, budget, sub_budget, state,
        doc_date, amount, current_month, wdd_status, owner, approver_name,
        created_date, create_time, line_remarks, comments, process_status,
        update_date, ocr_code,
    )


POSTED_BY_MONTH = {"09-2026": 42000.0}


def _mapped_row(**overrides):
    """A row as the reader returns it, with test-friendly defaults."""
    row = {
        "branch": "OIL",
        "doc_entry": 1001,
        "obj_type": "18",
        "obj_type_label": "A/P Invoice",
        "line_num": 0,
        "acct_code": "512001",
        "acct_name": "Diesel Expense",
        "card_code": "VEND001",
        "card_name": "Bharat Fuels",
        "effect_month": "09-2026",
        "budget": "Factory",
        "sub_budget": "DIESEL",
        "state": "PB",
        "doc_date": "2026-09-01",
        "amount": 15000.0,
        "current_month": "09-2026",
        "current_month_posted_amount": 42000.0,
        "status": "W",
        "owner": "naresh",
        "approver": "rajeev",
        "created_date": "2026-09-01",
        "created_time": "09:33",
        "line_remarks": "September diesel",
        "comments": "Monthly fuel",
        "process_status": "W",
        "update_date": "2026-09-02",
        "ocr_code": "FCT",
    }
    row.update(overrides)
    return row


# ---------------------------------------------------------------------------
# 1. HanaBudgetApprovalReader Tests
# ---------------------------------------------------------------------------


class TestHanaBudgetApprovalReaderRowMapping(TestCase):
    """Tests for _map_row."""

    def setUp(self):
        from budget_approvals.hana_reader import HanaBudgetApprovalReader

        context = MagicMock()
        context.hana = {
            "host": "localhost",
            "port": 30015,
            "user": "u",
            "password": "p",
            "schema": "TEST",
        }
        self.reader = HanaBudgetApprovalReader(
            context, {"OIL": "TEST", "BEVERAGE": "TEST_BEV"}
        )
        self.index = {name: pos for pos, name in enumerate(READER_COLUMNS)}

    def test_map_row_basic_fields(self):
        result = self.reader._map_row(_make_reader_row(), self.index, POSTED_BY_MONTH)

        self.assertEqual(result["branch"], "OIL")
        self.assertEqual(result["doc_entry"], 1001)
        self.assertEqual(result["budget"], "Factory")
        self.assertEqual(result["sub_budget"], "DIESEL")
        self.assertEqual(result["card_name"], "Bharat Fuels")
        self.assertEqual(result["owner"], "naresh")

    def test_map_row_obj_type_label(self):
        result = self.reader._map_row(
            _make_reader_row(obj_type="67"), self.index, POSTED_BY_MONTH
        )
        self.assertEqual(result["obj_type_label"], "Inventory Transfer")

        result = self.reader._map_row(
            _make_reader_row(obj_type="999"), self.index, POSTED_BY_MONTH
        )
        self.assertEqual(result["obj_type_label"], "999")

    def test_map_row_posted_amount_lookup(self):
        result = self.reader._map_row(_make_reader_row(), self.index, POSTED_BY_MONTH)
        self.assertEqual(result["amount"], 15000.0)
        self.assertEqual(result["current_month_posted_amount"], 42000.0)

        # A month with no posted expense falls back to 0.
        result = self.reader._map_row(
            _make_reader_row(current_month="01-2020"), self.index, POSTED_BY_MONTH
        )
        self.assertEqual(result["current_month_posted_amount"], 0.0)

    def test_map_row_dates_and_time(self):
        result = self.reader._map_row(_make_reader_row(), self.index, POSTED_BY_MONTH)
        self.assertEqual(result["doc_date"], "2026-09-01")
        self.assertEqual(result["created_time"], "09:33")

    def test_map_row_approver(self):
        result = self.reader._map_row(_make_reader_row(), self.index, POSTED_BY_MONTH)
        self.assertEqual(result["approver"], "rajeev")

    def test_merge_approver_rows_combines_same_line(self):
        from budget_approvals.hana_reader import HanaBudgetApprovalReader

        base = self.reader._map_row(_make_reader_row(), self.index, POSTED_BY_MONTH)
        second = dict(base, approver="suresh")
        merged = HanaBudgetApprovalReader._merge_approver_rows([base, second])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["approver"], "rajeev, suresh")

    def test_merge_approver_rows_keeps_distinct_statuses(self):
        from budget_approvals.hana_reader import HanaBudgetApprovalReader

        approved = self.reader._map_row(
            _make_reader_row(wdd_status="Y"), self.index, POSTED_BY_MONTH
        )
        pending = self.reader._map_row(
            _make_reader_row(wdd_status="W", approver_name="suresh"),
            self.index,
            POSTED_BY_MONTH,
        )
        merged = HanaBudgetApprovalReader._merge_approver_rows([approved, pending])
        self.assertEqual(len(merged), 2)

    def test_map_row_null_values(self):
        row = _make_reader_row(
            doc_date=None,
            create_time=None,
            card_name=None,
            line_remarks=None,
        )
        result = self.reader._map_row(row, self.index, POSTED_BY_MONTH)
        self.assertIsNone(result["doc_date"])
        self.assertEqual(result["created_time"], "")
        self.assertEqual(result["card_name"], "")
        self.assertEqual(result["line_remarks"], "")


# ---------------------------------------------------------------------------
# 2. BudgetApprovalService Tests
# ---------------------------------------------------------------------------


class TestBudgetApprovalService(TestCase):
    """Tests filtering, pagination and summary with a mocked reader."""

    def setUp(self):
        cache.clear()

    def _service(self, rows):
        with patch("budget_approvals.services.CompanyContext"), \
                patch("budget_approvals.services.HanaBudgetApprovalReader") as MockReader:
            MockReader.return_value.get_draft_approval_rows.return_value = rows
            MockReader.return_value.schema = "TEST"
            from budget_approvals.services import BudgetApprovalService
            return BudgetApprovalService()

    def test_keeps_only_factory_budget_rows(self):
        rows = [
            _mapped_row(doc_entry=1, budget="Factory"),
            _mapped_row(doc_entry=2, budget="FACTORY"),
            _mapped_row(doc_entry=3, budget="Sales"),
            _mapped_row(doc_entry=4, budget=""),
        ]
        result = self._service(rows).get_report()
        entries = {r["doc_entry"] for r in result["data"]}
        self.assertEqual(entries, {1, 2})
        self.assertEqual(result["meta"]["budget"], "FACTORY")

    def test_status_filter(self):
        rows = [
            _mapped_row(doc_entry=1, status="W"),
            _mapped_row(doc_entry=2, status="Y"),
            _mapped_row(doc_entry=3, status="N"),
        ]
        result = self._service(rows).get_report(status="pending")
        self.assertEqual([r["doc_entry"] for r in result["data"]], [1])

    def test_branch_and_month_filters(self):
        rows = [
            _mapped_row(doc_entry=1, branch="OIL", effect_month="09-2026"),
            _mapped_row(doc_entry=2, branch="BEVERAGE", effect_month="09-2026"),
            _mapped_row(doc_entry=3, branch="OIL", effect_month="08-2026"),
        ]
        service = self._service(rows)
        result = service.get_report(branch="BEVERAGE")
        self.assertEqual([r["doc_entry"] for r in result["data"]], [2])

        result = service.get_report(effect_month="08-2026")
        self.assertEqual([r["doc_entry"] for r in result["data"]], [3])

    def test_search_filter(self):
        rows = [
            _mapped_row(doc_entry=101, card_code="VENDA", card_name="Bharat Fuels"),
            _mapped_row(doc_entry=202, card_code="VENDB", card_name="Verka Dairy"),
        ]
        service = self._service(rows)
        result = service.get_report(search="verka")
        self.assertEqual([r["doc_entry"] for r in result["data"]], [202])

        # Doc entry matches exactly, not as a substring.
        result = service.get_report(search="101")
        self.assertEqual([r["doc_entry"] for r in result["data"]], [101])

    def test_pagination(self):
        rows = [_mapped_row(doc_entry=n) for n in range(1, 8)]
        service = self._service(rows)

        page1 = service.get_report(page=1, page_size=3)
        self.assertEqual(len(page1["data"]), 3)
        self.assertEqual(page1["meta"]["total_rows"], 7)
        self.assertEqual(page1["meta"]["total_pages"], 3)

        page3 = service.get_report(page=3, page_size=3)
        self.assertEqual(len(page3["data"]), 1)

        # Out-of-range pages clamp instead of erroring.
        clamped = service.get_report(page=99, page_size=3)
        self.assertEqual(clamped["meta"]["page"], 3)

    def test_summary_counts_and_amounts(self):
        rows = [
            _mapped_row(doc_entry=1, status="W", amount=100.0),
            _mapped_row(doc_entry=1, line_num=1, status="W", amount=50.0),
            _mapped_row(doc_entry=2, status="Y", amount=200.0),
        ]
        result = self._service(rows).get_report()
        summary = result["summary"]
        self.assertEqual(summary["total_lines"], 3)
        self.assertEqual(summary["total_documents"], 2)
        self.assertEqual(summary["total_amount"], 350.0)
        self.assertEqual(summary["pending_lines"], 2)
        self.assertEqual(summary["pending_amount"], 150.0)

    def test_summary_reflects_filters(self):
        rows = [
            _mapped_row(doc_entry=1, status="W", amount=100.0),
            _mapped_row(doc_entry=2, status="Y", amount=200.0),
        ]
        result = self._service(rows).get_report(status="approved")
        self.assertEqual(result["summary"]["total_lines"], 1)
        self.assertEqual(result["summary"]["total_amount"], 200.0)

    def test_column_filters(self):
        rows = [
            _mapped_row(doc_entry=1, sub_budget="DIESEL", owner="naresh"),
            _mapped_row(doc_entry=2, sub_budget="POWER", owner="naresh"),
            _mapped_row(doc_entry=3, sub_budget="DIESEL", owner="jashan"),
        ]
        service = self._service(rows)
        result = service.get_report(
            column_filters={"sub_budget": ["DIESEL"], "owner": ["naresh"]}
        )
        self.assertEqual([r["doc_entry"] for r in result["data"]], [1])

        # Unknown fields and empty selections are ignored.
        result = service.get_report(column_filters={"amount": ["1"], "owner": []})
        self.assertEqual(result["meta"]["total_rows"], 3)

    def test_sorting(self):
        rows = [
            _mapped_row(doc_entry=1, amount=50.0, card_name="Zeta"),
            _mapped_row(doc_entry=2, amount=200.0, card_name="alpha"),
            _mapped_row(doc_entry=3, amount=100.0, card_name="Mid"),
        ]
        service = self._service(rows)

        result = service.get_report(sort_by="amount", sort_dir="asc")
        self.assertEqual([r["doc_entry"] for r in result["data"]], [1, 3, 2])

        result = service.get_report(sort_by="card_name", sort_dir="desc")
        self.assertEqual([r["doc_entry"] for r in result["data"]], [1, 3, 2])

        # Unknown sort field keeps the default newest-first order untouched.
        result = service.get_report(sort_by="not_a_field")
        self.assertEqual(result["meta"]["total_rows"], 3)

    def test_column_values_exclude_own_filter(self):
        rows = [
            _mapped_row(doc_entry=1, sub_budget="DIESEL", owner="naresh"),
            _mapped_row(doc_entry=2, sub_budget="POWER", owner="naresh"),
            _mapped_row(doc_entry=3, sub_budget="DIESEL", owner="jashan"),
        ]
        service = self._service(rows)
        result = service.get_column_values(
            field="sub_budget",
            column_filters={"sub_budget": ["DIESEL"], "owner": ["naresh"]},
        )
        # Own column's filter is ignored; the owner filter still applies.
        self.assertEqual(
            result["values"],
            [{"value": "DIESEL", "count": 1}, {"value": "POWER", "count": 1}],
        )

    def test_options_come_from_full_dataset(self):
        rows = [
            _mapped_row(doc_entry=1, branch="OIL", effect_month="08-2026"),
            _mapped_row(doc_entry=2, branch="BEVERAGE", effect_month="09-2026"),
        ]
        result = self._service(rows).get_report(branch="OIL")
        self.assertEqual(result["options"]["branches"], ["BEVERAGE", "OIL"])
        self.assertEqual(result["options"]["effect_months"], ["09-2026", "08-2026"])

    def test_cache_avoids_second_procedure_call(self):
        rows = [_mapped_row(doc_entry=1)]
        with patch("budget_approvals.services.CompanyContext"), \
                patch("budget_approvals.services.HanaBudgetApprovalReader") as MockReader:
            MockReader.return_value.get_draft_approval_rows.return_value = rows
            MockReader.return_value.schema = "TEST"
            from budget_approvals.services import BudgetApprovalService

            first = BudgetApprovalService().get_report()
            second = BudgetApprovalService().get_report()
            self.assertFalse(first["meta"]["from_cache"])
            self.assertTrue(second["meta"]["from_cache"])
            self.assertEqual(
                MockReader.return_value.get_draft_approval_rows.call_count, 1
            )

            refreshed = BudgetApprovalService().get_report(refresh=True)
            self.assertFalse(refreshed["meta"]["from_cache"])
            self.assertEqual(
                MockReader.return_value.get_draft_approval_rows.call_count, 2
            )


# ---------------------------------------------------------------------------
# 3. API View Tests
# ---------------------------------------------------------------------------


class TestBudgetApprovalAPIViews(APITestCase):
    """
    Tests API views by mocking BudgetApprovalService to avoid real SAP calls.
    """

    def setUp(self):
        from django.contrib.auth import get_user_model
        from company.models import Company, UserCompany, UserRole

        User = get_user_model()
        self.user = User.objects.create_user(
            email="approver@test.com",
            password="testpass123",
            full_name="Test Approver",
            employee_code="EMP009",
        )
        from django.contrib.auth.models import Permission
        from django.contrib.contenttypes.models import ContentType
        from budget_approvals.models import BudgetApprovalPermission

        ct = ContentType.objects.get_for_model(BudgetApprovalPermission)
        perm, _ = Permission.objects.get_or_create(
            codename="can_view_budget_approvals",
            content_type=ct,
            defaults={"name": "Can view Budget Approvals Dashboard"},
        )
        self.user.user_permissions.add(perm)
        self.user.save()

        self.company = Company.objects.create(
            name="Jivo Oil", code="JIVO_OIL", is_active=True
        )
        role = UserRole.objects.create(name="Approver")
        UserCompany.objects.create(
            user=self.user,
            company=self.company,
            role=role,
            is_default=True,
            is_active=True,
        )

        refresh = RefreshToken.for_user(self.user)
        self.client = APIClient()
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}",
            HTTP_COMPANY_CODE="JIVO_OIL",
        )

    def _mock_report_response(self):
        return {
            "data": [_mapped_row()],
            "summary": {
                "total_lines": 1,
                "total_documents": 1,
                "total_amount": 15000.0,
                "pending_lines": 1,
                "pending_amount": 15000.0,
                "by_status": [
                    {
                        "status": "W",
                        "status_label": "Pending",
                        "line_count": 1,
                        "total_amount": 15000.0,
                    }
                ],
            },
            "options": {"branches": ["OIL"], "effect_months": ["09-2026"]},
            "meta": {
                "budget": "FACTORY",
                "page": 1,
                "page_size": 50,
                "total_rows": 1,
                "total_pages": 1,
                "fetched_at": "2026-09-03T10:30:00+00:00",
                "from_cache": False,
            },
        }

    URL = "/api/v1/dashboards/budget-approvals/report/"

    @patch("budget_approvals.views.BudgetApprovalService")
    def test_report_returns_200(self, MockService):
        MockService.return_value.get_report.return_value = self._mock_report_response()
        response = self.client.get(self.URL)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("data", response.data)
        self.assertIn("summary", response.data)
        self.assertIn("options", response.data)
        self.assertIn("meta", response.data)
        self.assertEqual(response.data["data"][0]["budget"], "Factory")

    @patch("budget_approvals.views.BudgetApprovalService")
    def test_report_passes_filters_to_service(self, MockService):
        MockService.return_value.get_report.return_value = self._mock_report_response()
        response = self.client.get(
            self.URL,
            {"status": "pending", "branch": "OIL", "page": 2, "page_size": 25},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        MockService.return_value.get_report.assert_called_once_with(
            status="pending",
            branch="OIL",
            effect_month="",
            search="",
            column_filters={},
            sort_by="",
            sort_dir="desc",
            page=2,
            page_size=25,
            refresh=False,
        )

    @patch("budget_approvals.views.BudgetApprovalService")
    def test_report_parses_column_filters_json(self, MockService):
        MockService.return_value.get_report.return_value = self._mock_report_response()
        response = self.client.get(
            self.URL,
            {"column_filters": '{"owner": ["naresh"], "sub_budget": ["DIESEL"]}'},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        kwargs = MockService.return_value.get_report.call_args.kwargs
        self.assertEqual(
            kwargs["column_filters"],
            {"owner": ["naresh"], "sub_budget": ["DIESEL"]},
        )

    def test_report_rejects_bad_column_filters(self):
        response = self.client.get(self.URL, {"column_filters": "not json"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        response = self.client.get(self.URL, {"column_filters": '{"amount": ["1"]}'})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @patch("budget_approvals.views.BudgetApprovalService")
    def test_column_values_endpoint(self, MockService):
        MockService.return_value.get_column_values.return_value = {
            "field": "owner",
            "values": [{"value": "naresh", "count": 3}],
            "meta": {
                "total_values": 1,
                "truncated": False,
                "fetched_at": "2026-09-03T10:30:00+00:00",
            },
        }
        response = self.client.get(
            "/api/v1/dashboards/budget-approvals/column-values/",
            {"field": "owner"},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["values"][0]["value"], "naresh")

    def test_column_values_requires_valid_field(self):
        response = self.client.get(
            "/api/v1/dashboards/budget-approvals/column-values/",
            {"field": "amount"},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_report_invalid_status_returns_400(self):
        response = self.client.get(self.URL, {"status": "bogus"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_report_invalid_page_returns_400(self):
        response = self.client.get(self.URL, {"page": 0})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_report_requires_authentication(self):
        client = APIClient()
        response = client.get(self.URL)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_report_requires_permission(self):
        from django.contrib.auth import get_user_model
        from company.models import UserCompany, UserRole

        User = get_user_model()
        user = User.objects.create_user(
            email="nobody@test.com",
            password="testpass123",
            full_name="No Permission",
            employee_code="EMP010",
        )
        role = UserRole.objects.create(name="Viewer")
        UserCompany.objects.create(
            user=user,
            company=self.company,
            role=role,
            is_default=True,
            is_active=True,
        )
        refresh = RefreshToken.for_user(user)
        client = APIClient()
        client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}",
            HTTP_COMPANY_CODE="JIVO_OIL",
        )
        response = client.get(self.URL)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @patch("budget_approvals.views.BudgetApprovalService")
    def test_report_sap_connection_error_returns_503(self, MockService):
        from sap_client.exceptions import SAPConnectionError

        MockService.return_value.get_report.side_effect = SAPConnectionError("down")
        response = self.client.get(self.URL)
        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)

    @patch("budget_approvals.views.BudgetApprovalService")
    def test_report_sap_data_error_returns_502(self, MockService):
        from sap_client.exceptions import SAPDataError

        MockService.return_value.get_report.side_effect = SAPDataError("bad data")
        response = self.client.get(self.URL)
        self.assertEqual(response.status_code, status.HTTP_502_BAD_GATEWAY)
