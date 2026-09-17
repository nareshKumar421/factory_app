"""
Tests for the daily attendance sheet.

What is worth pinning down here is not that a punch becomes a row -- it is the
handful of rules that, if they broke, would break quietly and be discovered by
somebody's pay slip.

**The machine's reading is immutable.** Every correction path, and a re-run of
the sync over a corrected day, must leave ``machine_status`` exactly as the
machine wrote it. If that ever stops being true, "what did the machine actually
record?" becomes unanswerable and the whole module is pointless.

**A correction survives a re-sync.** The sync runs nightly over the last couple
of days; if it recomputed the effective status it would silently undo every
correction made that afternoon.

**A reason is mandatory.** Both halves of it. The module exists to answer "why
does this differ from the machine?", and a blank reason is not an answer.

**Deriving a status from punches.** Especially the one-punch case -- 14% of
person-days -- which is neither present nor absent.

**Overriding is a separate grant from viewing.** Getting this wrong is a silent
403 for some users, or worse, a correction right for all of them.
"""

from datetime import date, datetime, time

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.test import TestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from company.models import Company
from employee_hierarchy.models import Employee

from . import services
from .biometrics import Punch
from .models import (
    AttendanceOverrideLog,
    AttendanceStatus,
    DailyAttendance,
    OverrideAction,
    OverrideReason,
)

User = get_user_model()
BASE = "/api/v1/attendance"

#: 2026-09-16 is a Wednesday; 2026-09-13 a Sunday. The weekly-off rule turns on
#: the weekday, so both are needed.
WEDNESDAY = date(2026, 9, 16)
SUNDAY = date(2026, 9, 13)


def punch(code, at, device="NCD8244900570"):
    return Punch(code, at, device)


class Fixture(TestCase):
    def setUp(self):
        self.company, _ = Company.objects.get_or_create(
            code="JIVO_OIL", defaults={"name": "Jivo Oil"}
        )
        self.employee = Employee.objects.create(
            company=self.company, employee_code="JWPL0593", first_name="Vishal", last_name="Tyagi"
        )
        self.other = Employee.objects.create(
            company=self.company, employee_code="TP080", first_name="Ram", last_name="Lal"
        )

    def sync(self, day, punches):
        by_code = {}
        for item in punches:
            by_code.setdefault(item.employee_code, []).append(item)
        return services.sync_day(day, by_code, [self.employee, self.other])

    def row(self, employee=None, day=WEDNESDAY):
        return DailyAttendance.objects.get(employee=employee or self.employee, date=day)


