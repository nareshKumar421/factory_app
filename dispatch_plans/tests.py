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

from .models import DispatchPlan
from .serializers import (
    DispatchBillFilterSerializer,
    DispatchPlanSerializer,
    DispatchPlanUpdateSerializer,
)
from .services import DispatchPlansService

User = get_user_model()


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

    def test_new_bill_joins_inside_vehicle_load(self):
        # The truck is already inside (live gate-in) and not yet photo-locked at
        # docking, so a newly-booked bill joins the current load (cover added +
        # linked) instead of asking the gate to register the same truck again.
        from gate_core.models import EmptyVehicleGateInCover

        self._make_dispatch_gate_in(GateEntryStatus.COMPLETED)
        service = DispatchPlansService(company_code=self.company.code)

        plan = service.update_plan(
            sap_invoice_doc_entry=700199,
            data={"vehicle_id": self.vehicle.id, "booking_status": "BOOKED"},
            user=self.user,
        )

        plan.refresh_from_db()
        self.assertEqual(plan.linked_vehicle_entry_id, self.vehicle_entry.id)
        self.assertTrue(
            EmptyVehicleGateInCover.objects.filter(
                dispatch_plan=plan, sap_doc_entry=700199
            ).exists()
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

    def test_new_bill_blocked_once_load_photo_locked(self):
        # Once *this* gate-in's truck photo is attached the load is fixed, so a new
        # bill no longer joins it — it waits for a fresh gate-in.
        self._make_dispatch_gate_in(GateEntryStatus.COMPLETED)
        docked = DispatchPlan.objects.create(
            company=self.company,
            sap_invoice_doc_entry=700298,
            booking_status="BOOKED",
            vehicle=self.vehicle,
        )
        self._photo_locked_docking("DOCK-LOCK-1", docked, self.vehicle_entry)
        service = DispatchPlansService(company_code=self.company.code)

        plan = service.update_plan(
            sap_invoice_doc_entry=700299,
            data={"vehicle_id": self.vehicle.id, "booking_status": "BOOKED"},
            user=self.user,
        )

        plan.refresh_from_db()
        self.assertIsNone(plan.linked_vehicle_entry_id)

    def test_new_bill_not_blocked_by_unrelated_locked_docking(self):
        # A photo-locked docking from a DIFFERENT (earlier) trip of the same vehicle
        # must not block joining the current live gate-in's load.
        self._make_dispatch_gate_in(GateEntryStatus.COMPLETED)
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
