"""
Tests for handing cash from one person straight to another.

It used to be typed as "cash taken back" off one person and "cash given" to
the other: the balances came out right, but the screen said the box had the
cash in between, and nothing tied the two rows together -- type one and forget
the other and the cash book's "cash in hand" showed notes that were never in
the box. A handover is still those two rows, so every balance reads them as it
always has; what is tested here is that they now come and go together and say
who was on the other side.
"""

from datetime import date
from decimal import Decimal
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command
from rest_framework.exceptions import ValidationError

from . import services
from .models import AdvanceDirection, AdvanceEntry, CashDirection
from .test_api import BASE, CashBookAPITestCase

User = get_user_model()

DAY = date(2026, 10, 13)


class HandoverTestCase(CashBookAPITestCase):
    """Kamal and Sukhmeet each holding cash; Manoj about to get some of it."""

    def setUp(self):
        super().setUp()
        self.kamal = self._person("Kamal HR")
        self.sukhmeet = self._person("Sukhmeet Singh")
        self.manoj = self._person("Manoj")
        # Cash into the box, so the reconciliation has something to hold.
        services.record_entry(
            user=self.custodian,
            company=self.company,
            entry_date="2026-10-01",
            direction=CashDirection.IN,
            amount=Decimal("50000.00"),
            detail="Cash receive by ATM card",
        )
        self.give(self.kamal, "12000.00")
        self.give(self.sukhmeet, "3000.00")

    def _person(self, name):
        return User.objects.create(
            email=f"{name.split()[0].lower()}@cash-book.local",
            full_name=name,
            is_active=False,
        )

    def give(self, person, amount, direction=AdvanceDirection.GIVEN, day="2026-10-01"):
        return services.record_advance(
            user=self.custodian,
            company=self.company,
            person=person,
            entry_date=day,
            direction=direction,
            amount=Decimal(amount),
        )

    def hand_over(self, sources, to=None, detail="manoj ko deye motor repair k"):
        return services.record_handover(
            user=self.custodian,
            company=self.company,
            to_person=to or self.manoj,
            entry_date=DAY,
            sources=[(person, Decimal(amount)) for person, amount in sources],
            detail=detail,
        )

    def balance(self, person):
        return services.advance_balance(self.company, person)


class RecordingAHandoverTests(HandoverTestCase):
    def test_it_moves_cash_from_each_giver_to_the_receiver(self):
        self.hand_over([(self.kamal, "6300.00"), (self.sukhmeet, "500.00")])

        self.assertEqual(self.balance(self.kamal), Decimal("5700.00"))
        self.assertEqual(self.balance(self.sukhmeet), Decimal("2500.00"))
        self.assertEqual(self.balance(self.manoj), Decimal("6800.00"))

    def test_the_box_does_not_move(self):
        """Nothing came into the box and nothing left it."""
        before = services.reconciliation(self.company)
        self.hand_over([(self.kamal, "6300.00"), (self.sukhmeet, "500.00")])
        after = services.reconciliation(self.company)

        self.assertEqual(after["cash_in_hand"], before["cash_in_hand"])
        self.assertEqual(after["advance_given"], before["advance_given"])
        self.assertEqual(after["difference"], Decimal("0.00"))

    def test_each_giver_is_its_own_pair_naming_the_other_side(self):
        legs = self.hand_over([(self.kamal, "6300.00"), (self.sukhmeet, "500.00")])

        self.assertEqual(len(legs), 4)
        for leg in legs:
            other = leg.counterpart
            self.assertEqual(other.counterpart_id, leg.id)
            self.assertEqual(other.amount, leg.amount)
            self.assertNotEqual(other.direction, leg.direction)

        into_manoj = sorted(
            (leg.counterpart.person.full_name, leg.amount)
            for leg in legs
            if leg.person == self.manoj
        )
        self.assertEqual(
            into_manoj,
            [("Kamal HR", Decimal("6300.00")), ("Sukhmeet Singh", Decimal("500.00"))],
        )

    def test_giving_more_than_they_hold_leaves_them_owed(self):
        """They made up the rest from their own pocket."""
        self.hand_over([(self.sukhmeet, "3400.00")])

        self.assertEqual(self.balance(self.sukhmeet), Decimal("-400.00"))
        self.assertEqual(self.balance(self.manoj), Decimal("3400.00"))
        self.assertEqual(
            services.reconciliation(self.company)["difference"], Decimal("0.00")
        )

    def test_nobody_hands_cash_to_themselves(self):
        with self.assertRaises(ValidationError):
            self.hand_over([(self.kamal, "100.00")], to=self.kamal)
        self.assertFalse(AdvanceEntry.objects.filter(entry_date=DAY).exists())

    def test_one_giver_on_two_lines_is_refused(self):
        with self.assertRaises(ValidationError):
            self.hand_over([(self.kamal, "100.00"), (self.kamal, "200.00")])
        self.assertFalse(AdvanceEntry.objects.filter(entry_date=DAY).exists())

    def test_a_refused_line_writes_none_of_the_others(self):
        """All of it or nothing, which is the whole point."""
        with self.assertRaises(ValidationError):
            self.hand_over([(self.sukhmeet, "500.00"), (self.manoj, "100.00")])
        self.assertFalse(AdvanceEntry.objects.filter(entry_date=DAY).exists())


