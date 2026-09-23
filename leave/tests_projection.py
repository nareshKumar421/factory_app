"""
Phase 3 -- approved leave reaching the attendance sheet, and coming back off.

    python manage.py test leave.tests_projection --settings=config.sqlite_test_settings

The invariant every test here is really guarding: **the machine's reading is
never written**. Everything else in this module is a convenience; that one is
what payroll disputes turn on.
"""

from datetime import timedelta
from io import StringIO

from django.core.management import CommandError, call_command
from django.test import TestCase

from attendance.models import (
    AttendanceOverrideLog,
    AttendanceStatus,
    DailyAttendance,
    OverrideReason,
)

from .constants import DayPortion, LeaveDayStatus
from .projection import (
    project_range,
    project_request,
    status_for,
    unproject_request,
)
from .services import apply_for_leave, approve, cancel
from .tests_base import WEDNESDAY, LeaveTestBase


class ProjectionTests(LeaveTestBase):
    def setUp(self):
        super().setUp()
        self.request = apply_for_leave(
            employee=self.worker,
            leave_type=self.casual,
            from_date=WEDNESDAY,
            to_date=WEDNESDAY,
            reason="Family function",
            applied_by=self.worker_user,
        )

    def _attendance(self, date=None, **kwargs):
        params = {
            "employee": self.worker,
            "date": date or WEDNESDAY,
            "machine_status": AttendanceStatus.ABSENT,
            "effective_status": AttendanceStatus.ABSENT,
        }
        params.update(kwargs)
        return DailyAttendance.objects.create(**params)

    # -- the core invariant --------------------------------------------------

    def test_projection_never_touches_the_machine_status(self):
        row = self._attendance(
            machine_status=AttendanceStatus.PRESENT,
            effective_status=AttendanceStatus.PRESENT,
        )
        approve(self.request, user=self.manager_user)
        project_request(self.request, user=self.manager_user)

        row.refresh_from_db()
        self.assertEqual(
            row.machine_status,
            AttendanceStatus.PRESENT,
            "the machine's reading must survive a leave projection untouched",
        )
        self.assertEqual(row.effective_status, AttendanceStatus.ON_LEAVE)

    def test_projection_writes_the_override_trail(self):
        self._attendance()
        approve(self.request, user=self.manager_user)
        project_request(self.request, user=self.manager_user)

        entry = AttendanceOverrideLog.objects.get()
        self.assertEqual(entry.to_status, AttendanceStatus.ON_LEAVE)
        self.assertEqual(entry.reason_code, OverrideReason.APPROVED_LEAVE)
        self.assertIn("Casual Leave approved", entry.reason)

    def test_a_full_day_reads_on_leave_and_a_half_day_reads_half_day(self):
        self.assertEqual(status_for(DayPortion.FULL), AttendanceStatus.ON_LEAVE)
        self.assertEqual(status_for(DayPortion.FIRST_HALF), AttendanceStatus.HALF_DAY)
        self.assertEqual(status_for(DayPortion.SECOND_HALF), AttendanceStatus.HALF_DAY)

    def test_half_day_leave_projects_as_half_day(self):
        half = apply_for_leave(
            employee=self.worker_two,
            leave_type=self.casual,
            from_date=WEDNESDAY,
            to_date=WEDNESDAY,
            portion=DayPortion.FIRST_HALF,
            reason="Doctor",
            applied_by=self.hr_user,
        )
        row = DailyAttendance.objects.create(
            employee=self.worker_two,
            date=WEDNESDAY,
            machine_status=AttendanceStatus.MISSING_PUNCH,
            effective_status=AttendanceStatus.MISSING_PUNCH,
        )
        approve(half, user=self.manager_user)
        project_request(half, user=self.manager_user)

        row.refresh_from_db()
        self.assertEqual(row.machine_status, AttendanceStatus.MISSING_PUNCH)
        self.assertEqual(row.effective_status, AttendanceStatus.HALF_DAY)

    # -- the timing problem --------------------------------------------------

    def test_a_day_with_no_attendance_row_yet_is_left_for_later(self):
        approve(self.request, user=self.manager_user)
        written = project_request(self.request, user=self.manager_user)

        self.assertEqual(written, 0)
        day = self.request.days.get()
        self.assertFalse(day.is_projected)

    def test_the_sweep_picks_it_up_once_the_row_exists(self):
        approve(self.request, user=self.manager_user)
        project_request(self.request, user=self.manager_user)  # too early

        self._attendance()  # the sync has now run
        written = project_range(WEDNESDAY, WEDNESDAY)

        self.assertEqual(written, 1)
        day = self.request.days.get()
        day.refresh_from_db()
        self.assertTrue(day.is_projected)

    def test_the_sweep_is_idempotent(self):
        self._attendance()
        approve(self.request, user=self.manager_user)
        project_request(self.request, user=self.manager_user)

        self.assertEqual(project_range(WEDNESDAY, WEDNESDAY), 0)
        self.assertEqual(AttendanceOverrideLog.objects.count(), 1)

    def test_a_multi_day_request_is_projected_piecemeal(self):
        multi = apply_for_leave(
            employee=self.worker_two,
            leave_type=self.casual,
            from_date=WEDNESDAY,
            to_date=WEDNESDAY + timedelta(days=2),
            reason="Three days",
            applied_by=self.hr_user,
        )
        approve(multi, user=self.manager_user)

        # Only the first date has been synced.
        DailyAttendance.objects.create(
            employee=self.worker_two, date=WEDNESDAY, machine_status=AttendanceStatus.ABSENT
        )
        self.assertEqual(project_range(WEDNESDAY, WEDNESDAY + timedelta(days=2)), 1)
        self.assertEqual(multi.days.filter(is_projected=True).count(), 1)

        # The sync catches up.
        DailyAttendance.objects.create(
            employee=self.worker_two,
            date=WEDNESDAY + timedelta(days=1),
            machine_status=AttendanceStatus.ABSENT,
        )
        self.assertEqual(project_range(WEDNESDAY, WEDNESDAY + timedelta(days=2)), 1)
        self.assertEqual(multi.days.filter(is_projected=True).count(), 2)

    def test_only_approved_days_are_projected(self):
        multi = apply_for_leave(
            employee=self.worker_two,
            leave_type=self.casual,
            from_date=WEDNESDAY,
            to_date=WEDNESDAY + timedelta(days=1),
            reason="Two days",
            applied_by=self.hr_user,
        )
        for date in (WEDNESDAY, WEDNESDAY + timedelta(days=1)):
            DailyAttendance.objects.create(
                employee=self.worker_two, date=date, machine_status=AttendanceStatus.ABSENT
            )
        approve(multi, user=self.manager_user, only_dates=[WEDNESDAY])

        self.assertEqual(project_range(WEDNESDAY, WEDNESDAY + timedelta(days=1)), 1)
        rejected_day = multi.days.get(date=WEDNESDAY + timedelta(days=1))
        self.assertEqual(rejected_day.status, LeaveDayStatus.REJECTED)
        self.assertFalse(rejected_day.is_projected)

    def test_a_pending_request_is_never_projected(self):
        self._attendance()
        self.assertEqual(project_range(WEDNESDAY, WEDNESDAY), 0)

    # -- taking it back off --------------------------------------------------

    def test_cancelling_reverts_the_sheet_to_the_machine(self):
        row = self._attendance(machine_status=AttendanceStatus.ABSENT)
        approve(self.request, user=self.manager_user)
        project_request(self.request, user=self.manager_user)

        cancel(self.request, user=self.hr_user, comment="Shutdown moved")
        reverted, skipped = unproject_request(self.request, user=self.hr_user)

        row.refresh_from_db()
        self.assertEqual((reverted, skipped), (1, 0))
        self.assertEqual(row.effective_status, AttendanceStatus.ABSENT)
        self.assertFalse(row.is_overridden)

    def test_cancelling_leaves_somebody_elses_correction_alone(self):
        """The case that would otherwise silently undo HR's work."""
        row = self._attendance()
        approve(self.request, user=self.manager_user)
        project_request(self.request, user=self.manager_user)

        # HR later corrects the same day for an unrelated reason.
        from attendance.services import override_status

        override_status(
            row,
            status=AttendanceStatus.ON_DUTY,
            reason_code=OverrideReason.ON_DUTY_OUTSIDE,
            reason="Sent to the Mart depot",
            user=self.hr_user,
        )

        cancel(self.request, user=self.hr_user, comment="Cancelled")
        reverted, skipped = unproject_request(self.request, user=self.hr_user)

        row.refresh_from_db()
        self.assertEqual((reverted, skipped), (0, 1))
        self.assertEqual(
            row.effective_status,
            AttendanceStatus.ON_DUTY,
            "a later unrelated correction must not be undone by a leave cancellation",
        )

    def test_unprojecting_clears_the_flag_so_it_can_be_projected_again(self):
        self._attendance()
        approve(self.request, user=self.manager_user)
        project_request(self.request, user=self.manager_user)
        cancel(self.request, user=self.hr_user, comment="Cancelled")
        unproject_request(self.request, user=self.hr_user)

        day = self.request.days.get()
        day.refresh_from_db()
        self.assertFalse(day.is_projected)

    def test_projection_events_land_on_the_leave_trail(self):
        self._attendance()
        approve(self.request, user=self.manager_user)
        project_request(self.request, user=self.manager_user)
        cancel(self.request, user=self.hr_user, comment="Cancelled")
        unproject_request(self.request, user=self.hr_user)

        actions = list(self.request.trail.order_by("id").values_list("action", flat=True))
        self.assertEqual(
            actions, ["APPLIED", "APPROVED", "PROJECTED", "CANCELLED", "UNPROJECTED"]
        )

    def test_someone_who_punched_in_anyway_keeps_both_readings(self):
        """The conflict case the two-column design exists for."""
        row = self._attendance(
            machine_status=AttendanceStatus.PRESENT,
            effective_status=AttendanceStatus.PRESENT,
            machine_punch_count=2,
        )
        approve(self.request, user=self.manager_user)
        project_request(self.request, user=self.manager_user)

        row.refresh_from_db()
        self.assertEqual(row.machine_status, AttendanceStatus.PRESENT)
        self.assertEqual(row.machine_punch_count, 2)
        self.assertEqual(row.effective_status, AttendanceStatus.ON_LEAVE)
        self.assertTrue(row.is_overridden)


