"""Phase 1: the project exists, has a budget, and somebody approved it."""

import threading
from datetime import timedelta
from decimal import Decimal

from django.db import connection
from django.test import TransactionTestCase
from django.utils import timezone
from rest_framework import status

from company.models import Company
from construction_projects import services
from construction_projects.constants import ProjectStatus
from construction_projects.models import Project, ProjectSequence

from .base import ConstructionTestCase


class ProjectNumberingTests(TransactionTestCase):
    """Two people creating a project at the same moment must not collide."""

    def test_concurrent_creates_get_distinct_codes(self):
        company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        codes, errors = [], []

        def take_one():
            try:
                codes.append(ProjectSequence.next_code(company))
            except Exception as exc:  # pragma: no cover - a failure is the report
                errors.append(exc)
            finally:
                connection.close()

        threads = [threading.Thread(target=take_one) for _ in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(codes), 5)
        self.assertEqual(len(set(codes)), 5, f"duplicate codes handed out: {codes}")

    def test_code_is_per_company_and_year(self):
        oil = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        mart = Company.objects.create(name="Jivo Mart", code="JIVO_MART")
        year = timezone.localdate().year
        self.assertEqual(ProjectSequence.next_code(oil), f"PRJ-{year}-001")
        self.assertEqual(ProjectSequence.next_code(oil), f"PRJ-{year}-002")
        # A second company starts its own count, not shared with the first.
        self.assertEqual(ProjectSequence.next_code(mart), f"PRJ-{year}-001")


