"""
Tests for entry-level approval and the six figures the register heads with.

Approval used to belong to the bunch, which meant nothing could be agreed
without first being bundled -- a payment typed on Tuesday waited on a batch
that went on Friday. It now belongs to the entry, and a bunch is what the
sheet has always meant by it: the bundle of paper walked over together.

The reconciliation reads down the six cards and comes to nothing left over:

    cash in - cash out (approved) - awaiting approval - cash in hand
            - advance given = 0
"""

from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase
from rest_framework.exceptions import ValidationError

from company.models import Company, UserCompany, UserRole

from . import services
from .models import (
    AdvanceDirection,
    CashBranch,
    CashDirection,
    EntryApprovalStatus,
)

User = get_user_model()


class ReconciliationTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        cls.branch = CashBranch.objects.create(company=cls.company, name="Oil")
        cls.custodian = User.objects.create(email="custodian@example.com")
        cls.approver = cls._make_approver(cls.company)
        cls.bunty = User.objects.create(email="bunty@example.com")

    @classmethod
    def _make_approver(cls, company, email="approver@example.com"):
        """Somebody a payment may be sent to: the right, and the company."""
        user = User.objects.create(email=email)
        user.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="cash_book",
                codename="can_approve_cash_entries",
            )
        )
        role, _ = UserRole.objects.get_or_create(name="Accounts")
        UserCompany.objects.create(user=user, company=company, role=role)
        return user

    def receipt(self, amount="50000.00"):
        return services.record_entry(
            user=self.custodian,
            company=self.company,
            entry_date=date(2026, 6, 4),
            direction=CashDirection.IN,
            amount=Decimal(amount),
            detail="Cash receive by ATM card",
        )

    def payment(self, amount="6000.00", **kwargs):
        return services.record_entry(
            user=self.custodian,
            company=self.company,
            entry_date=date(2026, 6, 4),
            direction=CashDirection.OUT,
            amount=Decimal(amount),
            detail="Cash paid for refreshment",
            branch=self.branch,
            gl_account_code="5630004",
            gl_account_name="REFRESHMENT",
            **{"approver": self.approver, **kwargs},
        )

    def decide(self, *entries, approve=True, note=""):
        return services.decide_entries(
            user=self.approver,
            company=self.company,
            entry_ids=[entry.id for entry in entries],
            approve=approve,
            note=note,
        )


class EntryApprovalTests(ReconciliationTestCase):
    def test_a_payment_is_waiting_the_moment_it_is_recorded(self):
        """There is no step in between, so there is no state for the gap."""
        entry = self.payment()
        self.assertEqual(entry.approval_state, EntryApprovalStatus.PENDING)
        self.assertIsNotNone(entry.approval_sent_at)

    def test_a_receipt_never_enters_the_queue(self):
        entry = self.receipt()
        self.assertEqual(entry.approval_state, EntryApprovalStatus.NOT_REQUIRED)
        self.assertFalse(entry.is_locked)

    def test_a_waiting_payment_is_still_editable(self):
        entry = self.payment()
        self.assertFalse(entry.is_locked)
        services.update_entry(user=self.custodian, entry=entry, detail="Fixed")

    def test_approving_is_what_freezes_it(self):
        entry = self.payment()
        self.decide(entry)
        entry.refresh_from_db()
        self.assertEqual(entry.approval_state, EntryApprovalStatus.APPROVED)
        self.assertTrue(entry.is_locked)
        self.assertEqual(entry.approval_decided_by, self.approver)
        self.assertIsNotNone(entry.approval_decided_at)

    def test_correcting_a_rejected_payment_puts_it_back_in_the_queue(self):
        """The correction IS the answer to the rejection -- nothing to resend."""
        entry = self.payment()
        self.decide(entry, approve=False, note="Bill number missing")

        entry.refresh_from_db()
        self.assertEqual(entry.approval_state, EntryApprovalStatus.REJECTED)
        self.assertFalse(entry.is_locked)
        self.assertEqual(entry.approval_note, "Bill number missing")

        services.update_entry(user=self.custodian, entry=entry, detail="Bill no. 128")
        entry.refresh_from_db()
        self.assertEqual(entry.approval_state, EntryApprovalStatus.PENDING)
        # The note that sent it back no longer describes it.
        self.assertEqual(entry.approval_note, "")

    def test_a_rejection_must_say_why(self):
        entry = self.payment()
        with self.assertRaises(ValidationError):
            self.decide(entry, approve=False)

    def test_an_approved_entry_cannot_be_decided_again(self):
        entry = self.payment()
        self.decide(entry)
        with self.assertRaises(ValidationError):
            self.decide(entry)

    def test_a_receipt_cannot_be_approved(self):
        receipt = self.receipt()
        with self.assertRaises(ValidationError):
            self.decide(receipt)

    def test_several_are_decided_in_one_go(self):
        first, second = self.payment(), self.payment("2000.00")
        self.decide(first, second)
        for entry in (first, second):
            entry.refresh_from_db()
            self.assertEqual(entry.approval_state, EntryApprovalStatus.APPROVED)

    def test_bundling_decides_nothing(self):
        """A bunch is a bundle of paper, not a judgement on what is in it."""
        entry = self.payment()
        self.decide(entry)
        services.create_bunch(
            user=self.custodian, company=self.company, entry_ids=[entry.id]
        )
        entry.refresh_from_db()
        self.assertIsNotNone(entry.bunch_id)
        self.assertEqual(entry.approval_state, EntryApprovalStatus.APPROVED)

    def test_a_cancelled_entry_cannot_be_decided(self):
        entry = self.payment()
        services.cancel_entry(user=self.custodian, entry=entry)
        with self.assertRaises(ValidationError):
            self.decide(entry)


