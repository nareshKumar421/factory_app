from datetime import date
from unittest.mock import MagicMock

from django.contrib.auth import get_user_model
from django.http import QueryDict
from django.utils import timezone
from django.test import SimpleTestCase, TestCase

from company.models import Company
from driver_management.models import Driver, VehicleEntry
from gate_core.enums import GateEntryStatus
from vehicle_management.models import Transporter, Vehicle, VehicleType

from .hana_reader import HanaDispatchBillReader
from .models import DispatchPlan, SelectedDispatchBill
from .serializers import (
    DispatchBillFilterSerializer,
    DispatchBillSelectionSerializer,
    DispatchPlanBulkDateSerializer,
    DispatchPlanSerializer,
    DispatchPlanUpdateSerializer,
)
from .services import DispatchPlansService

User = get_user_model()


class DispatchBillSelectionTests(TestCase):
    """Company-wide bill selection: reconcile only shown bills; Plan page filters."""

    def setUp(self):
        self.company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        self.user = User.objects.create(
            email="sel@example.com", employee_code="SEL1", full_name="Selector",
            is_active=True,
        )
        self.service = DispatchPlansService.__new__(DispatchPlansService)
        self.service.company = self.company

    def _active(self):
        return set(
            SelectedDispatchBill.objects.filter(company=self.company, is_active=True)
            .values_list("sap_invoice_doc_entry", flat=True)
        )

    def test_reconcile_only_touches_shown_bills(self):
        # A bill selected in another window must survive a submit that doesn't show it.
        SelectedDispatchBill.objects.create(
            company=self.company, sap_invoice_doc_entry=999, is_active=True,
        )
        res = self.service.reconcile_selection(
            shown_doc_entries=[1, 2, 3], selected_doc_entries=[1, 3], user=self.user,
        )
        self.assertEqual(res["selected"], 2)
        self.assertEqual(self._active(), {1, 3, 999})  # 2 unchecked, 999 untouched

    def _plan(self, doc_entry, booking_status):
        return DispatchPlan.objects.create(
            company=self.company,
            sap_invoice_doc_entry=doc_entry,
            sap_invoice_doc_num=str(doc_entry),
            booking_status=booking_status,
            created_by=self.user,
            updated_by=self.user,
        )

    def test_a_bill_selected_by_mistake_can_be_reversed_while_still_pending(self):
        """The whole point: nothing has been booked against it yet, so taking it
        back off planning costs nothing."""
        self.service.reconcile_selection(
            shown_doc_entries=[10], selected_doc_entries=[10], user=self.user,
        )
        self._plan(10, "PENDING")

        res = self.service.reconcile_selection(
            shown_doc_entries=[10], selected_doc_entries=[], user=self.user,
        )
        self.assertEqual(res["deselected"], 1)
        self.assertEqual(res["blocked"], [])
        self.assertEqual(self._active(), set())

    def test_a_booked_bill_cannot_be_reversed_here(self):
        """Deselecting would hide a booked vehicle from the Plan page while it
        stays live everywhere else."""
        self.service.reconcile_selection(
            shown_doc_entries=[11], selected_doc_entries=[11], user=self.user,
        )
        self._plan(11, "BOOKED")

        res = self.service.reconcile_selection(
            shown_doc_entries=[11], selected_doc_entries=[], user=self.user,
        )
        self.assertEqual(res["deselected"], 0)
        self.assertEqual(
            res["blocked"],
            [{"sap_invoice_doc_entry": 11, "booking_status": "BOOKED"}],
        )
        self.assertEqual(self._active(), {11})  # still selected

    def test_a_dispatched_bill_cannot_be_reversed_here(self):
        self.service.reconcile_selection(
            shown_doc_entries=[12], selected_doc_entries=[12], user=self.user,
        )
        self._plan(12, "DISPATCHED")

        res = self.service.reconcile_selection(
            shown_doc_entries=[12], selected_doc_entries=[], user=self.user,
        )
        self.assertEqual(res["deselected"], 0)
        self.assertEqual(self._active(), {12})

    def test_a_bill_with_no_plan_at_all_is_reversible(self):
        """Selected but never opened on the Plan page — there is no plan row yet."""
        self.service.reconcile_selection(
            shown_doc_entries=[13], selected_doc_entries=[13], user=self.user,
        )
        res = self.service.reconcile_selection(
            shown_doc_entries=[13], selected_doc_entries=[], user=self.user,
        )
        self.assertEqual(res["deselected"], 1)
        self.assertEqual(self._active(), set())

    def test_one_locked_bill_does_not_block_the_others_in_the_same_submit(self):
        self.service.reconcile_selection(
            shown_doc_entries=[14, 15], selected_doc_entries=[14, 15], user=self.user,
        )
        self._plan(14, "BOOKED")
        self._plan(15, "PENDING")

        res = self.service.reconcile_selection(
            shown_doc_entries=[14, 15], selected_doc_entries=[], user=self.user,
        )
        self.assertEqual(res["deselected"], 1)
        self.assertEqual([b["sap_invoice_doc_entry"] for b in res["blocked"]], [14])
        self.assertEqual(self._active(), {14})

    def test_reversing_keeps_the_row_for_audit_rather_than_deleting_it(self):
        self.service.reconcile_selection(
            shown_doc_entries=[16], selected_doc_entries=[16], user=self.user,
        )
        self.service.reconcile_selection(
            shown_doc_entries=[16], selected_doc_entries=[], user=self.user,
        )
        row = SelectedDispatchBill.objects.get(
            company=self.company, sap_invoice_doc_entry=16
        )
        self.assertFalse(row.is_active)
        self.assertEqual(row.created_by, self.user)

    # ─── removing an entry from the Plan page ─────────────────────────────

    def test_removing_a_pending_entry_takes_it_off_the_plan_page(self):
        """The whole point: nothing is booked against it, so removing costs nothing."""
        self.service.reconcile_selection(
            shown_doc_entries=[20], selected_doc_entries=[20], user=self.user,
        )
        self._plan(20, "PENDING")

        res = self.service.remove_from_plan(doc_entry=20, user=self.user)
        self.assertTrue(res["removed"])
        self.assertEqual(self._active(), set())

    def test_a_booked_entry_cannot_be_removed(self):
        self.service.reconcile_selection(
            shown_doc_entries=[21], selected_doc_entries=[21], user=self.user,
        )
        self._plan(21, "BOOKED")

        res = self.service.remove_from_plan(doc_entry=21, user=self.user)
        self.assertFalse(res["removed"])
        self.assertEqual(res["booking_status"], "BOOKED")
        self.assertIn("already booked", res["detail"])
        self.assertEqual(self._active(), {21})

    def test_a_dispatched_entry_cannot_be_removed(self):
        self.service.reconcile_selection(
            shown_doc_entries=[22], selected_doc_entries=[22], user=self.user,
        )
        self._plan(22, "DISPATCHED")

        res = self.service.remove_from_plan(doc_entry=22, user=self.user)
        self.assertFalse(res["removed"])
        self.assertEqual(self._active(), {22})

    def test_an_entry_never_opened_on_the_plan_page_can_still_be_removed(self):
        """Selected but never edited — there is no DispatchPlan row at all."""
        self.service.reconcile_selection(
            shown_doc_entries=[23], selected_doc_entries=[23], user=self.user,
        )
        res = self.service.remove_from_plan(doc_entry=23, user=self.user)
        self.assertTrue(res["removed"])
        self.assertEqual(self._active(), set())

    def test_removing_something_not_on_the_plan_page_is_not_an_error(self):
        res = self.service.remove_from_plan(doc_entry=24, user=self.user)
        self.assertFalse(res["removed"])
        self.assertEqual(res["booking_status"], "PENDING")
        self.assertIn("not on the Plan page", res["detail"])

    def test_removing_keeps_the_typed_planning_so_a_mistake_is_not_destructive(self):
        """Re-selecting brings the bill back exactly as it was."""
        self.service.reconcile_selection(
            shown_doc_entries=[25], selected_doc_entries=[25], user=self.user,
        )
        plan = self._plan(25, "PENDING")
        plan.remarks = "half-typed plan"
        plan.save(update_fields=["remarks"])

        self.service.remove_from_plan(doc_entry=25, user=self.user)
        self.service.reconcile_selection(
            shown_doc_entries=[25], selected_doc_entries=[25], user=self.user,
        )

        plan.refresh_from_db()
        self.assertEqual(plan.remarks, "half-typed plan")
        self.assertEqual(self._active(), {25})

    def test_removal_keeps_the_selection_row_for_audit(self):
        self.service.reconcile_selection(
            shown_doc_entries=[26], selected_doc_entries=[26], user=self.user,
        )
        self.service.remove_from_plan(doc_entry=26, user=self.user)
        row = SelectedDispatchBill.objects.get(
            company=self.company, sap_invoice_doc_entry=26
        )
        self.assertFalse(row.is_active)
        self.assertEqual(row.created_by, self.user)

    def test_resubmit_deselects_and_reselect_reuses_row(self):
        self.service.reconcile_selection(
            shown_doc_entries=[1, 2], selected_doc_entries=[1, 2], user=self.user,
        )
        res = self.service.reconcile_selection(
            shown_doc_entries=[1, 2], selected_doc_entries=[1], user=self.user,
        )
        self.assertEqual(res["deselected"], 1)
        self.assertEqual(self._active(), {1})
        # Re-selecting 2 flips the same row back on — no duplicate.
        self.service.reconcile_selection(
            shown_doc_entries=[1, 2], selected_doc_entries=[1, 2], user=self.user,
        )
        self.assertEqual(
            SelectedDispatchBill.objects.filter(
                company=self.company, sap_invoice_doc_entry=2
            ).count(),
            1,
        )
        self.assertEqual(self._active(), {1, 2})

    def test_get_bills_marks_is_selected_and_selected_only_filters(self):
        self.service.reader = MagicMock()
        self.service.reader.list_bills.return_value = [
            {"doc_entry": 1, "doc_num": "N1", "doc_total": 100, "total_litres": 5, "total_boxes": 2},
            {"doc_entry": 2, "doc_num": "N2", "doc_total": 200, "total_litres": 6, "total_boxes": 3},
        ]
        SelectedDispatchBill.objects.create(
            company=self.company, sap_invoice_doc_entry=1, is_active=True,
        )
        base = {"date_from": date(2026, 1, 1), "date_to": date(2026, 1, 31)}

        out = self.service.get_bills(base)
        flags = {r["doc_entry"]: r["is_selected"] for r in out["data"]}
        self.assertEqual(flags, {1: True, 2: False})

        out2 = self.service.get_bills({**base, "selected_only": True})
        self.assertEqual([r["doc_entry"] for r in out2["data"]], [1])

    def test_bill_serializer_exposes_is_selected(self):
        """Regression: is_selected must reach the API (it was being stripped)."""
        from .serializers import DispatchBillSerializer
        self.assertIn("is_selected", DispatchBillSerializer().fields)

    def test_selection_serializer_rejects_non_subset(self):
        ser = DispatchBillSelectionSerializer(
            data={"shown_doc_entries": [1, 2], "selected_doc_entries": [1, 3]}
        )
        self.assertFalse(ser.is_valid())