class ProjectCreateTests(ConstructionTestCase):
    def test_create_returns_a_generated_code(self):
        response = self.post("projects/", self.project_payload())
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(response.data["code"].startswith("PRJ-"))
        self.assertEqual(response.data["status"], ProjectStatus.DRAFT)
        # Nothing is sanctioned until somebody approves it.
        self.assertEqual(Decimal(response.data["sanctioned_budget"]), Decimal("0.00"))

    def test_end_date_before_start_is_refused(self):
        response = self.post(
            "projects/",
            self.project_payload(
                start_date=str(self.today),
                expected_end_date=str(self.today - timedelta(days=1)),
            ),
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertCode(response, "end_before_start")

    def test_creating_needs_the_permission(self):
        clerk = self.make_user("clerk@example.com", "CON900", ["can_view_project"])
        self.client.force_authenticate(clerk)
        response = self.post("projects/", self.project_payload())
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class ProjectApprovalTests(ConstructionTestCase):
    def test_submit_then_approve_sanctions_the_budget(self):
        project = self.make_project(approved=False)
        self.assertEqual(project.sanctioned_budget, Decimal("0.00"))

        response = self.post(f"projects/{project.id}/submit/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], ProjectStatus.PENDING_APPROVAL)

        response = self.post(
            f"projects/{project.id}/approve/", {"note": "Board agreed 12 Feb"}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], ProjectStatus.APPROVED)
        self.assertEqual(
            Decimal(response.data["sanctioned_budget"]), Decimal("800000.00")
        )
        project = self.refreshed(project)
        self.assertIsNotNone(project.sanctioned_at)
        self.assertEqual(project.decided_by_id, self.user.id)

    def test_approving_needs_the_approval_permission(self):
        project = self.make_project(approved=False)
        self.post(f"projects/{project.id}/submit/")

        manager = self.make_user(
            "pm@example.com",
            "CON901",
            ["can_view_all_projects", "can_view_project", "can_edit_project"],
        )
        self.client.force_authenticate(manager)
        response = self.post(f"projects/{project.id}/approve/")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_rejecting_a_project_without_a_reason_is_refused(self):
        project = self.make_project(approved=False)
        self.post(f"projects/{project.id}/submit/")
        response = self.post(f"projects/{project.id}/reject/")
        self.assertCode(response, "rejection_needs_a_reason")
        # The project is untouched by the refused rejection.
        self.assertEqual(
            self.refreshed(project).status, ProjectStatus.PENDING_APPROVAL
        )

    def test_a_draft_cannot_be_approved_without_being_submitted(self):
        project = self.make_project(approved=False)
        response = self.post(f"projects/{project.id}/approve/")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertCode(response, "not_pending_approval")

    def test_rejection_sends_it_back_and_sanctions_nothing(self):
        project = self.make_project(approved=False)
        self.post(f"projects/{project.id}/submit/")
        response = self.post(
            f"projects/{project.id}/reject/", {"note": "Get two more quotes"}
        )
        self.assertEqual(response.data["status"], ProjectStatus.REJECTED)
        project = self.refreshed(project)
        self.assertEqual(project.sanctioned_budget, Decimal("0.00"))
        # Nothing was sanctioned, but we still know who said no.
        self.assertIsNone(project.sanctioned_at)
        self.assertEqual(project.decided_by_id, self.user.id)
        self.assertIsNotNone(project.decided_at)
        # A rejected project is editable again, and re-submittable.
        response = self.post(f"projects/{project.id}/submit/")
        self.assertEqual(response.data["status"], ProjectStatus.PENDING_APPROVAL)


class ProjectEditTests(ConstructionTestCase):
    def test_a_draft_can_be_edited(self):
        project = self.make_project(approved=False)
        response = self.patch(
            f"projects/{project.id}/", {"estimated_cost": "950000.00"}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(Decimal(response.data["estimated_cost"]), Decimal("950000.00"))

    def test_an_approved_project_cannot_be_edited(self):
        """The budget and the dates move through a revision, not an edit."""
        project = self.make_project()
        response = self.patch(
            f"projects/{project.id}/", {"estimated_cost": "950000.00"}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertCode(response, "project_not_editable")
        self.assertEqual(
            self.refreshed(project).estimated_cost, Decimal("800000.00")
        )


class ProjectStatusTests(ConstructionTestCase):
    def test_hold_and_resume(self):
        project = self.make_project()
        response = self.post(f"projects/{project.id}/hold/", {"note": "Funds held"})
        self.assertEqual(response.data["status"], ProjectStatus.ON_HOLD)
        response = self.post(f"projects/{project.id}/resume/")
        # No logs yet, so it goes back to APPROVED rather than IN_PROGRESS.
        self.assertEqual(response.data["status"], ProjectStatus.APPROVED)

    def test_resume_only_from_hold(self):
        project = self.make_project()
        response = self.post(f"projects/{project.id}/resume/")
        self.assertCode(response, "not_on_hold")

    def test_complete_records_the_actual_end_date(self):
        project = self.make_project()
        response = self.post(
            f"projects/{project.id}/complete/", {"actual_end_date": str(self.today)}
        )
        self.assertEqual(response.data["status"], ProjectStatus.COMPLETED)
        self.assertEqual(response.data["actual_end_date"], str(self.today))

    def test_a_completed_project_is_closed_to_writes(self):
        project = self.make_project()
        self.post(f"projects/{project.id}/complete/")
        response = self.post(
            f"projects/{project.id}/expenses/",
            {
                "spend_date": str(self.today),
                "category": "MATERIAL",
                "description": "cement",
                "amount": "1000.00",
            },
        )
        self.assertCode(response, "project_closed")

    def test_cancelling_is_refused_once_money_has_been_spent(self):
        project = self.make_project()
        self.post(
            f"projects/{project.id}/expenses/",
            {
                "spend_date": str(self.today),
                "category": "MATERIAL",
                "description": "90 bags cement",
                "amount": "31500.00",
            },
        )
        response = self.post(f"projects/{project.id}/cancel/")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertCode(response, "cancel_with_spend")

    def test_cancelling_a_draft_never_sanctions_its_budget(self):
        """The regression behind keying the sanction on ``sanctioned_at``
        rather than on status: CANCELLED would otherwise look like a status a
        sanctioned project can hold, and a cancelled draft would acquire a
        budget it was never granted."""
        project = self.make_project(approved=False)
        self.post(f"projects/{project.id}/cancel/")
        project = self.refreshed(project)
        self.assertEqual(project.status, ProjectStatus.CANCELLED)
        self.assertIsNone(project.sanctioned_at)
        from construction_projects import services

        services.recompute_totals(project)
        self.assertEqual(self.refreshed(project).sanctioned_budget, Decimal("0.00"))

    def test_an_untouched_project_can_be_cancelled(self):
        project = self.make_project()
        response = self.post(f"projects/{project.id}/cancel/", {"note": "Dropped"})
        self.assertEqual(response.data["status"], ProjectStatus.CANCELLED)


class ProjectVisibilityTests(ConstructionTestCase):
    def test_another_companys_project_is_not_readable(self):
        other = Company.objects.create(name="Jivo Mart", code="JIVO_MART")
        theirs = self.make_project(company=other)
        response = self.get(f"projects/{theirs.id}/")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_a_stranger_without_view_all_cannot_see_it(self):
        """404 rather than 403: a project you may not see should not be
        distinguishable from one that does not exist."""
        project = self.make_project()
        stranger = self.make_user(
            "stranger@example.com", "CON902", ["can_view_project"]
        )
        self.client.force_authenticate(stranger)
        response = self.get(f"projects/{project.id}/")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_the_site_incharge_can_see_their_own_project(self):
        incharge = self.make_user(
            "incharge@example.com", "CON903", ["can_view_project"]
        )
        project = self.make_project(site_incharge=incharge)
        self.client.force_authenticate(incharge)
        response = self.get(f"projects/{project.id}/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["id"], project.id)

    def test_the_list_is_scoped_to_what_you_may_see(self):
        mine = self.make_project()
        other_manager = self.make_user("pm2@example.com", "CON904", [])
        theirs = self.make_project(manager=other_manager, site_incharge=None)

        viewer = self.make_user("viewer@example.com", "CON905", ["can_view_project"])
        self.client.force_authenticate(viewer)
        response = self.get("projects/")
        self.assertEqual(response.data, [])

        self.client.force_authenticate(self.user)
        ids = {row["id"] for row in self.get("projects/").data}
        self.assertEqual(ids, {mine.id, theirs.id})  # this user has view_all


class ProjectFilterTests(ConstructionTestCase):
    def test_over_budget_and_overdue_filters(self):
        healthy = self.make_project()
        overspent = self.make_project(name="Overspent")
        self.post(
            f"projects/{overspent.id}/expenses/",
            {
                "spend_date": str(self.today),
                "category": "MATERIAL",
                "description": "everything",
                "amount": "900000.00",
            },
        )
        late = self.make_project(
            name="Late",
            start_date=self.today - timedelta(days=100),
            expected_end_date=self.today - timedelta(days=5),
        )

        ids = {row["id"] for row in self.get("projects/", over_budget="true").data}
        self.assertEqual(ids, {overspent.id})

        ids = {row["id"] for row in self.get("projects/", overdue="true").data}
        self.assertEqual(ids, {late.id})

        ids = {row["id"] for row in self.get("projects/", search="Overspent").data}
        self.assertEqual(ids, {overspent.id})

        self.assertIn(healthy.id, {row["id"] for row in self.get("projects/").data})

    def test_the_status_filter_drives_the_register_tabs(self):
        """Each tab of the register sends a comma-separated list of statuses.

        The view once read every parameter except this one, so all four tabs
        returned the same projects and *Finished* listed work that was still
        running. Each tab is asserted separately because they only look right
        together -- three tabs agreeing is exactly what the bug looked like.
        """
        live = self.make_project(name="Live one")
        draft = self.make_project(name="Draft one", approved=False)
        finished = self.make_project(name="Finished one")
        services.complete_project(finished, user=self.user)

        def ids(**params):
            return {row["id"] for row in self.get("projects/", **params).data}

        self.assertEqual(ids(status="APPROVED,IN_PROGRESS,ON_HOLD"), {live.id})
        self.assertEqual(ids(status="DRAFT,PENDING_APPROVAL,REJECTED"), {draft.id})
        self.assertEqual(ids(status="COMPLETED,CANCELLED"), {finished.id})
        self.assertEqual(ids(), {live.id, draft.id, finished.id})

    def test_a_status_that_is_not_one_matches_nothing(self):
        """Not everything. A filter that quietly returns the whole register
        reads as though it worked, which is how the missing filter went
        unnoticed in the first place."""
        self.make_project()
        self.assertEqual(self.get("projects/", status="NOT_A_STATUS").data, [])

    def test_the_project_register_has_no_batch_filter(self):
        """``batch_status`` and ``batch`` were copied here from the expenses
        view, where ``Expense.batch`` is a real column. ``Project`` has no such
        field, so both raised FieldError -- a 500 for anyone who passed them.
        They are gone; the parameters are now simply ignored."""
        project = self.make_project()
        response = self.get("projects/", batch_status="OPEN", batch="1")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual({row["id"] for row in response.data}, {project.id})


class ProjectSummaryTests(ConstructionTestCase):
    def test_summary_reports_the_header_figures(self):
        project = self.make_project()
        self.post(
            f"projects/{project.id}/expenses/",
            {
                "spend_date": str(self.today),
                "category": "MATERIAL",
                "description": "90 bags cement",
                "amount": "200000.00",
            },
        )
        data = self.get(f"projects/{project.id}/summary/").data
        self.assertEqual(Decimal(data["sanctioned_budget"]), Decimal("800000.00"))
        self.assertEqual(Decimal(data["spent_amount"]), Decimal("200000.00"))
        self.assertEqual(Decimal(data["remaining"]), Decimal("600000.00"))
        self.assertEqual(Decimal(data["percent_used"]), Decimal("25.00"))
        self.assertFalse(data["is_over_budget"])
        self.assertEqual(Decimal(data["spent_today"]), Decimal("200000.00"))
        self.assertEqual(data["days_left"], 50)
        self.assertFalse(data["is_overdue"])
        self.assertIsNone(data["last_log_date"])
        self.assertIsNone(data["days_since_last_log"])


class DraftCompletenessTests(ConstructionTestCase):
    """A draft is a form somebody started; a submission is a request.

    The columns used to enforce completeness, which meant a project could not
    be parked half-filled. The requirement moved to ``submit_project``, so
    these tests hold both ends: almost nothing is needed to save, and
    everything is needed to send.
    """

    def test_a_draft_can_be_saved_with_almost_nothing_on_it(self):
        response = self.post("projects/", {"name": "Shed, someday"})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["status"], ProjectStatus.DRAFT)
        self.assertEqual(response.data["name"], "Shed, someday")
        self.assertIsNone(response.data["start_date"])
        self.assertIsNone(response.data["expected_end_date"])
        self.assertIsNone(response.data["estimated_cost"])
        self.assertIsNone(response.data["manager"])

    def test_a_draft_can_be_saved_with_nothing_on_it_at_all(self):
        """The register still has to be able to show it, which means the date
        and budget properties have to answer rather than raise."""
        response = self.post("projects/", {})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        listed = self.get("projects/").data
        row = next(r for r in listed if r["id"] == response.data["id"])
        self.assertIsNone(row["days_left"])
        self.assertFalse(row["is_overdue"])

    def test_an_incomplete_draft_cannot_be_sent_for_approval(self):
        project = self.make_project(approved=False, name="Half a plan")
        Project.objects.filter(pk=project.id).update(
            start_date=None, expected_end_date=None, estimated_cost=None, manager=None
        )
        response = self.post(f"projects/{project.id}/submit/")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["code"], "incomplete")
        self.assertEqual(
            response.data["context"]["missing"],
            [
                "when it starts",
                "when it is expected to finish",
                "the budget being asked for",
                "who runs it",
            ],
        )

    def test_the_missing_list_names_only_what_is_missing(self):
        project = self.make_project(approved=False)
        Project.objects.filter(pk=project.id).update(estimated_cost=None)
        response = self.post(f"projects/{project.id}/submit/")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            response.data["context"]["missing"], ["the budget being asked for"]
        )

    def test_a_complete_draft_still_submits(self):
        project = self.make_project(approved=False)
        response = self.post(f"projects/{project.id}/submit/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], ProjectStatus.PENDING_APPROVAL)

    def test_the_date_order_is_still_checked_at_submit(self):
        """`_validate_dates` skips a draft holding one date or neither, so the
        check has to run again when both are finally there."""
        project = self.make_project(approved=False)
        Project.objects.filter(pk=project.id).update(
            expected_end_date=self.today - timedelta(days=30)
        )
        response = self.post(f"projects/{project.id}/submit/")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["code"], "end_before_start")

    def test_a_draft_may_be_filled_in_a_field_at_a_time(self):
        created = self.post("projects/", {"name": "Slowly"})
        pid = created.data["id"]
        self.patch(f"projects/{pid}/", {"start_date": str(self.today)})
        self.patch(f"projects/{pid}/", {"estimated_cost": "500000.00"})
        response = self.post(f"projects/{pid}/submit/")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            response.data["context"]["missing"],
            ["when it is expected to finish", "who runs it"],
        )