class ReconciliationTests(ReconciliationTestCase):
    def test_an_empty_book_reconciles(self):
        figures = services.reconciliation(self.company)
        self.assertEqual(figures["difference"], Decimal("0.00"))
        self.assertEqual(figures["cash_in"], Decimal("0.00"))

    def test_money_in_and_nothing_spent_is_all_in_hand(self):
        self.receipt("50000.00")
        figures = services.reconciliation(self.company)
        self.assertEqual(figures["cash_in"], Decimal("50000.00"))
        self.assertEqual(figures["cash_in_hand"], Decimal("50000.00"))
        self.assertEqual(figures["difference"], Decimal("0.00"))

    def test_an_unapproved_payment_is_awaiting_not_spent(self):
        """It has left the box but nobody has agreed it yet."""
        self.receipt("50000.00")
        self.payment("6000.00")

        figures = services.reconciliation(self.company)
        self.assertEqual(figures["cash_out"], Decimal("0.00"))
        self.assertEqual(figures["awaiting_approval"], Decimal("6000.00"))
        self.assertEqual(figures["cash_in_hand"], Decimal("44000.00"))
        self.assertEqual(figures["difference"], Decimal("0.00"))

    def test_approving_moves_it_from_awaiting_to_spent(self):
        self.receipt("50000.00")
        entry = self.payment("6000.00")
        self.decide(entry)

        figures = services.reconciliation(self.company)
        self.assertEqual(figures["cash_out"], Decimal("6000.00"))
        self.assertEqual(figures["awaiting_approval"], Decimal("0.00"))
        self.assertEqual(figures["cash_in_hand"], Decimal("44000.00"))
        self.assertEqual(figures["difference"], Decimal("0.00"))

    def test_an_advance_moves_cash_out_of_hand_without_spending_it(self):
        self.receipt("50000.00")
        services.record_advance(
            user=self.custodian,
            company=self.company,
            person=self.bunty,
            entry_date=date(2026, 6, 4),
            direction=AdvanceDirection.GIVEN,
            amount=Decimal("15000.00"),
        )

        figures = services.reconciliation(self.company)
        self.assertEqual(figures["advance_given"], Decimal("15000.00"))
        self.assertEqual(figures["cash_in_hand"], Decimal("35000.00"))
        self.assertEqual(figures["difference"], Decimal("0.00"))

    def test_the_whole_chain_together(self):
        self.receipt("50000.00")
        approved = self.payment("6000.00")
        self.decide(approved)
        self.payment("2000.00")  # left awaiting
        services.record_advance(
            user=self.custodian,
            company=self.company,
            person=self.bunty,
            entry_date=date(2026, 6, 4),
            direction=AdvanceDirection.GIVEN,
            amount=Decimal("15000.00"),
        )

        figures = services.reconciliation(self.company)
        self.assertEqual(figures["cash_in"], Decimal("50000.00"))
        self.assertEqual(figures["cash_out"], Decimal("6000.00"))
        self.assertEqual(figures["awaiting_approval"], Decimal("2000.00"))
        self.assertEqual(figures["cash_in_hand"], Decimal("27000.00"))
        self.assertEqual(figures["advance_given"], Decimal("15000.00"))
        self.assertEqual(figures["difference"], Decimal("0.00"))

    def test_an_expense_explained_out_of_an_advance_lowers_both(self):
        self.receipt("50000.00")
        services.record_advance(
            user=self.custodian,
            company=self.company,
            person=self.bunty,
            entry_date=date(2026, 6, 4),
            direction=AdvanceDirection.GIVEN,
            amount=Decimal("15000.00"),
        )
        entry = self.payment("3400.00", advance_holder=self.bunty)
        self.decide(entry)

        figures = services.reconciliation(self.company)
        self.assertEqual(figures["cash_out"], Decimal("3400.00"))
        self.assertEqual(figures["advance_given"], Decimal("11600.00"))
        # The cash never came back to the box, so it is untouched.
        self.assertEqual(figures["cash_in_hand"], Decimal("35000.00"))
        self.assertEqual(figures["difference"], Decimal("0.00"))

    def test_a_cancelled_entry_drops_out_of_every_figure(self):
        self.receipt("50000.00")
        entry = self.payment("6000.00")
        services.cancel_entry(user=self.custodian, entry=entry)

        figures = services.reconciliation(self.company)
        self.assertEqual(figures["awaiting_approval"], Decimal("0.00"))
        self.assertEqual(figures["cash_in_hand"], Decimal("50000.00"))
        self.assertEqual(figures["difference"], Decimal("0.00"))