class DeriveStatusTests(Fixture):
    """What a day's punches amount to on their own."""

    def test_two_punches_a_full_span_is_present(self):
        self.sync(WEDNESDAY, [
            punch("JWPL0593", datetime(2026, 9, 16, 9, 9)),
            punch("JWPL0593", datetime(2026, 9, 16, 19, 30)),
        ])
        row = self.row()
        self.assertEqual(row.machine_status, AttendanceStatus.PRESENT)
        self.assertEqual(row.machine_first_punch, time(9, 9))
        self.assertEqual(row.machine_last_punch, time(19, 30))
        self.assertEqual(row.machine_punch_count, 2)
        self.assertEqual(row.machine_worked_minutes, 621)

    def test_a_short_span_is_a_half_day(self):
        self.sync(WEDNESDAY, [
            punch("JWPL0593", datetime(2026, 9, 16, 9, 0)),
            punch("JWPL0593", datetime(2026, 9, 16, 11, 0)),
        ])
        self.assertEqual(self.row().machine_status, AttendanceStatus.HALF_DAY)

    def test_one_punch_is_neither_present_nor_absent(self):
        """The 14% case. Marking them present guesses; absent is a falsehood."""
        self.sync(WEDNESDAY, [punch("JWPL0593", datetime(2026, 9, 16, 9, 9))])
        row = self.row()
        self.assertEqual(row.machine_status, AttendanceStatus.MISSING_PUNCH)
        self.assertEqual(row.machine_punch_count, 1)
        self.assertEqual(row.machine_worked_minutes, 0)

    def test_no_punches_on_a_working_day_is_absent(self):
        self.sync(WEDNESDAY, [])
        self.assertEqual(self.row().machine_status, AttendanceStatus.ABSENT)

    def test_no_punches_on_sunday_is_the_weekly_off(self):
        """Otherwise every Sunday buries the real absences under 300 false ones."""
        self.sync(SUNDAY, [])
        self.assertEqual(self.row(day=SUNDAY).machine_status, AttendanceStatus.WEEKLY_OFF)

    def test_punching_on_sunday_is_scored_normally(self):
        self.sync(SUNDAY, [
            punch("JWPL0593", datetime(2026, 9, 13, 9, 0)),
            punch("JWPL0593", datetime(2026, 9, 13, 18, 0)),
        ])
        self.assertEqual(self.row(day=SUNDAY).machine_status, AttendanceStatus.PRESENT)

    def test_everybody_gets_a_row_even_with_no_punches(self):
        """A missing row and an absence must never look alike."""
        self.sync(WEDNESDAY, [punch("JWPL0593", datetime(2026, 9, 16, 9, 0))])
        self.assertEqual(DailyAttendance.objects.filter(date=WEDNESDAY).count(), 2)
        self.assertEqual(self.row(self.other).machine_status, AttendanceStatus.ABSENT)

    def test_devices_are_recorded(self):
        self.sync(WEDNESDAY, [
            punch("JWPL0593", datetime(2026, 9, 16, 9, 0), device="GATE-A"),
            punch("JWPL0593", datetime(2026, 9, 16, 19, 0), device="GATE-B"),
        ])
        self.assertEqual(self.row().devices, "GATE-A,GATE-B")


