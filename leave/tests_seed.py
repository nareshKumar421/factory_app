"""
Tests for ``seed_leave_demo``.

    python manage.py test leave.tests_seed --settings=config.sqlite_test_settings

The one that matters is the guard. This command writes dozens of rows and
projects them onto the attendance sheet; run against the live database by
accident it would put fictional leave on real people's attendance, which
payroll is then run from.
"""

from io import StringIO

from django.core.management import CommandError, call_command
from django.test import override_settings

from attendance.models import DailyAttendance, OverrideReason

from .constants import LeaveRequestStatus
from .management.commands.seed_leave_demo import MARKER
from .models import Holiday, LeaveRequest
from .tests_base import LeaveTestBase


class SeedGuardTests(LeaveTestBase):
    def _run(self, *args, **kwargs):
        out = StringIO()
        call_command("seed_leave_demo", *args, stdout=out, stderr=out, **kwargs)
        return out.getvalue()

    @override_settings(DEBUG=False)
    def test_it_refuses_to_run_when_debug_is_off(self):
        """DEBUG off means .env, and .env points at production."""
        with self.assertRaisesMessage(CommandError, "probably the live database"):
            self._run("--commit")

    @override_settings(DEBUG=False)
    def test_force_overrides_the_guard(self):
        output = self._run("--commit", "--force", "--employees", "3")
        self.assertIn("Seeded", output)

    @override_settings(DEBUG=True)
    def test_unknown_company_is_refused(self):
        with self.assertRaises(CommandError):
            self._run("--commit", "--company", "NOPE")


@override_settings(DEBUG=True)
class SeedBehaviourTests(LeaveTestBase):
    def _run(self, *args):
        out = StringIO()
        call_command("seed_leave_demo", *args, stdout=out, stderr=out)
        return out.getvalue()

    def test_dry_run_writes_nothing(self):
        output = self._run("--employees", "5")
        self.assertIn("Dry run", output)
        self.assertEqual(LeaveRequest.objects.count(), 0)
        self.assertEqual(Holiday.objects.count(), 0)

    def test_commit_seeds_holidays_and_requests(self):
        self._run("--commit", "--employees", "6")
        self.assertGreater(Holiday.objects.filter(company=self.company).count(), 0)
        self.assertGreater(LeaveRequest.objects.count(), 0)

    def test_every_seeded_request_is_marked(self):
        self._run("--commit", "--employees", "6")
        for request in LeaveRequest.objects.all():
            self.assertIn(MARKER, request.reason)

    def test_it_produces_a_spread_of_statuses(self):
        self._run("--commit", "--employees", "12")
        statuses = set(LeaveRequest.objects.values_list("status", flat=True))
        # Not every status on a six-person org, but never just one.
        self.assertGreater(len(statuses), 1)
        self.assertIn(LeaveRequestStatus.PENDING, statuses)

    def test_a_type_needing_paperwork_is_not_silently_skipped(self):
        """Sick leave requires a document; without one the seeder used to drop it."""
        self._run("--commit", "--employees", "12")
        sick = LeaveRequest.objects.filter(leave_type=self.sick)
        self.assertTrue(sick.exists(), "document-requiring types must still be seeded")
        for request in sick:
            self.assertTrue(request.document, "a placeholder should have been attached")

    def test_seeded_decisions_go_through_the_trail(self):
        """Nothing is written straight to the tables."""
        self._run("--commit", "--employees", "6")
        for request in LeaveRequest.objects.all():
            self.assertGreater(
                request.trail.count(), 0, "every request must carry its trail"
            )

    def test_clear_removes_only_seeded_rows(self):
        from .services import apply_for_leave
        from .tests_base import WEDNESDAY

        real = apply_for_leave(
            employee=self.worker,
            leave_type=self.casual,
            from_date=WEDNESDAY,
            to_date=WEDNESDAY,
            reason="A genuine request",
            applied_by=self.worker_user,
        )
        self._run("--commit", "--employees", "6")
        self._run("--clear", "--commit")

        self.assertTrue(LeaveRequest.objects.filter(pk=real.pk).exists())
        self.assertEqual(LeaveRequest.objects.filter(reason__contains=MARKER).count(), 0)

    def test_clear_takes_seeded_leave_back_off_the_attendance_sheet(self):
        """Deleting the rows must not strand an override nobody can explain."""
        self._run("--commit", "--employees", "8")
        self._run("--clear", "--commit")

        stranded = DailyAttendance.objects.filter(
            override_reason_code=OverrideReason.APPROVED_LEAVE, is_overridden=True
        )
        self.assertEqual(stranded.count(), 0)

    def test_clear_dry_run_removes_nothing(self):
        self._run("--commit", "--employees", "6")
        before = LeaveRequest.objects.count()
        output = self._run("--clear")
        self.assertIn("Dry run", output)
        self.assertEqual(LeaveRequest.objects.count(), before)

    def test_the_same_seed_produces_the_same_run(self):
        self._run("--commit", "--employees", "6", "--seed", "42")
        first = sorted(
            LeaveRequest.objects.values_list("employee__employee_code", "from_date")
        )
        self._run("--clear", "--commit")
        self._run("--commit", "--employees", "6", "--seed", "42")
        second = sorted(
            LeaveRequest.objects.values_list("employee__employee_code", "from_date")
        )
        self.assertEqual(first, second)
