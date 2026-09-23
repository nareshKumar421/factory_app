"""
Phase 4 -- the HTTP surface, through the real permission stack.

    python manage.py test leave.tests_api --settings=config.sqlite_test_settings

These go through ``IsAuthenticated`` + ``HasCompanyContext`` like any real
request, so they also pin the thing that breaks most often in this codebase: a
missing or wrong ``Company-Code`` header.
"""

from datetime import timedelta

from django.urls import reverse
from rest_framework.test import APIClient

from attendance.models import AttendanceStatus, DailyAttendance
from company.models import UserCompany, UserRole

from .constants import LeaveRequestStatus
from .models import LeaveRequest
from .services import apply_for_leave
from .tests_base import WEDNESDAY, LeaveTestBase


class LeaveAPITestBase(LeaveTestBase):
    def setUp(self):
        super().setUp()
        self.role = UserRole.objects.get_or_create(name="Staff")[0]
        for user in (
            self.worker_user,
            self.manager_user,
            self.head_user,
            self.hr_user,
            self.other_head_user,
            self.ceo_user,
        ):
            UserCompany.objects.get_or_create(
                user=user, company=self.company, role=self.role, is_active=True
            )

    def client_for(self, user):
        client = APIClient()
        client.force_authenticate(user=user)
        client.credentials(HTTP_COMPANY_CODE=self.company.code)
        return client

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


class CompanyContextTests(LeaveAPITestBase):
    def test_missing_company_header_is_refused(self):
        client = APIClient()
        client.force_authenticate(user=self.worker_user)
        self.grant(self.worker_user, "leave.can_apply_leave")
        response = client.get(reverse("leave-request-list"))
        self.assertEqual(response.status_code, 403)

    def test_anonymous_is_refused(self):
        response = APIClient().get(reverse("leave-request-list"))
        self.assertIn(response.status_code, (401, 403))

    def test_no_leave_permission_at_all_is_refused(self):
        response = self.client_for(self.worker_user).get(reverse("leave-request-list"))
        self.assertEqual(response.status_code, 403)


