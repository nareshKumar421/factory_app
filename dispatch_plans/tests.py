from django.contrib.auth import get_user_model
from django.http import QueryDict
from django.test import SimpleTestCase, TestCase

from company.models import Company
from driver_management.models import Driver, VehicleEntry
from gate_core.enums import GateEntryStatus
from vehicle_management.models import Transporter, Vehicle, VehicleType

from .models import DispatchPlan
from .serializers import DispatchBillFilterSerializer, DispatchPlanUpdateSerializer
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
