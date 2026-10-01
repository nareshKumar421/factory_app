"""The plant board stepped back to an ended month: its month figures run to
that month's last day, against the SAP plan covering that day, while its live
tiles go on reading today."""

from datetime import date, timedelta
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from company.models import Company, UserCompany, UserRole

from .services import PlantBoardService

User = get_user_model()


def _plan(abs_id, start, end, is_current=False):
    return {"abs_id": abs_id, "code": f"P{abs_id}", "name": f"Plan {abs_id}",
            "start_date": start, "end_date": end, "is_current": is_current}


class PlantBoardPlanForTheBoardsDateTests(TestCase):
    def _service(self, plans, as_of):
        reader = MagicMock()
        reader.list_plans.return_value = {"data": plans}
        return PlantBoardService(
            company_code="JIVO_OIL",
            reader=MagicMock(),
            stock=MagicMock(),
            plans=reader,
            packing=MagicMock(),
            plan_reader=MagicMock(),
            today=date(2026, 10, 1),
            as_of=as_of,
        )

    def test_an_ended_month_reports_against_its_own_plan(self):
        october = _plan(2, "2026-10-01", "2026-10-31", is_current=True)
        september = _plan(1, "2026-09-01", "2026-09-30")
        plan = self._service([october, september], date(2026, 9, 30))._resolve_plan()

        self.assertEqual(plan["abs_id"], 1)
        # "This month's plan" for the month on the board.
        self.assertTrue(plan["is_current"])
        self.assertEqual(plan["days_elapsed"], 30)

    def test_the_live_tiles_keep_reading_today(self):
        service = self._service([], date(2026, 9, 30))

        self.assertEqual(service.today, date(2026, 10, 1))
        self.assertEqual(service._plan_window(None), (date(2026, 9, 1), date(2026, 9, 30)))

    def test_a_plan_not_covering_the_date_is_not_called_current(self):
        service = self._service([_plan(2, "2026-10-01", "2026-10-31", is_current=True)], date(2026, 9, 30))
        plan = service._resolve_plan()

        self.assertFalse(plan["is_current"])
        self.assertIn("30 Sep 2026", service._warnings[0])


class PlantBoardMonthParamTests(TestCase):
    def setUp(self):
        self.oil = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        self.user = User.objects.create_superuser(
            email="plant-month@example.com", password="x", full_name="Plant Month"
        )
        role, _ = UserRole.objects.get_or_create(name="Admin")
        UserCompany.objects.create(
            user=self.user, company=self.oil, role=role, is_default=True, is_active=True
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.client.credentials(HTTP_COMPANY_CODE="JIVO_OIL")

    @patch("plant_board.views.PlantBoardService")
    def test_an_ended_month_runs_the_month_to_its_last_day(self, service):
        service.return_value.build.return_value = {}
        last = timezone.localdate().replace(day=1) - timedelta(days=1)

        response = self.client.get(reverse("plant_board:plant-board"), {"month": last.strftime("%Y-%m")})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(service.call_args.kwargs["as_of"], last)
        # The live tiles' day is left to the service: today.
        self.assertNotIn("today", service.call_args.kwargs)

    def test_a_malformed_month_is_refused(self):
        response = self.client.get(reverse("plant_board:plant-board"), {"month": "September"})

        self.assertEqual(response.status_code, 400)
