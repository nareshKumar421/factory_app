"""
Tests for the card and the advances.

The thing worth proving here is the accounting the spreadsheet actually does,
because it is not the obvious one. The cash book's balance is what the
custodian is **accountable for** -- cash in the box plus whatever is out with
people who have not yet said where it went. So handing somebody an advance
moves nothing: the money has only changed pocket. It reaches the book later, as
the expenses they explain.

That is why not one of the sheet's advance handouts ("Bunty jo ko deye 15000")
appears in its cash register, while every expense those handouts paid for does.
"""

from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.exceptions import ValidationError

from company.models import Company

from . import services
from .models import (
    AdvanceDirection,
    AtmAccount,
    CashBranch,
    CashDirection,
)

User = get_user_model()


class CardAndAdvanceTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        cls.other_company = Company.objects.create(name="Jivo Mart", code="JIVO_MART")
        cls.branch = CashBranch.objects.create(company=cls.company, name="Oil")
        cls.custodian = User.objects.create(email="custodian@example.com")
        cls.bunty = User.objects.create(email="bunty@example.com")
        cls.jasmeet = User.objects.create(email="jasmeet@example.com")
        cls.card = AtmAccount.objects.create(
            company=cls.company,
            name="Ginni Vg Imprest Debit Card (Vishal)",
            opening_balance=Decimal("19538.00"),
        )

    def withdraw(self, amount="50000.00", card=None, **kwargs):
        return services.record_entry(
            user=self.custodian,
            company=self.company,
            entry_date=kwargs.pop("entry_date", date(2026, 6, 4)),
            direction=CashDirection.IN,
            amount=Decimal(amount),
            detail="Cash receive by ATM card",
            atm_account=self.card if card is None else card,
            **kwargs,
        )

    def expense(self, amount="6000.00", holder=None, **kwargs):
        return services.record_entry(
            user=self.custodian,
            company=self.company,
            entry_date=kwargs.pop("entry_date", date(2026, 6, 4)),
            direction=CashDirection.OUT,
            amount=Decimal(amount),
            detail=kwargs.pop("detail", "Cash paid for refreshment"),
            branch=self.branch,
            gl_account_code="5630004",
            gl_account_name="REFRESHMENT",
            advance_holder=holder,
            **kwargs,
        )

    def give(self, person, amount, **kwargs):
        return services.record_advance(
            user=self.custodian,
            company=self.company,
            person=person,
            entry_date=kwargs.pop("entry_date", date(2026, 6, 4)),
            direction=AdvanceDirection.GIVEN,
            amount=Decimal(amount),
            detail=kwargs.pop("detail", "Bunty jo ko deye"),
        )