class TakingAHandoverOutTests(HandoverTestCase):
    def setUp(self):
        super().setUp()
        self.legs = self.hand_over(
            [(self.kamal, "6300.00"), (self.sukhmeet, "500.00")]
        )

    def leg(self, person, giver=None):
        return next(
            leg
            for leg in self.legs
            if leg.person == person
            and (giver is None or leg.counterpart.person == giver)
        )

    def test_either_half_takes_the_other_with_it(self):
        services.cancel_advance(user=self.custodian, entry=self.leg(self.kamal))

        self.assertEqual(self.balance(self.kamal), Decimal("12000.00"))
        self.assertEqual(self.balance(self.manoj), Decimal("500.00"))

    def test_from_the_receivers_side_too(self):
        services.cancel_advance(
            user=self.custodian, entry=self.leg(self.manoj, giver=self.sukhmeet)
        )

        self.assertEqual(self.balance(self.sukhmeet), Decimal("3000.00"))
        self.assertEqual(self.balance(self.manoj), Decimal("6300.00"))
        # Kamal's part was its own pair and stays.
        self.assertEqual(self.balance(self.kamal), Decimal("5700.00"))

    def test_over_http(self):
        self.as_user(self.custodian)
        response = self.client.delete(
            f"{BASE}/advances/{self.leg(self.manoj, giver=self.kamal).id}/"
        )
        self.assertEqual(response.status_code, 204)
        self.assertEqual(self.balance(self.kamal), Decimal("12000.00"))


class LedgerNamesTheOtherSideTests(HandoverTestCase):
    def setUp(self):
        super().setUp()
        self.hand_over([(self.kamal, "6300.00")])

    def movements(self, person, **params):
        self.as_user(self.viewer)
        response = self.client.get(f"{BASE}/advances/holders/{person.id}/", params)
        self.assertEqual(response.status_code, 200)
        return response.data["movements"]

    def test_the_receiver_sees_who_it_came_from(self):
        (row,) = self.movements(self.manoj)
        self.assertEqual(row["kind"], "GIVEN")
        self.assertEqual(row["counterpart_person_id"], self.kamal.id)
        self.assertEqual(row["counterpart_person_name"], "Kamal HR")

    def test_the_giver_sees_who_it_went_to(self):
        row = next(r for r in self.movements(self.kamal) if r["kind"] == "RETURNED")
        self.assertEqual(row["counterpart_person_name"], "Manoj")
        self.assertEqual(Decimal(row["balance_after"]), Decimal("5700.00"))

    def test_a_box_handout_names_nobody(self):
        row = next(r for r in self.movements(self.kamal) if r["kind"] == "GIVEN")
        self.assertIsNone(row["counterpart_person_id"])
        self.assertIsNone(row["counterpart_person_name"])

    def test_a_taken_out_handover_still_names_the_other_side(self):
        leg = AdvanceEntry.objects.get(person=self.manoj)
        services.cancel_advance(user=self.custodian, entry=leg)
        (row,) = self.movements(self.manoj, include_cancelled="true")
        self.assertFalse(row["is_active"])
        self.assertEqual(row["counterpart_person_name"], "Kamal HR")


