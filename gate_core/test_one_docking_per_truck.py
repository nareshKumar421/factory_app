"""One docking per truck: the truck photo (load lock) is blocked while the truck
still has booked bills not on this docking, so they don't split onto a second
docking + gatepass. ``allow_partial`` overrides to dispatch what's loaded."""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from company.models import Company, UserCompany, UserRole
from dispatch_plans.models import DispatchPlan, DispatchPlanStatus
from driver_management.models import Driver, VehicleEntry
from gate_core.models import (
    EmptyVehicleGateIn,
    EmptyVehicleGateInCover,
    SalesDispatchDocumentType,
    SalesDispatchGateOut,
    SalesDispatchGateOutDocument,
    SalesDispatchGateOutStatus,
)
from gate_core.services.sales_dispatch_docking import undocked_booked_bills
from vehicle_management.models import Transporter, Vehicle


class OneDockingPerTruckTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Jivo Beverages", code="JIVO_BEV")
        self.role = UserRole.objects.create(name="Gate")
        self.user = get_user_model().objects.create_user(
            email="odt@example.com", password="p", full_name="ODT", employee_code="ODT1",
        )
        UserCompany.objects.create(user=self.user, company=self.company, role=self.role, is_default=True)
        self.user.user_permissions.add(*Permission.objects.filter(content_type__app_label="gate_core"))
        self.transporter = Transporter.objects.create(name="T")
        self.vehicle = Vehicle.objects.create(vehicle_number="DL01LY5728", transporter=self.transporter)
        self.driver = Driver.objects.create(name="D", mobile_no="9000000000", license_no="DL-1")
        self.client = APIClient()
        self.client.force_authenticate(self.user)

        # Gate-in with 3 booked bills (covers) for the truck.
        self.gate_ve = VehicleEntry.objects.create(
            entry_no="EVGI-VE", company=self.company, vehicle=self.vehicle, driver=self.driver,
            entry_type="EMPTY_VEHICLE", status="COMPLETED", created_by=self.user, updated_by=self.user,
        )
        self.gate_in = EmptyVehicleGateIn.objects.create(
            company=self.company, entry_no="EVGI-1", vehicle_entry=self.gate_ve, vehicle=self.vehicle,
            driver=self.driver, reason="DISPATCH", gate_in_date=timezone.localdate(),
            in_time=timezone.now().time(), created_by=self.user, updated_by=self.user,
        )
        self.plans = {}
        for de, num in [(16059, "626078171"), (16076, "626078179"), (16077, "626078180")]:
            plan = DispatchPlan.objects.create(
                company=self.company, sap_invoice_doc_entry=de, sap_invoice_doc_num=num,
                booking_status=DispatchPlanStatus.BOOKED, vehicle=self.vehicle,
                linked_vehicle_entry=self.gate_ve, created_by=self.user, updated_by=self.user,
            )
            EmptyVehicleGateInCover.objects.create(
                empty_vehicle_gate_in=self.gate_in, dispatch_plan=plan, sap_doc_entry=de,
                sap_doc_num=num, created_by=self.user, updated_by=self.user,
            )
            self.plans[de] = plan

    def _docking_with(self, *doc_entries):
        primary = self.plans[doc_entries[0]]
        ve = VehicleEntry.objects.create(
            entry_no="DOCKV-1", company=self.company, vehicle=self.vehicle, driver=self.driver,
            entry_type="SALES_DISPATCH", status="IN_PROGRESS", created_by=self.user, updated_by=self.user,
        )
        docking = SalesDispatchGateOut.objects.create(
            company=self.company, entry_no="DOCK-1", vehicle_entry=ve, dispatch_plan=primary,
            vehicle=self.vehicle, transporter=self.transporter, driver=self.driver,
            document_type=SalesDispatchDocumentType.INVOICE, sap_doc_entry=doc_entries[0],
            sap_doc_num=self.plans[doc_entries[0]].sap_invoice_doc_num,
            status=SalesDispatchGateOutStatus.DOCKED, created_by=self.user, updated_by=self.user,
        )
        for de in doc_entries:
            SalesDispatchGateOutDocument.objects.create(
                sales_dispatch=docking, company=self.company, dispatch_plan=self.plans[de],
                document_type=SalesDispatchDocumentType.INVOICE, sap_doc_entry=de,
                sap_doc_num=self.plans[de].sap_invoice_doc_num,
                created_by=self.user, updated_by=self.user,
            )
        return docking

    def _post_photo(self, docking, allow_partial=False):
        payload = {
            "attachment_type": "TRUCK_PHOTO",
            "file": SimpleUploadedFile("truck.jpg", b"img", content_type="image/jpeg"),
            "latitude": "28.6", "longitude": "77.2",
        }
        if allow_partial:
            payload["allow_partial"] = "true"
        return self.client.post(
            f"/api/v1/gate-core/sales-dispatch/{docking.id}/attachments/",
            payload, format="multipart", HTTP_COMPANY_CODE=self.company.code,
        )

    def test_helper_lists_undocked_booked_bills(self):
        docking = self._docking_with(16059)  # only bill 171 docked; 179, 180 booked but not
        undocked = undocked_booked_bills(docking)
        self.assertEqual({b["sap_doc_num"] for b in undocked}, {"626078179", "626078180"})

    def test_photo_blocked_when_bills_undocked(self):
        docking = self._docking_with(16059)
        resp = self._post_photo(docking)
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertTrue(resp.data.get("requires_partial_override"))
        self.assertEqual(len(resp.data["undocked_bills"]), 2)
        docking.refresh_from_db()
        self.assertEqual(docking.status, SalesDispatchGateOutStatus.DOCKED)  # not locked

    def test_allow_partial_overrides_the_block(self):
        docking = self._docking_with(16059)
        resp = self._post_photo(docking, allow_partial=True)
        self.assertIn(resp.status_code, (200, 201), resp.content)
        docking.refresh_from_db()
        self.assertEqual(docking.status, SalesDispatchGateOutStatus.PHOTO_ATTACHED)

    def test_photo_allowed_once_all_bills_docked(self):
        docking = self._docking_with(16059, 16076, 16077)  # all three docked
        self.assertEqual(undocked_booked_bills(docking), [])
        resp = self._post_photo(docking)
        self.assertIn(resp.status_code, (200, 201), resp.content)
        docking.refresh_from_db()
        self.assertEqual(docking.status, SalesDispatchGateOutStatus.PHOTO_ATTACHED)

    def _second_company_on_same_truck(self):
        """A sibling company gate-in on the SAME physical truck (own vehicle_entry),
        with one booked bill -- models a multi-company arrival."""
        other = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        UserCompany.objects.create(user=self.user, company=other, role=self.role)
        ve = VehicleEntry.objects.create(
            entry_no="EVGI-VE2", company=other, vehicle=self.vehicle, driver=self.driver,
            entry_type="EMPTY_VEHICLE", status="COMPLETED", created_by=self.user, updated_by=self.user,
        )
        gate_in = EmptyVehicleGateIn.objects.create(
            company=other, entry_no="EVGI-2", vehicle_entry=ve, vehicle=self.vehicle,
            driver=self.driver, reason="DISPATCH", gate_in_date=timezone.localdate(),
            in_time=timezone.now().time(), created_by=self.user, updated_by=self.user,
        )
        plan = DispatchPlan.objects.create(
            company=other, sap_invoice_doc_entry=99001, sap_invoice_doc_num="OIL-99001",
            booking_status=DispatchPlanStatus.BOOKED, vehicle=self.vehicle,
            linked_vehicle_entry=ve, created_by=self.user, updated_by=self.user,
        )
        EmptyVehicleGateInCover.objects.create(
            empty_vehicle_gate_in=gate_in, dispatch_plan=plan, sap_doc_entry=99001,
            sap_doc_num="OIL-99001", created_by=self.user, updated_by=self.user,
        )
        dve = VehicleEntry.objects.create(
            entry_no="DOCKV-2", company=other, vehicle=self.vehicle, driver=self.driver,
            entry_type="SALES_DISPATCH", status="IN_PROGRESS", created_by=self.user, updated_by=self.user,
        )
        docking = SalesDispatchGateOut.objects.create(
            company=other, entry_no="DOCK-2", vehicle_entry=dve, dispatch_plan=plan,
            vehicle=self.vehicle, transporter=self.transporter, driver=self.driver,
            document_type=SalesDispatchDocumentType.INVOICE, sap_doc_entry=99001, sap_doc_num="OIL-99001",
            status=SalesDispatchGateOutStatus.DOCKED, created_by=self.user, updated_by=self.user,
        )
        SalesDispatchGateOutDocument.objects.create(
            sales_dispatch=docking, company=other, dispatch_plan=plan,
            document_type=SalesDispatchDocumentType.INVOICE, sap_doc_entry=99001, sap_doc_num="OIL-99001",
            created_by=self.user, updated_by=self.user,
        )
        return other, docking

    def test_photo_gate_is_per_company_on_a_multi_company_truck(self):
        # Company A (beverages): only 1 of its 3 bills docked -> still blocked.
        bev_docking = self._docking_with(16059)
        # Company B (oil) on the SAME truck: its one bill is fully docked.
        _oil, oil_docking = self._second_company_on_same_truck()

        # The gate is scoped to each company's own gate-in: A sees its 2 un-docked
        # bills; B is complete and never blocked by A's bills (no cross-company merge).
        self.assertEqual({b["sap_doc_num"] for b in undocked_booked_bills(bev_docking)},
                         {"626078179", "626078180"})
        self.assertEqual(undocked_booked_bills(oil_docking), [])

        # Company B's docking photo passes (its bill is docked) even though A is partial.
        resp_b = self._post_photo(oil_docking)
        self.assertIn(resp_b.status_code, (200, 201), resp_b.content)
        # Company A's docking photo is still blocked on A's own bills.
        resp_a = self._post_photo(bev_docking)
        self.assertEqual(resp_a.status_code, 400)
