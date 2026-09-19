"""
accounts_board/tests.py

What these pin down, and why each is here rather than being obvious.

The centre of gravity is :class:`PeriodTests`. The screen carries a month
selector, three of its four headline figures follow it, and the fourth --
cash in hand -- must NOT. A "September cash in hand" is not a smaller truth than
the real one, it is a fabricated one: the drawer does not empty on the first of
the month. That bug would be invisible. The tile would read plausibly, the
period would look respected, and the number would be wrong by exactly whatever
was in the box on the 31st. It is tested from both ends -- the flows move with
the period, the balance does not.

:class:`NegativeBalanceTests` guards the other invisible one. A cash book can go
negative, the live book did during development, and the temptation to clamp it
at zero or to ``abs()`` it is real. A clamped negative is a screen that looks
healthy at exactly the moment it is not.

The rest guard decisions a later reader would otherwise "tidy up": advances and
reimbursements are not netted on purpose, a person settled to zero is dropped on
purpose, an unattributed salary row is kept in the total on purpose, and names
are masked for a feed-only reader on purpose.
"""

from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase

from cash_book.models import (
    AdvanceDirection,
    AdvanceEntry,
    AtmAccount,
    AtmReceipt,
    CashBranch,
    CashBunch,
    CashDirection,
    CashEntry,
    EntryApprovalStatus,
)
from company.models import Company

from .services import AccountsBoardService, month_bounds

User = get_user_model()

TODAY = date(2026, 9, 16)

#: Two heads that land in different board lines, so a test can move money
#: between lines without inventing an account.
FREIGHT = ("5670001", "FREIGHT AND CARTAGE OUTWARD-INDIRECT EXP")  # vendor_ap
REFRESHMENT = ("5630004", "REFRESHMENT")  # expenses
STAFF_DEBTOR = ("1101015", "SUNDRY DEBTORS STAFF")  # salary_adjustment


class AccountsBoardTestCase(TestCase):
    """Shared fixtures: one company, one branch, one card, two people."""

    @classmethod
    def setUpTestData(cls):
        cls.oil = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        cls.other = Company.objects.create(name="Jivo Mart", code="JIVO_MART")
        cls.branch = CashBranch.objects.create(company=cls.oil, name="Oil")
        cls.card = AtmAccount.objects.create(
            company=cls.oil, name="Imprest Debit Card (Vishal)"
        )
        cls.bunty = User.objects.create(email="bunty@jivo.in", full_name="Bunty")
        cls.jas = User.objects.create(email="jas@jivo.in", full_name="Jasmeet Singh")

    # ---------------------------------------------------------------- makers

    def receipt(self, amount, *, day=TODAY, card=None, company=None):
        return CashEntry.objects.create(
            company=company or self.oil,
            entry_date=day,
            direction=CashDirection.IN,
            amount=Decimal(amount),
            atm_account=card,
            detail="Cash in",
            approval_state=EntryApprovalStatus.NOT_REQUIRED,
        )

    def payment(
        self,
        amount,
        *,
        day=TODAY,
        head=REFRESHMENT,
        state=EntryApprovalStatus.APPROVED,
        holder=None,
        bunch=None,
        item="",
        company=None,
    ):
        return CashEntry.objects.create(
            company=company or self.oil,
            entry_date=day,
            direction=CashDirection.OUT,
            amount=Decimal(amount),
            branch=self.branch,
            gl_account_code=head[0],
            gl_account_name=head[1],
            item=item,
            detail="Paid out",
            approval_state=state,
            advance_holder=holder,
            bunch=bunch,
        )

    def load(self, amount, *, day=TODAY, card=None):
        """Money paid ONTO the imprest card -- what "imprest issued" counts."""
        return AtmReceipt.objects.create(
            account=card or self.card, received_on=day, amount=Decimal(amount)
        )

    def advance(self, person, amount, *, direction=AdvanceDirection.GIVEN, day=TODAY):
        return AdvanceEntry.objects.create(
            company=self.oil,
            person=person,
            entry_date=day,
            direction=direction,
            amount=Decimal(amount),
        )

    def board(self, *, period=None, user=None, company=None):
        return AccountsBoardService(
            company or self.oil, user=user, today=TODAY, period=period
        ).build()