class HandoverEndpointTests(HandoverTestCase):
    def post(self, payload, user=None):
        self.as_user(user or self.custodian)
        return self.client.post(f"{BASE}/advances/handovers/", payload, format="json")

    def payload(self, **overrides):
        return {
            "to_person": self.manoj.id,
            "entry_date": "2026-10-13",
            "detail": "motor repair",
            "sources": [
                {"person": self.kamal.id, "amount": "6300.00"},
                {"person": self.sukhmeet.id, "amount": "500.00"},
            ],
            **overrides,
        }

    def test_it_records_every_half(self):
        response = self.post(self.payload())
        self.assertEqual(response.status_code, 201)
        self.assertEqual(len(response.data), 4)
        self.assertEqual(self.balance(self.manoj), Decimal("6800.00"))
        self.assertEqual(
            {row["counterpart_person"] for row in response.data if row["person"] == self.manoj.id},
            {self.kamal.id, self.sukhmeet.id},
        )

    def test_a_viewer_cannot(self):
        self.assertEqual(self.post(self.payload(), user=self.viewer).status_code, 403)
        self.assertEqual(self.balance(self.manoj), Decimal("0.00"))

    def test_it_needs_somebody_to_have_given_it(self):
        response = self.post(self.payload(sources=[]))
        self.assertEqual(response.status_code, 400)
        self.assertIn("sources", response.data)

    def test_an_amount_of_nothing_is_refused(self):
        response = self.post(
            self.payload(sources=[{"person": self.kamal.id, "amount": "0"}])
        )
        self.assertEqual(response.status_code, 400)

    def test_the_receiver_as_a_giver_is_refused(self):
        response = self.post(
            self.payload(sources=[{"person": self.manoj.id, "amount": "100.00"}])
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("themselves", str(response.data["sources"]))

    def test_it_lands_in_the_callers_company(self):
        self.as_user(self.custodian, company=self.other_company)
        self.client.post(
            f"{BASE}/advances/handovers/", self.payload(), format="json"
        )
        self.assertEqual(self.balance(self.manoj), Decimal("0.00"))
        self.assertEqual(
            services.advance_balance(self.other_company, self.manoj),
            Decimal("6800.00"),
        )


class LinkingOldPairsTests(HandoverTestCase):
    """Pairs typed as "taken back" + "given" before handovers existed."""

    def old_pair(self, giver, receiver, amount, day="2026-09-29"):
        out_of = self.give(giver, amount, AdvanceDirection.RETURNED, day=day)
        into = self.give(receiver, amount, day=day)
        return out_of, into

    def test_a_clean_pair_is_suggested(self):
        out_of, into = self.old_pair(self.kamal, self.manoj, "918.00")
        self.assertEqual(
            services.handover_candidates(self.company), [(out_of, into)]
        )

    def test_two_possible_givers_suggest_nothing(self):
        """Either could be it, and a guess links the wrong person."""
        self.old_pair(self.kamal, self.manoj, "500.00")
        self.give(self.sukhmeet, "500.00", AdvanceDirection.RETURNED, day="2026-09-29")
        self.assertEqual(services.handover_candidates(self.company), [])

    def test_one_person_both_ways_is_not_a_handover(self):
        self.give(self.kamal, "70.00", day="2026-10-08")
        self.give(self.kamal, "70.00", AdvanceDirection.RETURNED, day="2026-10-08")
        self.assertEqual(services.handover_candidates(self.company), [])

    def test_linking_keeps_the_balances_and_ties_the_halves(self):
        out_of, into = self.old_pair(self.kamal, self.manoj, "918.00")
        services.link_handover(user=self.custodian, out_of=out_of, into=into)

        self.assertEqual(self.balance(self.kamal), Decimal("11082.00"))
        self.assertEqual(self.balance(self.manoj), Decimal("918.00"))
        services.cancel_advance(user=self.custodian, entry=into)
        self.assertEqual(self.balance(self.kamal), Decimal("12000.00"))
        self.assertEqual(self.balance(self.manoj), Decimal("0.00"))
        self.assertEqual(services.handover_candidates(self.company), [])

    def test_a_mismatched_pair_is_refused(self):
        out_of = self.give(self.kamal, "918.00", AdvanceDirection.RETURNED)
        into = self.give(self.manoj, "900.00")
        with self.assertRaises(ValidationError):
            services.link_handover(user=self.custodian, out_of=out_of, into=into)
        out_of.refresh_from_db()
        self.assertIsNone(out_of.counterpart_id)

    def run_command(self, *args):
        out = StringIO()
        call_command(
            "link_advance_handovers", "--company", self.company.code, *args, stdout=out
        )
        return out.getvalue()

    def test_the_command_writes_nothing_without_apply(self):
        out_of, into = self.old_pair(self.kamal, self.manoj, "918.00")
        output = self.run_command("--pair", f"{out_of.id}:{into.id}")
        self.assertIn("Nothing written", output)
        out_of.refresh_from_db()
        self.assertIsNone(out_of.counterpart_id)

    def test_the_command_links_with_apply(self):
        out_of, into = self.old_pair(self.kamal, self.manoj, "918.00")
        self.run_command("--pair", f"{out_of.id}:{into.id}", "--apply")
        out_of.refresh_from_db()
        self.assertEqual(out_of.counterpart_id, into.id)

    def test_one_bad_pair_links_none_of_them(self):
        good = self.old_pair(self.kamal, self.manoj, "918.00")
        bad_out = self.give(self.sukhmeet, "200.00", AdvanceDirection.RETURNED)
        bad_into = self.give(self.manoj, "250.00")
        with self.assertRaises(CommandError):
            self.run_command(
                "--pair", f"{good[0].id}:{good[1].id}",
                "--pair", f"{bad_out.id}:{bad_into.id}",
                "--apply",
            )
        good[0].refresh_from_db()
        self.assertIsNone(good[0].counterpart_id)
