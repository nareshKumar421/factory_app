"""
Phase 2 -- who may decide, and what a decision does.

    python manage.py test leave.tests_routing --settings=config.sqlite_test_settings

The tree these run against is in :mod:`leave.tests_base`::

    ceo ── head ── manager ── worker
                        └──── worker_two
        └── other_head ── other_worker
"""

from datetime import timedelta
from decimal import Decimal

from employee_hierarchy.constants import EmploymentStatus

from .constants import LeaveDayStatus, LeaveRequestStatus
from .models import LeaveRequest
from .routing import (
    AUTHORITY_HR,
    AUTHORITY_MANAGER,
    AUTHORITY_SKIP_LEVEL,
    authority_of,
    can_apply_for,
    can_cancel,
    can_decide,
    decidable_filter,
    responsible_manager,
    visible_filter,
)
from .services import LeaveRefused, apply_for_leave, approve, cancel, reject, withdraw
from .tests_base import WEDNESDAY, LeaveTestBase


class RoutingTestBase(LeaveTestBase):
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


class ResponsibleManagerTests(RoutingTestBase):
    def test_it_is_the_direct_manager(self):
        self.assertEqual(responsible_manager(self.worker), self.manager)

    def test_it_walks_past_a_manager_who_has_left(self):
        self.manager.employment_status = EmploymentStatus.RESIGNED
        self.manager.save(update_fields=["employment_status"])
        self.worker.refresh_from_db()
        self.assertEqual(responsible_manager(self.worker), self.head)

    def test_a_root_employee_falls_back_to_the_department_head(self):
        self.department.head = self.head
        self.department.save(update_fields=["head"])
        self.assertEqual(responsible_manager(self.ceo), self.head)

    def test_a_root_with_no_department_head_has_nobody(self):
        self.assertIsNone(responsible_manager(self.ceo))


class AuthorityTests(RoutingTestBase):
    def test_direct_manager_decides_as_manager(self):
        self.grant(self.manager_user, "leave.can_decide_leave")
        self.assertEqual(authority_of(self.manager_user, self.request), AUTHORITY_MANAGER)

    def test_skip_level_decides_as_skip_level(self):
        self.grant(self.head_user, "leave.can_decide_leave")
        self.assertEqual(
            authority_of(self.head_user, self.request), AUTHORITY_SKIP_LEVEL
        )

    def test_hr_decides_as_hr(self):
        self.grant(self.hr_user, "leave.can_decide_any_leave")
        self.assertEqual(authority_of(self.hr_user, self.request), AUTHORITY_HR)

    def test_the_permission_alone_is_not_enough(self):
        """A manager in another line holds the grant and still cannot act."""
        self.grant(self.other_head_user, "leave.can_decide_leave")
        self.assertIsNone(authority_of(self.other_head_user, self.request))

    def test_position_alone_is_not_enough(self):
        """The applicant's real manager, without the grant, cannot act."""
        self.assertIsNone(authority_of(self.manager_user, self.request))

    def test_nobody_approves_their_own_leave_even_as_hr(self):
        own = apply_for_leave(
            employee=self.head,
            leave_type=self.casual,
            from_date=WEDNESDAY,
            to_date=WEDNESDAY,
            reason="Mine",
            applied_by=self.head_user,
        )
        self.grant(self.head_user, "leave.can_decide_any_leave", "leave.can_decide_leave")
        self.assertIsNone(authority_of(self.head_user, own))

    def test_manager_authority_wins_over_hr_when_both_apply(self):
        """The more informative of the two entitlements is the one recorded."""
        self.grant(
            self.manager_user, "leave.can_decide_leave", "leave.can_decide_any_leave"
        )
        self.assertEqual(authority_of(self.manager_user, self.request), AUTHORITY_MANAGER)

    def test_a_login_with_no_employee_record_has_no_tree_position(self):
        self.grant(self.hr_user, "leave.can_decide_leave")
        self.assertIsNone(authority_of(self.hr_user, self.request))


