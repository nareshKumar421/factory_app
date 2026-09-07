"""The booking flow: the gate sees a return from its first page onwards.

The vehicle is the first thing the clerk fills in, and saving that page puts the
return in the gate's arrival queue while the items are still being keyed in.
These tests pin that hand-off -- and the editing it implies, since the truck can
pull up before the clerk has finished.

Uses the DEBIT_NOTE basis throughout: an invoice-basis return would call SAP.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase

from company.models import Company
from driver_management.models import Driver
from vehicle_management.models import Vehicle

from .models import GoodsReturnStatus
from .services import GoodsReturnService, list_expected_returns, mark_return_in


class GoodsReturnFlowTests(TestCase):
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

    def create(self, **overrides):
        data = {
            "basis": "DEBIT_NOTE",
            "customer_name": "Sharma Traders",
            "vehicle_id": self.vehicle.id,
            "driver_id": self.driver.id,
        }
        data.update(overrides)
        return self.service.create_return(data, self.user)

    # -- creation ------------------------------------------------------------

    def test_a_new_return_is_awaiting_arrival_not_a_draft(self):
        gr = self.create()
        self.assertEqual(gr.status, GoodsReturnStatus.AWAITING_ARRIVAL)
        self.assertEqual(gr.vehicle_id, self.vehicle.id)
        self.assertEqual(gr.driver_id, self.driver.id)

    def test_the_gate_can_see_it_before_a_single_item_is_entered(self):
        gr = self.create()
        self.assertEqual(gr.lines.count(), 0)
        self.assertIn(gr, list(list_expected_returns(self.allowed)))

    def test_no_vehicle_is_refused(self):
        with self.assertRaises(ValueError):
            self.create(vehicle_id=None)

    def test_no_driver_is_refused(self):
        with self.assertRaises(ValueError):
            self.create(driver_id=None)

    def test_an_unknown_vehicle_is_refused(self):
        with self.assertRaises(ValueError):
            self.create(vehicle_id=self.vehicle.id + 999)

    # -- editing while the gate waits ----------------------------------------

    def test_items_can_still_be_saved_once_it_is_awaiting_arrival(self):
        gr = self.create()
        gr = self.service.save_items(
            gr.id,
            [{"item_code": "FG1", "return_quantity": 2}],
            self.user,
            self.allowed,
        )
        self.assertEqual(len(gr.active_lines), 1)

    def test_items_can_still_be_saved_after_the_truck_is_inside(self):
        gr = self.create()
        mark_return_in(gr.id, self.user, {}, self.allowed)
        gr = self.service.save_items(
            gr.id,
            [{"item_code": "FG1", "return_quantity": 2}],
            self.user,
            self.allowed,
        )
        self.assertEqual(gr.status, GoodsReturnStatus.ARRIVED)
        self.assertEqual(len(gr.active_lines), 1)

    # -- the truck itself -----------------------------------------------------

    def test_the_vehicle_can_be_corrected_but_not_cleared(self):
        gr = self.create()
        other = Vehicle.objects.create(vehicle_number="PB02CD5678")
        gr = self.service.set_vehicle(
            gr.id, {"vehicle_id": other.id}, self.user, self.allowed
        )
        self.assertEqual(gr.vehicle_id, other.id)
        with self.assertRaises(ValueError):
            self.service.set_vehicle(
                gr.id, {"vehicle_id": None}, self.user, self.allowed
            )

    def test_the_vehicle_cannot_be_swapped_once_it_is_inside(self):
        gr = self.create()
        mark_return_in(gr.id, self.user, {}, self.allowed)
        other = Vehicle.objects.create(vehicle_number="PB03EF9012")
        with self.assertRaises(ValueError):
            self.service.set_vehicle(
                gr.id, {"vehicle_id": other.id}, self.user, self.allowed
            )

    def test_the_gate_marks_in_the_vehicle_booked_on_the_first_page(self):
        gr = self.create()
        gr = mark_return_in(gr.id, self.user, {}, self.allowed)
        self.assertEqual(gr.status, GoodsReturnStatus.ARRIVED)
        self.assertEqual(gr.vehicle_entry.vehicle_id, self.vehicle.id)
        self.assertEqual(gr.vehicle_entry.entry_type, "GOODS_RETURN")
        # Off the gate's queue once it is in.
        self.assertNotIn(gr, list(list_expected_returns(self.allowed)))

    # -- submit ---------------------------------------------------------------

    def test_submit_records_the_clerk_without_moving_an_arrived_return_back(self):
        gr = self.create()
        self.service.save_items(
            gr.id,
            [{"item_code": "FG1", "return_quantity": 2}],
            self.user,
            self.allowed,
        )
        self.service.upload_attachment(
            gr.id, _a_file(), "DEBIT_NOTE", "", self.user, self.allowed
        )
        mark_return_in(gr.id, self.user, {}, self.allowed)

        gr = self.service.submit(gr.id, self.user, self.allowed)
        self.assertEqual(gr.status, GoodsReturnStatus.ARRIVED)
        self.assertIsNotNone(gr.submitted_at)

    def test_submit_still_needs_items_and_a_document(self):
        gr = self.create()
        with self.assertRaises(ValueError):
            self.service.submit(gr.id, self.user, self.allowed)


def _a_file():
    from django.core.files.uploadedfile import SimpleUploadedFile

    return SimpleUploadedFile("debit-note.pdf", b"%PDF-1.4", content_type="application/pdf")