class MonthBoundsTests(TestCase):
    """The range is inclusive at both ends.

    A half-open range would need the first of the NEXT month as its upper bound,
    which is the shape that silently lends every month the next one's first day.
    """

    def test_february_in_a_leap_year(self):
        self.assertEqual(
            month_bounds(2024, 2), (date(2024, 2, 1), date(2024, 2, 29))
        )

    def test_december_does_not_roll_into_january(self):
        self.assertEqual(
            month_bounds(2026, 12), (date(2026, 12, 1), date(2026, 12, 31))
        )


class PeriodTests(AccountsBoardTestCase):
    """Flows follow the month. The balance does not.

    This is the class to read first: it pins the one distinction the whole
    screen rests on.
    """

    def setUp(self):
        self.load(80000, day=date(2026, 8, 5))
        self.load(40000, day=date(2026, 9, 5))
        # Drawn off the card -- what "cash issued" counts.
        self.receipt(100000, day=date(2026, 8, 10), card=self.card)
        self.receipt(50000, day=date(2026, 9, 10), card=self.card)
        self.payment(30000, day=date(2026, 8, 12))
        self.payment(20000, day=date(2026, 9, 12))

    def test_flows_are_filtered_to_the_month(self):
        september = self.board(period=(2026, 9))["headline"]
        self.assertEqual(september["imprest_issued"], 40000.0)
        self.assertEqual(september["cash_issued"], 50000.0)
        self.assertEqual(september["into_box"], 50000.0)
        self.assertEqual(september["paid_out"], 20000.0)

        august = self.board(period=(2026, 8))["headline"]
        self.assertEqual(august["imprest_issued"], 80000.0)
        self.assertEqual(august["cash_issued"], 100000.0)
        self.assertEqual(august["paid_out"], 30000.0)

    def test_no_period_means_the_whole_book(self):
        whole = self.board()["headline"]
        self.assertEqual(whole["imprest_issued"], 120000.0)
        self.assertEqual(whole["cash_issued"], 150000.0)
        self.assertEqual(whole["paid_out"], 50000.0)

    def test_latest_resolves_to_the_newest_month_with_entries(self):
        """The screen's default. Resolved here so the page opens on September
        in one round trip rather than fetching the book to find out."""
        board = self.board(period=AccountsBoardService.LATEST)

        self.assertEqual(board["meta"]["period"]["year"], 2026)
        self.assertEqual(board["meta"]["period"]["month"], 9)
        self.assertEqual(board["headline"]["imprest_issued"], 40000.0)
        self.assertEqual(board["headline"]["cash_issued"], 50000.0)

    def test_cash_in_hand_ignores_the_period_entirely(self):
        """The drawer does not reset on the first of the month.

        Same balance under every filter, including a month the book never
        traded in. If this ever starts varying, somebody has "fixed" the
        in-hand tile to respect the selector and broken the only figure on the
        screen that describes the present.
        """
        balances = {
            self.board(period=period)["headline"]["cash_in_hand"]
            for period in (None, (2026, 8), (2026, 9), (2026, 1))
        }
        self.assertEqual(balances, {100000.0})

    def test_a_month_with_no_entries_is_zero_not_missing(self):
        january = self.board(period=(2026, 1))
        self.assertEqual(january["headline"]["imprest_issued"], 0.0)
        self.assertEqual(january["headline"]["cash_issued"], 0.0)
        # Present and empty, never None: a null tile means "could not read".
        self.assertEqual(january["imprest"]["rows"], [])
        self.assertNotIn("imprest", january["meta"]["degraded"])

    def test_only_months_with_entries_are_offered(self):
        labels = [p["label"] for p in self.board()["meta"]["periods"]]
        self.assertEqual(labels, ["September 2026", "August 2026"])


