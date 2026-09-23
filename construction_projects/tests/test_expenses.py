"""Phase 2: how much of the budget is spent, and what it went on."""

from datetime import timedelta
from decimal import Decimal

from rest_framework import status

from construction_projects.models import Expense

from .base import ConstructionTestCase


def spend(day, amount, category="MATERIAL", description="90 bags cement", **extra):
    payload = {
        "spend_date": str(day),
        "category": category,
        "description": description,
        "amount": str(amount),
    }
    payload.update(extra)
    return payload


class ExpenseTests(ConstructionTestCase):
    def setUp(self):
        super().setUp()
        self.project = self.make_project()  # sanctioned 800000

    def test_recording_spend_moves_the_project_total(self):
        response = self.post(
            f"projects/{self.project.id}/expenses/",
            spend(self.today, "31500.00", paid_to="Verma Traders", payment_mode="CREDIT"),
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertIsNone(response.data["warning"])
        self.assertEqual(
            self.refreshed(self.project).spent_amount, Decimal("31500.00")
        )

    def test_spent_amount_always_matches_the_rows(self):
        for amount in ("1000.00", "2500.50", "17.25"):
            self.post(f"projects/{self.project.id}/expenses/", spend(self.today, amount))
        self.assertEqual(
            self.refreshed(self.project).spent_amount, Decimal("3517.75")
        )

    def test_overspending_is_recorded_and_flagged_not_refused(self):
        """The money is already gone; refusing to record it only makes the books
        wrong while the shed still gets built."""
        response = self.post(
            f"projects/{self.project.id}/expenses/", spend(self.today, "842000.00")
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        warning = response.data["warning"]
        self.assertEqual(warning["code"], "budget_exceeded")
        self.assertEqual(Decimal(warning["sanctioned"]), Decimal("800000.00"))
        self.assertEqual(Decimal(warning["spent"]), Decimal("842000.00"))
        self.assertEqual(Decimal(warning["over_by"]), Decimal("42000.00"))

        project = self.refreshed(self.project)
        self.assertTrue(project.is_over_budget)
        self.assertEqual(project.remaining_budget, Decimal("-42000.00"))

    def test_future_spend_is_refused(self):
        response = self.post(
            f"projects/{self.project.id}/expenses/",
            spend(self.today + timedelta(days=1), "100.00"),
        )
        self.assertCode(response, "spend_date_in_future")

    def test_spend_before_the_project_started_is_refused(self):
        response = self.post(
            f"projects/{self.project.id}/expenses/",
            spend(self.project.start_date - timedelta(days=1), "100.00"),
        )
        self.assertCode(response, "spend_date_before_start")

    def test_recording_needs_the_permission(self):
        viewer = self.make_user("v2@example.com", "CON920", ["can_view_project"])
        project = self.make_project(site_incharge=viewer)
        self.client.force_authenticate(viewer)
        response = self.post(
            f"projects/{project.id}/expenses/", spend(self.today, "100.00")
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_editing_an_expense_recomputes_the_total(self):
        created = self.post(
            f"projects/{self.project.id}/expenses/", spend(self.today, "1000.00")
        ).data["expense"]
        response = self.patch(
            f"expenses/{created['id']}/",
            {
                "spend_date": str(self.today),
                "category": "MATERIAL",
                "description": "90 bags cement (corrected)",
                "amount": "1750.00",
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            self.refreshed(self.project).spent_amount, Decimal("1750.00")
        )

    def test_deleting_an_expense_is_soft_and_recomputes(self):
        created = self.post(
            f"projects/{self.project.id}/expenses/", spend(self.today, "5000.00")
        ).data["expense"]
        response = self.delete(f"expenses/{created['id']}/")
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

        self.assertEqual(self.refreshed(self.project).spent_amount, Decimal("0.00"))
        # The row is still there, just inactive -- the audit trail survives.
        self.assertFalse(Expense.objects.get(pk=created["id"]).is_active)

    def test_filters(self):
        self.post(
            f"projects/{self.project.id}/expenses/",
            spend(self.today, "1000.00", category="MATERIAL"),
        )
        self.post(
            f"projects/{self.project.id}/expenses/",
            spend(self.today, "2000.00", category="LABOUR", description="wages"),
        )
        rows = self.get(
            f"projects/{self.project.id}/expenses/", category="LABOUR"
        ).data["results"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(Decimal(rows[0]["amount"]), Decimal("2000.00"))


class DayViewTests(ConstructionTestCase):
    def test_the_day_screen_shows_the_log_the_spend_and_the_running_total(self):
        project = self.make_project(start_date=self.today - timedelta(days=5))
        yesterday = self.today - timedelta(days=1)

        self.post(
            f"projects/{project.id}/daily-logs/",
            {
                "log_date": str(yesterday),
                "work_done": "Dug the footings.",
                "workers_count": 9,
            },
        )
        self.post(
            f"projects/{project.id}/daily-logs/",
            {
                "log_date": str(self.today),
                "work_done": "Cast the slab.",
                "workers_count": 14,
                "progress_percent": "55.00",
            },
        )
        for day, category, description, amount in (
            (yesterday, "LABOUR", "digging", "4000.00"),
            (self.today, "MATERIAL", "cement", "31500.00"),
            (self.today, "TRANSPORT", "sand", "3300.00"),
        ):
            self.post(
                f"projects/{project.id}/expenses/",
                {
                    "spend_date": str(day),
                    "category": category,
                    "description": description,
                    "amount": amount,
                },
            )

        data = self.get(f"projects/{project.id}/day/", date=str(self.today)).data
        # ``response.data`` is pre-render: a serializer's DateField has already
        # become a string, but a value the service put in a plain dict is still
        # a date object. Money is the exception -- see services._money.
        self.assertEqual(data["date"], self.today)
        self.assertEqual(data["log"]["workers_count"], 14)
        self.assertEqual(len(data["expenses"]), 2)
        self.assertEqual(Decimal(data["spent_today"]), Decimal("34800.00"))
        self.assertEqual(Decimal(data["spent_to_date"]), Decimal("38800.00"))
        self.assertEqual(
            Decimal(data["budget_remaining"]), Decimal("800000.00") - Decimal("38800.00")
        )

    def test_a_day_with_nothing_on_it_reads_empty_not_404(self):
        project = self.make_project()
        data = self.get(f"projects/{project.id}/day/", date=str(self.today)).data
        self.assertIsNone(data["log"])
        self.assertEqual(data["expenses"], [])
        self.assertEqual(Decimal(data["spent_today"]), Decimal("0.00"))

    def test_the_date_is_required(self):
        project = self.make_project()
        response = self.get(f"projects/{project.id}/day/")
        self.assertCode(response, "date_required")


class SpendSummaryTests(ConstructionTestCase):
    def test_summary_groups_by_category_and_counts_days_lost(self):
        project = self.make_project(start_date=self.today - timedelta(days=6))
        self.post(
            f"projects/{project.id}/expenses/",
            spend(self.today, "31500.00", category="MATERIAL"),
        )
        self.post(
            f"projects/{project.id}/expenses/",
            spend(self.today, "5000.00", category="MATERIAL", description="steel"),
        )
        self.post(
            f"projects/{project.id}/expenses/",
            spend(self.today, "7200.00", category="LABOUR", description="wages"),
        )

        for offset, reasons in (
            (1, ["RAIN"]),
            (2, ["RAIN", "NO_MATERIAL"]),  # a day can be both
            (3, ["NO_MATERIAL"]),
        ):
            self.post(
                f"projects/{project.id}/daily-logs/",
                {
                    "log_date": str(self.today - timedelta(days=offset)),
                    "work_done": "No work.",
                    "work_stopped": True,
                    "stopped_reasons": reasons,
                },
            )

        data = self.get(f"projects/{project.id}/spend-summary/").data
        self.assertEqual(Decimal(data["total_spent"]), Decimal("43700.00"))

        by_category = {row["category"]: row for row in data["by_category"]}
        self.assertEqual(Decimal(by_category["MATERIAL"]["amount"]), Decimal("36500.00"))
        self.assertEqual(by_category["MATERIAL"]["count"], 2)
        self.assertEqual(Decimal(by_category["LABOUR"]["amount"]), Decimal("7200.00"))

        # "We lost 2 days to rain and 2 waiting for material" -- the whole
        # justification for a timeline extension, as a query. The buckets sum to
        # 4 while only 3 days were lost, because the middle day was both; that
        # is the honest answer, not a double count to be corrected.
        days_lost = {row["reason"]: row["days"] for row in data["days_lost"]}
        self.assertEqual(days_lost, {"RAIN": 2, "NO_MATERIAL": 2})
        self.assertEqual(data["days_lost_total"], 3)
        self.assertEqual(data["days_logged"], 3)
