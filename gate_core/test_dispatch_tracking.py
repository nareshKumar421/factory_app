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
        self.assertEqual(resp.data["count"], 1)
        self.assertEqual(len(resp.data["results"]), 1)
        row = resp.data["results"][0]
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
        self.assertEqual(board.data["results"][0]["current_status"], "IN_TRANSIT")
        self.assertEqual(board.data["results"][0]["update_count"], 1)

        timeline = self.client.get(
            f"/api/v1/gate-core/dispatch-tracking/{self.arrival.id}/updates/", **self.hdr
        )
        self.assertEqual(len(timeline.data), 1)
        self.assertEqual(timeline.data[0]["status"], "IN_TRANSIT")
        self.assertEqual(timeline.data[0]["location"], "NH-48")
        self.assertEqual(timeline.data[0]["created_by_name"], "DT User")

    def test_in_transit_past_reach_date_is_late(self):
        from datetime import timedelta
        from django.utils import timezone
        past = (timezone.localdate() - timedelta(days=2)).isoformat()
        resp = self.client.post(
            f"/api/v1/gate-core/dispatch-tracking/{self.arrival.id}/updates/",
            {"status": "IN_TRANSIT", "expected_reach_date": past},
            **self.hdr,
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        row = self.client.get("/api/v1/gate-core/dispatch-tracking/", **self.hdr).data["results"][0]
        self.assertEqual(str(row["expected_reach_date"]), past)
        self.assertTrue(row["is_late"])
        self.assertEqual(row["days_overdue"], 2)

    def test_future_reach_date_is_not_late(self):
        from datetime import timedelta
        from django.utils import timezone
        future = (timezone.localdate() + timedelta(days=3)).isoformat()
        self.client.post(
            f"/api/v1/gate-core/dispatch-tracking/{self.arrival.id}/updates/",
            {"status": "IN_TRANSIT", "expected_reach_date": future}, **self.hdr,
        )
        row = self.client.get("/api/v1/gate-core/dispatch-tracking/", **self.hdr).data["results"][0]
        self.assertFalse(row["is_late"])
        self.assertEqual(row["days_overdue"], 0)

    def test_reached_is_not_late_even_after_reach_date(self):
        from datetime import timedelta
        from django.utils import timezone
        past = (timezone.localdate() - timedelta(days=1)).isoformat()
        self.client.post(
            f"/api/v1/gate-core/dispatch-tracking/{self.arrival.id}/updates/",
            {"status": "IN_TRANSIT", "expected_reach_date": past}, **self.hdr,
        )
        self.client.post(
            f"/api/v1/gate-core/dispatch-tracking/{self.arrival.id}/updates/",
            {"status": "REACHED_DESTINATION"}, **self.hdr,
        )
        row = self.client.get("/api/v1/gate-core/dispatch-tracking/", **self.hdr).data["results"][0]
        self.assertEqual(row["current_status"], "REACHED_DESTINATION")
        self.assertFalse(row["is_late"])

    def test_proof_url_is_absolute(self):
        """Uploaded proof comes back as an absolute URL (built from the request),
        so it resolves against the API host and not the frontend origin."""
        from django.core.files.uploadedfile import SimpleUploadedFile

        photo = SimpleUploadedFile("proof.jpg", b"\xff\xd8\xff\xd9", content_type="image/jpeg")
        resp = self.client.post(
            f"/api/v1/gate-core/dispatch-tracking/{self.arrival.id}/updates/",
            {"status": "DELIVERED", "proof": photo},
            format="multipart",
            **self.hdr,
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertTrue(resp.data["proof"].startswith("http"), resp.data["proof"])
        self.assertIn("/media/dispatch_tracking/proof/", resp.data["proof"])

        timeline = self.client.get(
            f"/api/v1/gate-core/dispatch-tracking/{self.arrival.id}/updates/", **self.hdr
        )
        self.assertTrue(timeline.data[0]["proof"].startswith("http"), timeline.data[0]["proof"])

    def test_requires_view_permission(self):
        self.user.user_permissions.clear()
        resp = self.client.get("/api/v1/gate-core/dispatch-tracking/", **self.hdr)
        self.assertEqual(resp.status_code, 403, resp.content)

    # --- summary (dashboard) endpoint ---

    SUMMARY_URL = "/api/v1/gate-core/dispatch-tracking/summary/"

    def test_summary_defaults_a_dispatched_truck(self):
        """A truck with no updates counts as DISPATCHED / no-update-yet, and the
        funnel shows it only at the Dispatched stage."""
        resp = self.client.get(self.SUMMARY_URL, **self.hdr)
        self.assertEqual(resp.status_code, 200, resp.content)
        d = resp.data
        self.assertEqual(d["total_dispatched"], 1)
        self.assertEqual(d["status_counts"]["DISPATCHED"], 1)
        self.assertEqual(d["no_update_yet"], 1)
        self.assertEqual(d["active"], 1)
        self.assertEqual(d["completed"], 0)
        funnel = {s["stage"]: s["count"] for s in d["funnel"]}
        self.assertEqual(funnel, {"Dispatched": 1, "In transit": 0, "Reached": 0, "Delivered": 0})
        self.assertEqual(d["late"]["count"], 0)

    def test_summary_counts_in_transit_and_late(self):
        from datetime import timedelta
        past = (timezone.localdate() - timedelta(days=2)).isoformat()
        self.client.post(
            f"/api/v1/gate-core/dispatch-tracking/{self.arrival.id}/updates/",
            {"status": "IN_TRANSIT", "expected_reach_date": past}, **self.hdr,
        )
        d = self.client.get(self.SUMMARY_URL, **self.hdr).data
        self.assertEqual(d["status_counts"]["IN_TRANSIT"], 1)
        self.assertEqual(d["status_counts"]["DISPATCHED"], 0)
        self.assertEqual(d["no_update_yet"], 0)
        funnel = {s["stage"]: s["count"] for s in d["funnel"]}
        self.assertEqual(funnel["In transit"], 1)
        self.assertEqual(funnel["Reached"], 0)
        self.assertEqual(d["late"]["count"], 1)
        self.assertEqual(d["late"]["trucks"][0]["days_overdue"], 2)
        self.assertEqual(d["late"]["trucks"][0]["arrival"], self.arrival.id)

    def test_summary_delivered_kpis(self):
        """A delivered truck moves to completed, fills the Delivered funnel stage,
        and contributes to the transit-time / delivered-today KPIs."""
        self.client.post(
            f"/api/v1/gate-core/dispatch-tracking/{self.arrival.id}/updates/",
            {"status": "IN_TRANSIT"}, **self.hdr,
        )
        self.client.post(
            f"/api/v1/gate-core/dispatch-tracking/{self.arrival.id}/updates/",
            {"status": "DELIVERED"}, **self.hdr,
        )
        d = self.client.get(self.SUMMARY_URL, **self.hdr).data
        self.assertEqual(d["status_counts"]["DELIVERED"], 1)
        self.assertEqual(d["completed"], 1)
        self.assertEqual(d["active"], 0)
        funnel = {s["stage"]: s["count"] for s in d["funnel"]}
        self.assertEqual((funnel["In transit"], funnel["Reached"], funnel["Delivered"]), (1, 1, 1))
        self.assertEqual(d["delivered_today"], 1)
        self.assertEqual(d["avg_transit_days"], 0.0)  # dispatched and delivered same day

    def test_summary_respects_date_range(self):
        """A truck dispatched outside the window is excluded."""
        resp = self.client.get(self.SUMMARY_URL + "?from_date=2020-01-01&to_date=2020-01-31", **self.hdr)
        self.assertEqual(resp.data["total_dispatched"], 0)

    def test_summary_requires_view_permission(self):
        self.user.user_permissions.clear()
        resp = self.client.get(self.SUMMARY_URL, **self.hdr)
        self.assertEqual(resp.status_code, 403, resp.content)
