"""End-to-end integration test: one bill's journey from BOOKED to DISPATCHED.

This exercises the real cross-module wiring in one readable scenario --
`dispatch_plans` (booking + status derivation), `gate_core` (gate-in covers,
arrival, docking, dispatch settlement), and `driver_management` /
`vehicle_management` (the physical entities) -- against a real test database.

It complements the per-primitive tests in `test_docking_merge.py` /
`test_pipeline_status.py`: those check one transition each; this asserts the whole
chain stays consistent at every hop (cover created at gate-in gates docking; a
dispatch consumes the cover, retires the gate-in, and departs the arrival; the
derived "Vehicle Status" reflects each stage). SAP is not involved -- covers and
dispatch settlement are DB-only; the SAP-backed create/link endpoints are covered
separately with mocks.
"""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from company.models import Company, UserCompany, UserRole
from dispatch_plans.models import DispatchPlan, DispatchPlanStatus
from dispatch_plans.services import compute_pipeline_status, pipeline_gate_out_prefetch
from driver_management.models import Driver, VehicleEntry
from gate_core.models import (
    EmptyVehicleGateIn,
    EmptyVehicleGateInCover,
    SalesDispatchBoxScan,
    SalesDispatchDocumentType,
    SalesDispatchGateOut,
    SalesDispatchGateOutDocument,
    SalesDispatchGateOutStatus,
    VehicleArrival,
)
from gate_core.services.empty_vehicle_dispatch import (
    record_dispatch_covers,
    replicate_dispatch_gate_in_across_companies,
)
from gate_core.services.sales_dispatch_dispatch import mark_docking_dispatched
from vehicle_management.models import Transporter, Vehicle
from weighment.models import Weighment


class DispatchLifecycleTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        self.role = UserRole.objects.create(name="Gate")
        self.user = get_user_model().objects.create_user(
            email="life@example.com", password="x", full_name="Life", employee_code="LF1"
        )
        UserCompany.objects.create(
            user=self.user, company=self.company, role=self.role, is_default=True
        )
        self.user.user_permissions.add(
            *Permission.objects.filter(content_type__app_label="gate_core")
        )
        self.transporter = Transporter.objects.create(name="T")
        self.vehicle = Vehicle.objects.create(
            vehicle_number="DL01LIFE01", transporter=self.transporter
        )
        self.driver = Driver.objects.create(
            name="D", mobile_no="9000000001", license_no="L-LIFE"
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.company_header = {"HTTP_COMPANY_CODE": self.company.code}

    # ----- builders that mirror the real flow ------------------------------

    def _booked_plan(self, doc_entry):
        """The planner's output: a bill booked to a vehicle, not yet gated in."""
        return DispatchPlan.objects.create(
            company=self.company,
            sap_invoice_doc_entry=doc_entry,
            sap_invoice_doc_num=str(doc_entry),
            booking_status=DispatchPlanStatus.BOOKED,
            dispatch_date=timezone.localdate(),
            vehicle=self.vehicle,
            created_by=self.user,
            updated_by=self.user,
        )

    def _complete_gate_in(self):
        """Run the real gate-in completion services: snapshot covers + arrival."""
        vehicle_entry = VehicleEntry.objects.create(
            entry_no="EVGI-LIFE-1",
            company=self.company,
            vehicle=self.vehicle,
            driver=self.driver,
            entry_type="EMPTY_VEHICLE",
            status="COMPLETED",
            created_by=self.user,
            updated_by=self.user,
        )
        Weighment.objects.create(
            vehicle_entry=vehicle_entry,
            tare_weight=Decimal("5000.000"),
            created_by=self.user,
            updated_by=self.user,
        )
        gate_in = EmptyVehicleGateIn.objects.create(
            company=self.company,
            entry_no="EVGI-LIFE-1",
            vehicle_entry=vehicle_entry,
            vehicle=self.vehicle,
            driver=self.driver,
            reason="DISPATCH",
            gate_in_date=timezone.localdate(),
            in_time=timezone.now().time(),
            created_by=self.user,
            updated_by=self.user,
        )
        record_dispatch_covers(gate_in, self.user)
        replicate_dispatch_gate_in_across_companies(gate_in, self.user, [self.company.id])
        gate_in.refresh_from_db()
        return gate_in

    def _dock(self, plan, gate_in):
        """Dock the bill: its own gate-out vehicle entry (with gross+tare) + document."""
        vehicle_entry = VehicleEntry.objects.create(
            entry_no="DOCKV-LIFE-1",
            company=self.company,
            vehicle=self.vehicle,
            driver=self.driver,
            entry_type="SALES_DISPATCH",
            status="IN_PROGRESS",
            created_by=self.user,
            updated_by=self.user,
        )
        Weighment.objects.create(
            vehicle_entry=vehicle_entry,
            tare_weight=Decimal("5000.000"),
            gross_weight=Decimal("9000.000"),
            created_by=self.user,
            updated_by=self.user,
        )
        docking = SalesDispatchGateOut.objects.create(
            company=self.company,
            entry_no="DOCK-LIFE-1",
            vehicle_entry=vehicle_entry,
            dispatch_plan=plan,
            arrival=gate_in.arrival,
            vehicle=self.vehicle,
            transporter=self.transporter,
            driver=self.driver,
            document_type=SalesDispatchDocumentType.INVOICE,
            sap_doc_entry=plan.sap_invoice_doc_entry,
            status=SalesDispatchGateOutStatus.DOCKED,
            created_by=self.user,
            updated_by=self.user,
        )
        SalesDispatchGateOutDocument.objects.create(
            sales_dispatch=docking,
            company=self.company,
            dispatch_plan=plan,
            document_type=SalesDispatchDocumentType.INVOICE,
            sap_doc_entry=plan.sap_invoice_doc_entry,
            created_by=self.user,
            updated_by=self.user,
        )
        return docking

    def _label(self, plan):
        """The derived Vehicle Status, fetched via the real prefetch (so the
        box-scan 'scanning' refinement and the is_active filter both apply)."""
        prefetched = (
            DispatchPlan.objects.prefetch_related(*pipeline_gate_out_prefetch()).get(id=plan.id)
        )
        return compute_pipeline_status(prefetched)["module_label"]

    # ----- the happy path: BOOKED -> DISPATCHED ----------------------------

    def test_bill_walks_from_booked_to_dispatched(self):
        plan = self._booked_plan(70001)
        self.assertEqual(self._label(plan), "not entered")  # BOOKED

        gate_in = self._complete_gate_in()
        plan.refresh_from_db()
        self.assertIsNotNone(plan.linked_vehicle_entry_id)  # linked to the gate-in
        self.assertTrue(
            EmptyVehicleGateInCover.objects.filter(
                dispatch_plan=plan, consumed_at__isnull=True
            ).exists()
        )  # cover snapshotted, not yet consumed
        self.assertIsNotNone(gate_in.arrival_id)  # wrapped in an arrival
        self.assertEqual(self._label(plan), "pending at dock")  # READY_TO_DOCK

        docking = self._dock(plan, gate_in)
        self.assertEqual(self._label(plan), "docked at dock")  # DOCKED

        SalesDispatchBoxScan.objects.create(
            company=self.company,
            sales_dispatch=docking,
            document=docking.documents.first(),
            box_barcode="BC-LIFE-1",
            scanned_by=self.user,
            created_by=self.user,
            updated_by=self.user,
        )
        self.assertEqual(self._label(plan), "scanning at dock")  # DOCKED + scans

        # Reach the dispatch precondition (print committed + gatepass + gross weight).
        docking.status = SalesDispatchGateOutStatus.PRINT_COMMITTED
        docking.gatepass_no = "GP-LIFE-1"
        docking.print_committed_at = timezone.now()
        docking.save(update_fields=["status", "gatepass_no", "print_committed_at"])

        mark_docking_dispatched(docking, self.user)  # the real dispatch settlement

        docking.refresh_from_db()
        gate_in.refresh_from_db()
        plan.refresh_from_db()
        cover = EmptyVehicleGateInCover.objects.get(dispatch_plan=plan)
        self.assertEqual(docking.status, SalesDispatchGateOutStatus.DISPATCHED)
        self.assertIsNotNone(cover.consumed_at)  # cover consumed
        self.assertIsNotNone(gate_in.retired_at)  # gate-in retired (truck gone)
        self.assertEqual(
            VehicleArrival.objects.get(id=gate_in.arrival_id).status, "DEPARTED"
        )  # trip departed
        self.assertEqual(plan.booking_status, DispatchPlanStatus.DISPATCHED)
        self.assertEqual(self._label(plan), "dispatched at sales dispatch out")

    # ----- reversals -------------------------------------------------------

    def test_cancel_midflow_returns_bill_to_ready_to_dock(self):
        plan = self._booked_plan(70002)
        gate_in = self._complete_gate_in()
        docking = self._dock(plan, gate_in)
        self.assertEqual(self._label(plan), "docked at dock")

        response = self.client.post(
            f"/api/v1/gate-core/sales-dispatch/{docking.id}/cancel/",
            {"reason": "re-dock"},
            format="json",
            **self.company_header,
        )
        self.assertEqual(response.status_code, 200, response.data)

        gate_in.refresh_from_db()
        # Re-dockable on the SAME truck: the bill reverts to its live gate-in stage,
        # not stuck REJECTED, and the truck is still inside (gate-in not retired).
        self.assertEqual(self._label(plan), "pending at dock")
        self.assertIsNone(gate_in.retired_at)

    def test_empty_out_midflow_releases_bill_and_departs_the_trip(self):
        from gate_core.views import release_dispatch_plans_for_empty_out

        plan = self._booked_plan(70003)
        gate_in = self._complete_gate_in()
        self.assertEqual(self._label(plan), "pending at dock")

        release_dispatch_plans_for_empty_out(gate_in.vehicle_entry, self.user)

        gate_in.refresh_from_db()
        plan.refresh_from_db()
        self.assertIsNotNone(gate_in.retired_at)  # gate-in retired
        self.assertEqual(
            VehicleArrival.objects.get(id=gate_in.arrival_id).status, "DEPARTED"
        )  # single-company empty-out departs the trip
        self.assertIsNone(plan.linked_vehicle_entry_id)  # released for a fresh trip
        self.assertEqual(self._label(plan), "not entered")  # back to Expected Dispatch
