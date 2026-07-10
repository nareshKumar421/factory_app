from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from company.models import Company, UserCompany, UserRole
from dispatch_plans.models import DispatchPlan, DispatchPlanStatus
from driver_management.models import Driver
from gate_core.models import (
    EmptyVehicleGateInCover,
    EmptyVehicleGateOut,
)
from gate_core.services.empty_vehicle_dispatch import create_vehicle_arrival
from vehicle_management.models import Vehicle, VehicleType
from weighment.models import Weighment


class InsideVehicleFlowTests(TestCase):
    """Add-bill-to-inside-vehicle (Part B) and cross-company empty-out (Part C)."""

    def setUp(self):
        self.beverages = Company.objects.create(name="Jivo Beverages", code="JIVO_BEVERAGES")
        self.oil = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        role = UserRole.objects.create(name="Gate")
        self.user = get_user_model().objects.create_user(
            email="inside@example.com",
            password="testpass123",
            full_name="Inside User",
            employee_code="INS001",
        )
        UserCompany.objects.create(
            user=self.user, company=self.beverages, role=role, is_active=True
        )
        UserCompany.objects.create(user=self.user, company=self.oil, role=role, is_active=True)
        vt = VehicleType.objects.create(name="TRUCK-INS")
        self.vehicle = Vehicle.objects.create(vehicle_number="DL01INS0001", vehicle_type=vt)
        self.driver = Driver.objects.create(
            name="Inside Driver", mobile_no="9000000001", license_no="DL-INS-0001"
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def _booked(self, company, doc_entry):
        return DispatchPlan.objects.create(
            company=company,
            sap_invoice_doc_entry=doc_entry,
            sap_invoice_doc_num=str(doc_entry),
            booking_status=DispatchPlanStatus.BOOKED,
            dispatch_date=timezone.localdate(),
            vehicle=self.vehicle,
        )

    def _arrival(self, tare=Decimal("1500.000")):
        return create_vehicle_arrival(
            vehicle=self.vehicle,
            driver=self.driver,
            company_ids=[self.beverages.id, self.oil.id],
            gate_in_date=timezone.localdate(),
            in_time=timezone.now().time(),
            tare_weight=tare,
            user=self.user,
        )

    # ---- Part B: add a bill to a vehicle that is already inside --------------
    def test_add_bill_to_inside_vehicle_creates_cover_and_links(self):
        self._booked(self.beverages, 80001)
        arrival = self._arrival()
        gate_in = arrival.gate_ins.get(company=self.beverages)
        new_plan = self._booked(self.beverages, 80009)  # a late second bill

        resp = self.client.post(
            "/api/v1/gate-core/inside-dispatch-vehicles/add-bill/",
            {"vehicle_entry_id": gate_in.vehicle_entry_id, "sap_doc_entry": 80009},
            format="json",
            HTTP_COMPANY_CODE=self.beverages.code,
        )

        self.assertEqual(resp.status_code, 200, resp.data)
        new_plan.refresh_from_db()
        self.assertEqual(new_plan.linked_vehicle_entry_id, gate_in.vehicle_entry_id)
        self.assertTrue(
            EmptyVehicleGateInCover.objects.filter(
                empty_vehicle_gate_in=gate_in, sap_doc_entry=80009, is_active=True
            ).exists()
        )

    def test_add_bill_rejects_vehicle_not_inside(self):
        resp = self.client.post(
            "/api/v1/gate-core/inside-dispatch-vehicles/add-bill/",
            {"vehicle_entry_id": 999999, "sap_doc_entry": 80001},
            format="json",
            HTTP_COMPANY_CODE=self.beverages.code,
        )
        self.assertEqual(resp.status_code, 404, resp.data)

    # ---- Part C: empty-out cascades across arrival siblings ------------------
    def test_empty_out_cascades_to_sibling_company(self):
        bev_plan = self._booked(self.beverages, 81001)
        oil_plan = self._booked(self.oil, 81002)
        arrival = self._arrival()
        bev_gate_in = arrival.gate_ins.get(company=self.beverages)
        oil_gate_in = arrival.gate_ins.get(company=self.oil)

        # The empty-out POST requires a full gross+tare weighment on the acting
        # (Beverages) entry; give it one (the physical exit weighment).
        weighment = Weighment.objects.get(vehicle_entry=bev_gate_in.vehicle_entry)
        weighment.gross_weight = Decimal("3000.000")
        weighment.save(update_fields=["gross_weight"])

        resp = self.client.post(
            "/api/v1/gate-core/empty-vehicle-outs/",
            {
                "vehicle_entry_id": bev_gate_in.vehicle_entry_id,
                "gate_out_date": timezone.localdate().isoformat(),
                "out_time": "17:00:00",
            },
            format="json",
            HTTP_COMPANY_CODE=self.beverages.code,
        )

        self.assertEqual(resp.status_code, 201, resp.data)
        # Both companies' entries are marked out empty...
        self.assertTrue(
            EmptyVehicleGateOut.objects.filter(
                vehicle_entry=bev_gate_in.vehicle_entry, status="COMPLETED"
            ).exists()
        )
        self.assertTrue(
            EmptyVehicleGateOut.objects.filter(
                vehicle_entry=oil_gate_in.vehicle_entry, status="COMPLETED"
            ).exists()
        )
        # ...and both companies' bills are released + gate-ins retired.
        bev_plan.refresh_from_db()
        oil_plan.refresh_from_db()
        self.assertIsNone(bev_plan.linked_vehicle_entry_id)
        self.assertIsNone(oil_plan.linked_vehicle_entry_id)
        bev_gate_in.refresh_from_db()
        oil_gate_in.refresh_from_db()
        self.assertIsNotNone(bev_gate_in.retired_at)
        self.assertIsNotNone(oil_gate_in.retired_at)

    def test_empty_out_eligible_entries_supports_all_companies(self):
        self._booked(self.beverages, 82001)
        self._booked(self.oil, 82002)
        self._arrival()
        # A gross+tare weighment keeps the entries eligible-list-able; not required
        # for listing, but mirrors real data.
        resp = self.client.get(
            "/api/v1/gate-core/empty-vehicle-outs/eligible-entries/?all_companies=1",
            HTTP_COMPANY_CODE=self.beverages.code,
        )
        self.assertEqual(resp.status_code, 200, resp.data)
        companies = {row.get("company_code") for row in resp.data}
        # Both companies' inside vehicles show up in the aggregated board.
        self.assertIn(self.beverages.code, companies)
        self.assertIn(self.oil.code, companies)