class OverrideTests(Fixture):
    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user(
            email="hr@example.com", password="x", full_name="HR Person",
            employee_code="U-HR",
        )
        self.sync(WEDNESDAY, [punch("JWPL0593", datetime(2026, 9, 16, 9, 9))])

    def test_override_never_touches_the_machine_reading(self):
        row = self.row()
        services.override_status(
            row, status=AttendanceStatus.PRESENT,
            reason_code=OverrideReason.FORGOT_PUNCH,
            reason="Forgot to punch out; supervisor confirms he left at 19:00.",
            user=self.user,
        )
        row.refresh_from_db()
        self.assertEqual(row.effective_status, AttendanceStatus.PRESENT)
        # The evidence the correction is judged against.
        self.assertEqual(row.machine_status, AttendanceStatus.MISSING_PUNCH)
        self.assertTrue(row.is_overridden)
        self.assertEqual(row.overridden_by, self.user)

    def test_override_is_logged(self):
        row = self.row()
        services.override_status(
            row, status=AttendanceStatus.PRESENT, reason_code=OverrideReason.FORGOT_PUNCH,
            reason="Forgot to punch out.", user=self.user,
        )
        entry = AttendanceOverrideLog.objects.get(daily_attendance=row)
        self.assertEqual(entry.action, OverrideAction.OVERRIDE)
        self.assertEqual(entry.from_status, AttendanceStatus.MISSING_PUNCH)
        self.assertEqual(entry.to_status, AttendanceStatus.PRESENT)
        self.assertEqual(entry.machine_status, AttendanceStatus.MISSING_PUNCH)
        self.assertEqual(entry.performed_by, self.user)

    def test_a_reason_is_required(self):
        row = self.row()
        with self.assertRaises(services.OverrideRefused):
            services.override_status(
                row, status=AttendanceStatus.PRESENT,
                reason_code=OverrideReason.FORGOT_PUNCH, reason="   ", user=self.user,
            )
        with self.assertRaises(services.OverrideRefused):
            services.override_status(
                row, status=AttendanceStatus.PRESENT, reason_code="",
                reason="Something", user=self.user,
            )
        row.refresh_from_db()
        self.assertFalse(row.is_overridden)

    def test_a_second_override_appends_rather_than_amending_the_first(self):
        row = self.row()
        services.override_status(
            row, status=AttendanceStatus.PRESENT, reason_code=OverrideReason.FORGOT_PUNCH,
            reason="Forgot to punch out.", user=self.user,
        )
        services.override_status(
            row, status=AttendanceStatus.HALF_DAY, reason_code=OverrideReason.HALF_DAY_APPROVED,
            reason="Actually left at lunch; HOD approved a half day.", user=self.user,
        )
        entries = list(AttendanceOverrideLog.objects.filter(daily_attendance=row).order_by("id"))
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[1].action, OverrideAction.AMEND)
        self.assertEqual(entries[1].from_status, AttendanceStatus.PRESENT)
        row.refresh_from_db()
        self.assertEqual(row.effective_status, AttendanceStatus.HALF_DAY)

    def test_revert_restores_the_machine_status_and_is_logged(self):
        row = self.row()
        services.override_status(
            row, status=AttendanceStatus.PRESENT, reason_code=OverrideReason.FORGOT_PUNCH,
            reason="Forgot to punch out.", user=self.user,
        )
        services.revert_to_machine(row, reason="Claim could not be confirmed.", user=self.user)
        row.refresh_from_db()
        self.assertFalse(row.is_overridden)
        self.assertEqual(row.effective_status, AttendanceStatus.MISSING_PUNCH)
        self.assertEqual(row.override_reason, "")
        self.assertEqual(
            AttendanceOverrideLog.objects.filter(
                daily_attendance=row, action=OverrideAction.REVERT
            ).count(),
            1,
        )

    def test_resync_keeps_the_correction(self):
        """The nightly job must not undo what HR did that afternoon."""
        row = self.row()
        services.override_status(
            row, status=AttendanceStatus.PRESENT, reason_code=OverrideReason.FORGOT_PUNCH,
            reason="Forgot to punch out.", user=self.user,
        )
        # The machine later reports the missing second punch.
        _created, _updated, kept = self.sync(WEDNESDAY, [
            punch("JWPL0593", datetime(2026, 9, 16, 9, 9)),
            punch("JWPL0593", datetime(2026, 9, 16, 19, 0)),
        ])
        row.refresh_from_db()
        self.assertEqual(kept, 1)
        # Machine columns refreshed...
        self.assertEqual(row.machine_status, AttendanceStatus.PRESENT)
        self.assertEqual(row.machine_punch_count, 2)
        # ...and the correction still stands, with its reason.
        self.assertTrue(row.is_overridden)
        self.assertEqual(row.override_reason, "Forgot to punch out.")

    def test_resync_updates_a_day_nobody_touched(self):
        self.sync(WEDNESDAY, [
            punch("JWPL0593", datetime(2026, 9, 16, 9, 9)),
            punch("JWPL0593", datetime(2026, 9, 16, 19, 0)),
        ])
        row = self.row()
        self.assertEqual(row.machine_status, AttendanceStatus.PRESENT)
        self.assertEqual(row.effective_status, AttendanceStatus.PRESENT)

    def test_summary_reports_both_readings(self):
        row = self.row()
        services.override_status(
            row, status=AttendanceStatus.PRESENT, reason_code=OverrideReason.FORGOT_PUNCH,
            reason="Forgot to punch out.", user=self.user,
        )
        summary = services.summarise(DailyAttendance.objects.filter(date=WEDNESDAY))
        self.assertEqual(summary["total"], 2)
        self.assertEqual(summary["overridden"], 1)
        self.assertEqual(summary["machine"][AttendanceStatus.MISSING_PUNCH], 1)
        self.assertEqual(summary["effective"][AttendanceStatus.PRESENT], 1)

    def test_summary_survives_an_ordered_queryset(self):
        """The counts must not depend on how the caller sorted the rows.

        Django folds a surviving ORDER BY into the GROUP BY, so an ordered
        queryset grouped by (status, employee) rather than (status) -- one row
        per person, and the dict below kept whichever came last, so a status
        held by twenty people counted 1. ``DailyAttendanceViewSet.get_queryset``
        ends ``.order_by("employee__full_name")``, so that is the queryset the
        API actually passes.

        Two employees must share a status for this to bite: with one person per
        status the collapse is invisible, which is how it survived the suite.
        """
        third = Employee.objects.create(
            company=self.company, employee_code="JWPL0001",
            first_name="Absent", last_name="Also",
        )
        # Nobody punches: all three are ABSENT, one status held by three people.
        services.sync_day(WEDNESDAY, {}, [self.employee, self.other, third])

        ordered = (
            DailyAttendance.objects.filter(date=WEDNESDAY)
            .select_related("employee")
            .order_by("employee__full_name")
        )
        summary = services.summarise(ordered)

        self.assertEqual(summary["total"], 3)
        self.assertEqual(summary["machine"][AttendanceStatus.ABSENT], 3)
        self.assertEqual(summary["effective"][AttendanceStatus.ABSENT], 3)