class MoneyStaffLaidOutTests(ReconciliationTestCase):
    """Somebody paying for the factory out of their own pocket.

    This is how a negative balance actually arises: a payment is recorded
    against a person who was never handed any cash. The expense is real and
    goes in the book, so the balance drops -- but the notes never left the
    drawer, because they came out of his own. The box is therefore richer
    than the book alone implies, by exactly what he is owed.

    Which is why the two directions are reported apart. Netting them into one
    "advance" figure understates what is genuinely out with people and hides
    the debt completely; and leaving the money out of the box's arithmetic
    would say the drawer is short when it is not.
    """

    def laid_out(self, person, amount):
        """An expense the factory owes for, paid by somebody with no float."""
        entry = self.payment(amount, advance_holder=person)
        self.decide(entry)
        return entry

    def test_it_is_reported_as_owed_not_as_a_smaller_advance(self):
        self.receipt("50000.00")
        self.laid_out(self.bunty, "15232.00")

        figures = services.reconciliation(self.company)
        self.assertEqual(figures["owed_to_people"], Decimal("15232.00"))
        self.assertEqual(figures["advance_given"], Decimal("0.00"))
        self.assertEqual(figures["difference"], Decimal("0.00"))

    def test_the_drawer_is_untouched_because_he_paid_it_himself(self):
        """The expense is in the book; the notes are still in the box."""
        self.receipt("50000.00")
        self.laid_out(self.bunty, "15232.00")

        figures = services.reconciliation(self.company)
        self.assertEqual(figures["cash_out"], Decimal("15232.00"))
        self.assertEqual(figures["cash_in_hand"], Decimal("50000.00"))

    def test_it_does_not_shrink_what_others_are_holding(self):
        """The bug this replaces: one netted figure, stating neither."""
        self.receipt("50000.00")
        services.record_advance(
            user=self.custodian,
            company=self.company,
            person=self.approver,
            entry_date=date(2026, 6, 4),
            direction=AdvanceDirection.GIVEN,
            amount=Decimal("20000.00"),
        )
        self.laid_out(self.bunty, "15000.00")

        figures = services.reconciliation(self.company)
        self.assertEqual(figures["advance_given"], Decimal("20000.00"))
        self.assertEqual(figures["owed_to_people"], Decimal("15000.00"))
        # Netted, these two would have read as a single 5,000 advance.
        self.assertNotEqual(figures["advance_given"], Decimal("5000.00"))
        self.assertEqual(figures["difference"], Decimal("0.00"))

    def test_one_person_can_only_be_on_one_side_at_a_time(self):
        """Handed 5,000 and having laid out 8,000 leaves him owed 3,000."""
        self.receipt("50000.00")
        services.record_advance(
            user=self.custodian,
            company=self.company,
            person=self.bunty,
            entry_date=date(2026, 6, 4),
            direction=AdvanceDirection.GIVEN,
            amount=Decimal("5000.00"),
        )
        self.laid_out(self.bunty, "8000.00")

        figures = services.reconciliation(self.company)
        self.assertEqual(figures["advance_given"], Decimal("0.00"))
        self.assertEqual(figures["owed_to_people"], Decimal("3000.00"))
        self.assertEqual(figures["difference"], Decimal("0.00"))

    def test_paying_him_back_clears_it(self):
        """A float handed over settles what he is owed, and both figures go."""
        self.receipt("50000.00")
        self.laid_out(self.bunty, "15232.00")
        services.record_advance(
            user=self.custodian,
            company=self.company,
            person=self.bunty,
            entry_date=date(2026, 6, 5),
            direction=AdvanceDirection.GIVEN,
            amount=Decimal("15232.00"),
            detail="Paying back what he laid out",
        )

        figures = services.reconciliation(self.company)
        self.assertEqual(figures["owed_to_people"], Decimal("0.00"))
        self.assertEqual(figures["advance_given"], Decimal("0.00"))
        # Now the cash really has gone: the book's balance and the drawer agree.
        self.assertEqual(figures["cash_in_hand"], Decimal("34768.00"))
        self.assertEqual(figures["difference"], Decimal("0.00"))
