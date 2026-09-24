"""
Phase 5 -- balances and notifications.

    python manage.py test leave.tests_balance --settings=config.sqlite_test_settings
"""

from datetime import timedelta
from decimal import Decimal

from django.urls import reverse

from notifications.models import Notification

from .balance import balance_for, balances_for
from .constants import DayPortion
from .services import apply_for_leave, approve, reject
from .tests_api import LeaveAPITestBase
from .tests_base import WEDNESDAY, LeaveTestBase

YEAR = WEDNESDAY.year


class BalanceTests(LeaveTestBase):
    def _apply(self, **kwargs):
        params = {
            "employee": self.worker,
            "leave_type": self.casual,
            "from_date": WEDNESDAY,
            "to_date": WEDNESDAY,
            "reason": "Reason",
            "applied_by": self.worker_user,
        }
        params.update(kwargs)
        return apply_for_leave(**params)

    def test_a_fresh_employee_has_the_full_quota(self):
        position = balance_for(self.worker, self.casual, YEAR)
        self.assertEqual(position["quota"], Decimal("12"))
        self.assertEqual(position["used"], Decimal("0"))
        self.assertEqual(position["pending"], Decimal("0"))
        self.assertEqual(position["available"], Decimal("12"))

    def test_a_pending_request_counts_as_pending_not_used(self):
        self._apply()
        position = balance_for(self.worker, self.casual, YEAR)
        self.assertEqual(position["used"], Decimal("0"))
        self.assertEqual(position["pending"], Decimal("1.0"))
        # Not yet spent -- the figure must not drop until somebody approves.
        self.assertEqual(position["available"], Decimal("12"))

    def test_approval_moves_days_from_pending_to_used(self):
        request = self._apply()
        approve(request, user=self.manager_user)
        position = balance_for(self.worker, self.casual, YEAR)
        self.assertEqual(position["used"], Decimal("1.0"))
        self.assertEqual(position["pending"], Decimal("0"))
        self.assertEqual(position["available"], Decimal("11"))

    def test_a_rejection_costs_nothing(self):
        request = self._apply()
        reject(request, user=self.manager_user, comment="Peak season")
        position = balance_for(self.worker, self.casual, YEAR)
        self.assertEqual(position["used"], Decimal("0"))
        self.assertEqual(position["pending"], Decimal("0"))
        self.assertEqual(position["available"], Decimal("12"))

    def test_a_half_day_costs_half(self):
        request = self._apply(portion=DayPortion.FIRST_HALF)
        approve(request, user=self.manager_user)
        position = balance_for(self.worker, self.casual, YEAR)
        self.assertEqual(position["used"], Decimal("0.5"))
        self.assertEqual(position["available"], Decimal("11.5"))

    def test_a_partial_approval_only_costs_the_approved_days(self):
        request = self._apply(to_date=WEDNESDAY + timedelta(days=2))
        approve(request, user=self.manager_user, only_dates=[WEDNESDAY])
        position = balance_for(self.worker, self.casual, YEAR)
        self.assertEqual(position["used"], Decimal("1.0"))

    def test_an_untracked_type_reports_no_quota_and_no_available(self):
        unpaid = self.casual
        unpaid.pk = None
        unpaid.code = "LWP"
        unpaid.annual_quota = 0
        unpaid.save()

        position = balance_for(self.worker, unpaid, YEAR)
        self.assertFalse(position["tracked"])
        self.assertIsNone(position["quota"])
        self.assertIsNone(position["available"])

    def test_available_never_goes_negative(self):
        """An overshoot is only reachable deliberately, and still reads as 0."""
        self.casual.annual_quota = 1
        self.casual.save(update_fields=["annual_quota"])
        request = self._apply(to_date=WEDNESDAY + timedelta(days=2), allow_overdraw=True)
        approve(request, user=self.manager_user)

        position = balance_for(self.worker, self.casual, YEAR)
        self.assertEqual(position["used"], Decimal("3.0"))
        self.assertEqual(position["available"], Decimal("0"), "an overshoot is not a negative")

    def test_another_year_is_a_separate_balance(self):
        request = self._apply()
        approve(request, user=self.manager_user)
        self.assertEqual(balance_for(self.worker, self.casual, YEAR - 1)["used"], Decimal("0"))

    def test_one_persons_leave_does_not_touch_anothers(self):
        request = self._apply()
        approve(request, user=self.manager_user)
        self.assertEqual(balance_for(self.worker_two, self.casual, YEAR)["used"], Decimal("0"))

    def test_balances_for_lists_every_active_type_only(self):
        rows = balances_for(self.worker, YEAR)
        codes = {row["leave_type_code"] for row in rows}
        self.assertEqual(codes, {"CL", "SL"})


