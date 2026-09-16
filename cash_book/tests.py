"""
Tests for the cash book.

The running balance and the approval lock are the two things worth proving: a
cash book whose balance column can drift is worse than no cash book, and an
entry that can be edited while it sits with an approver makes the approval
meaningless.

These exercise the service layer directly. SAP is never reached -- the G/L
snapshot happens in the view, and the reader is the only thing that talks to
HANA.
"""

from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.exceptions import ValidationError

from company.models import Company

from . import services
from .models import (
    BunchStatus,
    CashBranch,
    CashDirection,
    CashEntry,
    EntryApprovalStatus,
)

User = get_user_model()


class CashBookTestCase(TestCase):
    """Shared fixture: one company, one custodian, one approver, one branch."""

    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        cls.other_company = Company.objects.create(name="Jivo Mart", code="JIVO_MART")
        cls.branch = CashBranch.objects.create(company=cls.company, name="Oil")
        cls.other_branch = CashBranch.objects.create(
            company=cls.other_company, name="Oil"
        )
        cls.custodian = User.objects.create(email="custodian@example.com")
        cls.approver = User.objects.create(email="approver@example.com")

    def receipt(self, amount="50000.00", **kwargs):
        return services.record_entry(
            user=self.custodian,
            company=kwargs.pop("company", self.company),
            entry_date=kwargs.pop("entry_date", date(2026, 6, 4)),
            direction=CashDirection.IN,
            amount=Decimal(amount),
            detail=kwargs.pop("detail", "Cash receive by ATM card"),
            **kwargs,
        )

    def payment(self, amount="6000.00", **kwargs):
        # A branch belongs to one company, so the default has to follow the
        # company the payment is being made in.
        company = kwargs.pop("company", self.company)
        default_branch = self.branch if company == self.company else self.other_branch
        return services.record_entry(
            user=self.custodian,
            company=company,
            entry_date=kwargs.pop("entry_date", date(2026, 6, 4)),
            direction=CashDirection.OUT,
            amount=Decimal(amount),
            detail=kwargs.pop("detail", "Cash paid to Ravi kumar for refreshment"),
            branch=kwargs.pop("branch", default_branch),
            gl_account_code=kwargs.pop("gl_account_code", "5630004"),
            gl_account_name=kwargs.pop("gl_account_name", "REFRESHMENT"),
            **kwargs,
        )


class RunningBalanceTests(CashBookTestCase):
    def test_book_starts_empty(self):
        self.assertEqual(services.current_balance(self.company), Decimal("0.00"))

    def test_receipt_then_payments_walk_the_balance_down(self):
        """The sheet's first five rows, in the order they were written."""
        self.assertEqual(self.receipt("50000.00").balance_after, Decimal("50000.00"))
        self.assertEqual(self.payment("6000.00").balance_after, Decimal("44000.00"))
        self.assertEqual(self.payment("6000.00").balance_after, Decimal("38000.00"))
        self.assertEqual(self.payment("2000.00").balance_after, Decimal("36000.00"))
        self.assertEqual(self.payment("2770.00").balance_after, Decimal("33230.00"))
        self.assertEqual(services.current_balance(self.company), Decimal("33230.00"))

    def test_a_back_dated_entry_is_appended_not_inserted(self):
        """The balance follows recording order, which is what the sheet does."""
        self.receipt("50000.00")
        late = self.payment("6000.00", entry_date=date(2026, 6, 5))
        early = self.payment("2000.00", entry_date=date(2026, 6, 3))

        self.assertEqual(late.balance_after, Decimal("44000.00"))
        self.assertEqual(early.balance_after, Decimal("42000.00"))

    def test_each_company_keeps_its_own_balance(self):
        self.receipt("50000.00")
        self.receipt("10000.00", company=self.other_company)

        self.assertEqual(services.current_balance(self.company), Decimal("50000.00"))
        self.assertEqual(
            services.current_balance(self.other_company), Decimal("10000.00")
        )

    def test_correcting_an_amount_rewrites_the_rows_after_it(self):
        self.receipt("50000.00")
        wrong = self.payment("6000.00")
        later = self.payment("2000.00")

        services.update_entry(
            user=self.custodian, entry=wrong, amount=Decimal("600.00")
        )

        wrong.refresh_from_db()
        later.refresh_from_db()
        self.assertEqual(wrong.balance_after, Decimal("49400.00"))
        self.assertEqual(later.balance_after, Decimal("47400.00"))
        self.assertEqual(services.current_balance(self.company), Decimal("47400.00"))

    def test_cancelling_an_entry_takes_it_out_of_the_balance(self):
        self.receipt("50000.00")
        mistake = self.payment("6000.00")
        later = self.payment("2000.00")

        services.cancel_entry(user=self.custodian, entry=mistake)

        mistake.refresh_from_db()
        later.refresh_from_db()
        self.assertFalse(mistake.is_active)
        self.assertEqual(later.balance_after, Decimal("48000.00"))
        self.assertEqual(services.current_balance(self.company), Decimal("48000.00"))

    def test_recompute_is_a_no_op_on_a_book_that_is_already_right(self):
        self.receipt("50000.00")
        self.payment("6000.00")
        self.assertEqual(services.recompute_balances(self.company), 0)


