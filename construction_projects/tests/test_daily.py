"""Phase 2: what happened today, and what it cost -- in one submission."""

from datetime import timedelta
from decimal import Decimal

from rest_framework import status

from construction_projects.constants import ProjectStatus
from construction_projects.models import DailyLog, Expense

from .base import ConstructionTestCase


def log_payload(day, **overrides):
    payload = {
        "log_date": str(day),
        "work_done": "Cast the slab, grid A1-A6. Curing started.",
        "workers_count": 14,
    }
    payload.update(overrides)
    return payload


class DailyLogTests(ConstructionTestCase):
    def setUp(self):
        super().setUp()
        self.project = self.make_project()

    def test_the_day_records_work_only(self):
        """Spend was moved out of this form: payments are approved in batches of
        their own, and burying them in the diary made a page half the site could
        not submit."""
        response = self.post(
            f"projects/{self.project.id}/daily-logs/",
            log_payload(self.today, progress_percent="55.00"),
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertNotIn("expenses", response.data)
        self.assertIsNone(response.data["warning"])

        project = self.refreshed(self.project)
        self.assertEqual(project.progress_percent, Decimal("55.00"))
        self.assertEqual(project.spent_amount, Decimal("0.00"))
        self.assertEqual(Expense.objects.count(), 0)

    def test_spend_sent_to_the_day_form_is_ignored(self):
        """An old client posting the removed field must not silently succeed at
        recording nothing, nor fail — the log is what this endpoint is for."""
        response = self.post(
            f"projects/{self.project.id}/daily-logs/",
            log_payload(
                self.today,
                expenses=[
                    {"category": "MATERIAL", "description": "cement", "amount": "100.00"}
                ],
            ),
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(Expense.objects.count(), 0)

    def test_the_first_log_starts_the_project(self):
        self.assertEqual(self.project.status, ProjectStatus.APPROVED)
        self.post(f"projects/{self.project.id}/daily-logs/", log_payload(self.today))
        self.assertEqual(
            self.refreshed(self.project).status, ProjectStatus.IN_PROGRESS
        )

    def test_re_posting_a_date_updates_that_day(self):
        """The site in-charge who remembers something at 9pm should not have to
        hunt for yesterday's row."""
        self.post(f"projects/{self.project.id}/daily-logs/", log_payload(self.today))
        response = self.post(
            f"projects/{self.project.id}/daily-logs/",
            log_payload(self.today, work_done="Also stripped the shuttering.", workers_count=16),
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(DailyLog.objects.count(), 1)
        log = DailyLog.objects.get()
        self.assertEqual(log.workers_count, 16)
        self.assertIn("shuttering", log.work_done)

    def test_a_future_day_cannot_be_logged(self):
        response = self.post(
            f"projects/{self.project.id}/daily-logs/",
            log_payload(self.today + timedelta(days=1)),
        )
        self.assertCode(response, "log_date_in_future")

    def test_a_day_before_the_project_started_cannot_be_logged(self):
        response = self.post(
            f"projects/{self.project.id}/daily-logs/",
            log_payload(self.project.start_date - timedelta(days=1)),
        )
        self.assertCode(response, "log_date_before_start")

    def test_work_stopped_needs_at_least_one_reason(self):
        response = self.post(
            f"projects/{self.project.id}/daily-logs/",
            log_payload(self.today, work_stopped=True),
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("stopped_reasons", response.data)

    def test_a_day_can_be_stopped_for_several_reasons(self):
        """It rained AND the material had not arrived. Both are true."""
        response = self.post(
            f"projects/{self.project.id}/daily-logs/",
            log_payload(
                self.today,
                work_done="Nothing doing.",
                work_stopped=True,
                stopped_reasons=["RAIN", "NO_MATERIAL"],
            ),
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(
            sorted(response.data["log"]["stopped_reasons"]), ["NO_MATERIAL", "RAIN"]
        )
        self.assertEqual(
            sorted(response.data["log"]["stopped_reasons_display"]),
            ["Material not available", "Rain"],
        )

    def test_re_posting_replaces_the_reasons_rather_than_adding(self):
        payload = log_payload(
            self.today, work_stopped=True, stopped_reasons=["RAIN", "NO_MATERIAL"]
        )
        self.post(f"projects/{self.project.id}/daily-logs/", payload)
        response = self.post(
            f"projects/{self.project.id}/daily-logs/",
            log_payload(self.today, work_stopped=True, stopped_reasons=["NO_LABOUR"]),
        )
        self.assertEqual(response.data["log"]["stopped_reasons"], ["NO_LABOUR"])

    def test_the_same_reason_twice_is_refused(self):
        response = self.post(
            f"projects/{self.project.id}/daily-logs/",
            log_payload(self.today, work_stopped=True, stopped_reasons=["RAIN", "RAIN"]),
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("stopped_reasons", response.data)

    def test_clearing_work_stopped_clears_the_reasons(self):
        self.post(
            f"projects/{self.project.id}/daily-logs/",
            log_payload(self.today, work_stopped=True, stopped_reasons=["RAIN"]),
        )
        response = self.post(
            f"projects/{self.project.id}/daily-logs/",
            log_payload(self.today, work_stopped=False, stopped_reasons=["RAIN"]),
        )
        self.assertEqual(response.data["log"]["stopped_reasons"], [])

    def test_logging_against_an_unapproved_project_is_refused(self):
        draft = self.make_project(approved=False)
        response = self.post(
            f"projects/{draft.id}/daily-logs/", log_payload(self.today)
        )
        self.assertCode(response, "project_not_approved")

    def test_logging_needs_the_permission(self):
        viewer = self.make_user("v@example.com", "CON910", ["can_view_project"])
        project = self.make_project(site_incharge=viewer)
        self.client.force_authenticate(viewer)
        response = self.post(
            f"projects/{project.id}/daily-logs/", log_payload(self.today)
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class BackdatingTests(ConstructionTestCase):
    def setUp(self):
        super().setUp()
        self.project = self.make_project(
            start_date=self.today - timedelta(days=60),
            expected_end_date=self.today + timedelta(days=60),
        )

    def test_within_the_window_anybody_can_backdate(self):
        logger = self.make_user(
            "l2@example.com", "CON912", ["can_view_project", "can_log_daily_work"]
        )
        project = self.make_project(
            site_incharge=logger, start_date=self.today - timedelta(days=60)
        )
        self.client.force_authenticate(logger)
        response = self.post(
            f"projects/{project.id}/daily-logs/",
            log_payload(self.today - timedelta(days=3)),
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_beyond_the_window_it_needs_edit_rights(self):
        logger = self.make_user(
            "l3@example.com", "CON913", ["can_view_project", "can_log_daily_work"]
        )
        project = self.make_project(
            site_incharge=logger, start_date=self.today - timedelta(days=60)
        )
        self.client.force_authenticate(logger)
        stale = log_payload(self.today - timedelta(days=30))
        response = self.post(f"projects/{project.id}/daily-logs/", stale)
        self.assertCode(response, "log_too_old")

        # The same request from somebody who may edit the project is allowed.
        self.client.force_authenticate(self.user)
        response = self.post(f"projects/{project.id}/daily-logs/", stale)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)


class ProgressTests(ConstructionTestCase):
    def setUp(self):
        super().setUp()
        self.logger = self.make_user(
            "l4@example.com", "CON914", ["can_view_project", "can_log_daily_work"]
        )
        self.project = self.make_project(
            site_incharge=self.logger, start_date=self.today - timedelta(days=10)
        )

    def test_progress_cannot_go_backwards(self):
        """A typo that drops a project from 60% to 6% otherwise sits there until
        somebody notices the chart."""
        self.client.force_authenticate(self.logger)
        self.post(
            f"projects/{self.project.id}/daily-logs/",
            log_payload(self.today - timedelta(days=2), progress_percent="60.00"),
        )
        response = self.post(
            f"projects/{self.project.id}/daily-logs/",
            log_payload(self.today, progress_percent="6.00"),
        )
        self.assertCode(response, "progress_went_backwards")
        self.assertEqual(response.data["context"]["previous_highest"], "60.00")

    def test_progress_forward_is_fine(self):
        self.client.force_authenticate(self.logger)
        self.post(
            f"projects/{self.project.id}/daily-logs/",
            log_payload(self.today - timedelta(days=2), progress_percent="60.00"),
        )
        response = self.post(
            f"projects/{self.project.id}/daily-logs/",
            log_payload(self.today, progress_percent="72.50"),
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(
            self.refreshed(self.project).progress_percent, Decimal("72.50")
        )

    def test_somebody_who_may_edit_can_correct_it(self):
        self.client.force_authenticate(self.logger)
        self.post(
            f"projects/{self.project.id}/daily-logs/",
            log_payload(self.today - timedelta(days=2), progress_percent="60.00"),
        )
        self.client.force_authenticate(self.user)  # holds can_edit_project
        response = self.post(
            f"projects/{self.project.id}/daily-logs/",
            log_payload(self.today, progress_percent="45.00"),
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)


class DailyLogListTests(ConstructionTestCase):
    def test_the_list_carries_each_days_spend(self):
        """The day's cost still shows on its row -- it is just recorded on the
        Expenses screen rather than in the diary form."""
        project = self.make_project(start_date=self.today - timedelta(days=5))
        yesterday = self.today - timedelta(days=1)
        self.post(f"projects/{project.id}/daily-logs/", log_payload(yesterday))
        self.post(f"projects/{project.id}/daily-logs/", log_payload(self.today))
        for day, amount in (
            (yesterday, "5000.00"),
            (self.today, "1200.00"),
            (self.today, "800.00"),
        ):
            self.post(
                f"projects/{project.id}/expenses/",
                {
                    "spend_date": str(day),
                    "category": "LABOUR",
                    "description": "wages",
                    "amount": amount,
                },
            )
        rows = self.get(f"projects/{project.id}/daily-logs/").data
        self.assertEqual(len(rows), 2)
        by_date = {row["log_date"]: row for row in rows}
        self.assertEqual(
            Decimal(by_date[str(self.today)]["spent_on_day"]), Decimal("2000.00")
        )
        self.assertEqual(
            Decimal(by_date[str(self.today - timedelta(days=1))]["spent_on_day"]),
            Decimal("5000.00"),
        )
