from django.contrib.auth import get_user_model
from django.http import QueryDict
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
