"""Phase 3: extend the budget according to need, and extend the timeline."""

from datetime import timedelta
from decimal import Decimal

from rest_framework import status

from construction_projects.constants import ProjectStatus, RevisionStatus

from .base import ConstructionTestCase


class RevisionRequestTests(ConstructionTestCase):
    def setUp(self):
        super().setUp()
        self.project = self.make_project()  # 800000, ends today + 50

    def test_asking_for_money_and_time_together(self):
        new_end = self.today + timedelta(days=110)
        response = self.post(
            f"projects/{self.project.id}/revisions/",
            {
                "additional_amount": "300000.00",
                "new_end_date": str(new_end),
                "reason": "Foundation depth revised after the soil test.",
            },
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        data = response.data
        self.assertEqual(data["revision_no"], 1)
        self.assertEqual(data["status"], RevisionStatus.PENDING)
        # The approver sees the before and the after, not a bare number.
        self.assertEqual(Decimal(data["budget_before"]), Decimal("800000.00"))
        self.assertEqual(Decimal(data["budget_after"]), Decimal("1100000.00"))
        self.assertEqual(data["end_date_before"], str(self.today + timedelta(days=50)))
        self.assertEqual(data["end_date_after"], str(new_end))
        self.assertEqual(data["extension_days"], 60)

    def test_money_only(self):
        response = self.post(
            f"projects/{self.project.id}/revisions/",
            {"additional_amount": "50000.00", "reason": "Steel price rose."},
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["extension_days"], 0)
        self.assertEqual(
            response.data["end_date_after"], str(self.today + timedelta(days=50))
        )

    def test_time_only(self):
        response = self.post(
            f"projects/{self.project.id}/revisions/",
            {
                "new_end_date": str(self.today + timedelta(days=80)),
                "reason": "Eleven days lost to rain.",
            },
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(Decimal(response.data["additional_amount"]), Decimal("0.00"))
        self.assertEqual(
            Decimal(response.data["budget_after"]), Decimal("800000.00")
        )

    def test_a_revision_must_ask_for_something(self):
        response = self.post(
            f"projects/{self.project.id}/revisions/", {"reason": "Just checking in."}
        )
        self.assertCode(response, "revision_asks_nothing")

    def test_the_new_end_date_must_be_later(self):
        """This form extends a timeline; pulling a date in is a different
        conversation."""
        response = self.post(
            f"projects/{self.project.id}/revisions/",
            {
                "new_end_date": str(self.today + timedelta(days=20)),
                "reason": "Going faster than planned.",
            },
        )
        self.assertCode(response, "new_end_date_not_later")

    def test_a_reason_is_always_required(self):
        response = self.post(
            f"projects/{self.project.id}/revisions/",
            {"additional_amount": "50000.00"},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("reason", response.data)

    def test_only_one_revision_may_be_pending(self):
        self.post(
            f"projects/{self.project.id}/revisions/",
            {"additional_amount": "50000.00", "reason": "First."},
        )
        response = self.post(
            f"projects/{self.project.id}/revisions/",
            {"additional_amount": "10000.00", "reason": "Second."},
        )
        self.assertCode(response, "revision_already_pending")

    def test_an_unapproved_project_is_edited_not_revised(self):
        draft = self.make_project(approved=False)
        response = self.post(
            f"projects/{draft.id}/revisions/",
            {"additional_amount": "10000.00", "reason": "More."},
        )
        self.assertCode(response, "project_not_approved")


class RevisionDecisionTests(ConstructionTestCase):
    def setUp(self):
        super().setUp()
        self.project = self.make_project()
        self.new_end = self.today + timedelta(days=110)
        self.revision_id = self.post(
            f"projects/{self.project.id}/revisions/",
            {
                "additional_amount": "300000.00",
                "new_end_date": str(self.new_end),
                "reason": "Foundation depth revised after the soil test.",
            },
        ).data["id"]

    def test_approving_raises_the_budget_and_moves_the_date(self):
        response = self.post(
            f"revisions/{self.revision_id}/approve/", {"note": "Agreed."}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], RevisionStatus.APPROVED)

        project = self.refreshed(self.project)
        self.assertEqual(project.sanctioned_budget, Decimal("1100000.00"))
        self.assertEqual(project.expected_end_date, self.new_end)
        # estimated_cost is the original ask and does not move.
        self.assertEqual(project.estimated_cost, Decimal("800000.00"))

    def test_rejecting_changes_nothing_on_the_project(self):
        original_end = self.project.expected_end_date
        response = self.post(
            f"revisions/{self.revision_id}/reject/", {"note": "Find it elsewhere."}
        )
        self.assertEqual(response.data["status"], RevisionStatus.REJECTED)

        project = self.refreshed(self.project)
        self.assertEqual(project.sanctioned_budget, Decimal("800000.00"))
        self.assertEqual(project.expected_end_date, original_end)

    def test_a_decided_revision_cannot_be_decided_again(self):
        self.post(f"revisions/{self.revision_id}/approve/")
        response = self.post(f"revisions/{self.revision_id}/reject/", {"note": "No."})
        self.assertCode(response, "revision_not_pending")

    def test_rejecting_without_a_reason_is_refused(self):
        """Refusing somebody's budget without saying why leaves them with
        nothing to act on."""
        response = self.post(f"revisions/{self.revision_id}/reject/")
        self.assertCode(response, "rejection_needs_a_reason")
        response = self.post(f"revisions/{self.revision_id}/reject/", {"note": "   "})
        self.assertCode(response, "rejection_needs_a_reason")

    def test_approving_needs_no_note(self):
        response = self.post(f"revisions/{self.revision_id}/approve/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_deciding_needs_the_approval_permission(self):
        manager = self.make_user(
            "pm3@example.com",
            "CON930",
            ["can_view_all_projects", "can_view_project", "can_create_project"],
        )
        self.client.force_authenticate(manager)
        response = self.post(f"revisions/{self.revision_id}/approve/")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_only_the_requester_may_withdraw(self):
        other = self.make_user(
            "pm4@example.com",
            "CON931",
            ["can_view_all_projects", "can_view_project", "can_create_project"],
        )
        self.client.force_authenticate(other)
        response = self.post(f"revisions/{self.revision_id}/withdraw/")
        self.assertCode(response, "not_requester")

        self.client.force_authenticate(self.user)
        response = self.post(f"revisions/{self.revision_id}/withdraw/")
        self.assertEqual(response.data["status"], RevisionStatus.WITHDRAWN)


class RevisionHistoryTests(ConstructionTestCase):
    def test_snapshots_hold_after_a_later_revision_lands(self):
        """Revision 1's "before" must still read 800000 once revision 2 has
        moved the budget on."""
        project = self.make_project()

        first = self.post(
            f"projects/{project.id}/revisions/",
            {"additional_amount": "300000.00", "reason": "Soil test."},
        ).data
        self.post(f"revisions/{first['id']}/approve/")

        second = self.post(
            f"projects/{project.id}/revisions/",
            {"additional_amount": "150000.00", "reason": "Steel price."},
        ).data
        self.post(f"revisions/{second['id']}/approve/")

        self.assertEqual(
            self.refreshed(project).sanctioned_budget, Decimal("1250000.00")
        )

        rows = {row["revision_no"]: row for row in self.get(
            f"projects/{project.id}/revisions/"
        ).data}
        self.assertEqual(Decimal(rows[1]["budget_before"]), Decimal("800000.00"))
        self.assertEqual(Decimal(rows[1]["budget_after"]), Decimal("1100000.00"))
        self.assertEqual(Decimal(rows[2]["budget_before"]), Decimal("1100000.00"))
        self.assertEqual(Decimal(rows[2]["budget_after"]), Decimal("1250000.00"))

    def test_a_rejected_revision_does_not_count_towards_the_budget(self):
        project = self.make_project()
        rejected = self.post(
            f"projects/{project.id}/revisions/",
            {"additional_amount": "300000.00", "reason": "Ask one."},
        ).data
        self.post(f"revisions/{rejected['id']}/reject/", {"note": "Too much."})

        approved = self.post(
            f"projects/{project.id}/revisions/",
            {"additional_amount": "100000.00", "reason": "Ask two, smaller."},
        ).data
        self.post(f"revisions/{approved['id']}/approve/")

        self.assertEqual(
            self.refreshed(project).sanctioned_budget, Decimal("900000.00")
        )


class OverBudgetToRevisionTests(ConstructionTestCase):
    def test_the_loop_closes(self):
        """Overspend, get flagged, account for it, ask for more, be back in budget.

        The middle step is the new one: spend has to be approved before more
        budget can be asked for, so the loop now runs through the batch rather
        than straight from the warning to the revision.
        """
        project = self.make_project()
        warning = self.post(
            f"projects/{project.id}/expenses/",
            {
                "spend_date": str(self.today),
                "category": "MATERIAL",
                "description": "everything",
                "amount": "842000.00",
            },
        ).data["warning"]
        self.assertEqual(warning["code"], "budget_exceeded")
        self.assertEqual(Decimal(warning["over_by"]), Decimal("42000.00"))

        # Asking now is refused: nobody has checked what was spent.
        blocked = self.post(
            f"projects/{project.id}/revisions/",
            {"additional_amount": warning["over_by"], "reason": "Cement went up."},
        )
        self.assertCode(blocked, "expenses_awaiting_approval")

        # Account for it: send the batch and have it approved.
        batch_id = self.get(f"projects/{project.id}/expense-batches/").data["open"]["id"]
        self.post(f"projects/{project.id}/expense-batches/")
        self.post(f"expense-batches/{batch_id}/decide/", {"decision": "APPROVED"})

        revision = self.post(
            f"projects/{project.id}/revisions/",
            {
                "additional_amount": warning["over_by"],
                "reason": "Cement went up mid-pour.",
            },
        ).data
        self.post(f"revisions/{revision['id']}/approve/")

        project = self.refreshed(project)
        self.assertFalse(project.is_over_budget)
        self.assertEqual(project.remaining_budget, Decimal("0.00"))


class ApprovalQueueTests(ConstructionTestCase):
    def test_the_queue_carries_projects_and_revisions(self):
        waiting = self.make_project(approved=False)
        self.post(f"projects/{waiting.id}/submit/")

        live = self.make_project()
        self.post(
            f"projects/{live.id}/revisions/",
            {"additional_amount": "10000.00", "reason": "A bit more."},
        )

        data = self.get("approvals/").data
        self.assertEqual([row["id"] for row in data["projects"]], [waiting.id])
        self.assertEqual(len(data["revisions"]), 1)
        self.assertEqual(data["revisions"][0]["project_code"], live.code)

    def test_the_queue_needs_the_approval_permission(self):
        viewer = self.make_user("v3@example.com", "CON940", ["can_view_project"])
        self.client.force_authenticate(viewer)
        response = self.get("approvals/")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
