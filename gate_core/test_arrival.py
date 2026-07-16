from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
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

    def test_dispatch_empty_in_created_under_bill_company_not_header(self):
        from gate_core.models import EmptyVehicleGateIn

        # Active header = Beverages, but the truck only carries an Oil bill: the
        # entry is created under Oil (the company selector is a decorator).
        self._booked(self.oil, 91001)
        client = APIClient()
        client.force_authenticate(self.user)

        response = client.post(
            "/api/v1/gate-core/empty-vehicle-ins/",
            {
                "vehicle_id": self.vehicle.id,
                "driver_id": self.driver.id,
                "reason": "DISPATCH",
                "gate_in_date": timezone.localdate().isoformat(),
                "in_time": "10:00:00",
            },
            format="json",
            HTTP_COMPANY_CODE=self.beverages.code,
        )

        self.assertEqual(response.status_code, 201, response.data)
        gate_in = EmptyVehicleGateIn.objects.get(vehicle=self.vehicle, reason="DISPATCH")
        self.assertEqual(gate_in.company_id, self.oil.id)
        self.assertEqual(gate_in.vehicle_entry.company_id, self.oil.id)

    def test_complete_empty_in_resolves_sibling_company(self):
        from driver_management.models import VehicleEntry
        from weighment.models import Weighment

        from gate_core.models import EmptyVehicleGateIn

        ve = VehicleEntry.objects.create(
            entry_no="EVGI-COMP-1",
            company=self.beverages,
            vehicle=self.vehicle,
            driver=self.driver,
            entry_type="EMPTY_VEHICLE",
            status="IN_PROGRESS",
            created_by=self.user,
            updated_by=self.user,
        )
        Weighment.objects.create(
            vehicle_entry=ve, tare_weight=Decimal("1500.000"),
            created_by=self.user, updated_by=self.user,
        )
        gate_in = EmptyVehicleGateIn.objects.create(
            company=self.beverages, entry_no="EVGI-COMP-1", vehicle_entry=ve,
            vehicle=self.vehicle, driver=self.driver, reason="DISPATCH",
            gate_in_date=timezone.localdate(), in_time=timezone.now().time(),
            created_by=self.user, updated_by=self.user,
        )
        client = APIClient()
        client.force_authenticate(self.user)

        # Active header = Oil, gate-in belongs to Beverages -> still completes in place.
        response = client.post(
            f"/api/v1/gate-core/empty-vehicle-ins/{gate_in.id}/complete/",
            HTTP_COMPANY_CODE=self.oil.code,
        )

        self.assertEqual(response.status_code, 200, response.data)
        ve.refresh_from_db()
        self.assertEqual(ve.status, "COMPLETED")

    def test_weighment_loads_for_sibling_company_vehicle_entry(self):
        from driver_management.models import VehicleEntry
        from weighment.models import Weighment

        ve = VehicleEntry.objects.create(
            entry_no="EVGI-WEIGH-1", company=self.beverages, vehicle=self.vehicle,
            driver=self.driver, entry_type="EMPTY_VEHICLE", status="COMPLETED",
            created_by=self.user, updated_by=self.user,
        )
        Weighment.objects.create(
            vehicle_entry=ve, tare_weight=Decimal("1630.000"),
            created_by=self.user, updated_by=self.user,
        )
        client = APIClient()
        client.force_authenticate(self.user)

        # Active header = Oil, vehicle entry belongs to Beverages -> tare still loads
        # (the weighment step is cross-company; the selector is a decorator).
        response = client.get(
            f"/api/v1/weighment/gate-entries/{ve.id}/weighment/view/",
            HTTP_COMPANY_CODE=self.oil.code,
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(Decimal(response.data["tare_weight"]), Decimal("1630.000"))

    def test_attachment_upload_for_sibling_company_vehicle_entry(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        from driver_management.models import VehicleEntry

        ve = VehicleEntry.objects.create(
            entry_no="EVGI-ATT-1", company=self.beverages, vehicle=self.vehicle,
            driver=self.driver, entry_type="EMPTY_VEHICLE", status="IN_PROGRESS",
            created_by=self.user, updated_by=self.user,
        )
        client = APIClient()
        client.force_authenticate(self.user)

        # Active header = Oil, vehicle entry belongs to Beverages -> upload still works
        # (the gate attachment step is cross-company).
        response = client.post(
            f"/api/v1/gate-core/gate-attachments/{ve.id}/",
            {"file": SimpleUploadedFile("doc.pdf", b"x", content_type="application/pdf")},
            format="multipart",
            HTTP_COMPANY_CODE=self.oil.code,
        )

        self.assertEqual(response.status_code, 201, response.data)

    def test_regular_gate_in_replicates_across_companies(self):
        # A regular (arrival-less) Oil empty-vehicle-in for a truck that also carries
        # a Beverages bill must, on replication, wrap into one arrival and create a
        # sibling Beverages gate-in (exact copy: covers + tare) so the truck is marked
        # in for both companies.
        from driver_management.models import VehicleEntry
        from weighment.models import Weighment

        from gate_core.models import EmptyVehicleGateIn
        from gate_core.services.empty_vehicle_dispatch import (
            record_dispatch_covers,
            replicate_dispatch_gate_in_across_companies,
        )

        self._booked(self.beverages, 90001)
        self._booked(self.oil, 90002)
        ve = VehicleEntry.objects.create(
            entry_no="EVGI-REG-1", company=self.oil, vehicle=self.vehicle,
            driver=self.driver, entry_type="EMPTY_VEHICLE", status="COMPLETED",
            created_by=self.user, updated_by=self.user,
        )
        Weighment.objects.create(
            vehicle_entry=ve, tare_weight=Decimal("1500.000"),
            created_by=self.user, updated_by=self.user,
        )
        gate_in = EmptyVehicleGateIn.objects.create(
            company=self.oil, entry_no="EVGI-REG-1", vehicle_entry=ve, vehicle=self.vehicle,
            driver=self.driver, reason="DISPATCH", gate_in_date=timezone.localdate(),
            in_time=timezone.now().time(), arrival=None,
            created_by=self.user, updated_by=self.user,
        )
        record_dispatch_covers(gate_in, self.user)
        self.assertIsNone(gate_in.arrival_id)

        replicate_dispatch_gate_in_across_companies(
            gate_in, self.user, [self.beverages.id, self.oil.id]
        )

        gate_in.refresh_from_db()
        self.assertIsNotNone(gate_in.arrival_id)  # wrapped into a cross-company trip
        bev_gate_in = gate_in.arrival.gate_ins.filter(company=self.beverages).first()
        self.assertIsNotNone(bev_gate_in)
        self.assertEqual(bev_gate_in.vehicle_entry.weighment.tare_weight, Decimal("1500.000"))
        self.assertTrue(bev_gate_in.covers.filter(sap_doc_entry=90001).exists())
        bev_plan = DispatchPlan.objects.get(company=self.beverages, sap_invoice_doc_entry=90001)
        self.assertEqual(bev_plan.linked_vehicle_entry_id, bev_gate_in.vehicle_entry_id)

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

    def test_dispatch_auto_departs_arrival_once_all_chains_retired(self):
        # A single-company truck: dispatching its only bill must close the arrival
        # automatically, so it can't linger LOADING and be reused on the next visit.
        bev_plan = self._booked(self.beverages, 90001)
        arrival = create_vehicle_arrival(
            vehicle=self.vehicle,
            driver=self.driver,
            company_ids=[self.beverages.id],
            gate_in_date=timezone.localdate(),
            in_time=timezone.now().time(),
            tare_weight=Decimal("1500.000"),
            user=self.user,
        )
        self.assertEqual(arrival.status, VehicleArrivalStatus.INSIDE)

        bev_plan.booking_status = DispatchPlanStatus.DISPATCHED
        bev_plan.save(update_fields=["booking_status"])
        consume_covers_for_dispatched_plans([bev_plan], self.user)

        arrival.refresh_from_db()
        self.assertEqual(arrival.status, VehicleArrivalStatus.DEPARTED)
        self.assertIsNotNone(arrival.departed_at)

    def test_dispatch_does_not_depart_while_a_chain_is_inside(self):
        # Multi-company truck: one company dispatched must NOT depart the truck.
        bev_plan = self._booked(self.beverages, 90001)
        self._booked(self.oil, 90002)
        arrival = self._create_arrival()

        bev_plan.booking_status = DispatchPlanStatus.DISPATCHED
        bev_plan.save(update_fields=["booking_status"])
        consume_covers_for_dispatched_plans([bev_plan], self.user)

        arrival.refresh_from_db()
        self.assertIn(
            arrival.status,
            (VehicleArrivalStatus.INSIDE, VehicleArrivalStatus.LOADING),
        )
        self.assertIsNone(arrival.departed_at)

    def test_undispatch_reopens_auto_departed_arrival(self):
        # Dispatch auto-departs the truck; un-dispatching (reject/cancel) must bring
        # the arrival back so it isn't DEPARTED with a live gate-in.
        from gate_core.services.empty_vehicle_dispatch import unconsume_covers_for_plans

        bev_plan = self._booked(self.beverages, 90001)
        arrival = create_vehicle_arrival(
            vehicle=self.vehicle,
            driver=self.driver,
            company_ids=[self.beverages.id],
            gate_in_date=timezone.localdate(),
            in_time=timezone.now().time(),
            tare_weight=Decimal("1500.000"),
            user=self.user,
        )
        bev_plan.booking_status = DispatchPlanStatus.DISPATCHED
        bev_plan.save(update_fields=["booking_status"])
        consume_covers_for_dispatched_plans([bev_plan], self.user)
        arrival.refresh_from_db()
        self.assertEqual(arrival.status, VehicleArrivalStatus.DEPARTED)

        unconsume_covers_for_plans([bev_plan], self.user)
        arrival.refresh_from_db()
        self.assertIn(
            arrival.status,
            (VehicleArrivalStatus.INSIDE, VehicleArrivalStatus.LOADING),
        )
        self.assertIsNone(arrival.departed_at)
        self.assertTrue(
            arrival.gate_ins.filter(is_active=True, retired_at__isnull=True).exists()
        )

    def test_stale_open_arrival_not_reused_for_new_bill(self):
        # A zombie arrival (somehow still LOADING but all chains retired) must not
        # adopt a freshly booked bill -- the bill should stay free to surface as an
        # expected arrival instead of being glued onto a dead trip.
        from gate_core.models import EmptyVehicleGateIn
        from gate_core.services.empty_vehicle_dispatch import attach_bill_to_inside_vehicle

        self._booked(self.beverages, 90001)
        arrival = create_vehicle_arrival(
            vehicle=self.vehicle,
            driver=self.driver,
            company_ids=[self.beverages.id],
            gate_in_date=timezone.localdate(),
            in_time=timezone.now().time(),
            tare_weight=Decimal("1500.000"),
            user=self.user,
        )
        # Simulate the legacy stuck state: gate-in retired but arrival left open.
        EmptyVehicleGateIn.objects.filter(arrival=arrival).update(
            retired_at=timezone.now(), retired_reason="DISPATCHED"
        )
        VehicleArrival.objects.filter(id=arrival.id).update(
            status=VehicleArrivalStatus.LOADING
        )

        new_plan = self._booked(self.beverages, 90003)
        self.assertFalse(attach_bill_to_inside_vehicle(new_plan, self.user))
        new_plan.refresh_from_db()
        self.assertIsNone(new_plan.linked_vehicle_entry_id)
        # The dead arrival gained no new gate-in.
        self.assertEqual(arrival.gate_ins.count(), 1)

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

    def test_late_bill_for_new_company_joins_existing_arrival(self):
        from gate_core.services.empty_vehicle_dispatch import attach_bill_to_inside_vehicle

        # Truck gated in for Beverages only -> one gate-in under the arrival.
        self._booked(self.beverages, 90001)
        arrival = self._create_arrival()
        self.assertEqual(arrival.gate_ins.count(), 1)

        # Later an Oil bill is booked to the SAME truck (no Oil gate-in yet).
        oil_plan = self._booked(self.oil, 90002)
        self.assertTrue(attach_bill_to_inside_vehicle(oil_plan, self.user))

        # A gate-in for Oil now exists under the same arrival, with a cover + link.
        self.assertEqual(arrival.gate_ins.count(), 2)
        oil_gate_in = arrival.gate_ins.get(company=self.oil)
        oil_plan.refresh_from_db()
        self.assertEqual(oil_plan.linked_vehicle_entry_id, oil_gate_in.vehicle_entry_id)
        self.assertTrue(oil_gate_in.covers.filter(sap_doc_entry=90002).exists())

    def _committed_docking(self, arrival, company, plan, suffix):
        from driver_management.models import VehicleEntry
        from weighment.models import Weighment

        from gate_core.models import SalesDispatchDocumentType, SalesDispatchGateOut

        entry = VehicleEntry.objects.create(
            entry_no=f"DDOCKV-{suffix}", company=company, vehicle=self.vehicle,
            driver=self.driver, entry_type="SALES_DISPATCH", status="IN_PROGRESS",
            created_by=self.user, updated_by=self.user,
        )
        docking = SalesDispatchGateOut.objects.create(
            company=company, entry_no=f"DDOCK-{suffix}", arrival=arrival,
            dispatch_plan=plan, vehicle_entry=entry, vehicle=self.vehicle,
            driver=self.driver, document_type=SalesDispatchDocumentType.INVOICE,
            sap_doc_entry=int(suffix), sap_doc_num=f"INV-{suffix}",
            status="PRINT_COMMITTED", gatepass_no=f"DCK/{company.code}/2026-27/{suffix}",
            random_code="rc", printed_by=self.user, printed_at=timezone.now(),
            print_committed_by=self.user, print_committed_at=timezone.now(),
            created_by=self.user, updated_by=self.user,
        )
        Weighment.objects.create(
            vehicle_entry=entry, gross_weight=Decimal("1000.000"),
            tare_weight=Decimal("250.000"), created_by=self.user, updated_by=self.user,
        )
        return docking

    def test_arrival_dispatch_marks_all_dockings_then_depart(self):
        bev_plan = self._booked(self.beverages, 90001)
        oil_plan = self._booked(self.oil, 90002)
        arrival = self._create_arrival()
        bev_dock = self._committed_docking(arrival, self.beverages, bev_plan, "111")
        oil_dock = self._committed_docking(arrival, self.oil, oil_plan, "222")
        client = APIClient()
        client.force_authenticate(self.user)

        response = client.post(f"/api/v1/gate-core/arrivals/{arrival.id}/dispatch/")
        self.assertEqual(response.status_code, 200)

        bev_dock.refresh_from_db()
        oil_dock.refresh_from_db()
        self.assertEqual(bev_dock.status, "DISPATCHED")
        self.assertEqual(oil_dock.status, "DISPATCHED")
        bev_plan.refresh_from_db()
        oil_plan.refresh_from_db()
        self.assertEqual(bev_plan.booking_status, DispatchPlanStatus.DISPATCHED)
        self.assertEqual(oil_plan.booking_status, DispatchPlanStatus.DISPATCHED)
        # Covers consumed -> gate-ins retired -> the truck may now physically depart.
        self.assertFalse(
            arrival.gate_ins.filter(is_active=True, retired_at__isnull=True).exists()
        )
        depart = client.post(f"/api/v1/gate-core/arrivals/{arrival.id}/depart/")
        self.assertEqual(depart.status_code, 200)
        arrival.refresh_from_db()
        self.assertEqual(arrival.status, VehicleArrivalStatus.DEPARTED)

    def test_arrival_dispatch_rolls_back_when_one_company_not_committed(self):
        bev_plan = self._booked(self.beverages, 90001)
        oil_plan = self._booked(self.oil, 90002)
        arrival = self._create_arrival()
        bev_dock = self._committed_docking(arrival, self.beverages, bev_plan, "111")
        oil_dock = self._committed_docking(arrival, self.oil, oil_plan, "222")
        oil_dock.status = "GATEPASS_PRINTED"  # printed but not committed
        oil_dock.print_committed_at = None
        oil_dock.save(update_fields=["status", "print_committed_at"])
        client = APIClient()
        client.force_authenticate(self.user)

        response = client.post(f"/api/v1/gate-core/arrivals/{arrival.id}/dispatch/")
        self.assertEqual(response.status_code, 400)
        bev_dock.refresh_from_db()
        self.assertEqual(bev_dock.status, "PRINT_COMMITTED")  # rolled back, not dispatched

    def test_dispatch_cascades_across_companies_without_a_shared_arrival(self):
        # The failure mode: two companies' bills on ONE physical truck, but their
        # dockings were never threaded onto a shared VehicleArrival (arrival=None).
        # Dispatching one must still take the whole physical truck out -- grouping
        # by the vehicle, not the (missing) arrival FK.
        from gate_core.services.sales_dispatch_dispatch import dispatch_vehicle_trip

        bev_plan = self._booked(self.beverages, 90001)
        oil_plan = self._booked(self.oil, 90002)
        bev_dock = self._committed_docking(None, self.beverages, bev_plan, "111")
        oil_dock = self._committed_docking(None, self.oil, oil_plan, "222")
        self.assertIsNone(bev_dock.arrival_id)
        self.assertIsNone(oil_dock.arrival_id)

        dispatch_vehicle_trip(bev_dock, self.user)

        bev_dock.refresh_from_db()
        oil_dock.refresh_from_db()
        self.assertEqual(bev_dock.status, "DISPATCHED")
        self.assertEqual(oil_dock.status, "DISPATCHED")  # sibling co-dispatched
        bev_plan.refresh_from_db()
        oil_plan.refresh_from_db()
        self.assertEqual(bev_plan.booking_status, DispatchPlanStatus.DISPATCHED)
        self.assertEqual(oil_plan.booking_status, DispatchPlanStatus.DISPATCHED)

    def test_dispatch_without_arrival_rolls_back_when_a_sibling_not_committed(self):
        # One truck, one exit still holds without an arrival: a not-ready sibling
        # blocks the whole dispatch so the truck can't leave for one company while
        # another is still loading.
        from gate_core.services.sales_dispatch_dispatch import dispatch_vehicle_trip

        bev_plan = self._booked(self.beverages, 90001)
        oil_plan = self._booked(self.oil, 90002)
        bev_dock = self._committed_docking(None, self.beverages, bev_plan, "111")
        oil_dock = self._committed_docking(None, self.oil, oil_plan, "222")
        oil_dock.status = "GATEPASS_PRINTED"  # printed but not committed
        oil_dock.print_committed_at = None
        oil_dock.save(update_fields=["status", "print_committed_at"])

        with self.assertRaises(ValueError):
            dispatch_vehicle_trip(bev_dock, self.user)

        bev_dock.refresh_from_db()
        oil_dock.refresh_from_db()
        self.assertEqual(bev_dock.status, "PRINT_COMMITTED")  # rolled back
        self.assertEqual(oil_dock.status, "GATEPASS_PRINTED")

    def test_dispatch_ignores_a_docking_on_a_different_arrival(self):
        # A stale docking left DOCKED on a PRIOR arrival of the same vehicle must
        # not be pulled into -- nor block -- the current trip's dispatch.
        from gate_core.models import VehicleArrival
        from gate_core.services.sales_dispatch_dispatch import dispatch_vehicle_trip

        today = VehicleArrival.objects.create(
            arrival_no="ARV-TODAY", vehicle=self.vehicle, driver=self.driver,
            gate_in_date=timezone.localdate(), in_time=timezone.now().time(),
        )
        prior = VehicleArrival.objects.create(
            arrival_no="ARV-PRIOR", vehicle=self.vehicle, driver=self.driver,
            gate_in_date=timezone.localdate(), in_time=timezone.now().time(),
        )
        ready = self._committed_docking(today, self.beverages, self._booked(self.beverages, 92001), "321")
        stale = self._committed_docking(prior, self.oil, self._booked(self.oil, 92002), "322")
        stale.status = "DOCKED"  # left DOCKED on the older trip
        stale.print_committed_at = None
        stale.save(update_fields=["status", "print_committed_at"])

        dispatch_vehicle_trip(ready, self.user)

        ready.refresh_from_db()
        stale.refresh_from_db()
        self.assertEqual(ready.status, "DISPATCHED")  # current trip went out cleanly
        self.assertEqual(stale.status, "DOCKED")  # other arrival untouched, did not block


class CombinedGatepassTests(TestCase):
    """One ARV/... gatepass spanning a multi-company truck's per-company dockings."""

    def setUp(self):
        self.beverages = Company.objects.create(name="Jivo Beverages", code="JIVO_BEVERAGES")
        self.oil = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        role = UserRole.objects.create(name="Gate")
        self.user = get_user_model().objects.create_user(
            email="combined@example.com",
            password="testpass123",
            full_name="Combined User",
            employee_code="CMB001",
        )
        UserCompany.objects.create(
            user=self.user, company=self.beverages, role=role, is_active=True
        )
        UserCompany.objects.create(user=self.user, company=self.oil, role=role, is_active=True)
        vehicle_type = VehicleType.objects.create(name="TRUCK-CMB")
        self.vehicle = Vehicle.objects.create(
            vehicle_number="DL01CMB0001", vehicle_type=vehicle_type
        )
        self.driver = Driver.objects.create(
            name="Combined Driver", mobile_no="9111111111", license_no="DL-CMB-0001"
        )
        self.arrival = VehicleArrival.objects.create(
            arrival_no="ARV-CMB-0001",
            vehicle=self.vehicle,
            driver=self.driver,
            gate_in_date=timezone.localdate(),
            in_time=timezone.now().time(),
            tare_weight=Decimal("250.000"),
            status=VehicleArrivalStatus.LOADING,
            created_by=self.user,
            updated_by=self.user,
        )

    def _client(self):
        client = APIClient()
        client.force_authenticate(self.user)
        return client

    def _ready_docking(self, company, suffix, *, status_value="READY_FOR_GATEPASS", ready=True):
        from django.core.files.uploadedfile import SimpleUploadedFile

        from driver_management.models import VehicleEntry
        from weighment.models import Weighment

        from gate_core.models import (
            SalesDispatchAttachment,
            SalesDispatchAttachmentType,
            SalesDispatchDocumentType,
            SalesDispatchGateOut,
            SalesDispatchGateOutItem,
        )

        entry = VehicleEntry.objects.create(
            entry_no=f"DOCKV-{suffix}", company=company, vehicle=self.vehicle,
            driver=self.driver, entry_type="SALES_DISPATCH", status="IN_PROGRESS",
            created_by=self.user, updated_by=self.user,
        )
        docking = SalesDispatchGateOut.objects.create(
            company=company, entry_no=f"DOCK-{suffix}", arrival=self.arrival,
            vehicle_entry=entry, vehicle=self.vehicle, driver=self.driver,
            document_type=SalesDispatchDocumentType.INVOICE,
            sap_doc_entry=int(suffix), sap_doc_num=f"INV-{suffix}",
            sap_doc_total=Decimal("1000.00"), status=status_value,
            truck_photo=f"sales_dispatch/truck_photos/{suffix}.jpg",
            photo_latitude=Decimal("28.613900"), photo_longitude=Decimal("77.209000"),
            bilty_no=f"BLT-{suffix}" if ready else "",
            bilty_date=timezone.localdate() if ready else None,
            created_by=self.user, updated_by=self.user,
        )
        SalesDispatchGateOutItem.objects.create(
            sales_dispatch=docking, line_num=0, item_code=f"ITEM-{suffix}",
            item_name="Item", quantity=Decimal("10.000"), uom="BOX",
            created_by=self.user, updated_by=self.user,
        )
        Weighment.objects.create(
            vehicle_entry=entry, gross_weight=Decimal("1000.000"),
            tare_weight=Decimal("250.000"), created_by=self.user, updated_by=self.user,
        )
        if ready:
            SalesDispatchAttachment.objects.create(
                sales_dispatch=docking,
                attachment_type=SalesDispatchAttachmentType.BILTY,
                file=SimpleUploadedFile(
                    f"bilty-{suffix}.pdf", b"x", content_type="application/pdf"
                ),
                original_filename=f"bilty-{suffix}.pdf", uploaded_by=self.user,
            )
        return docking

    @override_settings(DOCKING_BOX_SCAN_OPTIONAL_COMPANY_CODES=["JIVO_BEVERAGES", "JIVO_OIL"])
    def test_print_assigns_arrival_number_and_each_docking(self):
        from gate_core.models import (
            SalesDispatchGatepassPrintLog,
            SalesDispatchGatepassPrintType,
        )

        bev_dock = self._ready_docking(self.beverages, "111")
        oil_dock = self._ready_docking(self.oil, "222")
        client = self._client()
        base = f"/api/v1/gate-core/arrivals/{self.arrival.id}/gatepass"

        readiness = client.get(f"{base}/readiness/")
        self.assertEqual(readiness.status_code, 200)
        self.assertTrue(readiness.data["ready"])
        self.assertEqual(len(readiness.data["companies"]), 2)

        response = client.post(f"{base}/print/")
        self.assertEqual(response.status_code, 200)

        self.arrival.refresh_from_db()
        self.assertTrue(self.arrival.gatepass_no.startswith("ARV/"))
        self.assertIsNotNone(self.arrival.gatepass_printed_at)
        for dock in (bev_dock, oil_dock):
            dock.refresh_from_db()
            self.assertTrue(dock.gatepass_no.startswith(f"DCK/{dock.company.code}/"))
            self.assertEqual(dock.status, "GATEPASS_PRINTED")
            self.assertEqual(
                SalesDispatchGatepassPrintLog.objects.filter(
                    sales_dispatch=dock,
                    print_type=SalesDispatchGatepassPrintType.ORIGINAL,
                ).count(),
                1,
            )

    @override_settings(DOCKING_BOX_SCAN_OPTIONAL_COMPANY_CODES=["JIVO_BEVERAGES", "JIVO_OIL"])
    def test_print_blocked_when_one_company_locked(self):
        from gate_core.models import SalesDispatchLock

        self._ready_docking(self.beverages, "111")
        self._ready_docking(self.oil, "222")
        lock = SalesDispatchLock.for_company(self.beverages)
        lock.is_locked = True
        lock.save(update_fields=["is_locked", "updated_at"])
        client = self._client()
        base = f"/api/v1/gate-core/arrivals/{self.arrival.id}/gatepass"

        readiness = client.get(f"{base}/readiness/")
        self.assertFalse(readiness.data["ready"])
        self.assertEqual(readiness.data["locked_companies"], ["JIVO_BEVERAGES"])

        response = client.post(f"{base}/print/")
        self.assertEqual(response.status_code, 423)
        self.assertEqual(response.data["locked_companies"], ["JIVO_BEVERAGES"])
        self.arrival.refresh_from_db()
        self.assertIsNone(self.arrival.gatepass_no)  # no number assigned to anyone

    @override_settings(DOCKING_BOX_SCAN_OPTIONAL_COMPANY_CODES=["JIVO_BEVERAGES", "JIVO_OIL"])
    def test_print_blocked_when_a_docking_not_ready(self):
        self._ready_docking(self.beverages, "111")
        self._ready_docking(self.oil, "222", ready=False)  # missing bilty
        client = self._client()
        base = f"/api/v1/gate-core/arrivals/{self.arrival.id}/gatepass"

        self.assertFalse(client.get(f"{base}/readiness/").data["ready"])
        response = client.post(f"{base}/print/")
        self.assertEqual(response.status_code, 400)
        self.arrival.refresh_from_db()
        self.assertIsNone(self.arrival.gatepass_no)

    @override_settings(DOCKING_BOX_SCAN_OPTIONAL_COMPANY_CODES=["JIVO_BEVERAGES", "JIVO_OIL"])
    def test_commit_then_reprint(self):
        from gate_core.models import (
            SalesDispatchGatepassPrintLog,
            SalesDispatchGatepassPrintType,
        )

        bev_dock = self._ready_docking(self.beverages, "111")
        oil_dock = self._ready_docking(self.oil, "222")
        client = self._client()
        base = f"/api/v1/gate-core/arrivals/{self.arrival.id}/gatepass"
        self.assertEqual(client.post(f"{base}/print/").status_code, 200)

        self.assertEqual(client.post(f"{base}/commit/").status_code, 200)
        self.arrival.refresh_from_db()
        self.assertIsNotNone(self.arrival.gatepass_committed_at)
        for dock in (bev_dock, oil_dock):
            dock.refresh_from_db()
            self.assertEqual(dock.status, "PRINT_COMMITTED")

        # Reprint requires a reason; with one it appends a REPRINT log per docking.
        self.assertEqual(client.post(f"{base}/reprint/", {}, format="json").status_code, 400)
        ok = client.post(f"{base}/reprint/", {"reprint_reason": "torn"}, format="json")
        self.assertEqual(ok.status_code, 200)
        for dock in (bev_dock, oil_dock):
            self.assertEqual(
                SalesDispatchGatepassPrintLog.objects.filter(
                    sales_dispatch=dock,
                    print_type=SalesDispatchGatepassPrintType.REPRINT,
                ).count(),
                1,
            )

    @override_settings(DOCKING_BOX_SCAN_OPTIONAL_COMPANY_CODES=["JIVO_BEVERAGES", "JIVO_OIL"])
    def test_arrival_with_out_of_scope_company_denied(self):
        mart = Company.objects.create(name="Jivo Mart", code="JIVO_MART")  # user NOT a member
        self._ready_docking(self.beverages, "111")
        self._ready_docking(mart, "222")
        client = self._client()
        base = f"/api/v1/gate-core/arrivals/{self.arrival.id}/gatepass"

        self.assertEqual(client.get(f"{base}/readiness/").status_code, 403)
        self.assertEqual(client.post(f"{base}/print/").status_code, 403)
