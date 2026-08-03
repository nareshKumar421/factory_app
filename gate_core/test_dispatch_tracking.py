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
    SalesDispatchGateOutDocument,
    SalesDispatchGateOutStatus,
    SalesDispatchGateOutItem,
    TruckDispatchPartialDeliveryItem,
    TruckDispatchPartialDeliveryLine,
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


class PartialDeliveryTests(TestCase):
    """Recording, item-wise, what the customer refused on a partial delivery."""

    def setUp(self):
        self.company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        self.role = UserRole.objects.create(name="Gate")
        self.user = get_user_model().objects.create_user(
            email="pd@example.com", password="p", full_name="PD User", employee_code="PD1",
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
        vt = VehicleType.objects.create(name="TRUCK-PD")
        self.vehicle = Vehicle.objects.create(vehicle_number="DL01PD0001", vehicle_type=vt)
        self.driver = Driver.objects.create(name="Driver PD", mobile_no="9111111111", license_no="DL-PD")
        self.arrival = VehicleArrival.objects.create(
            arrival_no="ARV-PD-0001", vehicle=self.vehicle, driver=self.driver,
            gate_in_date=timezone.localdate(), in_time=timezone.now().time(),
            status=VehicleArrivalStatus.DEPARTED, departed_at=timezone.now(),
            created_by=self.user, updated_by=self.user,
        )
        ve = VehicleEntry.objects.create(
            entry_no="DOCKV-PD", company=self.company, vehicle=self.vehicle, driver=self.driver,
            entry_type="SALES_DISPATCH", status="COMPLETED", created_by=self.user, updated_by=self.user,
        )
        self.docking = SalesDispatchGateOut.objects.create(
            company=self.company, entry_no="DOCK-PD", arrival=self.arrival, vehicle_entry=ve,
            vehicle=self.vehicle, driver=self.driver, document_type=SalesDispatchDocumentType.INVOICE,
            sap_doc_entry=2001, sap_doc_num="INV-2001", customer_name="ACME LTD",
            status=SalesDispatchGateOutStatus.DISPATCHED, dispatched_at=timezone.now(),
            created_by=self.user, updated_by=self.user,
        )
        # Mirrors production: total_boxes is 0 everywhere, quantity carries the data.
        self.bill_a = SalesDispatchGateOutDocument.objects.create(
            sales_dispatch=self.docking, company=self.company,
            document_type=SalesDispatchDocumentType.INVOICE,
            sap_doc_entry=2001, sap_doc_num="INV-2001", customer_name="ACME LTD",
            total_boxes=0, total_quantity=1000, created_by=self.user, updated_by=self.user,
        )
        self.bill_b = SalesDispatchGateOutDocument.objects.create(
            sales_dispatch=self.docking, company=self.company,
            document_type=SalesDispatchDocumentType.INVOICE,
            sap_doc_entry=2002, sap_doc_num="INV-2002", customer_name="BETA STORES",
            total_boxes=0, total_quantity=400, created_by=self.user, updated_by=self.user,
        )
        self.item_a1 = SalesDispatchGateOutItem.objects.create(
            sales_dispatch=self.docking, document=self.bill_a, line_num=1,
            item_code="FG0000328", item_name="PET BOTTLE 250 ML", quantity=600, uom="PCS",
            total_boxes=0, created_by=self.user, updated_by=self.user,
        )
        self.item_a2 = SalesDispatchGateOutItem.objects.create(
            sales_dispatch=self.docking, document=self.bill_a, line_num=2,
            item_code="FG0000422", item_name="MUSTARD 1L", quantity=400, uom="PCS",
            total_boxes=0, created_by=self.user, updated_by=self.user,
        )
        self.item_b1 = SalesDispatchGateOutItem.objects.create(
            sales_dispatch=self.docking, document=self.bill_b, line_num=3,
            item_code="FG0000500", item_name="OLIVE 500ML", quantity=400, uom="PCS",
            total_boxes=0, created_by=self.user, updated_by=self.user,
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.hdr = {"HTTP_COMPANY_CODE": self.company.code}
        self.updates_url = f"/api/v1/gate-core/dispatch-tracking/{self.arrival.id}/updates/"
        self.bills_url = f"/api/v1/gate-core/dispatch-tracking/{self.arrival.id}/bills/"

    def _partial(self, **overrides):
        payload = {
            "status": "PARTIALLY_DELIVERED",
            "partial_lines": [{
                "document": self.bill_a.id,
                "items": [
                    {"item": self.item_a1.id, "qty_delivered": "500", "qty_returned": "100"},
                ],
            }],
        }
        payload.update(overrides)
        return payload

    def test_bills_endpoint_lists_bills_with_their_items(self):
        resp = self.client.get(self.bills_url, **self.hdr)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual([b["sap_doc_num"] for b in resp.data], ["INV-2001", "INV-2002"])

        first = resp.data[0]
        self.assertEqual(
            [(i["item_code"], str(i["quantity"]), i["uom"]) for i in first["items"]],
            [("FG0000328", "600.000", "PCS"), ("FG0000422", "400.000", "PCS")],
        )

    def test_bills_endpoint_does_not_scale_queries_with_bill_count(self):
        """Bills, their company and their items are all prefetched.

        Regression: `company` is serialized per bill, so without the
        documents__company prefetch a 22-bill truck cost 22 extra queries.
        """
        from django.test.utils import CaptureQueriesContext
        from django.db import connection

        def count_queries():
            with CaptureQueriesContext(connection) as ctx:
                resp = self.client.get(self.bills_url, **self.hdr)
                self.assertEqual(resp.status_code, 200, resp.content)
            return len(ctx), len(resp.data)

        # Warm up first: the opening request also fills Django's permission cache.
        count_queries()
        before, n_before = count_queries()

        # Add a third bill with its own item, then re-measure.
        extra = SalesDispatchGateOutDocument.objects.create(
            sales_dispatch=self.docking, company=self.company,
            document_type=SalesDispatchDocumentType.INVOICE,
            sap_doc_entry=2003, sap_doc_num="INV-2003", customer_name="GAMMA TRADERS",
            total_boxes=0, total_quantity=90, created_by=self.user, updated_by=self.user,
        )
        SalesDispatchGateOutItem.objects.create(
            sales_dispatch=self.docking, document=extra, line_num=4,
            item_code="FG0000900", item_name="RICE BRAN 1L", quantity=90, uom="PCS",
            created_by=self.user, updated_by=self.user,
        )

        after, n_after = count_queries()

        self.assertEqual((n_before, n_after), (2, 3))
        self.assertEqual(
            before, after,
            f"bills endpoint N+1: {before} queries for 2 bills, {after} for 3",
        )

    def test_partial_delivery_records_item_wise_split(self):
        resp = self.client.post(
            self.updates_url,
            self._partial(
                delivered_date="2026-08-01",
                partial_lines=[{
                    "document": self.bill_a.id,
                    "remarks": "customer refused the 250ml",
                    "items": [
                        {"item": self.item_a1.id, "qty_delivered": "500", "qty_returned": "100"},
                        {"item": self.item_a2.id, "qty_delivered": "380", "qty_returned": "20"},
                    ],
                }],
            ),
            format="json", **self.hdr,
        )
        self.assertEqual(resp.status_code, 201, resp.content)

        line = resp.data["partial_lines"][0]
        self.assertEqual(line["sap_doc_num"], "INV-2001")
        # Bill totals are the sum of the item rows.
        self.assertEqual(str(line["qty_delivered"]), "880.000")
        self.assertEqual(str(line["qty_returned"]), "120.000")

        self.assertEqual(
            [(i["item_code"], str(i["qty_delivered"]), str(i["qty_returned"]), i["uom"])
             for i in line["items"]],
            [("FG0000328", "500.000", "100.000", "PCS"),
             ("FG0000422", "380.000", "20.000", "PCS")],
        )
        self.assertEqual(TruckDispatchPartialDeliveryItem.objects.count(), 2)

    def test_untouched_item_on_a_short_bill_is_not_recorded(self):
        resp = self.client.post(self.updates_url, self._partial(), format="json", **self.hdr)
        self.assertEqual(resp.status_code, 201, resp.content)
        line = resp.data["partial_lines"][0]
        self.assertEqual(len(line["items"]), 1)
        self.assertEqual(str(line["qty_returned"]), "100.000")

    def test_item_split_cannot_exceed_the_quantity_dispatched(self):
        resp = self.client.post(
            self.updates_url,
            self._partial(partial_lines=[{
                "document": self.bill_a.id,
                "items": [
                    {"item": self.item_a1.id, "qty_delivered": "500", "qty_returned": "200"},
                ],
            }]),
            format="json", **self.hdr,
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("exceeds", str(resp.data))

    def test_zero_total_boxes_does_not_block_a_partial_delivery(self):
        """Production has total_boxes = 0 on every bill; that must not gate entry."""
        self.assertEqual(self.bill_a.total_boxes, 0)
        resp = self.client.post(self.updates_url, self._partial(), format="json", **self.hdr)
        self.assertEqual(resp.status_code, 201, resp.content)

    def test_item_must_belong_to_the_named_bill(self):
        resp = self.client.post(
            self.updates_url,
            self._partial(partial_lines=[{
                "document": self.bill_a.id,
                "items": [{"item": self.item_b1.id, "qty_returned": "5"}],
            }]),
            format="json", **self.hdr,
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("is not on bill", str(resp.data))
        self.assertEqual(TruckDispatchPartialDeliveryLine.objects.count(), 0)

    def test_a_bill_line_needs_at_least_one_item(self):
        resp = self.client.post(
            self.updates_url,
            self._partial(partial_lines=[{"document": self.bill_a.id, "items": []}]),
            format="json", **self.hdr,
        )
        self.assertEqual(resp.status_code, 400, resp.content)

    def test_partial_delivery_requires_at_least_one_bill(self):
        resp = self.client.post(
            self.updates_url, {"status": "PARTIALLY_DELIVERED"}, format="json", **self.hdr
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("partial_lines", resp.data)

    def test_cannot_reference_a_bill_from_another_truck(self):
        other_arrival = VehicleArrival.objects.create(
            arrival_no="ARV-PD-0002", vehicle=self.vehicle, driver=self.driver,
            gate_in_date=timezone.localdate(), in_time=timezone.now().time(),
            status=VehicleArrivalStatus.DEPARTED, departed_at=timezone.now(),
            created_by=self.user, updated_by=self.user,
        )
        other_ve = VehicleEntry.objects.create(
            entry_no="DOCKV-PD2", company=self.company, vehicle=self.vehicle, driver=self.driver,
            entry_type="SALES_DISPATCH", status="COMPLETED", created_by=self.user, updated_by=self.user,
        )
        other_docking = SalesDispatchGateOut.objects.create(
            company=self.company, entry_no="DOCK-PD2", arrival=other_arrival, vehicle_entry=other_ve,
            vehicle=self.vehicle, driver=self.driver, document_type=SalesDispatchDocumentType.INVOICE,
            sap_doc_entry=3001, sap_doc_num="INV-3001", customer_name="GAMMA",
            status=SalesDispatchGateOutStatus.DISPATCHED, dispatched_at=timezone.now(),
            created_by=self.user, updated_by=self.user,
        )
        foreign_bill = SalesDispatchGateOutDocument.objects.create(
            sales_dispatch=other_docking, company=self.company,
            document_type=SalesDispatchDocumentType.INVOICE,
            sap_doc_entry=3001, sap_doc_num="INV-3001", customer_name="GAMMA",
            total_boxes=0, total_quantity=10, created_by=self.user, updated_by=self.user,
        )
        foreign_item = SalesDispatchGateOutItem.objects.create(
            sales_dispatch=other_docking, document=foreign_bill, line_num=1,
            item_code="FG-X", item_name="X", quantity=10, uom="PCS",
            created_by=self.user, updated_by=self.user,
        )
        resp = self.client.post(
            self.updates_url,
            self._partial(partial_lines=[{
                "document": foreign_bill.id,
                "items": [{"item": foreign_item.id, "qty_returned": "5"}],
            }]),
            format="json", **self.hdr,
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("not on this truck", str(resp.data))
        self.assertEqual(TruckDispatchPartialDeliveryLine.objects.count(), 0)

    # --- the return note is always optional ---------------------------------

    def test_partial_delivery_saves_without_a_return_note(self):
        resp = self.client.post(self.updates_url, self._partial(), format="json", **self.hdr)
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertIsNone(resp.data["return_note"])

    def test_return_note_can_be_attached_later(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        created = self.client.post(self.updates_url, self._partial(), format="json", **self.hdr)
        update_id = created.data["id"]
        self.assertIsNone(created.data["return_note"])

        note = SimpleUploadedFile("return-note.pdf", b"%PDF-1.4", content_type="application/pdf")
        resp = self.client.post(
            f"/api/v1/gate-core/dispatch-tracking/{self.arrival.id}/updates/{update_id}/return-note/",
            {"return_note": note}, format="multipart", **self.hdr,
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertIn("return-note", resp.data["return_note"])
        # The item rows are untouched by the later upload.
        self.assertEqual(len(resp.data["partial_lines"][0]["items"]), 1)

    def test_return_note_upload_rejected_on_a_non_partial_update(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        created = self.client.post(
            self.updates_url, {"status": "DELIVERED"}, format="json", **self.hdr
        )
        note = SimpleUploadedFile("n.pdf", b"%PDF-1.4", content_type="application/pdf")
        resp = self.client.post(
            f"/api/v1/gate-core/dispatch-tracking/{self.arrival.id}/updates/"
            f"{created.data['id']}/return-note/",
            {"return_note": note}, format="multipart", **self.hdr,
        )
        self.assertEqual(resp.status_code, 400, resp.content)

    def test_return_note_upload_requires_the_update_permission(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        created = self.client.post(self.updates_url, self._partial(), format="json", **self.hdr)
        self.user.user_permissions.clear()
        self.user.user_permissions.add(
            *Permission.objects.filter(
                content_type__app_label="gate_core", codename="can_view_dispatch_tracking",
            )
        )
        # The POST above populated Django's per-instance permission cache, so
        # re-authenticate with a fresh user or the revoked perm still reads True.
        self.user = get_user_model().objects.get(pk=self.user.pk)
        self.client.force_authenticate(self.user)
        note = SimpleUploadedFile("n.pdf", b"%PDF-1.4", content_type="application/pdf")
        resp = self.client.post(
            f"/api/v1/gate-core/dispatch-tracking/{self.arrival.id}/updates/"
            f"{created.data['id']}/return-note/",
            {"return_note": note}, format="multipart", **self.hdr,
        )
        self.assertEqual(resp.status_code, 403, resp.content)

    # --- shared behaviour ---------------------------------------------------

    def test_multipart_carries_files_with_rows_as_json(self):
        import json as _json
        from django.core.files.uploadedfile import SimpleUploadedFile

        proof = SimpleUploadedFile("proof.jpg", b"\xff\xd8\xff\xd9", content_type="image/jpeg")
        note = SimpleUploadedFile("return-note.pdf", b"%PDF-1.4", content_type="application/pdf")
        resp = self.client.post(
            self.updates_url,
            {
                "status": "PARTIALLY_DELIVERED",
                "delivered_date": "2026-08-01",
                "proof": proof,
                "return_note": note,
                "partial_lines": _json.dumps([{
                    "document": self.bill_a.id,
                    "items": [{"item": self.item_a1.id,
                               "qty_delivered": "500", "qty_returned": "100"}],
                }]),
            },
            format="multipart", **self.hdr,
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertIn("return-note", resp.data["return_note"])
        self.assertIn("proof", resp.data["proof"])
        self.assertEqual(str(resp.data["partial_lines"][0]["items"][0]["qty_returned"]), "100.000")

    def test_delivered_accepts_a_delivered_date(self):
        resp = self.client.post(
            self.updates_url, {"status": "DELIVERED", "delivered_date": "2026-08-02"},
            format="json", **self.hdr,
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(str(resp.data["delivered_date"]), "2026-08-02")

    def test_delivered_date_rejected_on_a_non_delivery_status(self):
        resp = self.client.post(
            self.updates_url, {"status": "IN_TRANSIT", "delivered_date": "2026-08-02"},
            format="json", **self.hdr,
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("delivered_date", resp.data)

    def test_lines_rejected_on_a_non_partial_status(self):
        resp = self.client.post(
            self.updates_url, self._partial(status="DELIVERED"), format="json", **self.hdr
        )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIn("partial_lines", resp.data)

    def test_back_dated_delivery_does_not_disturb_current_status(self):
        self.client.post(self.updates_url, {"status": "IN_TRANSIT"}, format="json", **self.hdr)
        self.client.post(
            self.updates_url, self._partial(delivered_date="2020-01-01"), format="json", **self.hdr
        )
        row = self.client.get("/api/v1/gate-core/dispatch-tracking/", **self.hdr).data["results"][0]
        self.assertEqual(row["current_status"], "PARTIALLY_DELIVERED")

    def test_update_requires_the_update_permission(self):
        self.user.user_permissions.clear()
        self.user.user_permissions.add(
            *Permission.objects.filter(
                content_type__app_label="gate_core", codename="can_view_dispatch_tracking",
            )
        )
        resp = self.client.post(self.updates_url, self._partial(), format="json", **self.hdr)
        self.assertEqual(resp.status_code, 403, resp.content)