class ProjectCommandTests(LeaveTestBase):
    def _run(self, *args):
        out = StringIO()
        call_command("project_approved_leave", *args, stdout=out, stderr=out)
        return out.getvalue()

    def test_days_window_projects_today(self):
        from django.utils import timezone

        today = timezone.localdate()
        request = apply_for_leave(
            employee=self.worker,
            leave_type=self.casual,
            from_date=today,
            to_date=today,
            reason="Today",
            applied_by=self.worker_user,
        )
        if not request.days.exists():
            self.skipTest("today is a weekly off in this environment")
        DailyAttendance.objects.create(
            employee=self.worker, date=today, machine_status=AttendanceStatus.ABSENT
        )
        approve(request, user=self.manager_user)

        output = self._run("--days", "1")
        self.assertIn("1 leave day(s) written", output)

    def test_explicit_range_is_accepted(self):
        output = self._run("--date-from", "2026-10-01", "--date-to", "2026-10-31")
        self.assertIn("Nothing to project", output)

    def test_a_window_is_required(self):
        with self.assertRaises(CommandError):
            self._run()

    def test_reversed_range_is_refused(self):
        with self.assertRaises(CommandError):
            self._run("--date-from", "2026-10-31", "--date-to", "2026-10-01")

    def test_unknown_company_is_refused(self):
        with self.assertRaises(CommandError):
            self._run("--days", "1", "--company", "NOPE")