class CashIssuedTests(AccountsBoardTestCase):
    """"Cash issued" is what came OFF the card, not what was spent."""

    def test_it_counts_withdrawals_not_vouchers(self):
        self.receipt(60000, card=self.card)
        self.payment(1000)
        self.payment(2000)

        headline = self.board()["headline"]
        self.assertEqual(headline["cash_issued"], 60000.0)
        self.assertEqual(headline["cash_issued_count"], 1)
        # Spending is a third figure and keeps its own name.
        self.assertEqual(headline["paid_out"], 3000.0)
        self.assertEqual(headline["paid_out_count"], 2)

    def test_cash_handed_straight_in_is_not_a_withdrawal(self):
        """It reached the box without touching the card.

        On the live register this is 16,149 over the whole book -- small, and
        exactly the kind of gap that makes "into the box" and "off the card"
        look interchangeable until they are not.
        """
        self.receipt(60000, card=self.card)
        self.receipt(5000)

        headline = self.board()["headline"]
        self.assertEqual(headline["cash_issued"], 60000.0)
        self.assertEqual(headline["into_box"], 65000.0)

    def test_the_card_balance_is_not_this_period_on_minus_off(self):
        """The subtraction the two cards invite, and why it is refused.

        The card opened at 19,538 before any of this. A month's top-ups less a
        month's withdrawals ignores that and every earlier month, and would be
        just as plausible on screen.
        """
        self.card.opening_balance = Decimal("19538")
        self.card.save(update_fields=["opening_balance"])
        self.load(100000)
        self.receipt(60000, card=self.card)

        board = self.board()
        self.assertEqual(board["imprest"]["card_balance"], 59538.0)
        self.assertNotEqual(
            board["imprest"]["card_balance"],
            board["headline"]["imprest_issued"] - board["headline"]["cash_issued"],
        )


class ReconciliationTests(AccountsBoardTestCase):
    """The headline is the register's own arithmetic, not a second opinion."""

    def test_the_book_balances(self):
        self.receipt(100000)
        self.payment(40000)
        self.advance(self.bunty, 10000)

        headline = self.board()["headline"]
        self.assertTrue(headline["reconciliation"]["balances"])
        self.assertEqual(headline["reconciliation"]["difference"], 0.0)

    def test_in_hand_is_not_receipts_minus_payments(self):
        """The money out with people is part of the answer.

        100,000 in, 40,000 spent, 10,000 handed to somebody who has not
        explained it: the drawer holds 50,000, not 60,000.
        """
        self.receipt(100000)
        self.payment(40000)
        self.advance(self.bunty, 10000)

        headline = self.board()["headline"]
        self.assertEqual(headline["cash_in_hand"], 50000.0)
        self.assertNotEqual(
            headline["cash_in_hand"],
            headline["imprest_issued"] - headline["cash_issued"],
        )
        self.assertNotEqual(
            headline["cash_in_hand"], headline["into_box"] - headline["paid_out"]
        )

    def test_another_companys_cash_is_not_counted(self):
        self.receipt(100000)
        self.receipt(999999, company=self.other)
        self.assertEqual(self.board()["headline"]["into_box"], 100000.0)


class NegativeBalanceTests(AccountsBoardTestCase):
    """A negative drawer is reported, never clamped or absolute-valued."""

    def test_negative_in_hand_survives_intact(self):
        self.receipt(1000)
        self.payment(5000)

        headline = self.board()["headline"]
        self.assertEqual(headline["cash_in_hand"], -4000.0)
        self.assertTrue(headline["in_hand_negative"])

    def test_a_healthy_balance_is_not_flagged(self):
        self.receipt(5000)
        self.payment(1000)
        self.assertFalse(self.board()["headline"]["in_hand_negative"])


