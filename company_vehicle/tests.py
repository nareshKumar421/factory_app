"""What has to keep working.

The mileage chain gets most of the attention, because it is the one number in
this module nobody can check by eye: a wrong figure looks exactly like a right
one until somebody acts on it.
"""

import tempfile
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.test import TestCase, override_settings

from .constants import ApprovalStatus, FuelType, VehicleCategory, VehicleStatus
from .models import DailyReading, FleetVehicle, FuelEntry, ServiceEntry, VehicleDocument
from .serializers import (
    DailyReadingSerializer,
    FuelEntryWriteSerializer,
    VehicleDocumentSerializer,
)
from .services import (
    fleet_summary,
    recalculate_fuel_metrics,
    running_log,
    running_log_by_vehicle,
    vehicle_summary,
)

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
    """A workshop bill counts once approved. A filling counts at once."""

    def setUp(self):
        self.vehicle = make_vehicle()

    def test_every_filling_is_counted(self):
        """Fuel has no approval, so nothing is held back from the total."""
        fill(self.vehicle, TODAY, 10_000, 50, amount="4500.00")
        fill(self.vehicle, TODAY + timedelta(days=2), 10_400, 50, amount="4500.00")
        summary = vehicle_summary(self.vehicle, None, None)
        self.assertEqual(summary["fuel_cost"], Decimal("9000.00"))
        self.assertNotIn("pending_fuel", summary)

    def test_a_pending_workshop_bill_is_not_counted(self):
        ServiceEntry.objects.create(
            vehicle=self.vehicle,
            entry_date=TODAY,
            total_amount=Decimal("2000.00"),
            approval_status=ApprovalStatus.PENDING,
        )
        summary = vehicle_summary(self.vehicle, None, None)
        self.assertEqual(summary["service_cost"], Decimal("0"))
        self.assertEqual(summary["pending_service"], 1)

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

    def test_fleet_summary_counts_only_workshop_bills_as_waiting(self):
        ServiceEntry.objects.create(
            vehicle=self.vehicle,
            entry_date=date.today(),
            total_amount=Decimal("2000.00"),
            approval_status=ApprovalStatus.PENDING,
        )
        summary = fleet_summary()
        self.assertEqual(summary["pending_approvals"]["total"], 1)
        self.assertEqual(summary["pending_approvals"]["service"], 1)
        self.assertNotIn("fuel", summary["pending_approvals"])

    def test_this_month_fuel_needs_no_approval_to_show_up(self):
        FuelEntry.objects.create(
            vehicle=self.vehicle,
            entry_date=date.today(),
            fuel_type=FuelType.DIESEL,
            odometer=10_000,
            quantity=Decimal("50"),
            amount=Decimal("4500.00"),
        )
        self.assertEqual(fleet_summary()["month_fuel_cost"], Decimal("4500.00"))


# Writes a real file, so it gets a throwaway MEDIA_ROOT rather than the
# configured one, which is shared with the running app.
@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class AttachmentLinkTests(TestCase):
    """Stored files are linked through the API, never at their media path.

    A `/media/` link is unauthenticated and relative to whatever host served
    the page, so it would both leak a fuel bill and break wherever the SPA and
    the API are not the same origin.
    """

    def setUp(self):
        self.vehicle = make_vehicle()

    def test_a_document_with_no_file_has_no_link(self):
        document = VehicleDocument.objects.create(
            vehicle=self.vehicle, doc_type="INSURANCE", expiry_date=date(2027, 3, 31)
        )
        self.assertIsNone(VehicleDocumentSerializer(document).data["file_url"])

    def test_a_filed_document_links_through_the_permission_checked_endpoint(self):
        document = VehicleDocument.objects.create(
            vehicle=self.vehicle, doc_type="INSURANCE", expiry_date=date(2027, 3, 31)
        )
        document.file.save("policy.pdf", ContentFile(b"%PDF-1.4"), save=True)
        url = VehicleDocumentSerializer(document).data["file_url"]
        self.assertEqual(url, f"/api/v1/company-vehicles/attachments/document/{document.pk}/")
        self.assertNotIn("/media/", url)

    def test_the_expiry_state_is_what_colours_the_row(self):
        past = VehicleDocument.objects.create(
            vehicle=self.vehicle, doc_type="PUC", expiry_date=date.today() - timedelta(days=1)
        )
        soon = VehicleDocument.objects.create(
            vehicle=self.vehicle, doc_type="FITNESS", expiry_date=date.today() + timedelta(days=10)
        )
        later = VehicleDocument.objects.create(
            vehicle=self.vehicle, doc_type="PERMIT", expiry_date=date.today() + timedelta(days=200)
        )
        self.assertEqual(VehicleDocumentSerializer(past).data["expiry_state"], "EXPIRED")
        self.assertEqual(VehicleDocumentSerializer(soon).data["expiry_state"], "EXPIRING")
        self.assertEqual(VehicleDocumentSerializer(later).data["expiry_state"], "OK")


