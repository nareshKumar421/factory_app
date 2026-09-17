"""Several distributors' bills on one return.

A return is a truckload. A vehicle coming back off a market run carries the bills
of whoever it called on, and until now the second bill was refused -- "All
invoices on a return must be for the same customer" -- which forced the clerk to
book one return per customer for a single truck, each of them claiming the same
vehicle and the same gate arrival.

Nothing needed that rule: every bill posts its own A/R Return under its own
CardCode (see `tests_posting`), so the customer belongs to the bill. These tests
pin that the bills are accepted, that each keeps its own customer, and that the
header -- which the list row, the search and the item picker all read -- names
the first of them and hands on when that one is removed.

No SAP: the bill lookup is patched at the seam `_attach_invoice` reads it from.
"""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from company.models import Company
from driver_management.models import Driver
from vehicle_management.models import Vehicle

from .models import GoodsReturnStatus
from .serializers import GoodsReturnListSerializer
from .services import GoodsReturnService

BILLS = {
    "1500": {
        "doc_entry": 5001,
        "doc_num": "1500",
        "card_code": "CUST001",
        "card_name": "Sharma Traders",
        "items": [
            {
                "line_num": 0,
                "item_code": "FG0000151",
                "item_name": "Olive 1L",
                "uom": "PCS",
                "quantity": 100,
                "rate": 250,
                "tax_code": "CG+SG@5",
            }
        ],
    },
    "1501": {
        "doc_entry": 5002,
        "doc_num": "1501",
        "card_code": "CUST002",
        "card_name": "Verma Distributors",
        "items": [
            {
                "line_num": 0,
                "item_code": "FG0000329",
                "item_name": "Canola 5L",
                "uom": "PCS",
                "quantity": 40,
                "rate": 900,
                "tax_code": "CG+SG@5",
            }
        ],
    },
    # A second bill of the first customer -- one market run can call twice.
    "1502": {
        "doc_entry": 5003,
        "doc_num": "1502",
        "card_code": "CUST001",
        "card_name": "Sharma Traders",
        "items": [],
    },
}


class MultiCustomerReturnTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Jivo Oil", code="OIL")
        self.user = get_user_model().objects.create(
            email="clerk@example.com", full_name="Return Clerk"
        )
        self.vehicle = Vehicle.objects.create(vehicle_number="PB01AB1234")
        self.driver = Driver.objects.create(
            name="Ranjit", mobile_no="9990001111", license_no="DL-1"
        )
        self.service = GoodsReturnService(self.company)
        self.allowed = [self.company.id]

        patcher = patch.object(
            GoodsReturnService,
            "_lookup_bill",
            lambda self, company, number: BILLS[str(number)],
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def create(self, invoice_numbers):
        return self.service.create_return(
            {
                "basis": "INVOICE",
                "invoice_numbers": invoice_numbers,
                "vehicle_id": self.vehicle.id,
                "driver_id": self.driver.id,
            },
            self.user,
        )

    # -- booking ---------------------------------------------------------------

    def test_bills_of_two_customers_ride_one_return(self):
        gr = self.create(["1500", "1501"])
        self.assertEqual(gr.status, GoodsReturnStatus.AWAITING_ARRIVAL)
        self.assertEqual(
            [ref.customer_code for ref in gr.active_invoice_refs],
            ["CUST001", "CUST002"],
        )

    def test_a_second_customers_bill_can_be_added_after_creation(self):
        gr = self.create(["1500"])
        gr = self.service.add_invoice_ref(gr.id, "1501", self.user, self.allowed)
        self.assertEqual(
            [ref.sap_invoice_doc_num for ref in gr.active_invoice_refs], ["1500", "1501"]
        )

    def test_each_bills_lines_are_snapshotted_under_it(self):
        gr = self.create(["1500", "1501"])
        first, second = gr.active_invoice_refs
        self.assertEqual(
            [line.item_code for line in gr.lines.filter(invoice_ref=first)], ["FG0000151"]
        )
        self.assertEqual(
            [line.item_code for line in gr.lines.filter(invoice_ref=second)], ["FG0000329"]
        )

    def test_the_same_bill_twice_is_still_refused(self):
        gr = self.create(["1500"])
        with self.assertRaisesMessage(ValueError, "already added"):
            self.service.add_invoice_ref(gr.id, "1500", self.user, self.allowed)

    # -- the header's customer -------------------------------------------------

    def test_the_header_names_the_first_bills_customer(self):
        gr = self.create(["1500", "1501"])
        self.assertEqual(gr.customer_code, "CUST001")
        self.assertEqual(gr.customer_name, "Sharma Traders")

    def test_removing_the_first_bill_hands_the_header_to_the_next(self):
        """Else the return keeps naming a customer no longer on it."""
        gr = self.create(["1500", "1501"])
        first = gr.active_invoice_refs[0]
        gr = self.service.remove_invoice_ref(gr.id, first.id, self.user, self.allowed)
        self.assertEqual(gr.customer_code, "CUST002")
        self.assertEqual(gr.customer_name, "Verma Distributors")

    def test_removing_a_later_bill_leaves_the_header_alone(self):
        gr = self.create(["1500", "1501"])
        second = gr.active_invoice_refs[1]
        gr = self.service.remove_invoice_ref(gr.id, second.id, self.user, self.allowed)
        self.assertEqual(gr.customer_code, "CUST001")

    # -- what the list row shows -----------------------------------------------

    def test_the_row_names_every_customer_on_the_return(self):
        gr = self.create(["1500", "1501"])
        row = GoodsReturnListSerializer(gr).data
        self.assertEqual(row["customer_names"], ["Sharma Traders", "Verma Distributors"])

    def test_one_customers_two_bills_are_named_once(self):
        gr = self.create(["1500", "1502"])
        row = GoodsReturnListSerializer(gr).data
        self.assertEqual(row["customer_names"], ["Sharma Traders"])
