from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from company.models import Company, UserCompany, UserRole
from dispatch_plans.models import DispatchPlan, DispatchPlanStatus
from driver_management.models import Driver
from gate_core.models import VehicleArrival, VehicleArrivalStatus
from gate_core.services.empty_vehicle_dispatch import (
    consume_covers_for_dispatched_plans,
    create_vehicle_arrival,
)
from vehicle_management.models import Vehicle, VehicleType


class VehicleArrivalTests(TestCase):
    def setUp(self):
        self.beverages = Company.objects.create(name="Jivo Beverages", code="JIVO_BEVERAGES")
        self.oil = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        role = UserRole.objects.create(name="Gate")
        self.user = get_user_model().objects.create_user(
            email="arrival@example.com",
            password="testpass123",
            full_name="Arrival User",
            employee_code="ARR001",
        )
        UserCompany.objects.create(
            user=self.user, company=self.beverages, role=role, is_active=True
        )
        UserCompany.objects.create(user=self.user, company=self.oil, role=role, is_active=True)
        vehicle_type = VehicleType.objects.create(name="TRUCK-ARR")
        self.vehicle = Vehicle.objects.create(
            vehicle_number="DL01ARR0001", vehicle_type=vehicle_type
        )
        self.driver = Driver.objects.create(
            name="Arrival Driver", mobile_no="9000000000", license_no="DL-ARR-0001"
        )

    def _booked(self, company, doc_entry):
        return DispatchPlan.objects.create(
            company=company,
            sap_invoice_doc_entry=doc_entry,
            sap_invoice_doc_num=str(doc_entry),
            booking_status=DispatchPlanStatus.BOOKED,
            dispatch_date=timezone.localdate(),
            vehicle=self.vehicle,
        )

    def _create_arrival(self, tare=Decimal("1500.000")):
        return create_vehicle_arrival(
            vehicle=self.vehicle,
            driver=self.driver,
            company_ids=[self.beverages.id, self.oil.id],
            gate_in_date=timezone.localdate(),
            in_time=timezone.now().time(),
            tare_weight=tare,
            user=self.user,
        )

    def test_create_makes_one_gate_in_per_company_with_shared_tare_and_covers(self):
        bev_plan = self._booked(self.beverages, 90001)
        oil_plan = self._booked(self.oil, 90002)

        arrival = self._create_arrival()

        self.assertIsNotNone(arrival)
        self.assertEqual(arrival.status, VehicleArrivalStatus.INSIDE)
        gate_ins = list(arrival.gate_ins.all())
        self.assertEqual(len(gate_ins), 2)
        self.assertEqual(
            {g.company_id for g in gate_ins}, {self.beverages.id, self.oil.id}
        )
        for gate_in in gate_ins:
            self.assertEqual(gate_in.covers.count(), 1)
            self.assertEqual(
                gate_in.vehicle_entry.weighment.tare_weight, Decimal("1500.000")
            )
        bev_plan.refresh_from_db()
        oil_plan.refresh_from_db()
        self.assertIsNotNone(bev_plan.linked_vehicle_entry_id)
        self.assertIsNotNone(oil_plan.linked_vehicle_entry_id)

    def test_create_returns_none_when_no_bills(self):
        self.assertIsNone(self._create_arrival())

    def test_gate_in_document_reference_computed_from_covers(self):
        # document_reference/notes are derived from the gate-in's covers on read,
        # not stored (no redundant copy of the bill text).
        from gate_core.serializers import EmptyVehicleGateInSerializer

        self._booked(self.beverages, 90001)
        self._booked(self.oil, 90002)
        arrival = self._create_arrival()
        gate_in = arrival.gate_ins.filter(company=self.beverages).first()

        self.assertEqual(gate_in.document_reference, "")  # nothing stored
        data = EmptyVehicleGateInSerializer(gate_in).data
        self.assertEqual(data["document_reference"], "Dispatch 90001")  # computed

    def test_expected_endpoint_groups_bills_by_company(self):
        self._booked(self.beverages, 90001)
        self._booked(self.oil, 90002)
        client = APIClient()
        client.force_authenticate(self.user)

        response = client.get(
            "/api/v1/gate-core/arrivals/expected/", {"vehicle_id": self.vehicle.id}
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data["companies"]), 2)

    def test_create_endpoint_then_open_arrival_guard(self):
        self._booked(self.beverages, 90001)
        self._booked(self.oil, 90002)
        client = APIClient()
        client.force_authenticate(self.user)
        payload = {
            "vehicle_id": self.vehicle.id,
            "driver_id": self.driver.id,
            "gate_in_date": timezone.localdate().isoformat(),
            "in_time": "10:00",
            "tare_weight": "1500.000",
        }

        response = client.post("/api/v1/gate-core/arrivals/", payload, format="json")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(len(response.data["gate_ins"]), 2)

        blocked = client.post("/api/v1/gate-core/arrivals/", payload, format="json")
        self.assertEqual(blocked.status_code, 400)

    def test_depart_requires_all_companies_dispatched(self):
        bev_plan = self._booked(self.beverages, 90001)
        oil_plan = self._booked(self.oil, 90002)
        arrival = self._create_arrival()
        client = APIClient()
        client.force_authenticate(self.user)
        url = f"/api/v1/gate-core/arrivals/{arrival.id}/depart/"

        self.assertEqual(client.post(url).status_code, 400)  # nothing dispatched

        bev_plan.booking_status = DispatchPlanStatus.DISPATCHED
        bev_plan.save(update_fields=["booking_status"])
        consume_covers_for_dispatched_plans([bev_plan], self.user)
        self.assertEqual(client.post(url).status_code, 400)  # oil still outstanding

        oil_plan.booking_status = DispatchPlanStatus.DISPATCHED
        oil_plan.save(update_fields=["booking_status"])
        consume_covers_for_dispatched_plans([oil_plan], self.user)
        self.assertEqual(client.post(url).status_code, 200)

        arrival.refresh_from_db()
        self.assertEqual(arrival.status, VehicleArrivalStatus.DEPARTED)
        self.assertIsNotNone(arrival.departed_at)

    def test_empty_out_resets_all_companies(self):
        bev_plan = self._booked(self.beverages, 90001)
        oil_plan = self._booked(self.oil, 90002)
        arrival = self._create_arrival()
        client = APIClient()
        client.force_authenticate(self.user)

        response = client.post(
            f"/api/v1/gate-core/arrivals/{arrival.id}/empty-out/", {}, format="json"
        )

        self.assertEqual(response.status_code, 200)
        arrival.refresh_from_db()
        self.assertEqual(arrival.status, VehicleArrivalStatus.CANCELLED)
        bev_plan.refresh_from_db()
        oil_plan.refresh_from_db()
        self.assertIsNone(bev_plan.linked_vehicle_entry_id)
        self.assertIsNone(oil_plan.linked_vehicle_entry_id)
        for gate_in in arrival.gate_ins.all():
            self.assertIsNotNone(gate_in.retired_at)

    def test_dashboards_aggregate_across_companies(self):
        from django.contrib.auth.models import Permission

        self.user.user_permissions.add(
            *Permission.objects.filter(content_type__app_label="gate_core")
        )
        self._booked(self.beverages, 90001)
        self._booked(self.oil, 90002)
        self._create_arrival()
        client = APIClient()
        client.force_authenticate(self.user)
        hdr = {"HTTP_COMPANY_CODE": self.oil.code}

        # Empty-vehicle-in: the active header company only, vs all the user's companies.
        single = client.get(
            "/api/v1/gate-core/empty-vehicle-ins/", {"reason": "DISPATCH"}, **hdr
        )
        self.assertEqual({e["company_code"] for e in single.data}, {self.oil.code})
        combined = client.get(
            "/api/v1/gate-core/empty-vehicle-ins/",
            {"reason": "DISPATCH", "all_companies": "1"},
            **hdr,
        )
        self.assertEqual(
            {e["company_code"] for e in combined.data},
            {self.oil.code, self.beverages.code},
        )

        # Pending dispatch bills aggregate across companies too, each tagged.
        pending = client.get(
            "/api/v1/gate-core/sales-dispatch/pending-bookings/",
            {"all_companies": "1"},
            **hdr,
        )
        self.assertEqual(
            {g["company_code"] for g in pending.data},
            {self.oil.code, self.beverages.code},
        )

    def test_write_resolves_company_from_record(self):
        from django.contrib.auth.models import Permission

        from driver_management.models import VehicleEntry
        from gate_core.models import SalesDispatchDocumentType, SalesDispatchGateOut

        mart = Company.objects.create(name="Jivo Mart", code="JIVO_MART")  # user NOT a member

        def _docking(company, suffix):
            ve = VehicleEntry.objects.create(
                entry_no=f"DKV-{suffix}", company=company, vehicle=self.vehicle,
                driver=self.driver, entry_type="SALES_DISPATCH", status="IN_PROGRESS",
                created_by=self.user, updated_by=self.user,
            )
            return SalesDispatchGateOut.objects.create(
                company=company, entry_no=f"DOCK-{suffix}", vehicle_entry=ve,
                vehicle=self.vehicle, driver=self.driver,
                document_type=SalesDispatchDocumentType.INVOICE, sap_doc_entry=int(suffix),
                status="DOCKED", created_by=self.user, updated_by=self.user,
            )

        bev_dock = _docking(self.beverages, "111")
        mart_dock = _docking(mart, "222")
        self.user.user_permissions.add(
            *Permission.objects.filter(content_type__app_label="gate_core")
        )
        client = APIClient()
        client.force_authenticate(self.user)
        hdr = {"HTTP_COMPANY_CODE": self.oil.code}  # active company = Oil

        # Active company is Oil, but the user belongs to Beverages -> can act on it.
        ok = client.get(
            f"/api/v1/gate-core/sales-dispatch/{bev_dock.id}/box-scans/", **hdr
        )
        self.assertEqual(ok.status_code, 200)
        # Mart belongs to a company the user is NOT in -> out of scope -> 404.
        denied = client.get(
            f"/api/v1/gate-core/sales-dispatch/{mart_dock.id}/box-scans/", **hdr
        )
        self.assertEqual(denied.status_code, 404)