def read(vehicle, day, odometer, remarks=""):
    return DailyReading.objects.create(
        vehicle=vehicle, reading_date=day, odometer=odometer, remarks=remarks
    )


class RunningLogTests(TestCase):
    """The day-wise log: how far each vehicle ran, and what it cost."""

    def setUp(self):
        self.vehicle = make_vehicle()

    def rows_by_date(self, date_from, date_to):
        log = running_log(self.vehicle, date_from, date_to)
        return {row["date"]: row for row in log["rows"]}, log["totals"]

    def test_a_row_for_every_day_even_with_nothing_on_it(self):
        rows, _ = self.rows_by_date(TODAY, TODAY + timedelta(days=4))
        self.assertEqual(len(rows), 5)
        self.assertIsNone(rows[TODAY + timedelta(days=3)]["odometer"])

    def test_distance_is_the_gap_between_two_readings(self):
        read(self.vehicle, TODAY, 10_000)
        read(self.vehicle, TODAY + timedelta(days=1), 10_180)
        rows, totals = self.rows_by_date(TODAY, TODAY + timedelta(days=1))
        self.assertEqual(rows[TODAY + timedelta(days=1)]["distance_km"], 180)
        self.assertEqual(rows[TODAY + timedelta(days=1)]["covers_days"], 1)
        self.assertEqual(totals["distance_km"], 180)

    def test_a_missed_day_is_not_billed_to_the_next_one(self):
        """The distance is still shown, but it says how many days it covers."""
        read(self.vehicle, TODAY, 10_000)
        read(self.vehicle, TODAY + timedelta(days=3), 10_600)
        rows, _ = self.rows_by_date(TODAY, TODAY + timedelta(days=3))
        row = rows[TODAY + timedelta(days=3)]
        self.assertEqual(row["distance_km"], 600)
        self.assertEqual(row["covers_days"], 3)
        self.assertIsNone(rows[TODAY + timedelta(days=1)]["distance_km"])

    def test_the_first_day_measures_against_a_reading_before_the_window(self):
        read(self.vehicle, TODAY - timedelta(days=1), 9_900)
        read(self.vehicle, TODAY, 10_000)
        rows, _ = self.rows_by_date(TODAY, TODAY)
        self.assertEqual(rows[TODAY]["distance_km"], 100)

    def test_a_filling_counts_as_that_day_s_reading(self):
        """Nobody types the meter twice: the pump slip already carried it."""
        read(self.vehicle, TODAY, 10_000)
        fill(self.vehicle, TODAY + timedelta(days=1), 10_250, 30, amount="2700.00")
        rows, totals = self.rows_by_date(TODAY, TODAY + timedelta(days=1))
        row = rows[TODAY + timedelta(days=1)]
        self.assertEqual(row["odometer"], 10_250)
        self.assertEqual(row["distance_km"], 250)
        self.assertEqual(row["fuel_cost"], Decimal("2700.00"))
        self.assertEqual(row["fuel_quantity"], Decimal("30"))
        self.assertEqual(totals["fuel_cost"], Decimal("2700.00"))

    def test_the_days_highest_reading_wins(self):
        """A morning reading and an evening fill: the closing figure is the fill."""
        read(self.vehicle, TODAY, 10_000)
        read(self.vehicle, TODAY + timedelta(days=1), 10_100)
        fill(self.vehicle, TODAY + timedelta(days=1), 10_400, 30)
        rows, _ = self.rows_by_date(TODAY, TODAY + timedelta(days=1))
        self.assertEqual(rows[TODAY + timedelta(days=1)]["odometer"], 10_400)
        self.assertEqual(rows[TODAY + timedelta(days=1)]["distance_km"], 400)

    def test_a_meter_that_went_backwards_claims_no_distance(self):
        read(self.vehicle, TODAY, 10_000)
        read(self.vehicle, TODAY + timedelta(days=1), 500, remarks="Meter replaced")
        rows, totals = self.rows_by_date(TODAY, TODAY + timedelta(days=1))
        self.assertIsNone(rows[TODAY + timedelta(days=1)]["distance_km"])
        self.assertEqual(totals["distance_km"], 0)

    def test_totals_count_the_days_nobody_wrote_anything_down(self):
        read(self.vehicle, TODAY, 10_000)
        _, totals = self.rows_by_date(TODAY, TODAY + timedelta(days=4))
        self.assertEqual(totals["days_in_range"], 5)
        self.assertEqual(totals["days_with_reading"], 1)
        self.assertEqual(totals["days_missing"], 4)

    def test_cost_per_km_over_the_window(self):
        read(self.vehicle, TODAY, 10_000)
        fill(self.vehicle, TODAY + timedelta(days=1), 10_400, 40, amount="3600.00")
        ServiceEntry.objects.create(
            vehicle=self.vehicle,
            entry_date=TODAY + timedelta(days=1),
            total_amount=Decimal("400.00"),
            approval_status=ApprovalStatus.APPROVED,
        )
        _, totals = self.rows_by_date(TODAY, TODAY + timedelta(days=1))
        self.assertEqual(totals["distance_km"], 400)
        self.assertEqual(totals["total_cost"], Decimal("4000.00"))
        self.assertEqual(totals["cost_per_km"], Decimal("10.00"))

    def test_a_pending_workshop_bill_stays_out_of_the_log_totals(self):
        read(self.vehicle, TODAY, 10_000)
        ServiceEntry.objects.create(
            vehicle=self.vehicle,
            entry_date=TODAY,
            total_amount=Decimal("5000.00"),
            approval_status=ApprovalStatus.PENDING,
        )
        _, totals = self.rows_by_date(TODAY, TODAY)
        self.assertEqual(totals["service_cost"], Decimal("0"))

    def test_the_fleet_view_gives_one_row_per_vehicle(self):
        other = make_vehicle(vehicle_number="PB65CD5678")
        read(self.vehicle, TODAY, 10_000)
        read(self.vehicle, TODAY + timedelta(days=1), 10_300)
        read(other, TODAY, 5_000)
        read(other, TODAY + timedelta(days=1), 5_050)
        rows = {r["vehicle_number"]: r for r in running_log_by_vehicle(TODAY, TODAY + timedelta(days=1))}
        self.assertEqual(rows["PB65AB1234"]["distance_km"], 300)
        self.assertEqual(rows["PB65CD5678"]["distance_km"], 50)