class CardTests(CardAndAdvanceTestCase):
    def test_a_new_card_holds_its_opening_balance(self):
        self.assertEqual(services.atm_balance(self.card), Decimal("19538.00"))

    def test_money_paid_on_raises_it(self):
        services.record_atm_receipt(
            user=self.custodian,
            account=self.card,
            received_on=date(2026, 6, 4),
            amount=Decimal("100000.00"),
            detail="Imprest received from Vicky Vg",
        )
        self.assertEqual(services.atm_balance(self.card), Decimal("119538.00"))

    def test_a_withdrawal_takes_it_off_the_card_and_puts_it_in_the_book(self):
        """The sheet's own first three movements: 19538 + 100000 - 50000."""
        services.record_atm_receipt(
            user=self.custodian,
            account=self.card,
            received_on=date(2026, 6, 4),
            amount=Decimal("100000.00"),
        )
        entry = self.withdraw("50000.00")

        self.assertEqual(services.atm_balance(self.card), Decimal("69538.00"))
        self.assertEqual(services.current_balance(self.company), Decimal("50000.00"))
        self.assertEqual(entry.atm_account_id, self.card.id)

    def test_cash_from_somewhere_other_than_a_card_leaves_the_card_alone(self):
        """"Cash receive by Arvinder vg" is real money with no card behind it."""
        loose = services.record_entry(
            user=self.custodian,
            company=self.company,
            entry_date=date(2026, 6, 4),
            direction=CashDirection.IN,
            amount=Decimal("20000.00"),
            detail="Cash receive by Arvinder vg",
        )

        self.assertIsNone(loose.atm_account_id)
        # In the book, but nothing came off the card.
        self.assertEqual(services.current_balance(self.company), Decimal("20000.00"))
        self.assertEqual(services.atm_balance(self.card), Decimal("19538.00"))
        self.assertEqual(services.atm_statement(self.card), [])

    def test_cancelling_a_withdrawal_puts_it_back_on_the_card(self):
        entry = self.withdraw("50000.00")
        services.cancel_entry(user=self.custodian, entry=entry)
        self.assertEqual(services.atm_balance(self.card), Decimal("19538.00"))

    def test_the_statement_runs_in_date_order_with_a_running_balance(self):
        services.record_atm_receipt(
            user=self.custodian,
            account=self.card,
            received_on=date(2026, 6, 4),
            amount=Decimal("100000.00"),
        )
        self.withdraw("50000.00", entry_date=date(2026, 6, 4))
        self.withdraw("50000.00", entry_date=date(2026, 6, 6))

        movements = services.atm_statement(self.card)
        self.assertEqual(
            [(row["kind"], str(row["balance_after"])) for row in movements],
            [
                ("RECEIPT", "119538.00"),
                ("WITHDRAWAL", "69538.00"),
                ("WITHDRAWAL", "19538.00"),
            ],
        )

    def test_a_payment_cannot_name_a_card(self):
        with self.assertRaises(ValidationError) as caught:
            self.expense(atm_account=self.card)
        self.assertIn("atm_account", caught.exception.detail)

    def test_another_companys_card_is_refused(self):
        theirs = AtmAccount.objects.create(
            company=self.other_company, name="Theirs", opening_balance=0
        )
        with self.assertRaises(ValidationError) as caught:
            self.withdraw("100.00", card=theirs)
        self.assertIn("atm_account", caught.exception.detail)

    def test_a_closed_card_cannot_be_drawn_on(self):
        self.card.is_active = False
        self.card.save(update_fields=["is_active"])
        with self.assertRaises(ValidationError):
            self.withdraw("100.00")


