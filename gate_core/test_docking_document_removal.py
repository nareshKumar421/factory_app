"""Removing a bill from a docking must fully unwind it — header, items, and box
scans — so nothing drifts (the header-stale + orphaned-scan bugs).

Covers the canonical ``remove_document_from_docking`` helper, the C1 remove-document
endpoint, the reader ``is_active`` filtering, and the inside-vehicle detach of a
*secondary* bill on a shared docking (previously left the document + scans + header
stale because the cancel filtered on the docking's primary plan only).
"""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from company.models import Company, UserCompany, UserRole
from dispatch_plans.models import DispatchPlan, DispatchPlanStatus
from driver_management.models import Driver, VehicleEntry
from gate_core.models import (
    EmptyVehicleGateIn,
    EmptyVehicleGateInCover,
    SalesDispatchBoxScan,
    SalesDispatchDocumentType,
    SalesDispatchGateOut,
    SalesDispatchGateOutDocument,
    SalesDispatchGateOutItem,
    SalesDispatchGateOutStatus,
)
from gate_core.services.empty_vehicle_dispatch import (
    bill_commit_reason,
    detach_bill_from_gate_in,
)
from gate_core.services.sales_dispatch_docking import remove_document_from_docking
from gate_core.services.sales_dispatch_gatepass import (
    requires_eway_bill,
    resolved_expected_box_count,
)
from vehicle_management.models import Transporter, Vehicle


class DockingDocumentRemovalTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        self.role = UserRole.objects.create(name="Gate")
        self.user = get_user_model().objects.create_user(
            email="rm@example.com", password="testpass123",
            full_name="RM User", employee_code="RM001",
        )
        UserCompany.objects.create(user=self.user, company=self.company, role=self.role, is_default=True)
        self.user.user_permissions.add(*Permission.objects.filter(content_type__app_label="gate_core"))
        self.transporter = Transporter.objects.create(name="T")
        self.vehicle = Vehicle.objects.create(vehicle_number="DL01LAC9967", transporter=self.transporter)
        self.driver = Driver.objects.create(name="Driver", mobile_no="9000000000", license_no="DL-1")
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    # ---- builders --------------------------------------------------------
    def _gate_in(self):
        ve = VehicleEntry.objects.create(
            entry_no="EVGI-1", company=self.company, vehicle=self.vehicle, driver=self.driver,
            entry_type="EMPTY_VEHICLE", status="COMPLETED", created_by=self.user, updated_by=self.user,
        )
        return EmptyVehicleGateIn.objects.create(
            company=self.company, entry_no="EVGI-1", vehicle_entry=ve, vehicle=self.vehicle,
            driver=self.driver, reason="DISPATCH", gate_in_date=timezone.localdate(),
            in_time=timezone.now().time(), created_by=self.user, updated_by=self.user,
        )

    def _plan(self, doc_entry, gate_in):
        plan = DispatchPlan.objects.create(
            company=self.company, sap_invoice_doc_entry=doc_entry, sap_invoice_doc_num=str(doc_entry),
            booking_status=DispatchPlanStatus.BOOKED, dispatch_date=timezone.localdate(),
            vehicle=self.vehicle, linked_vehicle_entry=gate_in.vehicle_entry,
            created_by=self.user, updated_by=self.user,
        )
        EmptyVehicleGateInCover.objects.create(
            empty_vehicle_gate_in=gate_in, dispatch_plan=plan, sap_doc_entry=doc_entry,
            sap_doc_num=str(doc_entry), created_by=self.user, updated_by=self.user,
        )
        return plan

    def _shared_docking(self, primary_plan, secondary_plan, status=SalesDispatchGateOutStatus.DOCKED):
        """A docking carrying two bills; header anchored to ``primary_plan``."""
        ve = VehicleEntry.objects.create(
            entry_no="DOCKV-1", company=self.company, vehicle=self.vehicle, driver=self.driver,
            entry_type="SALES_DISPATCH", status="IN_PROGRESS", created_by=self.user, updated_by=self.user,
        )
        docking = SalesDispatchGateOut.objects.create(
            company=self.company, entry_no="DOCK-1", vehicle_entry=ve, dispatch_plan=primary_plan,
            vehicle=self.vehicle, transporter=self.transporter, driver=self.driver,
            document_type=SalesDispatchDocumentType.INVOICE,
            sap_doc_entry=primary_plan.sap_invoice_doc_entry,
            sap_doc_num=f"{primary_plan.sap_invoice_doc_num}, {secondary_plan.sap_invoice_doc_num}",
            sap_branch_id=1, place_of_supply="PRIMARY-POS",
            total_quantity=Decimal("30.000"), total_boxes=Decimal("0.000"),
            status=status, created_by=self.user, updated_by=self.user,
        )
        docs = {}
        for idx, (plan, eway, pos, boxes, qty) in enumerate([
            (primary_plan, "EWAY-PRIMARY", "PRIMARY-POS", "20.000", "20.000"),
            (secondary_plan, "", "SECONDARY-POS", "10.000", "10.000"),
        ]):
            doc = SalesDispatchGateOutDocument.objects.create(
                sales_dispatch=docking, company=self.company, dispatch_plan=plan,
                document_type=SalesDispatchDocumentType.INVOICE,
                sap_doc_entry=plan.sap_invoice_doc_entry, sap_doc_num=plan.sap_invoice_doc_num,
                sap_doc_total=Decimal("60000.00") if eway else Decimal("100.00"),
                eway_bill=eway, place_of_supply=pos, sap_branch_id=1,
                total_boxes=Decimal(boxes), total_quantity=Decimal(qty),
                created_by=self.user, updated_by=self.user,
            )
            SalesDispatchGateOutItem.objects.create(
                sales_dispatch=docking, document=doc, line_num=idx,
                item_code=f"ITEM-{idx}", item_name=f"Item {idx}", quantity=Decimal(qty),
                total_boxes=Decimal(boxes), uom="BOX", created_by=self.user, updated_by=self.user,
            )
            docs[plan.sap_invoice_doc_entry] = doc
        return docking, docs

    def _scan(self, docking, document, barcode):
        return SalesDispatchBoxScan.objects.create(
            company=self.company, sales_dispatch=docking, document=document,
            box_barcode=barcode, item_code=document.items.first().item_code,
            quantity=Decimal("1.00"), created_by=self.user, updated_by=self.user,
        )

    # ---- helper ----------------------------------------------------------
    def test_remove_secondary_unwinds_scans_items_and_header(self):
        gi = self._gate_in()
        p1 = self._plan(70001, gi)
        p2 = self._plan(70002, gi)
        docking, docs = self._shared_docking(p1, p2)
        self._scan(docking, docs[70002], "BOX-SEC-1")
        self._scan(docking, docs[70002], "BOX-SEC-2")

        remove_document_from_docking(docking, docs[70002], self.user)

        docking.refresh_from_db()
        # Document + its items + its scans are all deactivated.
        self.assertFalse(SalesDispatchGateOutDocument.objects.get(id=docs[70002].id).is_active)
        self.assertEqual(docking.documents.filter(is_active=True).count(), 1)
        self.assertEqual(
            SalesDispatchBoxScan.objects.filter(document=docs[70002], is_active=True).count(), 0
        )
        self.assertEqual(
            SalesDispatchGateOutItem.objects.filter(document=docs[70002], is_active=True).count(), 0
        )
        # Header aggregates now reflect the surviving primary only.
        self.assertEqual(docking.sap_doc_num, "70001")
        self.assertEqual(docking.total_quantity, Decimal("20.000"))
        # Primary was NOT removed -> primary-anchored fields untouched.
        self.assertEqual(docking.sap_doc_entry, 70001)
        self.assertEqual(docking.dispatch_plan_id, p1.id)

    def test_remove_primary_repoints_header_and_fk(self):
        gi = self._gate_in()
        p1 = self._plan(70001, gi)
        p2 = self._plan(70002, gi)
        docking, docs = self._shared_docking(p1, p2)

        remove_document_from_docking(docking, docs[70001], self.user)

        docking.refresh_from_db()
        # Primary re-pointed to the surviving secondary bill.
        self.assertEqual(docking.sap_doc_entry, 70002)
        self.assertEqual(docking.dispatch_plan_id, p2.id)
        self.assertEqual(docking.place_of_supply, "SECONDARY-POS")
        self.assertEqual(docking.sap_doc_num, "70002")
        self.assertEqual(docking.total_quantity, Decimal("10.000"))

    def test_readers_exclude_removed_document(self):
        gi = self._gate_in()
        p1 = self._plan(70001, gi)
        p2 = self._plan(70002, gi)
        docking, docs = self._shared_docking(p1, p2)

        # Before: both bills counted; primary carries the e-way > threshold.
        self.assertEqual(resolved_expected_box_count(docking), 30)
        self.assertTrue(requires_eway_bill(docking))

        remove_document_from_docking(docking, docs[70001], self.user)
        docking = SalesDispatchGateOut.objects.get(id=docking.id)

        # After: only the surviving bill's 10 boxes; the e-way bill left with the primary.
        self.assertEqual(resolved_expected_box_count(docking), 10)
        self.assertFalse(requires_eway_bill(docking))

    # ---- cancel releases bills ------------------------------------------
    def test_cancel_releases_bills_so_they_dont_show_cancelled(self):
        """Cancelling a docking must free its bills (sever the FK + deactivate the
        document links) so a re-booked bill doesn't keep resolving to the dead
        docking and mis-show "rejected / cancelled" on the pipeline board."""
        from dispatch_plans.services import compute_pipeline_status

        gi = self._gate_in()
        p1 = self._plan(70001, gi)
        p2 = self._plan(70002, gi)
        docking, _docs = self._shared_docking(p1, p2)

        resp = self.client.post(
            f"/api/v1/gate-core/sales-dispatch/{docking.id}/cancel/",
            {"reason": "Wrong truck"}, format="json",
            HTTP_COMPANY_CODE=self.company.code,
        )
        self.assertEqual(resp.status_code, 200, resp.content)

        docking.refresh_from_db()
        self.assertEqual(docking.status, SalesDispatchGateOutStatus.CANCELLED)
        # Bills released: primary FK severed + every document link deactivated.
        self.assertIsNone(docking.dispatch_plan_id)
        self.assertEqual(docking.documents.filter(is_active=True).count(), 0)
        # Neither bill mis-shows "rejected / cancelled"; both fall back to the
        # gate-in (COMPLETED) stage, free to be re-docked.
        for plan in (p1, p2):
            plan.refresh_from_db()
            self.assertEqual(
                compute_pipeline_status(plan)["module_label"], "pending at dock"
            )

    def test_cancelled_docking_is_off_the_dispatch_out_board(self):
        """A cancelled docking keeps is_active=True and — once its last bill is
        pulled — a stale header pointing at that removed bill. It must not appear
        on the operational dispatch-out board (else the removed invoice number
        resurfaces there)."""
        from gate_core.views_sales_dispatch import sales_dispatch_list_queryset

        gi = self._gate_in()
        p1 = self._plan(70001, gi)
        p2 = self._plan(70002, gi)
        docking, _docs = self._shared_docking(p1, p2)

        # Control: a live docking is on the board.
        board_ids = set(sales_dispatch_list_queryset(self.company).values_list("id", flat=True))
        self.assertIn(docking.id, board_ids)

        resp = self.client.post(
            f"/api/v1/gate-core/sales-dispatch/{docking.id}/cancel/",
            {"reason": "Wrong truck"}, format="json",
            HTTP_COMPANY_CODE=self.company.code,
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        docking.refresh_from_db()
        self.assertEqual(docking.status, SalesDispatchGateOutStatus.CANCELLED)
        self.assertTrue(docking.is_active)  # kept for audit, not deleted

        board_ids = set(sales_dispatch_list_queryset(self.company).values_list("id", flat=True))
        self.assertNotIn(docking.id, board_ids)

    # ---- C1 endpoint -----------------------------------------------------
    def test_c1_remove_endpoint_unwinds_and_releases_plan(self):
        gi = self._gate_in()
        p1 = self._plan(70001, gi)
        p2 = self._plan(70002, gi)
        docking, docs = self._shared_docking(p1, p2)
        self._scan(docking, docs[70002], "BOX-SEC-1")

        resp = self.client.post(
            f"/api/v1/gate-core/sales-dispatch/{docking.id}/documents/{docs[70002].id}/remove/",
            HTTP_COMPANY_CODE=self.company.code,
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        docking.refresh_from_db()
        self.assertEqual(docking.documents.filter(is_active=True).count(), 1)
        self.assertEqual(docking.sap_doc_num, "70001")
        # Scans of the removed bill are cleared (never settle at dispatch).
        self.assertEqual(SalesDispatchBoxScan.objects.filter(document=docs[70002], is_active=True).count(), 0)
        # Bill returned to Expected Dispatch; its cover voided.
        p2.refresh_from_db()
        self.assertEqual(p2.booking_status, DispatchPlanStatus.BOOKED)
        self.assertIsNone(p2.linked_vehicle_entry_id)
        self.assertEqual(EmptyVehicleGateInCover.objects.filter(dispatch_plan=p2, is_active=True).count(), 0)

    def test_c1_refuses_removing_last_bill(self):
        gi = self._gate_in()
        p1 = self._plan(70001, gi)
        p2 = self._plan(70002, gi)
        docking, docs = self._shared_docking(p1, p2)
        remove_document_from_docking(docking, docs[70002], self.user)  # now single-bill
        resp = self.client.post(
            f"/api/v1/gate-core/sales-dispatch/{docking.id}/documents/{docs[70001].id}/remove/",
            HTTP_COMPANY_CODE=self.company.code,
        )
        self.assertEqual(resp.status_code, 400)

    # ---- inside-vehicle detach of a SECONDARY bill (the smoking gun) -----
    def test_detach_secondary_bill_removes_only_its_document(self):
        gi = self._gate_in()
        p1 = self._plan(70001, gi)
        p2 = self._plan(70002, gi)
        docking, docs = self._shared_docking(p1, p2)

        ok, detail = detach_bill_from_gate_in(gi, 70002, self.user)

        self.assertTrue(ok, detail)
        docking.refresh_from_db()
        # Only the secondary bill's document is gone; the primary load survives intact.
        self.assertEqual(docking.documents.filter(is_active=True).count(), 1)
        self.assertTrue(docking.documents.filter(sap_doc_entry=70001, is_active=True).exists())
        self.assertEqual(docking.status, SalesDispatchGateOutStatus.DOCKED)  # not cancelled
        self.assertEqual(docking.sap_doc_num, "70001")
        # Secondary released; its cover gone.
        p2.refresh_from_db()
        self.assertEqual(p2.booking_status, DispatchPlanStatus.PENDING)
        self.assertEqual(EmptyVehicleGateInCover.objects.filter(dispatch_plan=p2, is_active=True).count(), 0)

    def test_commit_reason_catches_scanned_secondary_bill(self):
        gi = self._gate_in()
        p1 = self._plan(70001, gi)
        p2 = self._plan(70002, gi)
        docking, docs = self._shared_docking(p1, p2)
        self._scan(docking, docs[70002], "BOX-SEC-1")  # scan on the SECONDARY bill

        # The guard must see the secondary bill as loading-started even though the
        # docking's primary FK points at a different bill.
        self.assertIsNotNone(bill_commit_reason(p2))
        ok, detail = detach_bill_from_gate_in(gi, 70002, self.user)
        self.assertFalse(ok)
        self.assertIn("loading has started", detail)