class EntryRuleTests(CashBookTestCase):
    def test_a_payment_must_name_a_branch(self):
        with self.assertRaises(ValidationError) as caught:
            self.payment(branch=None)
        self.assertIn("branch", caught.exception.detail)

    def test_a_payment_must_name_a_gl_head(self):
        with self.assertRaises(ValidationError) as caught:
            self.payment(gl_account_code="")
        self.assertIn("gl_account_code", caught.exception.detail)

    def test_a_receipt_needs_neither(self):
        entry = self.receipt()
        self.assertIsNone(entry.branch)
        self.assertEqual(entry.gl_account_code, "")

    def test_turning_a_payment_into_a_receipt_clears_its_head(self):
        """Otherwise a stale G/L head reads as this entry's own."""
        entry = self.payment()
        services.update_entry(
            user=self.custodian, entry=entry, direction=CashDirection.IN
        )
        entry.refresh_from_db()
        self.assertIsNone(entry.branch)
        self.assertEqual(entry.gl_account_code, "")
        self.assertEqual(entry.gl_account_name, "")


class BunchTests(CashBookTestCase):
    def test_bundling_groups_entries_without_deciding_anything(self):
        """A bunch is a bundle of paper, not a judgement on what is in it."""
        self.receipt("50000.00")
        first = self.payment("6000.00")
        second = self.payment("2000.00")

        bunch = services.send_for_approval(
            user=self.custodian,
            company=self.company,
            entry_ids=[first.id, second.id],
            remarks="June vouchers",
        )

        self.assertEqual(bunch.number, 1)
        first.refresh_from_db()
        self.assertEqual(first.bunch_id, bunch.id)
        # Bundled, but nobody has been asked anything yet.
        self.assertFalse(first.is_locked)
        self.assertEqual(first.approval_status, EntryApprovalStatus.UNSENT)

    def test_bunch_numbers_run_per_company(self):
        first = self.payment()
        services.send_for_approval(
            user=self.custodian, company=self.company, entry_ids=[first.id]
        )
        second = self.payment()
        third = services.send_for_approval(
            user=self.custodian, company=self.company, entry_ids=[second.id]
        )
        self.assertEqual(third.number, 2)

        elsewhere = self.payment(company=self.other_company)
        other = services.send_for_approval(
            user=self.custodian,
            company=self.other_company,
            entry_ids=[elsewhere.id],
        )
        self.assertEqual(other.number, 1)

    def test_an_entry_awaiting_approval_cannot_be_corrected(self):
        entry = self.payment()
        services.send_entries_for_approval(
            user=self.custodian, company=self.company, entry_ids=[entry.id]
        )
        entry.refresh_from_db()

        with self.assertRaises(ValidationError):
            services.update_entry(
                user=self.custodian, entry=entry, amount=Decimal("1.00")
            )
        with self.assertRaises(ValidationError):
            services.cancel_entry(user=self.custodian, entry=entry)

    def test_an_approved_entry_stays_frozen(self):
        entry = self.payment()
        services.send_entries_for_approval(
            user=self.custodian, company=self.company, entry_ids=[entry.id]
        )
        services.decide_entries(
            user=self.approver,
            company=self.company,
            entry_ids=[entry.id],
            approve=True,
        )
        entry.refresh_from_db()

        self.assertEqual(entry.approval_status, EntryApprovalStatus.APPROVED)
        with self.assertRaises(ValidationError):
            services.cancel_entry(user=self.custodian, entry=entry)

    def test_approval_stamps_who_and_when(self):
        entry = self.payment()
        bunch = services.send_for_approval(
            user=self.custodian, company=self.company, entry_ids=[entry.id]
        )
        services.approve_bunch(user=self.approver, bunch=bunch, note="Seen")

        bunch.refresh_from_db()
        self.assertEqual(bunch.status, BunchStatus.APPROVED)
        self.assertEqual(bunch.decided_by, self.approver)
        self.assertIsNotNone(bunch.decided_at)
        self.assertEqual(bunch.decision_note, "Seen")

    def test_rejection_needs_a_reason_and_unfreezes_the_entry(self):
        entry = self.payment()
        services.send_entries_for_approval(
            user=self.custodian, company=self.company, entry_ids=[entry.id]
        )

        with self.assertRaises(ValidationError):
            services.decide_entries(
                user=self.approver,
                company=self.company,
                entry_ids=[entry.id],
                approve=False,
            )

        services.decide_entries(
            user=self.approver,
            company=self.company,
            entry_ids=[entry.id],
            approve=False,
            note="Bill number missing",
        )
        entry.refresh_from_db()
        self.assertEqual(entry.approval_status, EntryApprovalStatus.REJECTED)
        self.assertFalse(entry.is_locked)

        # And the correction the rejection asked for now goes through.
        services.update_entry(
            user=self.custodian, entry=entry, detail="Bill no. 128"
        )

    def test_a_rejected_bunch_goes_back_up_under_its_own_number(self):
        entry = self.payment()
        bunch = services.send_for_approval(
            user=self.custodian, company=self.company, entry_ids=[entry.id]
        )
        services.reject_bunch(user=self.approver, bunch=bunch, note="Fix it")
        services.resend_bunch(user=self.custodian, bunch=bunch)

        bunch.refresh_from_db()
        self.assertEqual(bunch.number, 1)
        self.assertEqual(bunch.status, BunchStatus.PENDING)
        self.assertIsNone(bunch.decided_at)
        self.assertEqual(bunch.decision_note, "")

    def test_only_a_rejected_bunch_can_be_resent(self):
        entry = self.payment()
        bunch = services.send_for_approval(
            user=self.custodian, company=self.company, entry_ids=[entry.id]
        )
        with self.assertRaises(ValidationError):
            services.resend_bunch(user=self.custodian, bunch=bunch)

    def test_a_decided_bunch_cannot_be_decided_again(self):
        entry = self.payment()
        bunch = services.send_for_approval(
            user=self.custodian, company=self.company, entry_ids=[entry.id]
        )
        services.approve_bunch(user=self.approver, bunch=bunch)
        with self.assertRaises(ValidationError):
            services.reject_bunch(user=self.approver, bunch=bunch, note="No")

    def test_an_entry_already_in_a_bunch_cannot_be_sent_again(self):
        entry = self.payment()
        services.send_for_approval(
            user=self.custodian, company=self.company, entry_ids=[entry.id]
        )
        with self.assertRaises(ValidationError) as caught:
            services.send_for_approval(
                user=self.custodian, company=self.company, entry_ids=[entry.id]
            )
        self.assertIn("entry_ids", caught.exception.detail)

    def test_another_companys_entry_cannot_be_sent(self):
        elsewhere = self.payment(company=self.other_company)
        with self.assertRaises(ValidationError):
            services.send_for_approval(
                user=self.custodian, company=self.company, entry_ids=[elsewhere.id]
            )

    def test_a_cancelled_entry_cannot_be_sent(self):
        entry = self.payment()
        services.cancel_entry(user=self.custodian, entry=entry)
        with self.assertRaises(ValidationError):
            services.send_for_approval(
                user=self.custodian, company=self.company, entry_ids=[entry.id]
            )

    def test_sending_nothing_is_refused(self):
        with self.assertRaises(ValidationError):
            services.send_for_approval(
                user=self.custodian, company=self.company, entry_ids=[]
            )


