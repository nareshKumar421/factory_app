"""
Phase 1 -- applying for leave, and the calendar rules behind it.

    python manage.py test leave.tests_apply --settings=config.sqlite_test_settings
"""

from datetime import date, timedelta
from decimal import Decimal

from django.test import override_settings

from employee_hierarchy.constants import EmploymentStatus

from .calendar import working_dates
from .constants import DayPortion, LeaveDayStatus, LeaveRequestStatus
from .models import Holiday, LeaveApproval, LeaveRequestDay
from .services import LeaveRefused, apply_for_leave, approve, cancel
from .tests_base import WEDNESDAY, LeaveTestBase


class WorkingDatesTests(LeaveTestBase):
    def test_sunday_is_excluded(self):
        # Wednesday 2026-10-07 .. Monday 2026-10-12 spans one Sunday (the 11th).
        dates = working_dates(self.company, WEDNESDAY, WEDNESDAY + timedelta(days=5))
        self.assertNotIn(date(2026, 10, 11), dates)
        self.assertEqual(len(dates), 5)

    def test_mandatory_holiday_is_excluded_but_optional_is_not(self):
        Holiday.objects.create(
            company=self.company, date=WEDNESDAY, name="Founders Day"
        )
        Holiday.objects.create(
            company=self.company,
            date=WEDNESDAY + timedelta(days=1),
            name="Restricted",
            is_optional=True,
        )
        dates = working_dates(self.company, WEDNESDAY, WEDNESDAY + timedelta(days=1))
        self.assertNotIn(WEDNESDAY, dates)
        self.assertIn(WEDNESDAY + timedelta(days=1), dates)

    def test_another_companys_holiday_does_not_apply(self):
        Holiday.objects.create(
            company=self.other_company, date=WEDNESDAY, name="Mart only"
        )
        self.assertIn(WEDNESDAY, working_dates(self.company, WEDNESDAY, WEDNESDAY))

    @override_settings(ATTENDANCE_WEEKLY_OFF_DAYS=[5, 6])
    def test_weekly_offs_follow_the_attendance_setting(self):
        """The two modules must not be able to disagree about the off day."""
        dates = working_dates(self.company, WEDNESDAY, WEDNESDAY + timedelta(days=5))
        self.assertNotIn(date(2026, 10, 10), dates)  # Saturday
        self.assertNotIn(date(2026, 10, 11), dates)  # Sunday
        self.assertEqual(len(dates), 4)