class DetailBucketTests(AccountsBoardTestCase):
    """The four lines of the outstanding breakdown."""

    def setUp(self):
        self.payment(1000, head=FREIGHT)
        self.payment(2000, head=REFRESHMENT)
        self.payment(3000, head=STAFF_DEBTOR)
        self.payment(400, head=REFRESHMENT, state=EntryApprovalStatus.PENDING)

    def buckets(self, **kwargs):
        return {b["key"]: b for b in self.board(**kwargs)["detail"]["buckets"]}

    def test_heads_land_in_their_configured_line(self):
        buckets = self.buckets()
        self.assertEqual(buckets["vendor_ap"]["amount"], 1000.0)
        # 2,000 approved + 400 pending: the line is every payment on the head,
        # agreed or not, because it answers "what did refreshment cost".
        self.assertEqual(buckets["expenses"]["amount"], 2400.0)
        self.assertEqual(buckets["salary_adjustment"]["amount"], 3000.0)

    def test_payment_pending_reads_the_approval_state_not_a_head(self):
        """The fourth line crosses every G/L head and belongs to none.

        It is the whiteboard's "payment penalty" asked differently -- see
        ``constants.PAYMENT_PENDING_CODES`` -- so it must not be derivable from
        a code list, and it must report a real source rather than has_source
        False.
        """
        pending = self.buckets()["payment_pending"]
        self.assertEqual(pending["amount"], 400.0)
        self.assertEqual(pending["count"], 1)
        self.assertTrue(pending["has_source"])
        self.assertEqual(pending["heads"], [])

    def test_an_unclassified_head_is_shown_and_warned_about(self):
        """A head no line claims is a question for accounts, not a silent drop."""
        self.payment(75, head=("9999999", "SOMETHING NEW"))
        board = self.board()

        self.assertEqual(board["detail"]["other"]["amount"], 75.0)
        self.assertTrue(
            any("belong to no named line" in w for w in board["meta"]["warnings"])
        )

    def test_buckets_and_other_account_for_every_payment(self):
        board = self.board()
        counted = sum(b["amount"] for b in board["detail"]["buckets"]
                      if b["key"] != "payment_pending")
        counted += board["detail"]["other"]["amount"]
        self.assertEqual(counted, board["detail"]["total"])


class HolderTests(AccountsBoardTestCase):
    """Who holds our cash, and who we owe. Never one figure."""

    def test_advances_and_reimbursements_are_not_netted(self):
        """15,000 out with one person and 4,000 owed to another is not 11,000.

        Netting states neither, which is the reason ``cash_book.services``
        keeps them apart, and this board must not undo that on the way out.
        """
        self.advance(self.bunty, 15000)
        self.advance(self.jas, 4000, direction=AdvanceDirection.SPENT_OWN)

        holders = self.board()["cash_issued"]["holders"]
        self.assertEqual(holders["holding"]["total"], 15000.0)
        self.assertEqual(holders["holding"]["people"], 1)
        self.assertEqual(holders["owed"]["total"], 4000.0)
        self.assertEqual(holders["owed"]["people"], 1)

    def test_somebody_settled_back_to_zero_is_dropped(self):
        self.advance(self.bunty, 5000)
        self.advance(self.bunty, 5000, direction=AdvanceDirection.RETURNED)

        holders = self.board()["cash_issued"]["holders"]
        self.assertEqual(holders["holding"]["rows"], [])
        self.assertEqual(holders["owed"]["rows"], [])

    def test_last_cleared_is_the_last_payment_they_explained(self):
        self.advance(self.bunty, 10000, day=date(2026, 7, 1))
        self.payment(1000, day=date(2026, 7, 5), holder=self.bunty)
        self.payment(2000, day=date(2026, 8, 9), holder=self.bunty)

        row = self.board()["cash_issued"]["holders"]["holding"]["rows"][0]
        self.assertEqual(row["last_updated"], "2026-08-09")

    def test_last_cleared_looks_past_the_selected_period(self):
        """Settled in August, viewing September: still August, not blank.

        A date filtered to the period would read as "never cleared" for
        somebody who cleared last month, which is the opposite of the truth.
        """
        self.advance(self.bunty, 10000, day=date(2026, 7, 1))
        self.payment(1000, day=date(2026, 8, 9), holder=self.bunty)

        row = self.board(period=(2026, 9))["cash_issued"]["holders"]["holding"]["rows"][0]
        self.assertEqual(row["last_updated"], "2026-08-09")

    def test_never_cleared_is_null_rather_than_a_date(self):
        self.advance(self.bunty, 10000)
        row = self.board()["cash_issued"]["holders"]["holding"]["rows"][0]
        self.assertIsNone(row["last_updated"])