class TotalsTests(CashBookTestCase):
    def test_totals_add_up_over_the_filtered_set(self):
        self.receipt("50000.00")
        self.payment("6000.00")
        self.payment("2000.00")

        totals = services.totals(
            CashEntry.objects.filter(company=self.company, is_active=True)
        )
        self.assertEqual(totals["cash_in"], Decimal("50000.00"))
        self.assertEqual(totals["cash_out"], Decimal("8000.00"))
        self.assertEqual(totals["net"], Decimal("42000.00"))
        self.assertEqual(totals["count"], 3)

    def test_an_empty_set_totals_to_zero_rather_than_none(self):
        totals = services.totals(CashEntry.objects.none())
        self.assertEqual(totals["cash_in"], Decimal("0.00"))
        self.assertEqual(totals["cash_out"], Decimal("0.00"))


class LockingSQLTests(CashBookTestCase):
    """The send-for-approval lock has to be legal SQL on PostgreSQL.

    Row locking is a no-op on SQLite, so the suite cannot execute this
    difference -- it can only inspect the query that would be sent. That is
    enough to catch the regression, which was a `select_related("bunch")`
    turning the locked read into an outer join PostgreSQL refuses to lock.
    """

    def test_the_locked_read_does_not_join_the_nullable_bunch(self):
        entry = self.payment()
        sql = str(services._lock_entries(self.company, [entry.id]).query)
        self.assertNotIn("LEFT OUTER JOIN", sql.upper())
        self.assertNotIn("CASH_BOOK_CASHBUNCH", sql.upper())

    def test_sending_still_refuses_an_entry_already_in_a_bunch(self):
        """Proves the check still reads the bunch without selecting it."""
        entry = self.payment()
        services.send_for_approval(
            user=self.custodian, company=self.company, entry_ids=[entry.id]
        )
        with self.assertRaises(ValidationError):
            services.send_for_approval(
                user=self.custodian, company=self.company, entry_ids=[entry.id]
            )
