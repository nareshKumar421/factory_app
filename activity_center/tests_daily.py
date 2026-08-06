"""
Daily job sheet tests.

Several assertions here encode design decisions rather than mechanics — that the sheet
never reports a score, that an unobservable job reads as null rather than zero, and
that cadence stays independent of countability. A failure in those means the contract
was weakened, not that the test drifted.
"""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from company.models import Company, UserCompany, UserRole

from .daily import board_for_day, local_day_bounds, sheet_for_user
from .registry import (
    ACTIVITY_SOURCES,
    CADENCES,
    EXPECTED_CADENCES,
    is_countable,
)
from .services import permission_holders

User = get_user_model()


class CadenceIntegrityTests(TestCase):
    """Cadence drives how the sheet is grouped, so it must be complete and valid."""

    def test_every_source_declares_a_valid_cadence(self):
        bad = [
            "%s: %r" % (source.key, source.cadence)
            for source in ACTIVITY_SOURCES
            if source.cadence not in CADENCES
        ]
        self.assertEqual(bad, [], "Invalid cadence values")

    def test_countable_sources_pair_an_actor_with_a_date(self):
        broken = [
            source.key
            for source in ACTIVITY_SOURCES
            if bool(source.actor_field) != bool(source.actor_date_field)
        ]
        self.assertEqual(sorted(broken), [], "actor_field and actor_date_field come as a pair")

    def test_cadence_is_independent_of_countability(self):
        """
        The central claim of the design: cadence describes the job, not our ability to
        observe it.

        If this comes back empty, someone has tidied up by demoting unobservable jobs
        to EVENT — which hides them from the group where the person responsible
        actually looks for them.
        """
        expected_but_unobservable = [
            source.key
            for source in ACTIVITY_SOURCES
            if source.cadence in EXPECTED_CADENCES and not is_countable(source)
        ]
        self.assertNotEqual(
            expected_but_unobservable,
            [],
            "expected-cadence jobs with no actor field must stay in their real group",
        )

    def test_every_source_has_a_screen_to_link_to(self):
        self.assertEqual([s.key for s in ACTIVITY_SOURCES if not s.list_url], [])

    def test_a_source_missing_from_the_cadence_table_is_rejected(self):
        from dataclasses import replace

        from .registry import _with_cadence

        orphan = replace(ACTIVITY_SOURCES[0], key="not_in_the_cadence_table")
        with self.assertRaises(ValueError):
            _with_cadence([orphan])


class DailySheetTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Sheet Co", code="SHEET_CO")
        cls.role = UserRole.objects.create(name="Employee")
        cls.user = User.objects.create_user(
            email="sheet@example.com", full_name="Sheet User", employee_code="S1", password="x"
        )
        UserCompany.objects.create(
            user=cls.user, company=cls.company, role=cls.role, is_default=True
        )

    def sheet(self):
        return sheet_for_user(self.user, company=self.company)

    def test_uncountable_job_reports_null_not_zero(self):
        """
        The single most important assertion in this module.

        A job whose record does not store who acted must come back null so the UI can
        render "not tracked". A 0 would render as "you did nothing today" — a claim we
        have no evidence for.
        """
        jobs = [job for group in self.sheet()["groups"] for job in group["jobs"]]
        uncountable = [job for job in jobs if not job["countable"]]

        self.assertTrue(uncountable, "expected some unobservable jobs in scope")
        for job in uncountable:
            self.assertIsNone(job["done_today"], job["source_key"])
            self.assertIsNone(job["last_done_at"], job["source_key"])

    def test_tally_covers_only_observable_expected_jobs(self):
        sheet = self.sheet()
        jobs = [job for group in sheet["groups"] for job in group["jobs"]]

        countable_expected = [
            job for job in jobs if job["countable"] and job["cadence"] in EXPECTED_CADENCES
        ]
        self.assertEqual(sheet["tally"]["counted_jobs"], len(countable_expected))
        self.assertEqual(
            sheet["uncounted_jobs"], len([job for job in jobs if not job["countable"]])
        )
        self.assertEqual(
            sheet["tally"]["not_yet"],
            sheet["tally"]["counted_jobs"] - sheet["tally"]["done"],
        )

    def test_event_groups_never_contribute_to_the_tally(self):
        for group in self.sheet()["groups"]:
            if group["cadence"] not in EXPECTED_CADENCES:
                self.assertEqual(group["counted_jobs"], 0, group["cadence"])
                self.assertEqual(group["done"], 0, group["cadence"])

    def test_groups_are_in_display_order_and_never_empty(self):
        sheet = self.sheet()
        order = [group["cadence"] for group in sheet["groups"]]
        self.assertEqual(order, [c for c in CADENCES if c in order])
        for group in sheet["groups"]:
            self.assertTrue(group["jobs"], group["cadence"])

    def test_tally_ships_no_score(self):
        keys = set(self.sheet()["tally"])
        for banned in ("percent", "percentage", "score", "compliance", "rank", "missed"):
            self.assertNotIn(banned, keys)

    def test_day_bounds_are_local_midnight(self):
        start, end = local_day_bounds()
        self.assertEqual((start.hour, start.minute, start.second, start.microsecond), (0, 0, 0, 0))
        self.assertEqual(end - start, timedelta(days=1))
        self.assertEqual(timezone.localtime(start).date(), timezone.localtime().date())

    def test_an_earlier_day_is_reported_as_not_today(self):
        yesterday = (timezone.localtime() - timedelta(days=1)).date()
        sheet = sheet_for_user(self.user, company=self.company, day=yesterday)
        self.assertEqual(sheet["date"], yesterday)
        self.assertFalse(sheet["is_today"])
        self.assertTrue(self.sheet()["is_today"])


class DailyBoardTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Board Co", code="BOARD_CO")
        cls.role = UserRole.objects.create(name="Employee")
        cls.user = User.objects.create_user(
            email="board@example.com", full_name="Board User", employee_code="B1", password="x"
        )
        UserCompany.objects.create(
            user=cls.user, company=cls.company, role=cls.role, is_default=True
        )
        cls.supervisor_perm = Permission.objects.get(
            content_type__app_label="activity_center", codename="can_view_all_activities"
        )
        cls.reports_perm = Permission.objects.get(
            content_type__app_label="activity_center", codename="can_view_activity_reports"
        )
        cls.mine_perm = Permission.objects.get(
            content_type__app_label="activity_center", codename="can_view_my_activities"
        )

    def client_for(self, user):
        client = APIClient()
        client.force_authenticate(user=user)
        client.credentials(HTTP_COMPANY_CODE=self.company.code)
        return client

    def test_query_count_is_flat_in_the_number_of_users(self):
        """
        The regression guard that matters most here.

        Expectation is computed in Python from the registry crossed with the holder
        map, and each observable source contributes one grouped aggregate — so adding
        users must not add queries. A failure means a per-user loop crept back in and
        the board is now users x sources queries.
        """
        with CaptureQueriesContext(connection) as small:
            board_for_day(company=self.company)
        baseline = len(small.captured_queries)

        for index in range(25):
            User.objects.create_user(
                email="bulk%d@example.com" % index,
                full_name="Bulk %02d" % index,
                employee_code="BULK%d" % index,
                password="x",
            )

        with CaptureQueriesContext(connection) as large:
            board = board_for_day(company=self.company)

        self.assertEqual(len(large.captured_queries), baseline)
        self.assertEqual(board["totals"]["users"], 26)

    def test_board_rows_carry_no_score_field(self):
        row = board_for_day(company=self.company)["users"][0]
        for banned in ("percent", "percentage", "score", "compliance", "rank", "missed"):
            self.assertNotIn(banned, row, "the board must not ship a %s field" % banned)

    def test_board_is_sorted_by_name_not_by_output(self):
        User.objects.create_user(
            email="aaa@example.com", full_name="Aaa First", employee_code="A9", password="x"
        )
        names = [row["full_name"] for row in board_for_day(company=self.company)["users"]]
        self.assertEqual(names, sorted(names))

    def test_totals_partition_the_user_list(self):
        totals = board_for_day(company=self.company)["totals"]
        self.assertEqual(totals["with_activity"] + totals["no_activity_yet"], totals["users"])

    def test_not_yet_never_exceeds_what_was_expected(self):
        """
        `jobs_done` counts every observable job done, including event-driven ones that
        were never an expectation. `not_yet` must therefore be measured against the
        expected subset only — subtracting the wider count would let a busy day on
        event work drive the figure negative or nonsensical.
        """
        for row in board_for_day(company=self.company)["users"]:
            self.assertGreaterEqual(row["not_yet"], 0, row["full_name"])
            self.assertLessEqual(row["not_yet"], row["expected_counted"], row["full_name"])

    def test_today_requires_the_supervisor_permission(self):
        self.assertEqual(
            self.client_for(self.user).get(reverse("activity-users-today")).status_code, 403
        )

        self.user.user_permissions.add(self.supervisor_perm)
        fresh = self.client_for(User.objects.get(pk=self.user.pk))
        self.assertEqual(fresh.get(reverse("activity-users-today")).status_code, 200)

    def test_earlier_days_additionally_require_the_reports_permission(self):
        self.user.user_permissions.add(self.supervisor_perm)
        yesterday = (timezone.localtime() - timedelta(days=1)).date().isoformat()

        client = self.client_for(User.objects.get(pk=self.user.pk))
        self.assertEqual(
            client.get(reverse("activity-users-today"), {"date": yesterday}).status_code, 403
        )

        self.user.user_permissions.add(self.reports_perm)
        fresh = self.client_for(User.objects.get(pk=self.user.pk))
        self.assertEqual(
            fresh.get(reverse("activity-users-today"), {"date": yesterday}).status_code, 200
        )

    def test_future_and_malformed_dates_are_rejected(self):
        self.user.user_permissions.add(self.supervisor_perm, self.reports_perm)
        client = self.client_for(User.objects.get(pk=self.user.pk))
        tomorrow = (timezone.localtime() + timedelta(days=1)).date().isoformat()

        self.assertEqual(
            client.get(reverse("activity-users-today"), {"date": tomorrow}).status_code, 400
        )
        self.assertEqual(
            client.get(reverse("activity-users-today"), {"date": "nonsense"}).status_code, 400
        )

    def test_my_sheet_endpoint_requires_the_self_permission(self):
        self.assertEqual(
            self.client_for(self.user).get(reverse("activity-my-today")).status_code, 403
        )

        self.user.user_permissions.add(self.mine_perm)
        fresh = self.client_for(User.objects.get(pk=self.user.pk))
        response = fresh.get(reverse("activity-my-today"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("groups", response.data)
        self.assertIn("tally", response.data)


class PermissionHolderTests(TestCase):
    def test_a_directly_granted_permission_makes_a_holder(self):
        """
        Guards the bug that would have made the board lie: resolving only group
        permissions leaves a directly-granted user showing no expected work at all.
        """
        user = User.objects.create_user(
            email="direct@example.com", full_name="Direct", employee_code="D1", password="x"
        )
        perm_string = ACTIVITY_SOURCES[0].permission
        app_label, codename = perm_string.split(".", 1)
        user.user_permissions.add(
            Permission.objects.get(content_type__app_label=app_label, codename=codename)
        )

        self.assertIn(user.pk, permission_holders()[perm_string])
