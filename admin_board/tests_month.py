"""The admin board stepped back to an ended month.

The board is read as of that month's last day, against that month's SAP plan —
not the plan the real date makes current — and its dispatch drill lists that
month's bills.
"""

from datetime import date, timedelta
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from company.models import Company, UserCompany, UserRole

from .services import AdminBoardService

User = get_user_model()


def _plan(abs_id, start, end, is_current=False):
    return {"abs_id": abs_id, "code": f"P{abs_id}", "name": f"Plan {abs_id}",
            "start_date": start, "end_date": end, "is_current": is_current}


def _ended_month_last_day() -> date:
    return timezone.localdate().replace(day=1) - timedelta(days=1)


class AdminBoardPlanForTheBoardsDateTests(TestCase):
    def _service(self, plans, today):
        reader = MagicMock()
        reader.list_plans.return_value = {"data": plans}
        return AdminBoardService(company_code="JIVO_OIL", plans=reader, today=today)

    def test_an_ended_month_reports_against_its_own_plan(self):
        """On 1 October the plan list calls October's plan current; September's board must not use it."""
        october = _plan(2, date(2026, 10, 1), date(2026, 10, 31), is_current=True)
        september = _plan(1, date(2026, 9, 1), date(2026, 9, 30))
        service = self._service([october, september], date(2026, 9, 30))

        self.assertEqual(service._resolve_plan()["abs_id"], 1)
        self.assertEqual(service._warnings, [])

    def test_iso_string_dates_are_read_too(self):
        service = self._service([_plan(1, "2026-09-01", "2026-09-30")], date(2026, 9, 30))

        self.assertEqual(service._resolve_plan()["abs_id"], 1)

    def test_no_plan_for_the_date_falls_back_with_a_warning_naming_it(self):
        service = self._service([_plan(2, date(2026, 10, 1), date(2026, 10, 31))], date(2026, 8, 31))

        self.assertEqual(service._resolve_plan()["abs_id"], 2)
        self.assertIn("31 Aug 2026", service._warnings[0])


class AdminBoardMonthParamTests(TestCase):
    def setUp(self):
        self.oil = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        self.user = User.objects.create_superuser(
            email="admin-month@example.com", password="x", full_name="Admin Month"
        )
        role, _ = UserRole.objects.get_or_create(name="Admin")
        UserCompany.objects.create(
            user=self.user, company=self.oil, role=role, is_default=True, is_active=True
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.client.credentials(HTTP_COMPANY_CODE="JIVO_OIL")

    @patch("admin_board.views.AdminBoardService")
    def test_an_ended_month_is_read_as_of_its_last_day(self, service):
        service.return_value.build.return_value = {}
        last = _ended_month_last_day()

        response = self.client.get(reverse("admin_board:admin-board"), {"month": last.strftime("%Y-%m")})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(service.call_args.kwargs["today"], last)

    @patch("admin_board.views.AdminBoardService")
    def test_no_month_reads_now(self, service):
        service.return_value.build.return_value = {}

        self.client.get(reverse("admin_board:admin-board"))

        self.assertIsNone(service.call_args.kwargs["today"])

    def test_a_month_that_has_not_started_is_refused(self):
        ahead = (timezone.localdate().replace(day=1) + timedelta(days=40)).strftime("%Y-%m")

        response = self.client.get(reverse("admin_board:admin-board"), {"month": ahead})

        self.assertEqual(response.status_code, 400)

    @patch("admin_board.views.company_bills")
    def test_the_dispatch_drill_lists_the_boards_month(self, bills):
        bills.return_value = {"bills": []}
        last = _ended_month_last_day()

        response = self.client.get(
            reverse("admin_board:admin-board-dispatch-bills"),
            {"company": "JIVO_OIL", "month": last.strftime("%Y-%m")},
        )

        self.assertEqual(response.status_code, 200)
        _, _, start, end = bills.call_args.args
        self.assertEqual((start, end), (last.replace(day=1), last))
