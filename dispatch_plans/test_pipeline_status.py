"""Unified pipeline status: stage -> "X at Y" mapping, per-vehicle aggregate,
serializer exposure, and the prefetch-backed O(1) guarantee."""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from company.models import Company, UserCompany, UserRole
from dispatch_plans.models import DispatchPlan, DispatchPlanStatus
from dispatch_plans.serializers import DispatchPlanSerializer
from dispatch_plans.services import (
    aggregate_pipeline_status,
    compute_pipeline_status,
    pipeline_gate_out_prefetch,
    pipeline_module_status,
)
from driver_management.models import Driver, VehicleEntry
from gate_core.models import (
    EmptyVehicleGateIn,
    EmptyVehicleGateInCover,
    SalesDispatchDocumentType,
    SalesDispatchGateOut,
    SalesDispatchGateOutDocument,
    SalesDispatchGateOutStatus,
)
from gate_core.serializers import EmptyVehicleGateInSerializer
from gate_core.views import empty_in_pipeline_prefetch
from vehicle_management.models import Transporter, Vehicle


class PipelineModuleStatusMappingTests(TestCase):
    def test_labels_match_the_x_at_y_convention(self):
        expected = {
            "BOOKED": "not entered",
            "EMPTY_IN": "pending at gate",
            "READY_TO_DOCK": "pending at dock",
            "DOCKED": "docked at dock",
            "PHOTO_ATTACHED": "photo attached at dock",
            "READY_FOR_GATEPASS": "ready for gatepass at dock",
            "GATEPASS_PRINTED": "gatepass printed at dock",
            "PRINT_COMMITTED": "pending at sales dispatch out",
            "DISPATCHED": "dispatched at sales dispatch out",
            "REJECTED": "rejected / cancelled",
        }
        for stage, label in expected.items():
            self.assertEqual(pipeline_module_status(stage)["module_label"], label)

    def test_docked_refines_to_scanning_with_box_scans(self):
        self.assertEqual(
            pipeline_module_status("DOCKED", box_scan_count=3)["module_label"], "scanning at dock"
        )
        self.assertEqual(
            pipeline_module_status("DOCKED", box_scan_count=0)["module_label"], "docked at dock"
        )

    def test_dict_guard_on_serializer(self):
        ser = DispatchPlanSerializer()
        self.assertEqual(
            ser.get_pipeline_status({"pipeline_status": {"module_label": "x"}}),
            {"module_label": "x"},
        )
        self.assertIsNone(ser.get_pipeline_status({}))


class PipelineStatusDataTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        self.role = UserRole.objects.create(name="Gate")
        self.user = get_user_model().objects.create_user(
            email="ps@example.com", password="x", full_name="PS", employee_code="PS1"
        )
        UserCompany.objects.create(user=self.user, company=self.company, role=self.role, is_default=True)
        self.transporter = Transporter.objects.create(name="T")
        self.vehicle = Vehicle.objects.create(vehicle_number="DL01PS0001", transporter=self.transporter)
        self.driver = Driver.objects.create(name="D", mobile_no="9000000000", license_no="L1")

    def _ve(self, entry_no, status, entry_type="EMPTY_VEHICLE"):
        return VehicleEntry.objects.create(
            entry_no=entry_no, company=self.company, vehicle=self.vehicle, driver=self.driver,
            entry_type=entry_type, status=status, created_by=self.user, updated_by=self.user,
        )

    def _plan(self, doc_entry, *, status=DispatchPlanStatus.BOOKED, linked=None):
        return DispatchPlan.objects.create(
            company=self.company, sap_invoice_doc_entry=doc_entry, sap_invoice_doc_num=str(doc_entry),
            booking_status=status, dispatch_date=timezone.localdate(), vehicle=self.vehicle,
            linked_vehicle_entry=linked, created_by=self.user, updated_by=self.user,
        )

    def _gate_in(self, ve, entry_no):
        return EmptyVehicleGateIn.objects.create(
            company=self.company, entry_no=entry_no, vehicle_entry=ve, vehicle=self.vehicle,
            driver=self.driver, reason="DISPATCH", gate_in_date=timezone.localdate(),
            in_time=timezone.now().time(), created_by=self.user, updated_by=self.user,
        )

    def _cover(self, gate_in, plan):
        return EmptyVehicleGateInCover.objects.create(
            empty_vehicle_gate_in=gate_in, dispatch_plan=plan, sap_doc_entry=plan.sap_invoice_doc_entry,
            sap_doc_num=plan.sap_invoice_doc_num, created_by=self.user, updated_by=self.user,
        )

    def _docking(self, plan, status):
        ve = self._ve(f"DV-{plan.sap_invoice_doc_entry}", "IN_PROGRESS", entry_type="SALES_DISPATCH")
        return SalesDispatchGateOut.objects.create(
            company=self.company, entry_no=f"DK-{plan.sap_invoice_doc_entry}", vehicle_entry=ve,
            dispatch_plan=plan, vehicle=self.vehicle, transporter=self.transporter, driver=self.driver,
            document_type=SalesDispatchDocumentType.INVOICE, sap_doc_entry=plan.sap_invoice_doc_entry,
            status=status, created_by=self.user, updated_by=self.user,
        )

    def _document(self, docking, plan):
        # Attach a secondary bill to a multi-bill docking via documents (not the
        # direct dispatch_plan FK).
        return SalesDispatchGateOutDocument.objects.create(
            sales_dispatch=docking, company=self.company, dispatch_plan=plan,
            document_type=SalesDispatchDocumentType.INVOICE,
            sap_doc_entry=plan.sap_invoice_doc_entry, created_by=self.user, updated_by=self.user,
        )

    # ----- single-plan stages --------------------------------------------

    def test_compute_status_across_stages(self):
        self.assertEqual(compute_pipeline_status(self._plan(1))["module_label"], "not entered")
        self.assertEqual(
            compute_pipeline_status(self._plan(2, linked=self._ve("VE-IP", "IN_PROGRESS")))["module_label"],
            "pending at gate",
        )
        self.assertEqual(
            compute_pipeline_status(self._plan(3, linked=self._ve("VE-C", "COMPLETED")))["module_label"],
            "pending at dock",
        )
        p4 = self._plan(4, linked=self._ve("VE-D", "COMPLETED"))
        self._docking(p4, SalesDispatchGateOutStatus.DOCKED)
        self.assertEqual(compute_pipeline_status(p4)["module_label"], "docked at dock")
        p5 = self._plan(5, status=DispatchPlanStatus.DISPATCHED, linked=self._ve("VE-X", "COMPLETED"))
        self._docking(p5, SalesDispatchGateOutStatus.DISPATCHED)
        self.assertEqual(
            compute_pipeline_status(p5)["module_label"], "dispatched at sales dispatch out"
        )

    def test_secondary_bill_of_multibill_docking_uses_docking_stage(self):
        # Multi-bill load: the primary bill drives the docking via the direct FK; a
        # secondary bill rides it via documents. Even with the secondary bill's
        # gate-in link cancelled, it must reflect the docking's stage (DISPATCHED),
        # not fall back to "not entered".
        primary = self._plan(90, status=DispatchPlanStatus.DISPATCHED, linked=self._ve("VE-MB", "COMPLETED"))
        docking = self._docking(primary, SalesDispatchGateOutStatus.DISPATCHED)
        secondary = self._plan(91, status=DispatchPlanStatus.DISPATCHED, linked=self._ve("VE-MB-C", "CANCELLED"))
        self._document(docking, secondary)
        self.assertEqual(
            compute_pipeline_status(secondary)["module_label"], "dispatched at sales dispatch out"
        )

    # ----- per-vehicle aggregate -----------------------------------------

    def test_aggregate_uniform_is_the_shared_stage(self):
        ve = self._ve("VE-U", "COMPLETED")
        p1, p2 = self._plan(10, linked=ve), self._plan(11, linked=ve)
        result = aggregate_pipeline_status([p1, p2])
        self.assertEqual(result["module_label"], "pending at dock")
        self.assertEqual(result["counts"], {"total": 2, "rejected": 0})

    def test_aggregate_falls_back_to_least_advanced(self):
        ve = self._ve("VE-M", "COMPLETED")
        docked = self._plan(20, linked=ve)
        self._docking(docked, SalesDispatchGateOutStatus.DOCKED)
        ready = self._plan(21, linked=ve)
        result = aggregate_pipeline_status([docked, ready])
        self.assertEqual(result["module_label"], "pending at dock")  # READY_TO_DOCK lags DOCKED

    def test_aggregate_all_rejected(self):
        ve = self._ve("VE-R", "COMPLETED")
        p = self._plan(30, linked=ve)
        self._docking(p, SalesDispatchGateOutStatus.REJECTED)
        result = aggregate_pipeline_status([p])
        self.assertEqual(result["stage"], "REJECTED")
        self.assertEqual(result["counts"], {"total": 1, "rejected": 1})

    def test_aggregate_empty_is_none(self):
        self.assertIsNone(aggregate_pipeline_status([]))

    # ----- serializer exposure -------------------------------------------

    def test_plan_serializer_exposes_status(self):
        p = self._plan(40, linked=self._ve("VE-S", "COMPLETED"))
        self.assertEqual(DispatchPlanSerializer(p).data["pipeline_status"]["module_label"], "pending at dock")

    def test_gate_in_serializer_aggregates_covers(self):
        ve = self._ve("VE-G", "COMPLETED")
        gi = self._gate_in(ve, "GI-G")
        self._cover(gi, self._plan(50, linked=ve))
        self.assertEqual(
            EmptyVehicleGateInSerializer(gi).data["pipeline_status"]["module_label"], "pending at dock"
        )

    def test_gate_in_serializer_null_without_covers(self):
        gi = self._gate_in(self._ve("VE-N", "COMPLETED"), "GI-N")
        self.assertIsNone(EmptyVehicleGateInSerializer(gi).data["pipeline_status"])

    # ----- N+1 guards (the core perf risk) -------------------------------

    def test_plan_status_no_per_row_queries(self):
        ve = self._ve("VE-Q", "COMPLETED")
        for de in range(60, 66):
            p = self._plan(de, linked=ve)
            if de % 2:
                self._docking(p, SalesDispatchGateOutStatus.DOCKED)
        # Secondary bill via documents must also be covered by the prefetch.
        primary = self._plan(66, status=DispatchPlanStatus.DISPATCHED, linked=ve)
        docking = self._docking(primary, SalesDispatchGateOutStatus.DISPATCHED)
        self._document(docking, self._plan(67, status=DispatchPlanStatus.DISPATCHED, linked=ve))
        plans = list(
            DispatchPlan.objects.filter(company=self.company, is_active=True)
            .select_related("linked_vehicle_entry")
            .prefetch_related(*pipeline_gate_out_prefetch())
        )
        with self.assertNumQueries(0):
            for p in plans:
                compute_pipeline_status(p)

    def test_gate_in_aggregate_no_per_row_queries(self):
        for n in range(3):
            ve = self._ve(f"VE-B{n}", "COMPLETED")
            gi = self._gate_in(ve, f"GI-B{n}")
            self._cover(gi, self._plan(70 + n, linked=ve))
        gate_ins = list(
            EmptyVehicleGateIn.objects.filter(company=self.company, is_active=True)
            .prefetch_related(
                "covers",
                "covers__dispatch_plan",
                "covers__dispatch_plan__linked_vehicle_entry",
                *empty_in_pipeline_prefetch(),
            )
        )
        with self.assertNumQueries(0):
            for gi in gate_ins:
                aggregate_pipeline_status(
                    [c.dispatch_plan for c in gi.covers.all() if c.is_active and c.dispatch_plan_id]
                )
