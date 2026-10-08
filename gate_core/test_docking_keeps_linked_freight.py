"""A docking never wipes or overrides the freight entered at vehicle linking.

The docking page used to send its blank Freight / Total Freight boxes with every
transport save, and the sync wrote that None onto every bill on the truck --
which is how HR55BE1387's ₹67,999.87 (linked 7 Oct 2026) never reached its
Service GRPO.

    python manage.py test gate_core.test_docking_keeps_linked_freight --settings=config.sqlite_test_settings
"""

from decimal import Decimal
from unittest import mock

from django.test import SimpleTestCase

from gate_core import views_sales_dispatch as docking


class FakePlan:
    def __init__(self, id, freight=None, approval=None, litres="100"):
        self.id = id
        self.freight = self.total_freight = freight
        self.freight_approval_id = approval
        self.total_litres = Decimal(litres)
        self.invoice_weight = None
        self.invoice_amount = None
        self.eway_bill = ""
        self.updated_by = None
        self.saved = []

    def save(self, update_fields):
        self.saved.append(update_fields)


class DockingKeepsLinkedFreightTests(SimpleTestCase):
    def sync(self, plans, data):
        with mock.patch.object(docking, "get_sales_dispatch_dispatch_plans", return_value=plans):
            docking.sync_sales_dispatch_transport_to_plans(entry=object(), data=data, user=None)

    def test_blank_freight_boxes_clear_nothing(self):
        linked = FakePlan(1, Decimal("40000.00"), approval=7)
        unlinked = FakePlan(2, Decimal("900.00"))

        self.sync([linked, unlinked], {"eway_bill": "EWB1", "freight": None, "total_freight": ""})

        self.assertEqual(linked.total_freight, Decimal("40000.00"))
        self.assertEqual(unlinked.total_freight, Decimal("900.00"))
        self.assertEqual(linked.eway_bill, "EWB1")
        self.assertNotIn("freight", linked.saved[0])

    def test_a_docking_freight_never_overrides_the_linked_one(self):
        linked = FakePlan(1, Decimal("40000.00"), approval=7)

        self.sync([linked], {"freight": "50000", "total_freight": "50000"})

        self.assertEqual(linked.freight, Decimal("40000.00"))
        self.assertEqual(linked.total_freight, Decimal("40000.00"))

    def test_it_still_fills_bills_linked_before_freight_was_asked_at_linking(self):
        old_a, old_b = FakePlan(1), FakePlan(2, litres="300")
        linked = FakePlan(3, Decimal("10.00"), approval=7)

        self.sync([old_a, old_b, linked], {"total_freight": "400"})

        self.assertEqual(old_a.total_freight, Decimal("100.00"))
        self.assertEqual(old_b.total_freight, Decimal("300.00"))
        self.assertEqual(old_b.freight, Decimal("300.00"))
        self.assertEqual(linked.total_freight, Decimal("10.00"))