class VisibilityTests(RoutingTestBase):
    def _visible(self, user):
        return set(
            LeaveRequest.objects.filter(visible_filter(user)).values_list("pk", flat=True)
        )

    def _decidable(self, user):
        return set(
            LeaveRequest.objects.filter(decidable_filter(user)).values_list("pk", flat=True)
        )

    def test_an_applicant_sees_their_own(self):
        self.assertEqual(self._visible(self.worker_user), {self.request.pk})

    def test_an_applicant_cannot_decide_their_own(self):
        self.grant(self.worker_user, "leave.can_decide_leave")
        self.assertEqual(self._decidable(self.worker_user), set())

    def test_a_manager_sees_and_can_decide_their_subtree(self):
        self.grant(self.manager_user, "leave.can_decide_leave")
        self.assertEqual(self._visible(self.manager_user), {self.request.pk})
        self.assertEqual(self._decidable(self.manager_user), {self.request.pk})

    def test_another_line_sees_nothing(self):
        self.grant(self.other_head_user, "leave.can_decide_leave")
        self.assertEqual(self._visible(self.other_head_user), set())
        self.assertEqual(self._decidable(self.other_head_user), set())

    def test_hr_sees_everything(self):
        self.grant(self.hr_user, "leave.can_decide_any_leave")
        self.assertEqual(self._visible(self.hr_user), {self.request.pk})

    def test_the_time_office_sees_what_it_raised(self):
        raised = apply_for_leave(
            employee=self.worker_two,
            leave_type=self.casual,
            from_date=WEDNESDAY,
            to_date=WEDNESDAY,
            reason="On their behalf",
            applied_by=self.hr_user,
        )
        self.assertIn(raised.pk, self._visible(self.hr_user))

    def test_view_team_grants_sight_without_the_right_to_decide(self):
        self.grant(self.manager_user, "leave.can_view_team_leave")
        self.assertEqual(self._visible(self.manager_user), {self.request.pk})
        self.assertEqual(self._decidable(self.manager_user), set())


class CanApplyForTests(RoutingTestBase):
    def test_you_may_apply_for_yourself(self):
        self.grant(self.worker_user, "leave.can_apply_leave")
        self.assertTrue(can_apply_for(self.worker_user, self.worker))

    def test_you_may_not_apply_for_a_colleague(self):
        self.grant(self.worker_user, "leave.can_apply_leave")
        self.assertFalse(can_apply_for(self.worker_user, self.worker_two))

    def test_the_time_office_may_apply_for_anybody(self):
        self.grant(self.hr_user, "leave.can_apply_leave_for_others")
        self.assertTrue(can_apply_for(self.hr_user, self.worker_two))