class DailyReadingTests(TestCase):
    def setUp(self):
        self.vehicle = make_vehicle()

    def test_one_reading_per_vehicle_per_day(self):
        from django.db import IntegrityError, transaction

        read(self.vehicle, TODAY, 10_000)
        with self.assertRaises(IntegrityError), transaction.atomic():
            read(self.vehicle, TODAY, 10_050)

    def test_two_vehicles_may_share_a_date(self):
        other = make_vehicle(vehicle_number="PB65CD5678")
        read(self.vehicle, TODAY, 10_000)
        read(other, TODAY, 5_000)
        self.assertEqual(DailyReading.objects.count(), 2)

    def test_a_reading_below_the_last_one_needs_a_remark(self):
        read(self.vehicle, TODAY, 10_000)
        form = DailyReadingSerializer(
            data={
                "vehicle": self.vehicle.id,
                "reading_date": (TODAY + timedelta(days=1)).isoformat(),
                "odometer": 900,
            }
        )
        self.assertFalse(form.is_valid())
        self.assertIn("odometer", form.errors)

        form = DailyReadingSerializer(
            data={
                "vehicle": self.vehicle.id,
                "reading_date": (TODAY + timedelta(days=1)).isoformat(),
                "odometer": 900,
                "remarks": "Meter replaced",
            }
        )
        self.assertTrue(form.is_valid(), form.errors)

    def test_a_reading_cannot_be_dated_in_the_future(self):
        form = DailyReadingSerializer(
            data={
                "vehicle": self.vehicle.id,
                "reading_date": date(2099, 1, 1).isoformat(),
                "odometer": 10_000,
            }
        )
        self.assertFalse(form.is_valid())
        self.assertIn("reading_date", form.errors)