class PendingHeadOfficeTests(AccountsBoardTestCase):
    """What has not gone to head office, and the zero that is an answer."""

    def test_entries_in_an_unsent_bunch_are_waiting(self):
        bunch = CashBunch.objects.create(company=self.oil, number=1)
        self.payment(5000, bunch=bunch, item="Diesel")

        pending = self.board()["pending_ho"]
        self.assertEqual(pending["total"], 5000.0)
        self.assertEqual(pending["count"], 1)
        self.assertEqual(pending["rows"][0]["item"], "Diesel")
        self.assertEqual(pending["unsent_bunches"], 1)

    def test_an_approved_voucher_in_no_bunch_is_also_waiting(self):
        """Not even bundled is a step further back than bundled-but-unsent.

        Leaving it out would report a reassuring zero on a book where nothing
        has been bundled at all.
        """
        self.payment(1200, item="Courier")
        pending = self.board()["pending_ho"]
        self.assertEqual(pending["total"], 1200.0)
        self.assertEqual(pending["count"], 1)

    def test_a_sent_bunch_is_no_longer_waiting(self):
        from django.utils import timezone

        bunch = CashBunch.objects.create(
            company=self.oil, number=1, sent_at=timezone.now()
        )
        self.payment(5000, bunch=bunch)

        pending = self.board()["pending_ho"]
        self.assertEqual(pending["total"], 0.0)
        self.assertEqual(pending["sent_bunches"], 1)
        self.assertEqual(pending["unsent_bunches"], 0)

    def test_pending_ignores_the_period(self):
        """What is waiting is waiting, whenever it was recorded.

        A September filter would hide the August voucher that has been stuck
        longest, which is the one worth chasing.
        """
        self.payment(900, day=date(2026, 8, 3), item="Old one")
        self.assertEqual(self.board(period=(2026, 9))["pending_ho"]["total"], 900.0)

    def test_a_voucher_with_no_item_still_has_a_name(self):
        self.payment(300, item="", head=REFRESHMENT)
        row = self.board()["pending_ho"]["rows"][0]
        self.assertEqual(row["item"], "REFRESHMENT")


class SalaryTests(AccountsBoardTestCase):
    """Salary advances, one row per voucher, without reading any payroll."""

    def test_only_salary_heads_count(self):
        self.payment(5000, head=STAFF_DEBTOR, item="Parveen khatun")
        self.payment(9000, head=REFRESHMENT, item="Tea")

        salary = self.board()["salary"]
        self.assertEqual(salary["total"], 5000.0)
        self.assertEqual(salary["count"], 1)

    def test_a_row_is_labelled_with_the_item_when_it_says_something(self):
        self.payment(1000, head=STAFF_DEBTOR, item="Parveen khatun")
        self.assertEqual(
            self.board()["salary"]["rows"][0]["description"], "Parveen khatun"
        )

    def test_a_generic_item_falls_back_to_the_narrative(self):
        """"Advacne" is the custodian's word on 20 of the 26 live rows and
        carries no information; the name is in the narrative instead."""
        entry = self.payment(2000, head=STAFF_DEBTOR, item="Advacne")
        entry.detail = "Cash paid Advance to Sachin (Deduct of July month salary)"
        entry.save(update_fields=["detail"])

        self.assertEqual(
            self.board()["salary"]["rows"][0]["description"],
            "Cash paid Advance to Sachin (Deduct of July month salary)",
        )

    def test_every_voucher_is_a_row_so_the_rows_carry_the_total(self):
        """The panel used to show a total with almost nothing under it.

        It grouped by ``advance_holder``, which only 1 of 26 live salary
        vouchers has -- so 2,000 of September's money appeared as a total above
        an empty table reading "No salary advance in this period".
        """
        self.payment(1000, head=STAFF_DEBTOR, item="Parveen khatun")
        self.payment(1000, head=STAFF_DEBTOR, item="Shyam shukla")

        salary = self.board()["salary"]
        self.assertEqual(len(salary["rows"]), 2)
        self.assertEqual(sum(r["amount"] for r in salary["rows"]), salary["total"])

    def test_it_does_not_group_by_advance_holder(self):
        """``advance_holder`` is "whose float this clears", not "who it was for".

        The live register proves the two differ: its one holder-bearing salary
        row is booked to Jasmeet Singh and reads "Advance to Hardeep Singh".
        Grouping by it produces confident, wrong names.
        """
        entry = self.payment(2500, head=STAFF_DEBTOR, item="Advacne", holder=self.jas)
        entry.detail = "Cash paid Advance to Hardeep Singh (deduct of June salary)"
        entry.save(update_fields=["detail"])

        row = self.board()["salary"]["rows"][0]
        self.assertIn("Hardeep Singh", row["description"])
        self.assertNotIn("Jasmeet", row["description"])

    def test_newest_first(self):
        self.payment(1000, head=STAFF_DEBTOR, item="Older", day=date(2026, 7, 1))
        self.payment(2000, head=STAFF_DEBTOR, item="Newer", day=date(2026, 9, 1))

        rows = self.board()["salary"]["rows"]
        self.assertEqual([r["description"] for r in rows], ["Newer", "Older"])


