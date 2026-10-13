"""
A bill put on a truck is dated and selected, so the gate expects the truck.

The empty-vehicle-in board lists a truck as expected by its booked bills'
dispatch dates. Linking lists bills by invoice date, so a bill could be booked
with no date -- or never ticked on Bill Selection -- and the truck then turned
up at the gate unannounced. Worse, the linking sheet sent its first bill's date
for the whole truck, so one undated bill wiped every other bill's date.

What has to hold: an undated bill is dated today, a date already chosen stays
(unless it has passed when the bill goes on the truck), one bill's date never
lands on another, the bill is ticked on Bill Selection, and none of this fires
on a Plan page save or an unlink.
"""

from datetime import timedelta
from unittest.mock import MagicMock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from company.models import Company
from vehicle_management.models import Transporter, Vehicle, VehicleType

from .models import DispatchPlan, DispatchPlanStatus, SelectedDispatchBill
from .services import DispatchPlansService

User = get_user_model()


def _bill(doc_entry):
    return {
        "doc_entry": doc_entry,
        "doc_num": f"6260{doc_entry}",
        "card_code": "CUSTA000981",
        "card_name": "AGGARWAL AGENCIES",
        "branch_id": 1,
    }


class LinkDatesAndSelectsTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        self.user = User.objects.create_user(
            email="link-dates@example.com",
            password="testpass123",
            full_name="Link Dates",
            employee_code="LINKDATE01",
        )
        transporter = Transporter.objects.create(name="Bhargave Road Carrier")
        vehicle_type = VehicleType.objects.create(name="TRUCK-LINK-DATES")
        self.vehicle = Vehicle.objects.create(
            vehicle_number="HR67F8513", vehicle_type=vehicle_type, transporter=transporter
        )
        self.other_vehicle = Vehicle.objects.create(
            vehicle_number="HR55AA1234", vehicle_type=vehicle_type, transporter=transporter
        )
        self.service = DispatchPlansService(company_code=self.company.code)
        self.service.reader = MagicMock()
        self.service.reader.list_bills_by_doc_entries.side_effect = lambda entries: [
            _bill(entry) for entry in entries
        ]
        self.today = timezone.localdate()

    def _plan(self, doc_entry, **fields):
        return DispatchPlan.objects.create(
            company=self.company,
            sap_invoice_doc_entry=doc_entry,
            sap_invoice_doc_num=f"6260{doc_entry}",
            customer_code="CUSTA000981",
            customer_name="AGGARWAL AGENCIES",
            **fields,
        )

    def _link(self, doc_entries, *, dispatch_date=None, vehicle=None):
        """The payload the linking sheet sends: its primary bill's date, shared."""
        primary, *rest = doc_entries
        data = {
            "vehicle_id": (vehicle or self.vehicle).id,
            "booking_status": "BOOKED",
            "dispatch_date": dispatch_date,
        }
        if rest:
            data["linked_invoice_doc_entries"] = list(doc_entries)
        return self.service.update_plan(
            sap_invoice_doc_entry=primary, data=data, user=self.user
        )

    def _date(self, doc_entry):
        return DispatchPlan.objects.get(
            company=self.company, sap_invoice_doc_entry=doc_entry
        ).dispatch_date

    def _selected(self):
        return set(
            SelectedDispatchBill.objects.filter(
                company=self.company, is_active=True
            ).values_list("sap_invoice_doc_entry", flat=True)
        )

    def test_a_bill_never_planned_is_dated_today_and_selected(self):
        """No plan row, no selection: the bill was picked straight off linking."""
        self._link([81001])

        self.assertEqual(self._date(81001), self.today)
        self.assertEqual(self._selected(), {81001})
        selection = SelectedDispatchBill.objects.get(sap_invoice_doc_entry=81001)
        self.assertEqual(selection.created_by, self.user)
        self.assertEqual(selection.sap_invoice_doc_num, "626081001")

    def test_a_bill_booked_ahead_keeps_its_date(self):
        tomorrow = self.today + timedelta(days=1)
        self._plan(81002, dispatch_date=tomorrow)

        self._link([81002], dispatch_date=tomorrow)

        self.assertEqual(self._date(81002), tomorrow)
        self.assertEqual(self._selected(), {81002})

    def test_a_date_already_gone_by_moves_to_today(self):
        """A truck linked today cannot arrive yesterday."""
        self._plan(81003, dispatch_date=self.today - timedelta(days=3))

        self._link([81003], dispatch_date=self.today - timedelta(days=3))

        self.assertEqual(self._date(81003), self.today)

    def test_re_saving_a_link_keeps_a_past_date(self):
        """A bilty fix on yesterday's truck is not a new arrival."""
        yesterday = self.today - timedelta(days=1)
        self._plan(
            81004,
            dispatch_date=yesterday,
            booking_status=DispatchPlanStatus.BOOKED,
            vehicle=self.vehicle,
        )

        self._link([81004], dispatch_date=yesterday)

        self.assertEqual(self._date(81004), yesterday)

    def test_moving_a_bill_to_another_truck_counts_as_booking_it(self):
        yesterday = self.today - timedelta(days=1)
        self._plan(
            81005,
            dispatch_date=yesterday,
            booking_status=DispatchPlanStatus.BOOKED,
            vehicle=self.other_vehicle,
        )

        self._link([81005], dispatch_date=yesterday)

        self.assertEqual(self._date(81005), self.today)

    def test_an_undated_first_bill_no_longer_wipes_the_others(self):
        """The sheet sends its first bill's date for the whole truck."""
        tomorrow = self.today + timedelta(days=1)
        self._plan(81010)
        self._plan(81011, dispatch_date=tomorrow)

        self._link([81010, 81011], dispatch_date=None)

        self.assertEqual(self._date(81010), self.today)
        self.assertEqual(self._date(81011), tomorrow)
        self.assertEqual(self._selected(), {81010, 81011})

    def test_the_first_bill_s_date_does_not_land_on_the_others(self):
        tomorrow = self.today + timedelta(days=1)
        self._plan(81020, dispatch_date=tomorrow)
        self._plan(81021)

        self._link([81020, 81021], dispatch_date=tomorrow)

        self.assertEqual(self._date(81020), tomorrow)
        self.assertEqual(self._date(81021), self.today)

    def test_a_deselected_bill_is_selected_again(self):
        selection = SelectedDispatchBill.objects.create(
            company=self.company, sap_invoice_doc_entry=81030, is_active=False
        )

        self._link([81030])

        selection.refresh_from_db()
        self.assertTrue(selection.is_active)
        self.assertEqual(selection.updated_by, self.user)
        self.assertEqual(SelectedDispatchBill.objects.count(), 1)

    def test_a_plan_page_save_can_still_clear_a_date(self):
        """The Plan page sends no vehicle, so it is not a link."""
        self._plan(81040, dispatch_date=self.today)

        self.service.update_plan(
            sap_invoice_doc_entry=81040,
            data={"dispatch_date": None, "remarks": "not this week"},
            user=self.user,
        )

        self.assertIsNone(self._date(81040))
        self.assertEqual(self._selected(), set())

    def test_unlinking_leaves_the_date_and_selection(self):
        """The bill stays in planning; only its truck goes."""
        self._link([81050])

        self.service.update_plan(
            sap_invoice_doc_entry=81050,
            data={
                "vehicle_id": None,
                "transporter_id": None,
                "driver_id": None,
                "linked_vehicle_entry_id": None,
                "booking_status": "PENDING",
            },
            user=self.user,
        )

        self.assertEqual(self._date(81050), self.today)
        self.assertEqual(self._selected(), {81050})