class AdvanceTests(CardAndAdvanceTestCase):
    def test_a_new_person_holds_nothing(self):
        self.assertEqual(
            services.advance_balance(self.company, self.bunty), Decimal("0.00")
        )

    def test_handing_cash_over_does_not_move_the_cash_book(self):
        """The whole point: the custodian is accountable for the same total."""
        self.withdraw("50000.00")
        before = services.current_balance(self.company)

        self.give(self.bunty, "15000.00")

        self.assertEqual(services.current_balance(self.company), before)
        self.assertEqual(
            services.advance_balance(self.company, self.bunty), Decimal("15000.00")
        )

    def test_explaining_a_spend_clears_the_advance_and_books_the_expense(self):
        self.withdraw("50000.00")
        self.give(self.bunty, "15000.00")

        self.expense("3400.00", holder=self.bunty, detail="Unloading charge")

        self.assertEqual(
            services.advance_balance(self.company, self.bunty), Decimal("11600.00")
        )
        self.assertEqual(services.current_balance(self.company), Decimal("46600.00"))

    def test_an_expense_the_custodian_paid_names_nobody(self):
        self.withdraw("50000.00")
        self.expense("6000.00")
        self.assertEqual(services.current_balance(self.company), Decimal("44000.00"))
        self.assertEqual(
            services.advance_balance(self.company, self.bunty), Decimal("0.00")
        )

    def test_cash_handed_back_lowers_the_advance_and_leaves_the_book_alone(self):
        self.withdraw("50000.00")
        self.give(self.bunty, "15000.00")
        before = services.current_balance(self.company)

        services.record_advance(
            user=self.custodian,
            company=self.company,
            person=self.bunty,
            entry_date=date(2026, 6, 10),
            direction=AdvanceDirection.RETURNED,
            amount=Decimal("9000.00"),
            detail="Cash muje deya",
        )

        self.assertEqual(
            services.advance_balance(self.company, self.bunty), Decimal("6000.00")
        )
        self.assertEqual(services.current_balance(self.company), before)

    def test_two_people_are_counted_apart(self):
        self.give(self.bunty, "15000.00")
        self.give(self.jasmeet, "7830.00")
        self.expense("350.00", holder=self.jasmeet)

        self.assertEqual(
            services.advance_balance(self.company, self.bunty), Decimal("15000.00")
        )
        self.assertEqual(
            services.advance_balance(self.company, self.jasmeet), Decimal("7480.00")
        )

    def test_cancelling_an_expense_puts_it_back_on_the_advance(self):
        self.give(self.bunty, "15000.00")
        entry = self.expense("3400.00", holder=self.bunty)
        services.cancel_entry(user=self.custodian, entry=entry)
        self.assertEqual(
            services.advance_balance(self.company, self.bunty), Decimal("15000.00")
        )

    def test_cancelling_a_handout_takes_it_off_the_advance(self):
        given = self.give(self.bunty, "15000.00")
        services.cancel_advance(user=self.custodian, entry=given)
        self.assertEqual(
            services.advance_balance(self.company, self.bunty), Decimal("0.00")
        )

    def test_a_receipt_cannot_clear_an_advance(self):
        with self.assertRaises(ValidationError) as caught:
            self.withdraw("100.00", advance_holder=self.bunty)
        self.assertIn("advance_holder", caught.exception.detail)

    def test_spending_more_than_was_advanced_is_allowed_and_goes_negative(self):
        """They put their own money in. The register has to be able to say so."""
        self.give(self.bunty, "1000.00")
        self.expense("1500.00", holder=self.bunty)
        self.assertEqual(
            services.advance_balance(self.company, self.bunty), Decimal("-500.00")
        )

    def test_the_holder_list_carries_everyone_who_ever_held_a_float(self):
        self.give(self.bunty, "15000.00")
        self.give(self.jasmeet, "7830.00")
        services.record_advance(
            user=self.custodian,
            company=self.company,
            person=self.jasmeet,
            entry_date=date(2026, 6, 10),
            direction=AdvanceDirection.RETURNED,
            amount=Decimal("7830.00"),
        )

        holders = services.advance_holders(self.company)
        balances = {row["person"].email: row["balance"] for row in holders}
        self.assertEqual(balances["bunty@example.com"], Decimal("15000.00"))
        # Settled back to nothing, and still listed -- they are not a new face
        # the next time they take cash.
        self.assertEqual(balances["jasmeet@example.com"], Decimal("0.00"))

    def test_the_statement_shows_handouts_returns_and_explanations_together(self):
        self.give(self.bunty, "15000.00", entry_date=date(2026, 6, 4))
        self.expense(
            "3400.00", holder=self.bunty, entry_date=date(2026, 6, 6),
            detail="Unloading charge",
        )
        services.record_advance(
            user=self.custodian,
            company=self.company,
            person=self.bunty,
            entry_date=date(2026, 6, 10),
            direction=AdvanceDirection.RETURNED,
            amount=Decimal("1000.00"),
        )

        movements = services.advance_statement(self.company, self.bunty)
        self.assertEqual(
            [(row["kind"], str(row["balance_after"])) for row in movements],
            [
                ("GIVEN", "15000.00"),
                ("EXPLAINED", "11600.00"),
                ("RETURNED", "10600.00"),
            ],
        )

    def test_each_company_counts_its_own_advances(self):
        self.give(self.bunty, "15000.00")
        services.record_advance(
            user=self.custodian,
            company=self.other_company,
            person=self.bunty,
            entry_date=date(2026, 6, 4),
            direction=AdvanceDirection.GIVEN,
            amount=Decimal("500.00"),
        )
        self.assertEqual(
            services.advance_balance(self.company, self.bunty), Decimal("15000.00")
        )
        self.assertEqual(
            services.advance_balance(self.other_company, self.bunty), Decimal("500.00")
        )