class BalanceAPITests(LeaveAPITestBase):
    def test_your_own_balance(self):
        self.grant(self.worker_user, "leave.can_apply_leave")
        response = self.client_for(self.worker_user).get(reverse("leave-balance"))
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["employee_code"], self.worker.employee_code)
        self.assertEqual(len(response.data["balances"]), 2)

    def test_a_manager_may_read_their_reports_balance(self):
        self.grant(self.manager_user, "leave.can_view_team_leave")
        response = self.client_for(self.manager_user).get(
            reverse("leave-balance"), {"employee": self.worker.pk}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["employee"], self.worker.pk)

    def test_an_unrelated_employee_may_not(self):
        self.grant(self.other_head_user, "leave.can_apply_leave")
        response = self.client_for(self.other_head_user).get(
            reverse("leave-balance"), {"employee": self.worker.pk}
        )
        self.assertEqual(response.status_code, 403)

    def test_an_unlinked_login_gets_an_explanation(self):
        self.grant(self.hr_user, "leave.can_apply_leave")
        response = self.client_for(self.hr_user).get(reverse("leave-balance"))
        self.assertEqual(response.status_code, 400)
        self.assertIn("not linked to an employee record", response.data["detail"])

    def test_a_bad_year_is_refused(self):
        self.grant(self.worker_user, "leave.can_apply_leave")
        response = self.client_for(self.worker_user).get(
            reverse("leave-balance"), {"year": "soon"}
        )
        self.assertEqual(response.status_code, 400)


class NotificationTests(LeaveAPITestBase):
    def test_applying_notifies_the_approver(self):
        self.grant(self.worker_user, "leave.can_apply_leave")
        self.client_for(self.worker_user).post(
            reverse("leave-request-list"),
            {
                "leave_type": self.casual.pk,
                "from_date": str(WEDNESDAY),
                "to_date": str(WEDNESDAY),
                "reason": "Family function",
            },
            format="json",
        )
        note = Notification.objects.filter(recipient=self.manager_user).first()
        self.assertIsNotNone(note, "the reporting manager should have been told")
        self.assertEqual(note.notification_type, "LEAVE_REQUESTED")
        self.assertEqual(note.reference_type, "leave_request")
        self.assertIn("Worker", note.body)
        # Leave lives under Organisation in the frontend now.
        self.assertEqual(note.click_action_url, "/organization/leave/approvals")

    def test_a_decision_notifies_the_applicant(self):
        request = apply_for_leave(
            employee=self.worker,
            leave_type=self.casual,
            from_date=WEDNESDAY,
            to_date=WEDNESDAY,
            reason="Family function",
            applied_by=self.worker_user,
        )
        self.grant(self.manager_user, "leave.can_decide_leave")
        self.client_for(self.manager_user).post(
            reverse("leave-approve", args=[request.pk]), {}, format="json"
        )
        note = Notification.objects.filter(
            recipient=self.worker_user, notification_type="LEAVE_DECIDED"
        ).first()
        self.assertIsNotNone(note)
        self.assertIn("Approved", note.body)
        self.assertEqual(note.click_action_url, "/organization/leave")

    def test_an_applicant_with_no_login_notifies_whoever_raised_it(self):
        self.grant(self.hr_user, "leave.can_apply_leave_for_others")
        self.grant(self.manager_user, "leave.can_decide_leave")
        request = apply_for_leave(
            employee=self.worker_two,  # no login
            leave_type=self.casual,
            from_date=WEDNESDAY,
            to_date=WEDNESDAY,
            reason="Called in by phone",
            applied_by=self.hr_user,
        )
        self.client_for(self.manager_user).post(
            reverse("leave-approve", args=[request.pk]), {}, format="json"
        )
        self.assertTrue(
            Notification.objects.filter(
                recipient=self.hr_user, notification_type="LEAVE_DECIDED"
            ).exists(),
            "the time office should hear the answer for somebody who cannot",
        )

    def test_a_missing_approver_login_is_not_an_error(self):
        """The ceo has no manager, so there may be nobody to notify."""
        self.grant(self.ceo_user, "leave.can_apply_leave")
        response = self.client_for(self.ceo_user).post(
            reverse("leave-request-list"),
            {
                "leave_type": self.casual.pk,
                "from_date": str(WEDNESDAY),
                "to_date": str(WEDNESDAY),
                "reason": "Mine",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
