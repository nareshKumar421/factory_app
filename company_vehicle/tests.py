"""What has to keep working.

The mileage chain gets most of the attention, because it is the one number in
this module nobody can check by eye: a wrong figure looks exactly like a right
one until somebody acts on it.
"""

from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase

from .constants import ApprovalStatus, FuelType, VehicleCategory, VehicleStatus
from .models import FleetVehicle, FuelEntry, ServiceEntry
from .serializers import FuelEntryWriteSerializer
from .services import fleet_summary, recalculate_fuel_metrics, vehicle_summary

User = get_user_model()

TODAY = date(2026, 9, 1)


def make_vehicle(**overrides):
    fields = {
        "vehicle_number": "PB65AB1234",
        "category": VehicleCategory.TRUCK,
        "fuel_type": FuelType.DIESEL,
        "status": VehicleStatus.ACTIVE,
    }
    fields.update(overrides)
    return FleetVehicle.objects.create(**fields)


def fill(vehicle, day, odometer, quantity, amount="1000.00", full=True, fuel=None):
    return FuelEntry.objects.create(
        vehicle=vehicle,
        entry_date=day,
        fuel_type=fuel or vehicle.fuel_type,
        odometer=odometer,
        quantity=Decimal(str(quantity)),
        amount=Decimal(amount),
        is_tank_full=full,
        approval_status=ApprovalStatus.APPROVED,
    )


class VehicleNumberTests(TestCase):
    def test_number_is_stored_one_way(self):
        vehicle = make_vehicle(vehicle_number=" pb 65 ab 1234 ")
        self.assertEqual(vehicle.vehicle_number, "PB65AB1234")


class MileageTests(TestCase):
    def setUp(self):
        self.vehicle = make_vehicle()

    def test_first_fill_has_no_mileage(self):
        fill(self.vehicle, TODAY, 10_000, 50)
        recalculate_fuel_metrics(self.vehicle)
        entry = FuelEntry.objects.get()
        self.assertIsNone(entry.mileage)
        self.assertIsNone(entry.distance_km)

    def test_full_to_full_mileage(self):
        fill(self.vehicle, TODAY, 10_000, 50)
        fill(self.vehicle, TODAY + timedelta(days=5), 10_400, 50)
        recalculate_fuel_metrics(self.vehicle)
        second = FuelEntry.objects.order_by("odometer")[1]
        self.assertEqual(second.distance_km, 400)
        # 400 km on the 50 litres put in to bring it back to full.
        self.assertEqual(second.mileage, Decimal("8.00"))

    def test_part_fill_counts_towards_the_next_full_tank(self):
        """A part fill gets no mileage, but its litres are not lost."""
        fill(self.vehicle, TODAY, 10_000, 50)
        fill(self.vehicle, TODAY + timedelta(days=2), 10_200, 20, full=False)
        fill(self.vehicle, TODAY + timedelta(days=5), 10_400, 30)
        recalculate_fuel_metrics(self.vehicle)
        entries = list(FuelEntry.objects.order_by("odometer"))
        self.assertIsNone(entries[1].mileage)
        # 400 km on 20 + 30 litres, not on 30.
        self.assertEqual(entries[2].mileage, Decimal("8.00"))

    def test_each_fuel_is_its_own_chain(self):
        """Petrol km/litre and CNG km/kg must never be averaged together."""
        dual = make_vehicle(vehicle_number="PB65CD5678", fuel_type=FuelType.PETROL_CNG)
        fill(dual, TODAY, 20_000, 10, fuel=FuelType.PETROL)
        fill(dual, TODAY + timedelta(days=1), 20_100, 8, fuel=FuelType.CNG)
        fill(dual, TODAY + timedelta(days=2), 20_300, 10, fuel=FuelType.PETROL)
        fill(dual, TODAY + timedelta(days=3), 20_500, 8, fuel=FuelType.CNG)
        recalculate_fuel_metrics(dual)
        petrol = dual.fuel_entries.filter(fuel_type=FuelType.PETROL).order_by("odometer")
        cng = dual.fuel_entries.filter(fuel_type=FuelType.CNG).order_by("odometer")
        # Petrol: 20_300 - 20_000 = 300 km on 10 litres.
        self.assertEqual(petrol[1].mileage, Decimal("30.00"))
        # CNG: 20_500 - 20_100 = 400 km on 8 kg.
        self.assertEqual(cng[1].mileage, Decimal("50.00"))

    def test_meter_going_backwards_leaves_the_distance_blank(self):
        fill(self.vehicle, TODAY, 10_000, 50)
        fill(self.vehicle, TODAY + timedelta(days=5), 500, 50)
        recalculate_fuel_metrics(self.vehicle)
        second = FuelEntry.objects.order_by("entry_date")[1]
        self.assertIsNone(second.distance_km)
        self.assertIsNone(second.mileage)

    def test_deleting_the_middle_fill_rewrites_what_follows(self):
        fill(self.vehicle, TODAY, 10_000, 50)
        middle = fill(self.vehicle, TODAY + timedelta(days=2), 10_200, 25)
        fill(self.vehicle, TODAY + timedelta(days=5), 10_400, 25)
        recalculate_fuel_metrics(self.vehicle)
        middle.delete()
        recalculate_fuel_metrics(self.vehicle)
        last = FuelEntry.objects.order_by("odometer").last()
        self.assertEqual(last.distance_km, 400)
        self.assertEqual(last.mileage, Decimal("16.00"))