class DispatchPlanBulkDateTests(TestCase):
    """Bulk 'apply dispatch date' — set one date across many bills at once."""

    def setUp(self):
        self.company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        self.user = User.objects.create_user(
            email="bulk@example.com",
            password="testpass123",
            full_name="Bulk User",
            employee_code="BULK001",
        )

    def test_updates_existing_and_creates_missing_plan_rows(self):
        # 5001 already has a plan with an old date; 5002 has no plan row yet.
        DispatchPlan.objects.create(
            company=self.company,
            sap_invoice_doc_entry=5001,
            sap_invoice_doc_num="5001",
            booking_status="BOOKED",
            dispatch_date=date(2026, 1, 1),
            created_by=self.user,
            updated_by=self.user,
        )
        service = DispatchPlansService(company_code=self.company.code)
        # A newly-created plan must snapshot its DocNum from SAP, else the bill
        # later displays as its raw DocEntry (the "78009" inside-vehicle bug).
        service.reader = MagicMock()
        service.reader.list_bills_by_doc_entries.return_value = [
            {"doc_entry": 5002, "doc_num": "626005002"},
        ]

        result = service.bulk_set_dispatch_date(
            doc_entries=[5001, 5002, 5002],  # duplicate collapses to one
            dispatch_date=date(2026, 8, 1),
            user=self.user,
        )

        self.assertEqual(result["updated"], 2)
        p1 = DispatchPlan.objects.get(company=self.company, sap_invoice_doc_entry=5001)
        p2 = DispatchPlan.objects.get(company=self.company, sap_invoice_doc_entry=5002)
        self.assertEqual(p1.dispatch_date, date(2026, 8, 1))  # existing date overwritten
        self.assertEqual(p2.dispatch_date, date(2026, 8, 1))  # plan row created + dated
        self.assertEqual(p2.booking_status, "PENDING")  # new row defaults to PENDING
        # New row backfilled its DocNum from SAP; existing row's DocNum untouched
        # (its non-blank value means SAP is never consulted).
        self.assertEqual(p2.sap_invoice_doc_num, "626005002")
        self.assertEqual(p1.sap_invoice_doc_num, "5001")

    def test_serializer_rejects_empty_doc_entries(self):
        serializer = DispatchPlanBulkDateSerializer(
            data={"doc_entries": [], "dispatch_date": "2026-08-01"}
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("doc_entries", serializer.errors)

    def test_serializer_accepts_null_date(self):
        serializer = DispatchPlanBulkDateSerializer(
            data={"doc_entries": [1], "dispatch_date": None}
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertIsNone(serializer.validated_data["dispatch_date"])


class DispatchPlanUpdateSerializerTests(SimpleTestCase):
    def test_bill_filter_accepts_jivo_mart_transfer_exclusion_flag(self):
        serializer = DispatchBillFilterSerializer(
            data={
                "date_from": "2026-06-01",
                "date_to": "2026-06-13",
                "exclude_jivo_mart_transfer": "true",
            },
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertTrue(serializer.validated_data["exclude_jivo_mart_transfer"])

    def test_linked_invoice_doc_entries_accepts_json_integer_list(self):
        serializer = DispatchPlanUpdateSerializer(
            data={"linked_invoice_doc_entries": [72826, 72815]},
            partial=True,
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(
            serializer.validated_data["linked_invoice_doc_entries"],
            [72826, 72815],
        )

    def test_linked_invoice_doc_entries_accepts_repeated_multipart_values(self):
        serializer = DispatchPlanUpdateSerializer(
            data={"linked_invoice_doc_entries": ["72826", "72815"]},
            partial=True,
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(
            serializer.validated_data["linked_invoice_doc_entries"],
            [72826, 72815],
        )

    def test_linked_invoice_doc_entries_accepts_comma_separated_multipart_value(self):
        serializer = DispatchPlanUpdateSerializer(
            data={"linked_invoice_doc_entries": "72826,72815"},
            partial=True,
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(
            serializer.validated_data["linked_invoice_doc_entries"],
            [72826, 72815],
        )

    def test_linked_invoice_doc_entries_accepts_querydict_comma_value(self):
        data = QueryDict("", mutable=True)
        data.update({"linked_invoice_doc_entries": "72826,72815"})

        serializer = DispatchPlanUpdateSerializer(data=data, partial=True)

        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(
            serializer.validated_data["linked_invoice_doc_entries"],
            [72826, 72815],
        )

    def test_linked_invoice_doc_entries_rejects_non_integer_values(self):
        serializer = DispatchPlanUpdateSerializer(
            data={"linked_invoice_doc_entries": "72826,nope"},
            partial=True,
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("linked_invoice_doc_entries", serializer.errors)


class DispatchPlanInvoiceDefaultsTests(SimpleTestCase):
    def test_mineral_water_invoice_defaults_to_beverage_variety(self):
        self.assertEqual(
            DispatchPlansService._infer_product_variety(
                "FG0000324 - PET BOTTLE 500 ML JIVO NATURAL MINERAL SPECIAL EDITION"
            ),
            "Beverage",
        )

    def test_invoice_defaults_include_customer_from_bill(self):
        defaults = DispatchPlansService._invoice_defaults_from_bill(
            {
                "doc_num": "726065003",
                "card_code": "C0001",
                "card_name": "GOYAL KIRYANA STORE",
                "state": "DL",
            }
        )
        self.assertEqual(defaults["customer_name"], "GOYAL KIRYANA STORE")
        self.assertEqual(defaults["customer_code"], "C0001")

    def test_identifies_jivo_oil_to_jivo_mart_transfer(self):
        service = DispatchPlansService.__new__(DispatchPlansService)
        service.company_code = "JIVO_OIL"

        self.assertTrue(
            service._is_jivo_oil_to_jivo_mart_transfer(
                {
                    "card_code": "",
                    "card_name": "JIVO MART PRIVATE LIMITED",
                    "ship_to_code": "",
                    "ship_to_address": "",
                    "bp_gstin": "",
                }
            )
        )

    def test_transfer_filter_only_applies_to_jivo_oil_company(self):
        service = DispatchPlansService.__new__(DispatchPlansService)
        service.company_code = "JIVO_MART"

        self.assertFalse(
            service._is_jivo_oil_to_jivo_mart_transfer(
                {
                    "card_code": "",
                    "card_name": "JIVO MART PRIVATE LIMITED",
                    "ship_to_code": "",
                    "ship_to_address": "",
                    "bp_gstin": "",
                }
            )
        )


class DispatchPlanLinkedVehicleEntryTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        self.user = get_user_model().objects.create_user(
            email="dispatch@example.com",
            password="testpass123",
            full_name="Dispatch User",
            employee_code="DISP001",
        )
        self.transporter = Transporter.objects.create(
            name="ARNAV TRANSPORT SERVICE",
            contact_person="Arnav Contact",
            mobile_no="9811111111",
            gstin="07ABCDE1234F1Z5",
        )
        vehicle_type = VehicleType.objects.create(name="TRUCK-DISPATCH-LINK")
        self.vehicle = Vehicle.objects.create(
            vehicle_number="HR55AA1234",
            vehicle_type=vehicle_type,
            transporter=self.transporter,
        )
        self.driver = Driver.objects.create(
            name="Ramesh Driver",
            mobile_no="9898989898",
            license_no="DL0420260001",
            id_proof_type="AADHAAR",
            id_proof_number="123412341234",
        )
        self.vehicle_entry = VehicleEntry.objects.create(
            entry_no="VE-DISP-001",
            company=self.company,
            vehicle=self.vehicle,
            driver=self.driver,
            entry_type="SALES_DISPATCH",
            status=GateEntryStatus.DRAFT,
        )

    def test_update_with_only_linked_vehicle_entry_hydrates_transport_details(self):
        service = DispatchPlansService(company_code=self.company.code)

        plan = service.update_plan(
            sap_invoice_doc_entry=626050517,
            data={
                "sap_invoice_doc_num": "626050517",
                "linked_vehicle_entry_id": self.vehicle_entry.id,
            },
            user=self.user,
        )

        plan.refresh_from_db()
        self.assertEqual(plan.linked_vehicle_entry_id, self.vehicle_entry.id)
        self.assertEqual(plan.vehicle_id, self.vehicle.id)
        self.assertEqual(plan.driver_id, self.driver.id)
        self.assertEqual(plan.transporter_id, self.transporter.id)
        self.assertEqual(plan.vehicle_no, "HR55AA1234")
        self.assertEqual(plan.driver_name, "Ramesh Driver")
        self.assertEqual(plan.driver_mobile_no, "9898989898")
        self.assertEqual(plan.driver_license_no, "DL0420260001")
        self.assertEqual(plan.driver_id_proof_type, "AADHAAR")
        self.assertEqual(plan.driver_id_proof_number, "123412341234")
        self.assertEqual(plan.transporter_name, "ARNAV TRANSPORT SERVICE")
        self.assertEqual(plan.transporter_gstin, "07ABCDE1234F1Z5")
        self.assertEqual(plan.contact_person, "Arnav Contact")
        self.assertEqual(plan.mobile_no, "9811111111")
        self.assertEqual(DispatchPlan.objects.count(), 1)

    def _booked_plan_with_completed_entry(self):
        self.vehicle_entry.status = GateEntryStatus.COMPLETED
        self.vehicle_entry.save(update_fields=["status"])
        return DispatchPlan.objects.create(
            company=self.company,
            sap_invoice_doc_entry=626050517,
            sap_invoice_doc_num="626050517",
            booking_status="BOOKED",
            vehicle=self.vehicle,
            transporter=self.transporter,
            driver=self.driver,
            linked_vehicle_entry=self.vehicle_entry,
        )

    def _make_dispatch_gate_in(self, status):
        from gate_core.models import EmptyVehicleGateIn
        self.vehicle_entry.status = status
        self.vehicle_entry.save(update_fields=["status"])
        return EmptyVehicleGateIn.objects.create(
            company=self.company,
            entry_no=self.vehicle_entry.entry_no,
            vehicle_entry=self.vehicle_entry,
            vehicle=self.vehicle,
            driver=self.driver,
            reason="DISPATCH",
            gate_in_date=timezone.localdate(),
            in_time=timezone.now().time(),
        )

    def _add_cover(self, gate_in, sap_doc_entry, dispatch_plan=None, consumed=False):
        from gate_core.models import EmptyVehicleGateInCover
        return EmptyVehicleGateInCover.objects.create(
            empty_vehicle_gate_in=gate_in,
            dispatch_plan=dispatch_plan,
            sap_doc_entry=sap_doc_entry,
            sap_doc_num=str(sap_doc_entry),
            consumed_at=timezone.now() if consumed else None,
        )

    def test_late_booking_links_to_gate_in_that_covers_the_bill(self):
        # A gate-in already gated in to carry this bill (its cover exists; the plan
        # row syncs later) links the freshly-booked plan and back-fills the cover.
        gate_in = self._make_dispatch_gate_in(GateEntryStatus.COMPLETED)
        cover = self._add_cover(gate_in, 700100)
        service = DispatchPlansService(company_code=self.company.code)

        plan = service.update_plan(
            sap_invoice_doc_entry=700100,
            data={"vehicle_id": self.vehicle.id, "booking_status": "BOOKED"},
            user=self.user,
        )

        plan.refresh_from_db()
        cover.refresh_from_db()
        self.assertEqual(plan.linked_vehicle_entry_id, self.vehicle_entry.id)
        self.assertEqual(cover.dispatch_plan_id, plan.id)

    def test_new_bill_to_inside_vehicle_refused_from_linking_board(self):
        # A vehicle that is already inside (live gate-in) is frozen on the linking
        # board: a newly-booked bill is refused with guidance to the dedicated
        # 'Add Bills to Inside Vehicle' flow, instead of silently joining the load.
        self._make_dispatch_gate_in(GateEntryStatus.COMPLETED)
        service = DispatchPlansService(company_code=self.company.code)

        with self.assertRaises(ValueError) as ctx:
            service.update_plan(
                sap_invoice_doc_entry=700199,
                data={"vehicle_id": self.vehicle.id, "booking_status": "BOOKED"},
                user=self.user,
            )

        self.assertIn("already inside", str(ctx.exception).lower())
        # The refused request persisted nothing (guard runs before any save).
        self.assertFalse(
            DispatchPlan.objects.filter(sap_invoice_doc_entry=700199).exists()
        )

    def _photo_locked_docking(self, entry_no, plan, linked_vehicle_entry):
        from gate_core.models import SalesDispatchGateOut

        plan.linked_vehicle_entry = linked_vehicle_entry
        plan.save(update_fields=["linked_vehicle_entry"])
        dock_ve = VehicleEntry.objects.create(
            entry_no=f"DOCKV-{entry_no}",
            company=self.company,
            vehicle=self.vehicle,
            driver=self.driver,
            entry_type="SALES_DISPATCH",
            status="IN_PROGRESS",
        )
        return SalesDispatchGateOut.objects.create(
            company=self.company,
            entry_no=entry_no,
            vehicle_entry=dock_ve,
            vehicle=self.vehicle,
            driver=self.driver,
            document_type="INVOICE",
            sap_doc_entry=plan.sap_invoice_doc_entry,
            dispatch_plan=plan,
            status="PHOTO_ATTACHED",
        )

    def test_new_bill_to_inside_vehicle_refused_regardless_of_docking_lock(self):
        # The vehicle is inside, so the linking board refuses the new bill up front
        # regardless of docking photo-lock state (adding is now an explicit action
        # on the dedicated 'Add Bills to Inside Vehicle' flow).
        self._make_dispatch_gate_in(GateEntryStatus.COMPLETED)
        docked = DispatchPlan.objects.create(
            company=self.company,
            sap_invoice_doc_entry=700298,
            booking_status="BOOKED",
            vehicle=self.vehicle,
        )
        self._photo_locked_docking("DOCK-LOCK-1", docked, self.vehicle_entry)
        service = DispatchPlansService(company_code=self.company.code)

        with self.assertRaises(ValueError):
            service.update_plan(
                sap_invoice_doc_entry=700299,
                data={"vehicle_id": self.vehicle.id, "booking_status": "BOOKED"},
                user=self.user,
            )

    def test_covered_bill_still_links_to_inside_vehicle_from_board(self):
        # A bill the inside gate-in already covers is exempt from the guard: it may
        # re-link from the board (idempotent), so an unrelated old locked docking on
        # the same vehicle does not interfere.
        gate_in = self._make_dispatch_gate_in(GateEntryStatus.COMPLETED)
        self._add_cover(gate_in, 700295)
        old_ve = VehicleEntry.objects.create(
            entry_no="OLD-VE-1",
            company=self.company,
            vehicle=self.vehicle,
            driver=self.driver,
            entry_type="EMPTY_VEHICLE",
            status="COMPLETED",
        )
        old_plan = DispatchPlan.objects.create(
            company=self.company,
            sap_invoice_doc_entry=700296,
            booking_status="DISPATCHED",
            vehicle=self.vehicle,
        )
        self._photo_locked_docking("OLD-DOCK-1", old_plan, old_ve)
        service = DispatchPlansService(company_code=self.company.code)

        plan = service.update_plan(
            sap_invoice_doc_entry=700295,
            data={"vehicle_id": self.vehicle.id, "booking_status": "BOOKED"},
            user=self.user,
        )

        plan.refresh_from_db()
        self.assertEqual(plan.linked_vehicle_entry_id, self.vehicle_entry.id)

    def test_late_booking_skips_retired_gate_in(self):
        # A retired gate-in (visit ended) must not relink even its own covered bill.
        gate_in = self._make_dispatch_gate_in(GateEntryStatus.COMPLETED)
        self._add_cover(gate_in, 700101)
        gate_in.retired_at = timezone.now()
        gate_in.retired_reason = "DISPATCHED"
        gate_in.save(update_fields=["retired_at", "retired_reason"])
        service = DispatchPlansService(company_code=self.company.code)

        plan = service.update_plan(
            sap_invoice_doc_entry=700101,
            data={"vehicle_id": self.vehicle.id, "booking_status": "BOOKED"},
            user=self.user,
        )

        plan.refresh_from_db()
        self.assertIsNone(plan.linked_vehicle_entry_id)

    def test_late_booking_skips_consumed_cover(self):
        # A re-booked bill whose gate-in already dispatched (cover consumed, gate-in
        # retired) does not re-attach — the truck has left.
        gate_in = self._make_dispatch_gate_in(GateEntryStatus.COMPLETED)
        self._add_cover(gate_in, 700102, consumed=True)
        gate_in.retired_at = timezone.now()
        gate_in.retired_reason = "DISPATCHED"
        gate_in.save(update_fields=["retired_at", "retired_reason"])
        service = DispatchPlansService(company_code=self.company.code)

        plan = service.update_plan(
            sap_invoice_doc_entry=700102,
            data={"vehicle_id": self.vehicle.id, "booking_status": "BOOKED"},
            user=self.user,
        )

        plan.refresh_from_db()
        self.assertIsNone(plan.linked_vehicle_entry_id)

    def test_docking_queryset_requires_unconsumed_cover(self):
        from gate_core.views_sales_dispatch import pending_dispatch_plan_queryset

        gate_in = self._make_dispatch_gate_in(GateEntryStatus.COMPLETED)
        booked = DispatchPlan.objects.create(
            company=self.company,
            sap_invoice_doc_entry=700200,
            booking_status="BOOKED",
            vehicle=self.vehicle,
            linked_vehicle_entry=self.vehicle_entry,
        )
        cover = self._add_cover(gate_in, 700200, dispatch_plan=booked)
        # Unconsumed cover on a live gate-in -> dockable.
        self.assertIn(
            booked.id,
            [p.id for p in pending_dispatch_plan_queryset(self.company)],
        )
        # The bill dispatches -> its cover is consumed -> it drops off the board.
        cover.consumed_at = timezone.now()
        cover.save(update_fields=["consumed_at"])
        self.assertNotIn(
            booked.id,
            [p.id for p in pending_dispatch_plan_queryset(self.company)],
        )

    def test_docking_queryset_excludes_plan_without_cover(self):
        # A plan linked to a gate-in but with no cover (e.g. an old vehicle-only
        # link) is not dockable — covers are the source of truth.
        from gate_core.views_sales_dispatch import pending_dispatch_plan_queryset

        self._make_dispatch_gate_in(GateEntryStatus.COMPLETED)
        uncovered = DispatchPlan.objects.create(
            company=self.company,
            sap_invoice_doc_entry=700250,
            booking_status="BOOKED",
            vehicle=self.vehicle,
            linked_vehicle_entry=self.vehicle_entry,
        )
        self.assertNotIn(
            uncovered.id,
            [p.id for p in pending_dispatch_plan_queryset(self.company)],
        )

    def test_clear_consumed_dispatch_links_command(self):
        from io import StringIO

        from django.core.management import call_command

        gate_in = self._make_dispatch_gate_in(GateEntryStatus.COMPLETED)
        # Retired gate-in (visit ended) with a returning booking wrongly still linked.
        gate_in.retired_at = timezone.now()
        gate_in.retired_reason = "DISPATCHED"
        gate_in.save(update_fields=["retired_at", "retired_reason"])
        stale = DispatchPlan.objects.create(
            company=self.company,
            sap_invoice_doc_entry=700301,
            booking_status="BOOKED",
            vehicle=self.vehicle,
            linked_vehicle_entry=self.vehicle_entry,
        )

        out = StringIO()
        call_command("clear_consumed_dispatch_links", stdout=out)
        stale.refresh_from_db()
        self.assertEqual(stale.linked_vehicle_entry_id, self.vehicle_entry.id)  # dry run

        call_command("clear_consumed_dispatch_links", "--apply", stdout=out)
        stale.refresh_from_db()
        self.assertIsNone(stale.linked_vehicle_entry_id)
        self.assertEqual(stale.booking_status, "BOOKED")

    def test_fix_orphaned_dispatch_links_command(self):
        from io import StringIO

        from django.core.management import call_command

        other_vehicle = Vehicle.objects.create(
            vehicle_number="HR99XX0001",
            vehicle_type=self.vehicle.vehicle_type,
            transporter=self.transporter,
        )
        other_entry = VehicleEntry.objects.create(
            entry_no="VE-ORPHAN-1",
            company=self.company,
            vehicle=other_vehicle,
            driver=self.driver,
            entry_type="EMPTY_VEHICLE",
            status=GateEntryStatus.COMPLETED,
        )
        # Orphan: plan's vehicle differs from the linked empty-in entry's vehicle.
        orphan = DispatchPlan.objects.create(
            company=self.company,
            sap_invoice_doc_entry=700001,
            booking_status="BOOKED",
            vehicle=self.vehicle,
            linked_vehicle_entry=other_entry,
        )
        # Healthy: plan's vehicle matches its linked entry's vehicle.
        self.vehicle_entry.status = GateEntryStatus.COMPLETED
        self.vehicle_entry.save(update_fields=["status"])
        healthy = DispatchPlan.objects.create(
            company=self.company,
            sap_invoice_doc_entry=700002,
            booking_status="BOOKED",
            vehicle=self.vehicle,
            linked_vehicle_entry=self.vehicle_entry,
        )

        out = StringIO()
        call_command("fix_orphaned_dispatch_links", stdout=out)
        orphan.refresh_from_db()
        self.assertEqual(orphan.linked_vehicle_entry_id, other_entry.id)  # dry run

        call_command("fix_orphaned_dispatch_links", "--apply", stdout=out)
        orphan.refresh_from_db()
        healthy.refresh_from_db()
        self.assertIsNone(orphan.linked_vehicle_entry_id)
        self.assertEqual(orphan.booking_status, "BOOKED")
        self.assertEqual(healthy.linked_vehicle_entry_id, self.vehicle_entry.id)

    def test_relink_blocked_once_empty_vehicle_in_completed(self):
        self._booked_plan_with_completed_entry()
        other_vehicle = Vehicle.objects.create(
            vehicle_number="HR55ZZ9999",
            vehicle_type=self.vehicle.vehicle_type,
            transporter=self.transporter,
        )
        service = DispatchPlansService(company_code=self.company.code)

        with self.assertRaises(ValueError):
            service.update_plan(
                sap_invoice_doc_entry=626050517,
                data={"vehicle_id": other_vehicle.id},
                user=self.user,
            )

        plan = DispatchPlan.objects.get(sap_invoice_doc_entry=626050517)
        self.assertEqual(plan.vehicle_id, self.vehicle.id)

    def test_unlink_blocked_once_empty_vehicle_in_completed(self):
        self._booked_plan_with_completed_entry()
        service = DispatchPlansService(company_code=self.company.code)

        with self.assertRaises(ValueError):
            service.update_plan(
                sap_invoice_doc_entry=626050517,
                data={
                    "vehicle_id": None,
                    "transporter_id": None,
                    "driver_id": None,
                    "linked_vehicle_entry_id": None,
                    "booking_status": "PENDING",
                },
                user=self.user,
            )

    def test_unrelated_edit_allowed_once_empty_vehicle_in_completed(self):
        self._booked_plan_with_completed_entry()
        service = DispatchPlansService(company_code=self.company.code)

        plan = service.update_plan(
            sap_invoice_doc_entry=626050517,
            data={"remarks": "Reached dock 2"},
            user=self.user,
        )

        self.assertEqual(plan.remarks, "Reached dock 2")
        self.assertEqual(plan.vehicle_id, self.vehicle.id)

    def test_unlink_allowed_before_empty_vehicle_in_completed(self):
        DispatchPlan.objects.create(
            company=self.company,
            sap_invoice_doc_entry=626050517,
            sap_invoice_doc_num="626050517",
            booking_status="BOOKED",
            vehicle=self.vehicle,
            transporter=self.transporter,
            driver=self.driver,
        )
        service = DispatchPlansService(company_code=self.company.code)

        plan = service.update_plan(
            sap_invoice_doc_entry=626050517,
            data={
                "vehicle_id": None,
                "transporter_id": None,
                "driver_id": None,
                "linked_vehicle_entry_id": None,
                "booking_status": "PENDING",
            },
            user=self.user,
        )

        self.assertIsNone(plan.vehicle_id)
        self.assertEqual(plan.booking_status, "PENDING")
        self.assertEqual(plan.vehicle_no, "")

    def test_serializer_flags_locked_link_when_entry_completed(self):
        plan = self._booked_plan_with_completed_entry()
        plan = (
            DispatchPlan.objects.select_related("linked_vehicle_entry")
            .get(pk=plan.pk)
        )

        self.assertTrue(DispatchPlanSerializer(plan).data["is_vehicle_link_locked"])

    def test_link_locked_method_handles_model_and_dict(self):
        # DispatchBillSerializer re-serializes an already-serialized plan dict,
        # so the method must accept both a model instance and a plain dict.
        serializer = DispatchPlanSerializer()
        plan = self._booked_plan_with_completed_entry()
        plan = (
            DispatchPlan.objects.select_related("linked_vehicle_entry")
            .get(pk=plan.pk)
        )

        self.assertTrue(serializer.get_is_vehicle_link_locked(plan))
        self.assertTrue(
            serializer.get_is_vehicle_link_locked({"is_vehicle_link_locked": True})
        )
        self.assertFalse(
            serializer.get_is_vehicle_link_locked({"is_vehicle_link_locked": False})
        )
        self.assertFalse(serializer.get_is_vehicle_link_locked({}))


class GetBillsByDispatchDateTests(TestCase):
    """`by_dispatch_date` keys the bill window on the plan's scheduled dispatch_date
    (the gate's "expected dispatch" view) instead of the SAP invoice creation date."""

    def setUp(self):
        self.company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        self.user = User.objects.create_user(
            email="bills@example.com",
            password="testpass123",
            full_name="Bills User",
            employee_code="BILL001",
        )

    def _plan(self, doc_entry, dispatch_date, status="BOOKED"):
        return DispatchPlan.objects.create(
            company=self.company,
            sap_invoice_doc_entry=doc_entry,
            sap_invoice_doc_num=str(doc_entry),
            booking_status=status,
            dispatch_date=dispatch_date,
            created_by=self.user,
            updated_by=self.user,
        )

    def test_by_dispatch_date_fetches_in_window_plans_by_doc_entry(self):
        self._plan(1001, date(2026, 6, 22))                     # in window, BOOKED
        self._plan(1002, date(2026, 5, 1))                      # out of window
        self._plan(1003, date(2026, 6, 21), status="PENDING")   # in window, not BOOKED

        service = DispatchPlansService(company_code=self.company.code)
        service.reader = MagicMock()
        service.reader.list_bills.return_value = []

        service.get_bills(
            {
                "by_dispatch_date": True,
                "date_from": date(2026, 6, 20),
                "date_to": date(2026, 6, 22),
                "booking_status": "BOOKED",
                "limit": 200,
            }
        )

        called = service.reader.list_bills.call_args[0][0]
        # Fetched exactly the in-window BOOKED plan, by doc-entry (no invoice-date window).
        self.assertEqual(set(called["doc_entries"]), {1001})
        self.assertNotIn("date_from", called)

    def test_without_flag_uses_invoice_date_window_unchanged(self):
        service = DispatchPlansService(company_code=self.company.code)
        service.reader = MagicMock()
        service.reader.list_bills.return_value = []

        filters = {
            "date_from": date(2026, 6, 20),
            "date_to": date(2026, 6, 22),
            "booking_status": "all",
        }
        service.get_bills(filters)

        # Unchanged path: the original filters (invoice CreateDate window) pass through.
        self.assertIs(service.reader.list_bills.call_args[0][0], filters)


class GetBillsUnscheduledAndPagingTests(TestCase):
    """The Dispatch Plans page: a dispatch-date window that still shows the bills
    waiting for a date, and one page of rows at a time with whole-set totals."""

    def setUp(self):
        self.company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        self.user = User.objects.create(
            email="paging@example.com", employee_code="PG1", full_name="Planner",
            is_active=True,
        )
        self.service = DispatchPlansService(company_code=self.company.code)
        self.service.reader = MagicMock()

    def _select(self, doc_entry):
        return SelectedDispatchBill.objects.create(
            company=self.company,
            sap_invoice_doc_entry=doc_entry,
            sap_invoice_doc_num=str(doc_entry),
            created_by=self.user,
            updated_by=self.user,
        )

    def _plan(self, doc_entry, dispatch_date, status="PENDING"):
        return DispatchPlan.objects.create(
            company=self.company,
            sap_invoice_doc_entry=doc_entry,
            sap_invoice_doc_num=str(doc_entry),
            booking_status=status,
            dispatch_date=dispatch_date,
            created_by=self.user,
            updated_by=self.user,
        )

    @staticmethod
    def _row(doc_entry, **extra):
        row = {
            "doc_entry": doc_entry,
            "doc_num": str(doc_entry),
            "doc_total": 100.0,
            "total_litres": 10.0,
            "total_boxes": 1.0,
            "card_name": f"Party {doc_entry}",
            "city": "Delhi",
            "doc_date": "2026-06-20",
            "create_date": "2026-06-20",
            "create_time": "10:00",
        }
        row.update(extra)
        return row

    def _window(self, **extra):
        filters = {
            "by_dispatch_date": True,
            "date_from": date(2026, 6, 22),
            "date_to": date(2026, 6, 22),
            "booking_status": "all",
            "selected_only": True,
        }
        filters.update(extra)
        return filters

    def test_bills_awaiting_a_dispatch_date_ride_along_with_the_window(self):
        # This is the page where dispatch dates get typed, so a strict window
        # would hide exactly the bills that still need one.
        self._select(1001)
        self._plan(1001, date(2026, 6, 22))          # dated inside the window
        self._select(1002)
        self._plan(1002, None)                        # planned, no date yet
        self._select(1003)                            # never opened: no plan row
        self._select(1004)
        self._plan(1004, date(2026, 5, 1))           # dated outside the window

        self.service.reader.list_bills.return_value = []
        self.service.get_bills(self._window(include_unscheduled=True))

        called = self.service.reader.list_bills.call_args[0][0]
        self.assertEqual(set(called["doc_entries"]), {1001, 1002, 1003})

    def test_without_the_flag_only_dated_bills_in_the_window_are_fetched(self):
        self._select(1001)
        self._plan(1001, date(2026, 6, 22))
        self._select(1002)
        self._plan(1002, None)

        self.service.reader.list_bills.return_value = []
        self.service.get_bills(self._window())

        called = self.service.reader.list_bills.call_args[0][0]
        self.assertEqual(set(called["doc_entries"]), {1001})

    def test_an_unselected_bill_never_rides_along_as_unscheduled(self):
        # "Has no dispatch date" would otherwise mean every invoice SAP holds.
        self._plan(2002, None)

        self.service.reader.list_bills.return_value = []
        self.service.get_bills(self._window(include_unscheduled=True))

        self.service.reader.list_bills.assert_not_called()

    def test_a_search_finds_a_bill_scheduled_outside_the_shown_window(self):
        # Typing a bill number is a hunt for that bill -- and the reason to hunt is
        # usually to change the very date that put it out of view.
        self._select(1001)
        self._plan(1001, date(2026, 6, 22))          # inside the window
        self._select(1002)
        self._plan(1002, date(2026, 9, 5))           # scheduled well past the window

        self.service.reader.list_bills.return_value = [self._row(1002)]
        result = self.service.get_bills(self._window(search="1002"))

        called = self.service.reader.list_bills.call_args[0][0]
        self.assertEqual(set(called["doc_entries"]), {1001, 1002})
        self.assertEqual([row["doc_entry"] for row in result["data"]], [1002])

    def test_a_search_still_only_reaches_bills_in_planning(self):
        self._select(1001)
        self._plan(1001, date(2026, 9, 5))
        self._plan(2002, date(2026, 9, 6))           # never selected for planning

        self.service.reader.list_bills.return_value = []
        self.service.get_bills(self._window(search="2002"))

        called = self.service.reader.list_bills.call_args[0][0]
        self.assertEqual(set(called["doc_entries"]), {1001})

    def test_a_search_outside_the_plan_page_keeps_the_date_window(self):
        # The gate's expected-dispatch feed windows on the dispatch date without
        # `selected_only`; a search there must not quietly widen to everything.
        self._select(1001)
        self._plan(1001, date(2026, 6, 22))
        self._select(1002)
        self._plan(1002, date(2026, 9, 5))

        self.service.reader.list_bills.return_value = []
        self.service.get_bills(
            self._window(search="1002", selected_only=False)
        )

        called = self.service.reader.list_bills.call_args[0][0]
        self.assertEqual(set(called["doc_entries"]), {1001})

    def _dated_and_selected(self, doc_entries, rows=None):
        """Bills selected, scheduled inside the window, and returned by the reader."""
        for doc_entry in doc_entries:
            self._select(doc_entry)
            self._plan(doc_entry, date(2026, 6, 22))
        self.service.reader.list_bills.return_value = rows or [
            self._row(doc_entry) for doc_entry in doc_entries
        ]

    def test_a_page_carries_its_slice_while_the_totals_cover_everything(self):
        self._dated_and_selected([1, 2, 3, 4, 5])

        result = self.service.get_bills(self._window(page=2, page_size=2))

        self.assertEqual([row["doc_entry"] for row in result["data"]], [3, 4])
        self.assertEqual(
            result["pagination"],
            {"page": 2, "page_size": 2, "total": 5, "total_pages": 3},
        )
        # Summary cards read meta, which must describe the whole filtered set.
        self.assertEqual(result["meta"]["total_bills"], 5)
        self.assertEqual(result["meta"]["total_doc_value"], 500.0)

    def test_asking_past_the_last_page_lands_on_the_last_page(self):
        # A window that shrank under the reader (or a stale page number) must not
        # answer with an empty table.
        self._dated_and_selected([1, 2, 3])

        result = self.service.get_bills(self._window(page=9, page_size=2))

        self.assertEqual([row["doc_entry"] for row in result["data"]], [3])
        self.assertEqual(result["pagination"]["page"], 2)

    def test_no_page_params_returns_the_whole_window(self):
        self._dated_and_selected([1, 2, 3])

        result = self.service.get_bills(self._window())

        self.assertEqual(len(result["data"]), 3)
        self.assertEqual(
            result["pagination"],
            {"page": 1, "page_size": 3, "total": 3, "total_pages": 1},
        )

    def test_ordering_spans_the_whole_set_not_just_the_returned_page(self):
        self._dated_and_selected(
            [1, 2, 3],
            rows=[
                self._row(1, total_litres=5.0),
                self._row(2, total_litres=500.0),
                self._row(3, total_litres=50.0),
            ],
        )

        result = self.service.get_bills(
            self._window(ordering="litres_desc", page=1, page_size=1)
        )

        # The biggest load in the window, not the biggest on page one.
        self.assertEqual([row["doc_entry"] for row in result["data"]], [2])

    def test_unscheduled_bills_sort_last_when_ordering_by_dispatch_date(self):
        self._select(1)
        self._plan(1, date(2026, 6, 22))
        self._select(2)
        self._plan(2, None)
        self.service.reader.list_bills.return_value = [self._row(2), self._row(1)]

        result = self.service.get_bills(
            self._window(include_unscheduled=True, ordering="dispatch_date_asc")
        )

        self.assertEqual([row["doc_entry"] for row in result["data"]], [1, 2])


class GetBillsAllCompaniesTests(TestCase):
    """`all_companies` fans the SAP bill read out over every company the user
    belongs to and merges, so the gate's expected-dispatch view is cross-company."""

    def setUp(self):
        from company.models import UserCompany, UserRole

        self.oil = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        self.beverages = Company.objects.create(
            name="Jivo Beverages", code="JIVO_BEVERAGES"
        )
        role = UserRole.objects.create(name="DispatchViewer")
        self.user = get_user_model().objects.create_user(
            email="bills-xco@example.com",
            password="testpass123",
            full_name="Bills XCo",
            employee_code="BILLX01",
        )
        UserCompany.objects.create(
            user=self.user, company=self.oil, role=role, is_active=True
        )
        UserCompany.objects.create(
            user=self.user, company=self.beverages, role=role, is_active=True
        )

    def test_get_bills_all_companies_merges_and_sorts_across_companies(self):
        from unittest.mock import patch

        from dispatch_plans.views import DispatchBillListAPI

        oil_inst = MagicMock()
        oil_inst.get_bills.return_value = {
            "data": [{"doc_entry": 1, "plan": {"dispatch_date": "2026-06-22"}}],
        }
        bev_inst = MagicMock()
        bev_inst.get_bills.return_value = {
            "data": [{"doc_entry": 2, "plan": {"dispatch_date": "2026-06-23"}}],
        }
        by_code = {"JIVO_OIL": oil_inst, "JIVO_BEVERAGES": bev_inst}

        request = MagicMock()
        request.user = self.user
        with patch("dispatch_plans.views.DispatchPlansService") as ServiceMock:
            ServiceMock.side_effect = lambda company_code: by_code[company_code]
            ServiceMock._build_meta.return_value = {"total_bills": 2}
            result = DispatchBillListAPI._get_bills_all_companies(
                request, {"date_from": date(2026, 6, 20), "date_to": date(2026, 6, 23)}
            )

        doc_entries = [row["doc_entry"] for row in result["data"]]
        self.assertEqual(set(doc_entries), {1, 2})
        # Merged + sorted by dispatch_date desc -> the 06-23 bill (Beverages) first.
        self.assertEqual(doc_entries[0], 2)
        self.assertEqual(result["meta"], {"total_bills": 2})
        oil_inst.get_bills.assert_called_once()
        bev_inst.get_bills.assert_called_once()


class DispatchPlanRemoveAPITests(TestCase):
    """The Remove action on the Plan page, over HTTP.

    The service rules are covered in DispatchBillSelectionTests; these cover
    what only the endpoint can get wrong — permission gating, the status code a
    refusal returns, and that the reason reaches the caller.
    """

    URL = "/api/v1/dispatch-plans/bills/{}/plan/remove/"

    def setUp(self):
        from django.contrib.auth.models import Permission
        from rest_framework.test import APIClient

        from company.models import UserCompany, UserRole

        self.company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        self.role = UserRole.objects.create(name="Planner")
        self.user = User.objects.create(
            email="rm@example.com", employee_code="RM1", full_name="Remover",
            is_active=True,
        )
        UserCompany.objects.create(
            user=self.user, company=self.company, role=self.role, is_default=True
        )
        self.perm = Permission.objects.get(
            content_type__app_label="dispatch_plans",
            codename="can_select_dispatch_bills",
        )
        self.user.user_permissions.add(self.perm)

        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.hdr = {"HTTP_COMPANY_CODE": self.company.code}

        self.service = DispatchPlansService.__new__(DispatchPlansService)
        self.service.company = self.company

    def _select(self, doc_entry):
        self.service.reconcile_selection(
            shown_doc_entries=[doc_entry],
            selected_doc_entries=[doc_entry],
            user=self.user,
        )

    def _plan(self, doc_entry, booking_status):
        return DispatchPlan.objects.create(
            company=self.company,
            sap_invoice_doc_entry=doc_entry,
            sap_invoice_doc_num=str(doc_entry),
            booking_status=booking_status,
            created_by=self.user,
            updated_by=self.user,
        )

    def _is_on_plan_page(self, doc_entry):
        return SelectedDispatchBill.objects.filter(
            company=self.company, sap_invoice_doc_entry=doc_entry, is_active=True
        ).exists()

    def test_removing_a_pending_entry_returns_200_and_takes_it_off(self):
        self._select(30)
        self._plan(30, "PENDING")

        resp = self.client.post(self.URL.format(30), **self.hdr)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertTrue(resp.data["removed"])
        self.assertFalse(self._is_on_plan_page(30))

    def test_removing_a_booked_entry_is_refused_with_409_and_a_reason(self):
        """409, not 400: the request is well-formed, the data forbids it."""
        self._select(31)
        self._plan(31, "BOOKED")

        resp = self.client.post(self.URL.format(31), **self.hdr)
        self.assertEqual(resp.status_code, 409, resp.content)
        self.assertFalse(resp.data["removed"])
        self.assertIn("already booked", resp.data["detail"])
        self.assertTrue(self._is_on_plan_page(31))

    def test_removing_a_dispatched_entry_is_refused(self):
        self._select(32)
        self._plan(32, "DISPATCHED")

        resp = self.client.post(self.URL.format(32), **self.hdr)
        self.assertEqual(resp.status_code, 409, resp.content)
        self.assertTrue(self._is_on_plan_page(32))

    def test_removing_something_not_on_the_plan_page_is_200_not_an_error(self):
        resp = self.client.post(self.URL.format(33), **self.hdr)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertFalse(resp.data["removed"])
        self.assertIn("not on the Plan page", resp.data["detail"])

    def test_the_action_requires_the_bill_selection_permission(self):
        self._select(34)
        self.user.user_permissions.remove(self.perm)

        resp = self.client.post(self.URL.format(34), **self.hdr)
        self.assertEqual(resp.status_code, 403, resp.content)
        # Refused means untouched.
        self.assertTrue(self._is_on_plan_page(34))

    def test_another_companys_selection_is_not_reachable(self):
        """Company scoping comes from the header, not the doc entry."""
        other = Company.objects.create(name="Jivo Mart", code="JIVO_MART")
        other_service = DispatchPlansService.__new__(DispatchPlansService)
        other_service.company = other
        other_service.reconcile_selection(
            shown_doc_entries=[35], selected_doc_entries=[35], user=self.user,
        )

        resp = self.client.post(self.URL.format(35), **self.hdr)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertFalse(resp.data["removed"])
        # The other company's row survives untouched.
        self.assertTrue(
            SelectedDispatchBill.objects.filter(
                company=other, sap_invoice_doc_entry=35, is_active=True
            ).exists()
        )


class TotalLitresExpressionTests(SimpleTestCase):
    """Litres come from ``OITM.SalPackUn`` x the billed quantity, nothing else.

    SalPackUn is the litres in one billed unit and SAP populates it for the whole
    item master, so the old cascade (line UDF -> item UDF -> item-name volume ->
    production BOM -> 910 g/L weight guess) is gone. The name parse was the part
    that lied: a "1 LTR + 1 LTR COMBO 10 SET" bills two litres a set and a CSD
    "1 LTR 16 PCS" carton sixteen, and neither reads that way off the name.
    """

    ITEM_COLUMNS = {"ItemName", "U_IsLitre", "SalPackUn"}

    def test_litres_are_quantity_times_salpackun(self):
        expression = HanaDispatchBillReader._line_total_litres_expr(self.ITEM_COLUMNS)
        self.assertIn('IFNULL(L."Quantity", 0) *', expression)
        self.assertIn('IFNULL(I."SalPackUn", 0)', expression)

    def test_the_item_name_is_never_parsed_for_volume(self):
        expression = HanaDispatchBillReader._line_total_litres_expr(self.ITEM_COLUMNS)
        for dead_source in (
            "LITRES|LITERS",   # pack volume parsed off the name
            "GMS|GM|GRAMS",    # the 910 g/L weight guess
            "/ 910",
            "BOM.litres_per_piece",
            'L."U_UNE_LTS"',
            'I."U_UNE_TOTL"',
        ):
            self.assertNotIn(dead_source, expression)

    def test_only_litre_flagged_items_count(self):
        """Cartons, preforms and labels all carry a SalPackUn too -- without the
        U_IsLitre gate a line of 100,000 preforms would report 100,000 litres."""
        expression = HanaDispatchBillReader._line_total_litres_expr(self.ITEM_COLUMNS)
        self.assertIn("""IFNULL(TO_NVARCHAR(I."U_IsLitre"), 'N')""", expression)

    def test_a_schema_without_salpackun_reports_no_litres(self):
        expression = HanaDispatchBillReader._line_total_litres_expr(
            {"ItemName", "U_IsLitre"}
        )
        self.assertNotIn("SalPackUn", expression)
        self.assertIn("0", expression)