class ApplyForLeaveTests(LeaveTestBase):
    def _apply(self, **kwargs):
        params = {
            "employee": self.worker,
            "leave_type": self.casual,
            "from_date": WEDNESDAY,
            "to_date": WEDNESDAY,
            "reason": "Family function",
            "applied_by": self.worker_user,
        }
        params.update(kwargs)
        return apply_for_leave(**params)

    # -- the happy paths -----------------------------------------------------

    def test_single_day_creates_one_day_row_and_a_trail_entry(self):
        request = self._apply()
        self.assertEqual(request.status, LeaveRequestStatus.PENDING)
        self.assertEqual(request.days.count(), 1)
        self.assertEqual(request.total_days, Decimal("1.0"))
        self.assertEqual(request.trail.count(), 1)
        self.assertEqual(request.trail.first().action, "APPLIED")

    def test_multi_day_span_skips_the_weekly_off(self):
        request = self._apply(to_date=WEDNESDAY + timedelta(days=5))
        booked = list(request.days.values_list("date", flat=True))
        self.assertNotIn(date(2026, 10, 11), booked)
        self.assertEqual(len(booked), 5)
        self.assertEqual(request.total_days, Decimal("5.0"))

    def test_half_day_costs_half(self):
        request = self._apply(portion=DayPortion.FIRST_HALF)
        self.assertEqual(request.total_days, Decimal("0.5"))
        self.assertEqual(request.days.first().portion, DayPortion.FIRST_HALF)

    def test_holiday_inside_the_span_is_not_charged(self):
        Holiday.objects.create(
            company=self.company, date=WEDNESDAY + timedelta(days=1), name="Diwali"
        )
        request = self._apply(to_date=WEDNESDAY + timedelta(days=2))
        self.assertEqual(request.total_days, Decimal("2.0"))
        self.assertNotIn(
            WEDNESDAY + timedelta(days=1), list(request.days.values_list("date", flat=True))
        )

    def test_time_office_can_raise_it_for_somebody_with_no_login(self):
        """About half the workforce has no login; this is the normal case."""
        request = self._apply(employee=self.worker_two, applied_by=self.hr_user)
        self.assertEqual(request.employee, self.worker_two)
        self.assertEqual(request.applied_by, self.hr_user)

    def test_day_rows_carry_the_employee_for_the_overlap_constraint(self):
        request = self._apply()
        self.assertEqual(request.days.first().employee_id, self.worker.pk)

    # -- the refusals --------------------------------------------------------

    def test_end_before_start_is_refused(self):
        with self.assertRaisesMessage(LeaveRefused, "cannot be before"):
            self._apply(to_date=WEDNESDAY - timedelta(days=1))

    def test_absurdly_long_span_is_refused(self):
        with self.assertRaisesMessage(LeaveRefused, "more than"):
            self._apply(to_date=WEDNESDAY + timedelta(days=400))

    def test_employee_who_has_left_cannot_apply(self):
        self.worker.employment_status = EmploymentStatus.RESIGNED
        self.worker.save(update_fields=["employment_status"])
        with self.assertRaisesMessage(LeaveRefused, "not in service"):
            self._apply()

    def test_discontinued_leave_type_is_refused(self):
        with self.assertRaisesMessage(LeaveRefused, "no longer offered"):
            self._apply(leave_type=self.retired_type)

    def test_half_day_refused_for_a_type_that_does_not_allow_it(self):
        with self.assertRaisesMessage(LeaveRefused, "cannot be taken as a half day"):
            self._apply(leave_type=self.sick, portion=DayPortion.FIRST_HALF)

    def test_half_day_refused_across_a_multi_day_span(self):
        with self.assertRaisesMessage(LeaveRefused, "single date"):
            self._apply(to_date=WEDNESDAY + timedelta(days=2), portion=DayPortion.FIRST_HALF)

    def test_blank_reason_is_refused(self):
        with self.assertRaisesMessage(LeaveRefused, "reason is required"):
            self._apply(reason="   ")

    def test_span_of_only_weekly_offs_is_refused(self):
        sunday = date(2026, 10, 11)
        with self.assertRaisesMessage(LeaveRefused, "nothing to apply for"):
            self._apply(from_date=sunday, to_date=sunday)

    def test_overlapping_the_same_date_twice_is_refused(self):
        self._apply()
        with self.assertRaisesMessage(LeaveRefused, "already covered"):
            self._apply()

    def test_overlap_is_refused_even_when_only_one_date_collides(self):
        self._apply(to_date=WEDNESDAY + timedelta(days=1))
        with self.assertRaisesMessage(LeaveRefused, "already covered"):
            self._apply(
                from_date=WEDNESDAY + timedelta(days=1),
                to_date=WEDNESDAY + timedelta(days=2),
            )

    def test_a_colleague_may_hold_the_same_date(self):
        """The lock is per person, not per date."""
        self._apply()
        request = self._apply(employee=self.worker_two, applied_by=self.hr_user)
        self.assertEqual(request.days.count(), 1)

    def test_leave_type_from_another_company_is_refused(self):
        foreign = self.casual
        foreign.pk = None
        foreign.company = self.other_company
        foreign.save()
        with self.assertRaisesMessage(LeaveRefused, "another company"):
            self._apply(leave_type=foreign)

    def test_max_consecutive_days_is_enforced(self):
        self.casual.max_consecutive_days = 2
        self.casual.save(update_fields=["max_consecutive_days"])
        with self.assertRaisesMessage(LeaveRefused, "consecutive"):
            self._apply(to_date=WEDNESDAY + timedelta(days=4))

    def test_a_refused_application_writes_nothing_at_all(self):
        """The whole thing is one transaction -- no orphan request, no trail."""
        with self.assertRaises(LeaveRefused):
            self._apply(reason="")
        self.assertEqual(LeaveRequestDay.objects.count(), 0)
        self.assertEqual(LeaveApproval.objects.count(), 0)

    def test_a_cancelled_day_frees_the_date_again(self):
        request = self._apply()
        request.days.update(status=LeaveDayStatus.CANCELLED)
        # Re-applying for the same date must now succeed.
        again = self._apply()
        self.assertEqual(again.days.count(), 1)

    def test_cancelling_an_approved_leave_frees_the_date_again(self):
        """The same thing through the real path, rather than a hand-set status.

        The test above only proves the constraint reads ``CANCELLED``; it is
        this one that proves ``cancel`` actually puts the days there. An
        APPROVED row left behind on a cancelled request occupies its date for
        good, and the refusal the employee then gets names a request that no
        longer exists.
        """
        request = self._apply()
        approve(request, user=self.hr_user, authority="hr")
        cancel(request, user=self.hr_user, comment="Plant shutdown moved")

        self.assertEqual(
            list(request.days.values_list("status", flat=True)),
            [LeaveDayStatus.CANCELLED],
        )
        again = self._apply()
        self.assertEqual(again.days.count(), 1)

    def test_cancelling_a_partial_approval_leaves_the_refused_days_refused(self):
        """Only the approved days come back -- a refusal keeps its own record."""
        request = self._apply(to_date=WEDNESDAY + timedelta(days=1))
        approve(request, user=self.hr_user, authority="hr", only_dates=[WEDNESDAY])
        cancel(request, user=self.hr_user, comment="Plant shutdown moved")

        statuses = dict(request.days.values_list("date", "status"))
        self.assertEqual(statuses[WEDNESDAY], LeaveDayStatus.CANCELLED)
        self.assertEqual(
            statuses[WEDNESDAY + timedelta(days=1)], LeaveDayStatus.REJECTED
        )