class FuelFormTests(TestCase):
    def setUp(self):
        self.vehicle = make_vehicle(opening_odometer=10_000)

    def payload(self, **overrides):
        data = {
            "vehicle": self.vehicle.id,
            "entry_date": TODAY.isoformat(),
            "odometer": 10_500,
            "quantity": "50",
            "amount": "4500",
        }
        data.update(overrides)
        return data

    def test_rate_is_worked_out_from_the_slip(self):
        form = FuelEntryWriteSerializer(data=self.payload())
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.validated_data["rate"], Decimal("90.00"))

    def test_amount_is_worked_out_from_the_rate(self):
        form = FuelEntryWriteSerializer(data=self.payload(amount=None, rate="90"))
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.validated_data["amount"], Decimal("4500.00"))

    def test_single_fuel_vehicle_is_not_asked_which_fuel(self):
        form = FuelEntryWriteSerializer(data=self.payload())
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.validated_data["fuel_type"], FuelType.DIESEL)

    def test_dual_fuel_vehicle_must_say_which(self):
        dual = make_vehicle(vehicle_number="PB65CD5678", fuel_type=FuelType.PETROL_CNG)
        form = FuelEntryWriteSerializer(data=self.payload(vehicle=dual.id))
        self.assertFalse(form.is_valid())
        self.assertIn("fuel_type", form.errors)

    def test_a_fuel_the_vehicle_does_not_run_on_is_refused(self):
        form = FuelEntryWriteSerializer(data=self.payload(fuel_type=FuelType.CNG))
        self.assertFalse(form.is_valid())
        self.assertIn("fuel_type", form.errors)

    def test_meter_below_the_last_reading_needs_a_note(self):
        form = FuelEntryWriteSerializer(data=self.payload(odometer=9_000))
        self.assertFalse(form.is_valid())
        self.assertIn("odometer", form.errors)

        form = FuelEntryWriteSerializer(
            data=self.payload(odometer=9_000, odometer_note="Meter replaced on 28 Aug")
        )
        self.assertTrue(form.is_valid(), form.errors)

    def test_the_same_bill_twice_is_queried_once(self):
        fill(self.vehicle, TODAY, 10_400, 50, amount="4500.00")
        form = FuelEntryWriteSerializer(data=self.payload())
        self.assertFalse(form.is_valid())
        self.assertIn("confirm_duplicate", form.errors)

        form = FuelEntryWriteSerializer(data=self.payload(confirm_duplicate=True))
        self.assertTrue(form.is_valid(), form.errors)

    def test_a_future_filling_is_refused(self):
        form = FuelEntryWriteSerializer(data=self.payload(entry_date=date(2099, 1, 1).isoformat()))
        self.assertFalse(form.is_valid())
        self.assertIn("entry_date", form.errors)


class SpendTests(TestCase):
    """Only approved money is spend."""

    def setUp(self):
        self.vehicle = make_vehicle()

    def test_pending_fuel_is_not_counted(self):
        fill(self.vehicle, TODAY, 10_000, 50, amount="4500.00")
        FuelEntry.objects.create(
            vehicle=self.vehicle,
            entry_date=TODAY,
            fuel_type=FuelType.DIESEL,
            odometer=10_400,
            quantity=Decimal("50"),
            amount=Decimal("4500.00"),
            approval_status=ApprovalStatus.PENDING,
        )
        summary = vehicle_summary(self.vehicle, None, None)
        self.assertEqual(summary["fuel_cost"], Decimal("4500.00"))
        self.assertEqual(summary["pending_fuel"], 1)

    def test_cost_per_km_uses_fuel_and_service_together(self):
        fill(self.vehicle, TODAY, 10_000, 50, amount="4000.00")
        fill(self.vehicle, TODAY + timedelta(days=5), 10_400, 50, amount="4000.00")
        ServiceEntry.objects.create(
            vehicle=self.vehicle,
            entry_date=TODAY + timedelta(days=3),
            total_amount=Decimal("2000.00"),
            approval_status=ApprovalStatus.APPROVED,
        )
        summary = vehicle_summary(self.vehicle, None, None)
        self.assertEqual(summary["distance_km"], 400)
        # (4000 + 4000 + 2000) / 400
        self.assertEqual(summary["cost_per_km"], Decimal("25.00"))

    def test_one_filling_alone_gives_no_cost_per_km(self):
        fill(self.vehicle, TODAY, 10_000, 50, amount="4000.00")
        summary = vehicle_summary(self.vehicle, None, None)
        self.assertIsNone(summary["distance_km"])
        self.assertIsNone(summary["cost_per_km"])

    def test_fleet_summary_counts_what_is_waiting(self):
        FuelEntry.objects.create(
            vehicle=self.vehicle,
            entry_date=date.today(),
            fuel_type=FuelType.DIESEL,
            odometer=10_000,
            quantity=Decimal("50"),
            amount=Decimal("4500.00"),
            approval_status=ApprovalStatus.PENDING,
        )
        summary = fleet_summary()
        self.assertEqual(summary["pending_approvals"]["fuel"], 1)
        self.assertEqual(summary["month_fuel_cost"], Decimal("0"))
