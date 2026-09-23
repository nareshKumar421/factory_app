"""The payments list is a table: filtered, searched and ordered on the server.

Ordering is a whitelist rather than a pass-through to ``order_by``. Handing user
input straight to the ORM lets somebody sort by a related field they were never
meant to read, and it 500s on a typo instead of falling back.
"""

from decimal import Decimal

from rest_framework import status

from .base import ConstructionTestCase


class ExpenseTableTests(ConstructionTestCase):
    def setUp(self):
        super().setUp()
        self.project = self.make_project(start_date=self.today.replace(day=1))
        self.rows = [
            ("MATERIAL", "90 bags cement", "31500.00", "Verma Traders", 0),
            ("LABOUR", "Mason wages", "7200.00", "", 1),
            ("TRANSPORT", "2 loads sand", "3300.00", "Singh Transport", 2),
            ("MATERIAL", "Steel 8mm", "49600.00", "Verma Traders", 3),
        ]
        for category, description, amount, paid_to, offset in self.rows:
            self.post(
                f"projects/{self.project.id}/expenses/",
                {
                    "spend_date": str(self.today.replace(day=1 + offset)),
                    "category": category,
                    "description": description,
                    "amount": amount,
                    "paid_to": paid_to,
                },
            )

    def _list(self, **params):
        return self.get(f"projects/{self.project.id}/expenses/", **params).data

    # -- the envelope ------------------------------------------------------

    def test_the_list_carries_its_own_count_and_total(self):
        """The footer should not make the client re-add what it was just sent."""
        data = self._list()
        self.assertEqual(data["count"], 4)
        self.assertEqual(Decimal(data["total"]), Decimal("91600.00"))
        self.assertEqual(len(data["results"]), 4)

    def test_the_total_follows_the_filter(self):
        data = self._list(category="MATERIAL")
        self.assertEqual(data["count"], 2)
        self.assertEqual(Decimal(data["total"]), Decimal("81100.00"))

    # -- filters -----------------------------------------------------------

    def test_filter_by_several_categories_at_once(self):
        data = self._list(category="LABOUR,TRANSPORT")
        self.assertEqual(
            {row["category"] for row in data["results"]}, {"LABOUR", "TRANSPORT"}
        )

    def test_filter_by_date_range(self):
        data = self._list(
            **{
                "from": str(self.today.replace(day=2)),
                "to": str(self.today.replace(day=3)),
            }
        )
        self.assertEqual(data["count"], 2)

    def test_search_covers_description_payee_and_bill_number(self):
        self.assertEqual(self._list(search="cement")["count"], 1)
        self.assertEqual(self._list(search="Verma")["count"], 2)
        self.assertEqual(self._list(search="nothing here")["count"], 0)

    def test_search_is_case_insensitive(self):
        self.assertEqual(self._list(search="VERMA")["count"], 2)

    # -- ordering ----------------------------------------------------------

    def test_default_order_is_newest_first(self):
        dates = [row["spend_date"] for row in self._list()["results"]]
        self.assertEqual(dates, sorted(dates, reverse=True))

    def test_order_by_amount_both_ways(self):
        cheapest = self._list(ordering="amount")["results"]
        self.assertEqual(Decimal(cheapest[0]["amount"]), Decimal("3300.00"))

        dearest = self._list(ordering="-amount")["results"]
        self.assertEqual(Decimal(dearest[0]["amount"]), Decimal("49600.00"))

    def test_order_by_description(self):
        names = [row["description"] for row in self._list(ordering="description")["results"]]
        self.assertEqual(names, sorted(names))

    def test_an_unknown_ordering_falls_back_rather_than_erroring(self):
        """A typo, or somebody probing, must not 500 or leak a relation."""
        for attempt in ("nonsense", "project__company__code", "-created_by__password"):
            response = self.get(f"projects/{self.project.id}/expenses/", ordering=attempt)
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            dates = [row["spend_date"] for row in response.data["results"]]
            self.assertEqual(dates, sorted(dates, reverse=True), "fell back to default")

    def test_filter_and_order_together(self):
        data = self._list(category="MATERIAL", ordering="amount")
        self.assertEqual(data["count"], 2)
        self.assertEqual(
            [Decimal(row["amount"]) for row in data["results"]],
            [Decimal("31500.00"), Decimal("49600.00")],
        )

    def test_another_companys_payments_are_never_listed(self):
        from company.models import Company

        other = Company.objects.create(name="Jivo Mart", code="JIVO_MART")
        theirs = self.make_project(company=other)
        response = self.get(f"projects/{theirs.id}/expenses/")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
