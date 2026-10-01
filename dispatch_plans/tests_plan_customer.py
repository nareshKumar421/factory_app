"""
A plan is never saved without its customer while SAP can say who it is.

The single-bill save carries no customer field, so until this, every bill only
ever saved on its own kept a blank customer and printed as "Unnamed customer"
on every view that groups by it. What has to hold: a save fills the customer
from the SAP invoice, an already-blank plan is filled on its next save, a plan
that holds everything costs SAP nothing, a value the caller supplied is never
second-guessed, and a SAP failure never fails the save.
"""

from datetime import date
from unittest.mock import MagicMock

from django.contrib.auth import get_user_model
from django.test import TestCase

from company.models import Company

from .models import DispatchPlan
from .services import DispatchPlansService

User = get_user_model()


def _bill(doc_entry, **overrides):
    bill = {
        "doc_entry": doc_entry,
        "doc_num": f"6260{doc_entry}",
        "card_code": "CUSTA000981",
        "card_name": "AGGARWAL AGENCIES",
    }
    bill.update(overrides)
    return bill


class PlanCustomerFromSAPTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        self.user = User.objects.create_user(
            email="plan-customer@example.com",
            password="testpass123",
            full_name="Plan Customer",
            employee_code="PLANCUST01",
        )
        self.service = DispatchPlansService(company_code=self.company.code)
        self.service.reader = MagicMock()
        self.service.reader.list_bills_by_doc_entries.side_effect = lambda entries: [
            _bill(entry) for entry in entries
        ]

    def _plan(self, doc_entry, **fields):
        return DispatchPlan.objects.create(
            company=self.company,
            sap_invoice_doc_entry=doc_entry,
            created_by=self.user,
            updated_by=self.user,
            **fields,
        )

    def test_a_bill_saved_on_its_own_gets_its_customer(self):
        """The set-dispatch-date save sends a date and nothing else."""
        plan = self.service.update_plan(
            sap_invoice_doc_entry=81050,
            data={"dispatch_date": date(2026, 9, 30)},
            user=self.user,
        )

        plan.refresh_from_db()
        self.assertEqual(plan.customer_code, "CUSTA000981")
        self.assertEqual(plan.customer_name, "AGGARWAL AGENCIES")
        self.assertEqual(plan.sap_invoice_doc_num, "626081050")
        # One SAP read fills the DocNum and the customer together.
        self.service.reader.list_bills_by_doc_entries.assert_called_once_with([81050])

    def test_a_plan_saved_blank_before_is_filled_on_its_next_save(self):
        self._plan(81074, sap_invoice_doc_num="626090634", dispatch_date=date(2026, 9, 29))

        self.service.update_plan(
            sap_invoice_doc_entry=81074,
            data={"dispatch_date": date(2026, 9, 30)},
            user=self.user,
        )

        plan = DispatchPlan.objects.get(company=self.company, sap_invoice_doc_entry=81074)
        self.assertEqual(plan.customer_name, "AGGARWAL AGENCIES")
        # The DocNum it already held is not replaced by SAP's.
        self.assertEqual(plan.sap_invoice_doc_num, "626090634")

    def test_a_plan_holding_everything_costs_sap_nothing(self):
        self._plan(
            81081,
            sap_invoice_doc_num="626090637",
            customer_code="CUSTA000007",
            customer_name="ARJUN DASS & SONS PUNJAB NEW",
        )

        self.service.update_plan(
            sap_invoice_doc_entry=81081,
            data={"dispatch_date": date(2026, 9, 30)},
            user=self.user,
        )

        self.service.reader.list_bills_by_doc_entries.assert_not_called()

    def test_only_blanks_are_filled(self):
        """A code without a name gets the name; the code it had stays."""
        self._plan(81073, sap_invoice_doc_num="626090633", customer_code="CUSTA001111")
        self.service.reader.list_bills_by_doc_entries.side_effect = lambda entries: [
            _bill(81073, card_code="CUSTA999999", card_name="STARLITE INTERNATIONAL")
        ]

        self.service.update_plan(
            sap_invoice_doc_entry=81073,
            data={"dispatch_date": date(2026, 9, 30)},
            user=self.user,
        )

        plan = DispatchPlan.objects.get(company=self.company, sap_invoice_doc_entry=81073)
        self.assertEqual(plan.customer_code, "CUSTA001111")
        self.assertEqual(plan.customer_name, "STARLITE INTERNATIONAL")

    def test_bulk_dating_fills_every_bill(self):
        self._plan(81022, sap_invoice_doc_num="626090613")

        self.service.bulk_set_dispatch_date(
            doc_entries=[81022, 81023],
            dispatch_date=date(2026, 9, 30),
            user=self.user,
        )

        names = set(
            DispatchPlan.objects.filter(company=self.company).values_list(
                "customer_name", flat=True
            )
        )
        self.assertEqual(names, {"AGGARWAL AGENCIES"})

    def test_an_sap_failure_never_fails_the_save(self):
        self.service.reader.list_bills_by_doc_entries.side_effect = ConnectionError("HANA down")

        plan = self.service.update_plan(
            sap_invoice_doc_entry=81050,
            data={"dispatch_date": date(2026, 9, 30)},
            user=self.user,
        )

        plan.refresh_from_db()
        self.assertEqual(plan.dispatch_date, date(2026, 9, 30))
        self.assertEqual(plan.customer_name, "")

    def test_a_bill_sap_does_not_return_is_left_alone(self):
        self.service.reader.list_bills_by_doc_entries.side_effect = lambda entries: []

        plan = self.service.update_plan(
            sap_invoice_doc_entry=81050,
            data={"dispatch_date": date(2026, 9, 30)},
            user=self.user,
        )

        plan.refresh_from_db()
        self.assertEqual(plan.customer_code, "")
        self.assertEqual(plan.sap_invoice_doc_num, "")