class NameMaskingTests(AccountsBoardTestCase):
    """A wall screen gets the figures. It does not get the staff list.

    Every reader here holds the FEED right and not ``can_view_cash_book``,
    which is exactly the carousel display login's position: allowed to open the
    board, not allowed to know who is holding the money. A user holding neither
    is a different test -- they get nothing at all, which the section machinery
    already guarantees and ``WithheldTests`` pins.
    """

    def wall_user(self, name="wall"):
        """A login with the board feed right and no cash book right."""
        from django.contrib.auth.models import Permission
        from django.contrib.contenttypes.models import ContentType

        user = User.objects.create(email=f"{name}@jivo.in", full_name="Wall TV")
        # get_or_create rather than get: the feed rights are minted by a DATA
        # migration, and the sqlite test settings disable migrations.
        content_type, _ = ContentType.objects.get_or_create(
            app_label="control_boards", model="boardfeed"
        )
        permission, _ = Permission.objects.get_or_create(
            codename="can_read_cash_book_feed",
            content_type=content_type,
            defaults={"name": "Board feed: cash book"},
        )
        user.user_permissions.add(permission)
        # has_perm caches per instance; re-fetch so the next check is honest.
        return User.objects.get(pk=user.pk)

    def setUp(self):
        self.advance(self.bunty, 15000)
        self.payment(
            5000, head=STAFF_DEBTOR, holder=self.bunty, item="Hardeep Singh"
        )

    def test_a_reader_without_the_cash_book_right_sees_no_names(self):
        board = self.board(user=self.wall_user())

        self.assertFalse(board["meta"]["names_visible"])
        self.assertEqual(
            board["cash_issued"]["holders"]["holding"]["rows"][0]["name"], "Person 1"
        )
        # A narrative names somebody mid-sentence, so the label is masked
        # whole rather than partially.
        self.assertEqual(board["salary"]["rows"][0]["description"], "Voucher 1")
        self.assertEqual(board["salary"]["rows"][0]["detail"], "")

    def test_masking_hides_the_name_and_nothing_else(self):
        """The totals and counts are the point of the board; only who goes.

        10,000 rather than the 15,000 handed over, and that is the register
        working: ``setUp``'s 5,000 salary payment is booked against Bunty as
        ``advance_holder``, which is the act of explaining 5,000 of what he was
        holding. Masking a name must not disturb that arithmetic.
        """
        board = self.board(user=self.wall_user("wall2"))

        holding = board["cash_issued"]["holders"]["holding"]
        self.assertEqual(holding["total"], 10000.0)
        self.assertEqual(holding["people"], 1)
        self.assertEqual(board["salary"]["total"], 5000.0)

        # And the same figures reach a reader who may see the names, so the
        # mask is provably the only difference.
        named = self.board()["cash_issued"]["holders"]["holding"]
        self.assertEqual(named["total"], holding["total"])
        self.assertEqual(named["rows"][0]["name"], "Bunty")

    def test_masked_people_stay_distinguishable(self):
        """Thirteen holders must not collapse into one anonymous row."""
        self.advance(self.jas, 8000)

        rows = self.board(user=self.wall_user("wall3"))[
            "cash_issued"
        ]["holders"]["holding"]["rows"]
        self.assertEqual([r["name"] for r in rows], ["Person 1", "Person 2"])

    def test_a_card_is_named_even_when_people_are_not(self):
        """A card's name is printed on the card. It is not personal data."""
        AtmReceipt.objects.create(
            account=self.card, received_on=TODAY, amount=Decimal("1000")
        )
        rows = self.board(user=self.wall_user("wall4"))["imprest"]["rows"]
        self.assertEqual(rows[0]["name"], "Imprest Debit Card (Vishal)")