class DecisionTests(RoutingTestBase):
    def test_approve_marks_every_day_and_trails_the_authority(self):
        approve(self.request, user=self.manager_user, comment="Fine", authority=AUTHORITY_MANAGER)
        self.request.refresh_from_db()
        self.assertEqual(self.request.status, LeaveRequestStatus.APPROVED)
        self.assertEqual(self.request.decided_by, self.manager_user)
        self.assertTrue(
            all(day.status == LeaveDayStatus.APPROVED for day in self.request.days.all())
        )
        entry = self.request.trail.first()
        self.assertEqual(entry.action, "APPROVED")
        self.assertEqual(entry.authority, AUTHORITY_MANAGER)

    def test_partial_approval_splits_the_days_and_recounts(self):
        multi = apply_for_leave(
            employee=self.worker_two,
            leave_type=self.casual,
            from_date=WEDNESDAY,
            to_date=WEDNESDAY + timedelta(days=2),
            reason="Three days",
            applied_by=self.hr_user,
        )
        approve(
            multi,
            user=self.manager_user,
            only_dates=[WEDNESDAY, WEDNESDAY + timedelta(days=1)],
        )
        multi.refresh_from_db()
        self.assertEqual(multi.total_days, Decimal("2.0"))
        self.assertEqual(multi.days.filter(status=LeaveDayStatus.APPROVED).count(), 2)
        self.assertEqual(multi.days.filter(status=LeaveDayStatus.REJECTED).count(), 1)

    def test_partial_approval_of_a_date_not_in_the_request_is_refused(self):
        with self.assertRaisesMessage(LeaveRefused, "not part of this request"):
            approve(
                self.request,
                user=self.manager_user,
                only_dates=[WEDNESDAY + timedelta(days=30)],
            )

    def test_approving_no_dates_is_refused_as_a_disguised_rejection(self):
        with self.assertRaisesMessage(LeaveRefused, "is a rejection"):
            approve(self.request, user=self.manager_user, only_dates=[])

    def test_reject_requires_a_reason(self):
        with self.assertRaisesMessage(LeaveRefused, "reason is required"):
            reject(self.request, user=self.manager_user, comment="  ")

    def test_reject_zeroes_the_days_and_the_total(self):
        reject(self.request, user=self.manager_user, comment="Peak season")
        self.request.refresh_from_db()
        self.assertEqual(self.request.status, LeaveRequestStatus.REJECTED)
        self.assertEqual(self.request.total_days, Decimal("0"))
        self.assertTrue(
            all(day.status == LeaveDayStatus.REJECTED for day in self.request.days.all())
        )

    def test_a_decided_request_cannot_be_decided_again(self):
        approve(self.request, user=self.manager_user)
        with self.assertRaisesMessage(LeaveRefused, "already approved"):
            approve(self.request, user=self.manager_user)
        with self.assertRaisesMessage(LeaveRefused, "already approved"):
            reject(self.request, user=self.manager_user, comment="Changed my mind")

    def test_withdraw_frees_the_dates_again(self):
        withdraw(self.request, user=self.worker_user)
        self.request.refresh_from_db()
        self.assertEqual(self.request.status, LeaveRequestStatus.WITHDRAWN)
        # The date is free, so a fresh application for it succeeds.
        again = apply_for_leave(
            employee=self.worker,
            leave_type=self.casual,
            from_date=WEDNESDAY,
            to_date=WEDNESDAY,
            reason="Second thoughts",
            applied_by=self.worker_user,
        )
        self.assertEqual(again.days.count(), 1)

    def test_withdraw_only_works_before_a_decision(self):
        approve(self.request, user=self.manager_user)
        with self.assertRaises(LeaveRefused):
            withdraw(self.request, user=self.worker_user)

    def test_cancel_needs_an_approved_request_and_a_reason(self):
        with self.assertRaisesMessage(LeaveRefused, "Only an approved request"):
            cancel(self.request, user=self.hr_user, comment="No")
        approve(self.request, user=self.manager_user)
        with self.assertRaisesMessage(LeaveRefused, "reason is required"):
            cancel(self.request, user=self.hr_user, comment="")
        cancel(self.request, user=self.hr_user, comment="Plant shutdown moved")
        self.request.refresh_from_db()
        self.assertEqual(self.request.status, LeaveRequestStatus.CANCELLED)

    def test_can_cancel_needs_the_dedicated_grant(self):
        approve(self.request, user=self.manager_user)
        self.grant(self.manager_user, "leave.can_decide_leave")
        self.assertFalse(can_cancel(self.manager_user, self.request))
        self.grant(self.manager_user, "leave.can_cancel_approved_leave")
        self.assertTrue(can_cancel(self.manager_user, self.request))

    def test_every_transition_leaves_a_trail_entry(self):
        approve(self.request, user=self.manager_user, comment="ok")
        cancel(self.request, user=self.hr_user, comment="undo")
        actions = list(self.request.trail.order_by("id").values_list("action", flat=True))
        self.assertEqual(actions, ["APPLIED", "APPROVED", "CANCELLED"])

    def test_can_decide_is_the_public_predicate(self):
        self.grant(self.manager_user, "leave.can_decide_leave")
        self.assertTrue(can_decide(self.manager_user, self.request))
        self.assertFalse(can_decide(self.worker_user, self.request))
