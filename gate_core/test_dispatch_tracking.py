"""Post-dispatch truck tracking: list dispatched trucks + append status events."""
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from company.models import Company, UserCompany, UserRole
from driver_management.models import Driver, VehicleEntry
from gate_core.models import (
    SalesDispatchDocumentType,
    SalesDispatchGateOut,
    SalesDispatchGateOutStatus,
    VehicleArrival,
    VehicleArrivalStatus,
)
from vehicle_management.models import Vehicle, VehicleType


class DispatchTrackingTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        self.role = UserRole.objects.create(name="Gate")
        self.user = get_user_model().objects.create_user(
            email="dt@example.com", password="p", full_name="DT User", employee_code="DT1",
        )
        UserCompany.objects.create(
            user=self.user, company=self.company, role=self.role, is_default=True
        )
        self.user.user_permissions.add(
            *Permission.objects.filter(
                content_type__app_label="gate_core",
                codename__in=["can_view_dispatch_tracking", "can_update_dispatch_tracking"],
            )
        )
        vt = VehicleType.objects.create(name="TRUCK-DT")
        self.vehicle = Vehicle.objects.create(vehicle_number="DL01DT0001", vehicle_type=vt)
        self.driver = Driver.objects.create(name="Driver DT", mobile_no="9000000000", license_no="DL-DT")
        self.arrival = VehicleArrival.objects.create(
            arrival_no="ARV-DT-0001", vehicle=self.vehicle, driver=self.driver,
            gate_in_date=timezone.localdate(), in_time=timezone.now().time(),
            status=VehicleArrivalStatus.DEPARTED, departed_at=timezone.now(),
            created_by=self.user, updated_by=self.user,
        )
        ve = VehicleEntry.objects.create(
            entry_no="DOCKV-DT", company=self.company, vehicle=self.vehicle, driver=self.driver,
            entry_type="SALES_DISPATCH", status="COMPLETED", created_by=self.user, updated_by=self.user,
        )
        SalesDispatchGateOut.objects.create(
            company=self.company, entry_no="DOCK-DT", arrival=self.arrival, vehicle_entry=ve,
            vehicle=self.vehicle, driver=self.driver, document_type=SalesDispatchDocumentType.INVOICE,
            sap_doc_entry=1001, sap_doc_num="INV-1001", customer_name="ACME LTD",
            status=SalesDispatchGateOutStatus.DISPATCHED, dispatched_at=timezone.now(),
            created_by=self.user, updated_by=self.user,
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.hdr = {"HTTP_COMPANY_CODE": self.company.code}

    def test_list_shows_dispatched_truck_defaulting_to_dispatched(self):
        resp = self.client.get("/api/v1/gate-core/dispatch-tracking/", **self.hdr)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(len(resp.data), 1)
        row = resp.data[0]
        self.assertEqual(row["arrival_no"], "ARV-DT-0001")
        self.assertEqual(row["current_status"], "DISPATCHED")
        self.assertEqual(row["companies"], ["Jivo Oil"])
        self.assertEqual(row["documents"], ["INV-1001"])
        self.assertEqual(row["customers"], ["ACME LTD"])

    def test_add_update_changes_current_status_and_timeline(self):
        resp = self.client.post(
            f"/api/v1/gate-core/dispatch-tracking/{self.arrival.id}/updates/",
            {"status": "IN_TRANSIT", "location": "NH-48", "remarks": "on the way"},
            **self.hdr,
        )
        self.assertEqual(resp.status_code, 201, resp.content)

        board = self.client.get("/api/v1/gate-core/dispatch-tracking/", **self.hdr)
        self.assertEqual(board.data[0]["current_status"], "IN_TRANSIT")
        self.assertEqual(board.data[0]["update_count"], 1)

        timeline = self.client.get(
            f"/api/v1/gate-core/dispatch-tracking/{self.arrival.id}/updates/", **self.hdr
        )
        self.assertEqual(len(timeline.data), 1)
        self.assertEqual(timeline.data[0]["status"], "IN_TRANSIT")
        self.assertEqual(timeline.data[0]["location"], "NH-48")
        self.assertEqual(timeline.data[0]["created_by_name"], "DT User")

    def test_requires_view_permission(self):
        self.user.user_permissions.clear()
        resp = self.client.get("/api/v1/gate-core/dispatch-tracking/", **self.hdr)
        self.assertEqual(resp.status_code, 403, resp.content)