class SubmitPermissionTests(ConstructionTestCase):
    """Raising a project and sending it on are different rights.

    ``ProjectSubmitAPI`` gates on ``can_edit_project`` while creation gates on
    ``can_create_project``. The project form is built around that split -- it
    offers "Create & send for approval" only to somebody holding the edit
    right, because otherwise the send would 403 *after* the project already
    existed. Nothing asserted the split until now.
    """

    def test_creating_does_not_let_you_send_for_approval(self):
        raiser = self.make_user(
            "raiser@example.com", "CON960", ["can_view_project", "can_create_project"]
        )
        self.client.force_authenticate(raiser)

        created = self.post("projects/", self.project_payload())
        self.assertEqual(created.status_code, status.HTTP_201_CREATED)

        response = self.post(f"projects/{created.data['id']}/submit/")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_the_edit_right_is_what_sends_it(self):
        editor = self.make_user(
            "editor@example.com",
            "CON961",
            ["can_view_project", "can_create_project", "can_edit_project"],
        )
        self.client.force_authenticate(editor)

        # Named as the manager, because without ``can_view_all_projects`` a
        # project belonging to somebody else is invisible to them -- see
        # ``test_raising_a_project_for_somebody_else_hides_it_from_you``.
        created = self.post(
            "projects/", self.project_payload(manager=editor.id, site_incharge=editor.id)
        )
        self.assertEqual(created.status_code, status.HTTP_201_CREATED)
        response = self.post(f"projects/{created.data['id']}/submit/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], ProjectStatus.PENDING_APPROVAL)

    def test_a_viewer_cannot_raise_a_project_at_all(self):
        viewer = self.make_user("ro@example.com", "CON962", ["can_view_project"])
        self.client.force_authenticate(viewer)
        response = self.post("projects/", self.project_payload())
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_raising_a_project_for_somebody_else_hides_it_from_you(self):
        """A consequence of the visibility rule worth stating out loud.

        ``visible_projects`` shows a caller without ``can_view_all_projects``
        only what they manage or are site in-charge of. So somebody who may
        create a project, but names another person as its manager, cannot see
        the project they just created -- and gets a 404, not a 403, because a
        project you cannot see must not be distinguishable from one that does
        not exist.

        This is the designed model rather than a defect, and the fix for
        anybody it bites is ``can_view_all_projects``. It is pinned here so
        that changing it is a decision instead of an accident.
        """
        raiser = self.make_user(
            "raiser2@example.com",
            "CON963",
            ["can_view_project", "can_create_project", "can_edit_project"],
        )
        self.client.force_authenticate(raiser)

        created = self.post("projects/", self.project_payload())
        self.assertEqual(created.status_code, status.HTTP_201_CREATED)

        self.assertEqual(
            self.get(f"projects/{created.data['id']}/").status_code,
            status.HTTP_404_NOT_FOUND,
        )
        self.assertEqual(self.get("projects/").data, [])