class ApiTests(APITestCase):
    """The permission split, and that the payload always carries both statuses."""

    def setUp(self):
        self.company, _ = Company.objects.get_or_create(
            code="JIVO_OIL", defaults={"name": "Jivo Oil"}
        )
        self.employee = Employee.objects.create(
            company=self.company, employee_code="JWPL0593", first_name="Vishal", last_name="Tyagi"
        )
        self.row = DailyAttendance.objects.create(
            employee=self.employee,
            date=WEDNESDAY,
            machine_status=AttendanceStatus.MISSING_PUNCH,
            effective_status=AttendanceStatus.MISSING_PUNCH,
            machine_punch_count=1,
        )
        self.viewer = self._user("viewer", ["can_view_daily_attendance"])
        self.hr = self._user("hr", ["can_view_daily_attendance", "can_override_attendance_status"])

    def _user(self, name, codenames):
        user = User.objects.create_user(
            email=f"{name}@example.com", password="x", full_name=name.title(),
            employee_code=f"U-{name.upper()}",
        )
        group = Group.objects.create(name=f"{name}-group")
        group.permissions.add(
            *Permission.objects.filter(
                content_type__app_label="attendance", codename__in=codenames
            )
        )
        user.groups.add(group)
        return user

    def test_list_returns_both_statuses(self):
        self.client.force_authenticate(self.viewer)
        response = self.client.get(f"{BASE}/daily/?date={WEDNESDAY}")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        row = response.data[0]
        # Both, always -- the "machine only" view is a column choice in the UI.
        self.assertEqual(row["machine_status"], AttendanceStatus.MISSING_PUNCH)
        self.assertEqual(row["effective_status"], AttendanceStatus.MISSING_PUNCH)
        self.assertFalse(row["is_overridden"])
        self.assertEqual(row["employee_code"], "JWPL0593")

    def test_viewing_does_not_grant_overriding(self):
        self.client.force_authenticate(self.viewer)
        response = self.client.post(
            f"{BASE}/daily/{self.row.pk}/override/",
            {"status": "PRESENT", "reason_code": "FORGOT_PUNCH", "reason": "Forgot to punch out."},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.row.refresh_from_db()
        self.assertFalse(self.row.is_overridden)

    def test_hr_can_override(self):
        self.client.force_authenticate(self.hr)
        response = self.client.post(
            f"{BASE}/daily/{self.row.pk}/override/",
            {"status": "PRESENT", "reason_code": "FORGOT_PUNCH", "reason": "Forgot to punch out."},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["effective_status"], AttendanceStatus.PRESENT)
        self.assertEqual(response.data["machine_status"], AttendanceStatus.MISSING_PUNCH)
        self.assertTrue(response.data["is_overridden"])

    def test_override_without_a_reason_is_refused(self):
        self.client.force_authenticate(self.hr)
        response = self.client.post(
            f"{BASE}/daily/{self.row.pk}/override/",
            {"status": "PRESENT", "reason_code": "FORGOT_PUNCH", "reason": ""},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_status_cannot_be_patched_around_the_override(self):
        """The viewset is read-only; a PATCH would skip the reason and the log."""
        self.client.force_authenticate(self.hr)
        response = self.client.patch(
            f"{BASE}/daily/{self.row.pk}/", {"effective_status": "PRESENT"}, format="json"
        )
        self.assertIn(
            response.status_code,
            (status.HTTP_405_METHOD_NOT_ALLOWED, status.HTTP_403_FORBIDDEN),
        )
        self.row.refresh_from_db()
        self.assertEqual(self.row.effective_status, AttendanceStatus.MISSING_PUNCH)

    def test_history_lists_the_changes(self):
        self.client.force_authenticate(self.hr)
        self.client.post(
            f"{BASE}/daily/{self.row.pk}/override/",
            {"status": "PRESENT", "reason_code": "FORGOT_PUNCH", "reason": "Forgot to punch out."},
            format="json",
        )
        response = self.client.get(f"{BASE}/daily/{self.row.pk}/history/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["to_status"], AttendanceStatus.PRESENT)
        self.assertEqual(response.data[0]["reason"], "Forgot to punch out.")

    def test_summary_endpoint(self):
        self.client.force_authenticate(self.viewer)
        response = self.client.get(f"{BASE}/daily/summary/?date={WEDNESDAY}")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["total"], 1)
        self.assertEqual(response.data["overridden"], 0)

    def test_reasons_endpoint_publishes_the_vocabulary(self):
        self.client.force_authenticate(self.viewer)
        response = self.client.get(f"{BASE}/daily/reasons/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        codes = {item["value"] for item in response.data["reason_codes"]}
        self.assertIn(OverrideReason.FORGOT_PUNCH, codes)

    def test_anonymous_is_refused(self):
        response = self.client.get(f"{BASE}/daily/?date={WEDNESDAY}")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_list_defaults_to_today_rather_than_all_history(self):
        self.client.force_authenticate(self.viewer)
        response = self.client.get(f"{BASE}/daily/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # The fixture row is dated in the past, so "today" must exclude it.
        self.assertEqual(len(response.data), 0)


class MusterRollTests(ApiTests):
    """The month register, and the three states a cell can be in.

    A synced day, a day nobody synced, and a day that was never this person's:
    conflating any two of them turns an unfinished sync into a fortnight of
    invented absences, which is the whole reason the daily sheet has a
    source-status banner.
    """

    def setUp(self):
        super().setUp()
        self.client.force_authenticate(self.viewer)
        # WEDNESDAY is 2026-09-16, so the month under test is September 2026.
        self.employee.joining_date = date(2026, 1, 1)
        self.employee.save(update_fields=["joining_date"])

    def muster(self, month="2026-09", **params):
        query = "".join(f"&{k}={v}" for k, v in params.items())
        response = self.client.get(f"{BASE}/daily/muster/?month={month}{query}")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return response.data

    def row_for(self, payload, code="JWPL0593"):
        return next(r for r in payload["data"] if r["employee_code"] == code)

    def test_a_synced_day_carries_its_row_id_and_the_machine_reading(self):
        days = self.row_for(self.muster())["days"]
        self.assertEqual(days["16"], {"id": self.row.id, "m": AttendanceStatus.MISSING_PUNCH})

    def test_an_uncorrected_day_omits_the_effective_status(self):
        """``e`` is the override marker. Repeating ``m`` on every clean day
        would add a third key to ~7,000 cells to say nothing."""
        self.assertNotIn("e", self.row_for(self.muster())["days"]["16"])

    def test_a_corrected_day_carries_both_readings(self):
        services.override_status(
            self.row, status=AttendanceStatus.PRESENT,
            reason_code=OverrideReason.FORGOT_PUNCH, reason="Confirmed by supervisor.",
            user=self.hr,
        )
        cell = self.row_for(self.muster())["days"]["16"]
        self.assertEqual(cell["m"], AttendanceStatus.MISSING_PUNCH)
        self.assertEqual(cell["e"], AttendanceStatus.PRESENT)

    def test_e_is_keyed_off_is_overridden_not_off_the_values_differing(self):
        """HR may re-affirm the machine on the record. That day is corrected
        even though the two readings now agree, and must say so."""
        services.override_status(
            self.row, status=AttendanceStatus.MISSING_PUNCH,
            reason_code=OverrideReason.DATA_ERROR, reason="Machine was right after all.",
            user=self.hr,
        )
        cell = self.row_for(self.muster())["days"]["16"]
        self.assertEqual(cell["e"], cell["m"])
        self.assertIn("e", cell)

    def test_a_day_nobody_synced_is_null_and_never_absent(self):
        days = self.row_for(self.muster())["days"]
        self.assertIsNone(days["15"])
        self.assertNotEqual(days["15"], AttendanceStatus.ABSENT)
        self.assertEqual(self.row_for(self.muster())["totals"]["not_synced"], 29)

    def test_a_day_before_joining_has_no_key_at_all(self):
        """Different from unsynced: it was never theirs to have."""
        late = Employee.objects.create(
            company=self.company, employee_code="JWPL9999",
            first_name="Late", last_name="Joiner", joining_date=date(2026, 9, 10),
        )
        days = self.row_for(self.muster(), code=late.employee_code)["days"]
        self.assertNotIn("9", days)
        self.assertIn("10", days)

    def test_a_leaver_still_shows_the_days_they_worked(self):
        """The roll is not filtered by today's employment status: somebody who
        resigned on the 20th still worked the first nineteen days."""
        leaver = Employee.objects.create(
            company=self.company, employee_code="JWPL8888",
            first_name="Gone", last_name="Already",
            joining_date=date(2026, 1, 1), exit_date=date(2026, 9, 20),
            employment_status="RESIGNED",
        )
        DailyAttendance.objects.create(
            employee=leaver, date=WEDNESDAY,
            machine_status=AttendanceStatus.PRESENT,
            effective_status=AttendanceStatus.PRESENT,
        )
        days = self.row_for(self.muster(), code=leaver.employee_code)["days"]
        self.assertEqual(days["16"]["m"], AttendanceStatus.PRESENT)
        self.assertNotIn("21", days)

    def test_a_real_row_outranks_the_service_window(self):
        """``joining_date`` defaults to the day the directory was imported, so
        for most of this workforce it is a stand-in. A recorded punch is a fact
        and must never be hidden behind a guess."""
        self.employee.joining_date = date(2026, 9, 30)
        self.employee.save(update_fields=["joining_date"])
        days = self.row_for(self.muster())["days"]
        self.assertEqual(days["16"]["id"], self.row.id)

    def test_totals_follow_the_correction_not_the_machine(self):
        services.override_status(
            self.row, status=AttendanceStatus.ON_LEAVE,
            reason_code=OverrideReason.APPROVED_LEAVE, reason="Approved leave.",
            user=self.hr,
        )
        totals = self.row_for(self.muster())["totals"]
        self.assertEqual(totals.get(AttendanceStatus.ON_LEAVE), 1)
        self.assertNotIn(AttendanceStatus.MISSING_PUNCH, totals)

    def test_meta_is_unchanged_by_which_page_you_ask_for(self):
        for index in range(4):
            Employee.objects.create(
                company=self.company, employee_code=f"JWPL70{index}",
                first_name="Extra", last_name=str(index), joining_date=date(2026, 1, 1),
            )
        first = self.muster(page=1, page_size=2)
        second = self.muster(page=2, page_size=2)
        self.assertEqual(first["meta"], second["meta"])
        self.assertEqual(first["pagination"]["total"], 5)
        self.assertEqual(len(second["data"]), 2)

    def test_viewing_the_month_does_not_need_the_override_grant(self):
        self.assertEqual(self.muster()["month"], "2026-09")

    def test_an_unparsable_month_falls_back_to_the_current_one(self):
        response = self.client.get(f"{BASE}/daily/muster/?month=not-a-month")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        today = timezone.localdate()
        self.assertEqual(response.data["month"], f"{today.year}-{today.month:02d}")

    def test_anonymous_is_refused(self):
        self.client.force_authenticate(None)
        response = self.client.get(f"{BASE}/daily/muster/?month=2026-09")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