class ImprestTests(AccountsBoardTestCase):
    """What was loaded onto the card, and the drawer it is not."""

    def test_the_total_is_what_was_loaded_onto_the_card(self):
        self.load(100000)
        self.load(50000)

        imprest = self.board()["imprest"]
        self.assertEqual(imprest["total"], 150000.0)
        self.assertEqual(imprest["count"], 2)

    def test_the_drawer_is_reported_apart_and_never_added(self):
        """A card is loaded, then drawn off at a machine -- two views of one
        float. Summing them would double the money, and the two figures are
        close enough that a wrong one would never look wrong."""
        self.load(100000)
        self.receipt(60000, card=self.card)
        self.receipt(5000)

        imprest = self.board()["imprest"]
        self.assertEqual(imprest["total"], 100000.0)
        self.assertEqual(imprest["into_box"]["total"], 65000.0)
        self.assertEqual(imprest["into_box"]["drawn_off_card"], 60000.0)
        self.assertEqual(imprest["into_box"]["handed_in"], 5000.0)
        # The headline carries the same split, so no consumer has to add them.
        headline = self.board()["headline"]
        self.assertEqual(headline["imprest_issued"], 100000.0)
        self.assertEqual(headline["into_box"], 65000.0)

    def test_rows_are_individual_top_ups_newest_first(self):
        """One row per loading, not per card.

        There is a single card on the live book, so grouping by card produced a
        one-line table that answered nothing. The question is "when, and how
        much".
        """
        self.load(70000, day=date(2026, 9, 1))
        self.load(100000, day=date(2026, 9, 14))

        rows = self.board()["imprest"]["rows"]
        self.assertEqual([r["amount"] for r in rows], [100000.0, 70000.0])
        self.assertEqual([r["last_updated"] for r in rows], ["2026-09-14", "2026-09-01"])

    def test_top_ups_are_filtered_to_the_period(self):
        self.load(70000, day=date(2026, 8, 3))
        self.load(100000, day=date(2026, 9, 14))

        september = self.board(period=(2026, 9))["imprest"]
        self.assertEqual(september["total"], 100000.0)
        self.assertEqual(len(september["rows"]), 1)

    def test_a_card_is_still_named_for_the_holder_count(self):
        self.load(100000)
        imprest = self.board()["imprest"]

        self.assertEqual(imprest["holders"], 1)
        self.assertEqual(imprest["cards"][0]["name"], "Imprest Debit Card (Vishal)")
        self.assertEqual(imprest["cards"][0]["amount"], 100000.0)

    def test_another_companys_card_is_not_counted(self):
        theirs = AtmAccount.objects.create(company=self.other, name="Their card")
        self.load(100000)
        self.load(999999, card=theirs)

        self.assertEqual(self.board()["imprest"]["total"], 100000.0)
