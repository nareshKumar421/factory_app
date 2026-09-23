"""Payments are approved in batches, not one at a time.

Lines pile into the project's open batch as the site records them; the site
sends the batch; one decision settles every line in it. That matches how a site
is actually reviewed — "this week's spend is fine" — rather than making somebody
click through a hundred cement bills.

Two rules follow from it and are the point of this file:

* an approval covers **every** line in the batch, and
* **no revision may be raised while any spend is unapproved** — sanctioning
  fresh budget on top of unchecked spend is how a project quietly doubles.
"""

from decimal import Decimal

from rest_framework import status

from construction_projects.constants import ExpenseBatchStatus

from .base import ALL_PERMISSIONS, ConstructionTestCase


def spend(day, amount, **extra):
    payload = {
        "spend_date": str(day),
        "category": "MATERIAL",
        "description": "90 bags cement",
        "amount": str(amount),
    }
    payload.update(extra)
    return payload


class ExpenseBatchTests(ConstructionTestCase):
    def setUp(self):
        super().setUp()
        self.project = self.make_project()  # sanctioned 800000

    def _record(self, amount="10000.00", **extra):
        return self.post(
            f"projects/{self.project.id}/expenses/", spend(self.today, amount, **extra)
        ).data["expense"]

    def _batches(self):
        return self.get(f"projects/{self.project.id}/expense-batches/").data

    def _submit(self, expense_ids=None):
        payload = {"expense_ids": expense_ids} if expense_ids is not None else {}
        return self.post(f"projects/{self.project.id}/expense-batches/", payload)

    def _decide(self, batch_id, decision, note=""):
        return self.post(
            f"expense-batches/{batch_id}/decide/", {"decision": decision, "note": note}
        )

    # -- piling up ---------------------------------------------------------

    def test_the_first_expense_opens_a_batch(self):
        self.assertIsNone(self._batches()["open"])
        self._record()
        open_batch = self._batches()["open"]
        self.assertEqual(open_batch["batch_no"], 1)
        self.assertEqual(open_batch["status"], ExpenseBatchStatus.OPEN)
        self.assertEqual(open_batch["line_count"], 1)

    def test_every_expense_joins_the_same_open_batch(self):
        """"Piling into one approval" is the whole idea."""
        for _ in range(4):
            self._record("2500.00")
        open_batch = self._batches()["open"]
        self.assertEqual(len(self._batches()["batches"]), 1)
        self.assertEqual(open_batch["line_count"], 4)
        self.assertEqual(Decimal(open_batch["total"]), Decimal("10000.00"))

    def test_unapproved_spend_still_counts_against_the_budget(self):
        self._record("842000.00")
        project = self.refreshed(self.project)
        self.assertEqual(project.spent_amount, Decimal("842000.00"))
        self.assertTrue(project.is_over_budget)

    # -- one decision for the lot ------------------------------------------

    def test_approving_the_batch_approves_every_line_in_it(self):
        for _ in range(3):
            self._record("5000.00")
        batch_id = self._batches()["open"]["id"]
        self._submit()

        response = self._decide(batch_id, "APPROVED", "Week checked.")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["batch"]["status"], ExpenseBatchStatus.APPROVED)
        self.assertEqual(response.data["batch"]["line_count"], 3)

        summary = response.data["summary"]
        self.assertEqual(Decimal(summary["approved_amount"]), Decimal("15000.00"))
        self.assertEqual(Decimal(summary["pending_amount"]), Decimal("0.00"))
        self.assertTrue(summary["can_request_revision"])

    def test_a_settled_batch_closes_and_the_next_expense_opens_a_new_one(self):
        self._record()
        first = self._batches()["open"]["id"]
        self._submit()
        self._decide(first, "APPROVED")
        self.assertIsNone(self._batches()["open"])

        self._record("3000.00")
        open_batch = self._batches()["open"]
        self.assertEqual(open_batch["batch_no"], 2)
        self.assertNotEqual(open_batch["id"], first)

    # -- sending only some of them ----------------------------------------

    def test_only_the_picked_payments_are_sent(self):
        """A site often has one bill it is still chasing and should not have to
        hold the rest back for it."""
        keep = self._record("1000.00")
        send_a = self._record("2000.00")
        send_b = self._record("3000.00")
        batch_id = self._batches()["open"]["id"]

        response = self._submit([send_a["id"], send_b["id"]])
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["id"], batch_id, "the sent batch keeps its identity")
        self.assertEqual(response.data["line_count"], 2)
        self.assertEqual(Decimal(response.data["total"]), Decimal("5000.00"))

        # What was held back is still collecting, in a batch of its own.
        open_batch = self._batches()["open"]
        self.assertIsNotNone(open_batch)
        self.assertNotEqual(open_batch["id"], batch_id)
        self.assertEqual(open_batch["line_count"], 1)
        self.assertEqual(Decimal(open_batch["total"]), Decimal("1000.00"))
        self.assertEqual(open_batch["expenses"][0]["id"], keep["id"])

    def test_the_held_back_payments_stay_editable(self):
        keep = self._record("1000.00")
        send = self._record("2000.00")
        self._submit([send["id"]])
        response = self.patch(
            f"expenses/{keep['id']}/", spend(self.today, "1500.00")
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_the_sent_payments_are_locked(self):
        keep = self._record("1000.00")
        send = self._record("2000.00")
        self._submit([send["id"]])
        self.assertCode(
            self.patch(f"expenses/{send['id']}/", spend(self.today, "1.00")),
            "batch_not_editable",
        )
        self.assertEqual(keep["id"], keep["id"])

    def test_sending_all_of_them_leaves_no_open_batch(self):
        one = self._record("1000.00")
        two = self._record("2000.00")
        self._submit([one["id"], two["id"]])
        self.assertIsNone(self._batches()["open"])

    def test_batch_numbers_still_read_in_the_order_claims_were_made(self):
        self._record("1000.00")
        send = self._record("2000.00")
        first = self._batches()["open"]["batch_no"]
        self._submit([send["id"]])
        self.assertEqual(self._batches()["open"]["batch_no"], first + 1)

    def test_an_empty_pick_is_refused(self):
        self._record("1000.00")
        response = self.post(
            f"projects/{self.project.id}/expense-batches/", {"expense_ids": []}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_ids_from_another_project_send_nothing(self):
        self._record("1000.00")
        other = self.make_project(name="Other")
        theirs = self.post(
            f"projects/{other.id}/expenses/", spend(self.today, "9000.00")
        ).data["expense"]
        self.assertCode(self._submit([theirs["id"]]), "no_payments_chosen")

    def test_an_empty_batch_cannot_be_sent(self):
        self.assertCode(self._submit(), "batch_is_empty")

    def test_a_submitted_batch_is_locked_to_the_site(self):
        expense = self._record()
        self._submit()
        response = self.patch(f"expenses/{expense['id']}/", spend(self.today, "1.00"))
        self.assertCode(response, "batch_not_editable")
        self.assertCode(self.delete(f"expenses/{expense['id']}/"), "batch_not_editable")

    def test_a_returned_batch_goes_back_to_the_site_to_correct(self):
        """A return is not a disallowance — nothing is un-spent."""
        expense = self._record()
        batch_id = self._batches()["open"]["id"]
        self._submit()
        response = self._decide(batch_id, "RETURNED", "Where is the bill for line 1?")
        self.assertEqual(response.data["batch"]["status"], ExpenseBatchStatus.RETURNED)

        # Still the open batch, and editable again.
        self.assertEqual(self._batches()["open"]["id"], batch_id)
        self.assertEqual(
            self.patch(
                f"expenses/{expense['id']}/", spend(self.today, "9000.00")
            ).status_code,
            status.HTTP_200_OK,
        )
        # The money never stopped counting.
        self.assertEqual(self.refreshed(self.project).spent_amount, Decimal("9000.00"))

    def test_a_returned_batch_can_be_sent_again(self):
        self._record()
        batch_id = self._batches()["open"]["id"]
        self._submit()
        self._decide(batch_id, "RETURNED", "Fix it.")
        self._record("500.00")  # the correction joins the same batch
        self.assertEqual(self._submit().status_code, status.HTTP_200_OK)
        self.assertEqual(self._batches()["batches"][0]["line_count"], 2)

    def test_returning_without_a_reason_is_refused(self):
        self._record()
        batch_id = self._batches()["open"]["id"]
        self._submit()
        self.assertCode(self._decide(batch_id, "RETURNED"), "rejection_needs_a_reason")

    def test_only_a_submitted_batch_can_be_decided(self):
        self._record()
        batch_id = self._batches()["open"]["id"]
        self.assertCode(self._decide(batch_id, "APPROVED"), "batch_not_submitted")

    def test_an_approved_batch_cannot_be_decided_again(self):
        self._record()
        batch_id = self._batches()["open"]["id"]
        self._submit()
        self._decide(batch_id, "APPROVED")
        self.assertCode(self._decide(batch_id, "APPROVED"), "batch_not_submitted")


class RevisionBlockedByUnapprovedSpendTests(ConstructionTestCase):
    """No more budget until the last lot is accounted for."""

    def setUp(self):
        super().setUp()
        self.project = self.make_project()

    def _ask_for_more(self):
        return self.post(
            f"projects/{self.project.id}/revisions/",
            {"additional_amount": "100000.00", "reason": "Steel price rose."},
        )

    def test_a_revision_is_refused_while_spend_is_unapproved(self):
        self.post(
            f"projects/{self.project.id}/expenses/", spend(self.today, "5000.00")
        )
        response = self._ask_for_more()
        self.assertCode(response, "expenses_awaiting_approval")
        self.assertEqual(
            Decimal(response.data["context"]["unapproved_total"]), Decimal("5000.00")
        )

    def test_a_revision_is_refused_while_a_batch_sits_with_the_approver(self):
        self.post(
            f"projects/{self.project.id}/expenses/", spend(self.today, "5000.00")
        )
        self.post(f"projects/{self.project.id}/expense-batches/")
        self.assertCode(self._ask_for_more(), "expenses_awaiting_approval")

    def test_once_everything_is_approved_the_revision_is_allowed(self):
        self.post(
            f"projects/{self.project.id}/expenses/", spend(self.today, "5000.00")
        )
        batch_id = self.get(f"projects/{self.project.id}/expense-batches/").data["open"]["id"]
        self.post(f"projects/{self.project.id}/expense-batches/")
        self.post(f"expense-batches/{batch_id}/decide/", {"decision": "APPROVED"})

        self.assertEqual(self._ask_for_more().status_code, status.HTTP_201_CREATED)

    def test_a_project_with_no_spend_can_always_ask(self):
        self.assertEqual(self._ask_for_more().status_code, status.HTTP_201_CREATED)

    def test_the_summary_says_whether_a_revision_can_be_asked_for(self):
        self.assertTrue(
            self.get(f"projects/{self.project.id}/summary/").data["can_request_revision"]
        )
        self.post(
            f"projects/{self.project.id}/expenses/", spend(self.today, "5000.00")
        )
        self.assertFalse(
            self.get(f"projects/{self.project.id}/summary/").data["can_request_revision"]
        )


class BatchPermissionTests(ConstructionTestCase):
    permissions = [p for p in ALL_PERMISSIONS if p != "can_approve_expense"]

    def setUp(self):
        super().setUp()
        self.project = self.make_project()
        self.post(f"projects/{self.project.id}/expenses/", spend(self.today, "1000.00"))
        self.batch_id = self.get(
            f"projects/{self.project.id}/expense-batches/"
        ).data["open"]["id"]
        self.post(f"projects/{self.project.id}/expense-batches/")

    def test_recording_and_sending_does_not_let_you_approve(self):
        """The person who pays is not the person who signs it off."""
        response = self.post(
            f"expense-batches/{self.batch_id}/decide/", {"decision": "APPROVED"}
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_the_queue_carries_submitted_batches(self):
        reviewer = self.make_user(
            "batch@example.com",
            "CON980",
            ["can_view_all_projects", "can_view_project", "can_approve_expense"],
        )
        self.client.force_authenticate(reviewer)
        data = self.get("approvals/").data
        self.assertEqual(len(data["batches"]), 1)
        self.assertEqual(data["batches"][0]["line_count"], 1)
        self.assertEqual(len(data["batches"][0]["expenses"]), 1)