class ApplyAPITests(LeaveAPITestBase):
    def setUp(self):
        super().setUp()
        self.grant(self.worker_user, "leave.can_apply_leave")

    def test_apply_for_yourself_without_naming_an_employee(self):
        response = self.client_for(self.worker_user).post(
            reverse("leave-request-list"),
            {
                "leave_type": self.casual.pk,
                "from_date": str(WEDNESDAY),
                "to_date": str(WEDNESDAY),
                "reason": "Family function",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["employee"], self.worker.pk)
        self.assertEqual(response.data["status"], LeaveRequestStatus.PENDING)
        self.assertEqual(response.data["responsible_manager_name"], self.manager.full_name)

    def test_a_business_refusal_comes_back_as_400_with_its_message(self):
        self._apply()
        response = self.client_for(self.worker_user).post(
            reverse("leave-request-list"),
            {
                "leave_type": self.casual.pk,
                "from_date": str(WEDNESDAY),
                "to_date": str(WEDNESDAY),
                "reason": "Again",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("already covered", response.data["detail"])

    def test_applying_for_a_colleague_is_refused(self):
        response = self.client_for(self.worker_user).post(
            reverse("leave-request-list"),
            {
                "employee": self.worker_two.pk,
                "leave_type": self.casual.pk,
                "from_date": str(WEDNESDAY),
                "to_date": str(WEDNESDAY),
                "reason": "Not mine to take",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 403)

    def test_the_time_office_may_apply_for_somebody_with_no_login(self):
        self.grant(self.hr_user, "leave.can_apply_leave_for_others")
        response = self.client_for(self.hr_user).post(
            reverse("leave-request-list"),
            {
                "employee": self.worker_two.pk,
                "leave_type": self.casual.pk,
                "from_date": str(WEDNESDAY),
                "to_date": str(WEDNESDAY),
                "reason": "Called in sick by phone",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["employee"], self.worker_two.pk)

    def test_an_unlinked_login_applying_for_itself_gets_a_useful_message(self):
        """The commonest first-run failure: nobody is linked to an employee."""
        self.grant(self.hr_user, "leave.can_apply_leave")
        response = self.client_for(self.hr_user).post(
            reverse("leave-request-list"),
            {
                "leave_type": self.casual.pk,
                "from_date": str(WEDNESDAY),
                "to_date": str(WEDNESDAY),
                "reason": "Mine",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("not linked to an employee record", response.data["detail"])


class VisibilityAPITests(LeaveAPITestBase):
    def setUp(self):
        super().setUp()
        self.request = self._apply()

    def test_applicant_sees_only_their_own(self):
        self.grant(self.worker_user, "leave.can_apply_leave")
        response = self.client_for(self.worker_user).get(reverse("leave-request-list"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual([row["id"] for row in response.data], [self.request.pk])

    def test_another_line_sees_nothing(self):
        self.grant(self.other_head_user, "leave.can_decide_leave")
        response = self.client_for(self.other_head_user).get(reverse("leave-request-list"))
        self.assertEqual(response.data, [])

    def test_manager_queue_shows_what_they_may_decide(self):
        self.grant(self.manager_user, "leave.can_decide_leave")
        response = self.client_for(self.manager_user).get(reverse("leave-pending"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual([row["id"] for row in response.data], [self.request.pk])
        self.assertEqual(response.data[0]["my_authority"], "manager")
        self.assertTrue(response.data[0]["can_decide"])

    def test_the_badge_count_matches_the_queue(self):
        self.grant(self.manager_user, "leave.can_decide_leave")
        response = self.client_for(self.manager_user).get(reverse("leave-pending-count"))
        self.assertEqual(response.data, {"count": 1})

    def test_your_own_request_never_appears_in_your_queue(self):
        own = self._apply(employee=self.manager, applied_by=self.manager_user)
        self.grant(self.manager_user, "leave.can_decide_any_leave")
        response = self.client_for(self.manager_user).get(reverse("leave-pending"))
        self.assertNotIn(own.pk, [row["id"] for row in response.data])

    def test_detail_of_a_request_outside_your_reach_is_404(self):
        self.grant(self.other_head_user, "leave.can_decide_leave")
        response = self.client_for(self.other_head_user).get(
            reverse("leave-request-detail", args=[self.request.pk])
        )
        self.assertEqual(response.status_code, 404)


class DecisionAPITests(LeaveAPITestBase):
    def setUp(self):
        super().setUp()
        self.request = self._apply()
        self.grant(self.manager_user, "leave.can_decide_leave")

    def test_manager_approves(self):
        response = self.client_for(self.manager_user).post(
            reverse("leave-approve", args=[self.request.pk]),
            {"comment": "Fine"},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["status"], LeaveRequestStatus.APPROVED)

    def test_approval_projects_onto_an_existing_attendance_row(self):
        row = DailyAttendance.objects.create(
            employee=self.worker,
            date=WEDNESDAY,
            machine_status=AttendanceStatus.ABSENT,
            effective_status=AttendanceStatus.ABSENT,
        )
        self.client_for(self.manager_user).post(
            reverse("leave-approve", args=[self.request.pk]), {}, format="json"
        )
        row.refresh_from_db()
        self.assertEqual(row.effective_status, AttendanceStatus.ON_LEAVE)
        self.assertEqual(row.machine_status, AttendanceStatus.ABSENT)

    def test_somebody_outside_the_line_is_refused_with_an_explanation(self):
        self.grant(self.other_head_user, "leave.can_decide_leave")
        response = self.client_for(self.other_head_user).post(
            reverse("leave-approve", args=[self.request.pk]), {}, format="json"
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn("not this employee's approver", response.data["detail"])

    def test_skip_level_may_decide_and_is_recorded_as_such(self):
        self.grant(self.head_user, "leave.can_decide_leave")
        response = self.client_for(self.head_user).post(
            reverse("leave-approve", args=[self.request.pk]), {}, format="json"
        )
        self.assertEqual(response.status_code, 200)
        entry = self.request.trail.filter(action="APPROVED").first()
        self.assertEqual(entry.authority, "skip_level")

    def test_reject_without_a_reason_is_refused(self):
        response = self.client_for(self.manager_user).post(
            reverse("leave-reject", args=[self.request.pk]), {}, format="json"
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("reason is required", response.data["detail"])

    def test_partial_approval_through_the_api(self):
        multi = self._apply(
            employee=self.worker_two,
            to_date=WEDNESDAY + timedelta(days=2),
            applied_by=self.hr_user,
        )
        response = self.client_for(self.manager_user).post(
            reverse("leave-approve", args=[multi.pk]),
            {"only_dates": [str(WEDNESDAY)], "comment": "Only Wednesday"},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(str(response.data["total_days"]), "1.0")

    def test_withdraw_is_the_applicants_and_nobody_elses(self):
        refused = self.client_for(self.manager_user).post(
            reverse("leave-withdraw", args=[self.request.pk]), {}, format="json"
        )
        self.assertEqual(refused.status_code, 403)

        self.grant(self.worker_user, "leave.can_apply_leave")
        ok = self.client_for(self.worker_user).post(
            reverse("leave-withdraw", args=[self.request.pk]), {}, format="json"
        )
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.data["status"], LeaveRequestStatus.WITHDRAWN)

    def test_cancel_reverts_the_sheet_and_reports_how_much(self):
        row = DailyAttendance.objects.create(
            employee=self.worker,
            date=WEDNESDAY,
            machine_status=AttendanceStatus.ABSENT,
            effective_status=AttendanceStatus.ABSENT,
        )
        self.client_for(self.manager_user).post(
            reverse("leave-approve", args=[self.request.pk]), {}, format="json"
        )
        self.grant(
            self.hr_user, "leave.can_decide_any_leave", "leave.can_cancel_approved_leave"
        )
        response = self.client_for(self.hr_user).post(
            reverse("leave-cancel", args=[self.request.pk]),
            {"comment": "Shutdown moved"},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["attendance_reverted"], 1)
        row.refresh_from_db()
        self.assertEqual(row.effective_status, AttendanceStatus.ABSENT)

    def test_history_returns_the_trail(self):
        self.client_for(self.manager_user).post(
            reverse("leave-approve", args=[self.request.pk]), {}, format="json"
        )
        self.grant(self.worker_user, "leave.can_apply_leave")
        response = self.client_for(self.worker_user).get(
            reverse("leave-request-history", args=[self.request.pk])
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [row["action"] for row in response.data][::-1][:2], ["APPLIED", "APPROVED"]
        )


class MasterAPITests(LeaveAPITestBase):
    def test_anybody_in_the_module_may_read_the_types(self):
        self.grant(self.worker_user, "leave.can_apply_leave")
        response = self.client_for(self.worker_user).get(reverse("leave-type-list"))
        self.assertEqual(response.status_code, 200)
        codes = {row["code"] for row in response.data}
        self.assertEqual(codes, {"CL", "SL"})  # the inactive one is hidden

    def test_creating_a_type_needs_the_manage_grant(self):
        self.grant(self.worker_user, "leave.can_apply_leave")
        refused = self.client_for(self.worker_user).post(
            reverse("leave-type-list"), {"code": "EL", "name": "Earned"}, format="json"
        )
        self.assertEqual(refused.status_code, 403)

        self.grant(self.hr_user, "leave.can_manage_leave_types")
        ok = self.client_for(self.hr_user).post(
            reverse("leave-type-list"), {"code": "EL", "name": "Earned"}, format="json"
        )
        self.assertEqual(ok.status_code, 201, ok.data)

    def test_holidays_can_be_added_and_listed(self):
        self.grant(self.hr_user, "leave.can_manage_leave_types")
        client = self.client_for(self.hr_user)
        created = client.post(
            reverse("leave-holiday-list"),
            {"date": "2026-10-02", "name": "Gandhi Jayanti"},
            format="json",
        )
        self.assertEqual(created.status_code, 201, created.data)
        listed = client.get(reverse("leave-holiday-list"), {"year": 2026})
        self.assertEqual(len(listed.data), 1)

    def test_calendar_needs_a_window(self):
        self.grant(self.manager_user, "leave.can_view_team_leave")
        client = self.client_for(self.manager_user)
        self.assertEqual(client.get(reverse("leave-calendar")).status_code, 400)

        self._apply()
        response = client.get(
            reverse("leave-calendar"),
            {"from": str(WEDNESDAY), "to": str(WEDNESDAY + timedelta(days=7))},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 1)


class SetupGroupsTests(LeaveAPITestBase):
    def test_the_command_creates_four_groups_and_leaves_them_empty(self):
        from io import StringIO

        from django.contrib.auth.models import Group
        from django.core.management import call_command

        call_command("setup_leave_groups", stdout=StringIO())
        for name in (
            "Leave Applicant",
            "Leave Approver (Manager)",
            "Time Office",
            "Leave Administrator (HR)",
        ):
            group = Group.objects.get(name=name)
            self.assertGreater(group.permissions.count(), 0)
            self.assertEqual(group.user_set.count(), 0)

    def test_the_time_office_cannot_decide(self):
        from io import StringIO

        from django.contrib.auth.models import Group
        from django.core.management import call_command

        call_command("setup_leave_groups", stdout=StringIO())
        codenames = set(
            Group.objects.get(name="Time Office").permissions.values_list(
                "codename", flat=True
            )
        )
        self.assertIn("can_apply_leave_for_others", codenames)
        self.assertNotIn("can_decide_leave", codenames)
        self.assertNotIn("can_decide_any_leave", codenames)
