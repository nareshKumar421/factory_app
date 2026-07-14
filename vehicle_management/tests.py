from decimal import Decimal

from django.contrib.auth.models import Permission
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import User
from company.models import Company, UserCompany, UserRole
from driver_management.models import Driver, VehicleEntry
from gate_core.enums import GateEntryStatus
from quality_control.enums import ArrivalSlipStatus, InspectionStatus, InspectionWorkflowStatus
from quality_control.models import MaterialArrivalSlip, RawMaterialInspection
from raw_material_gatein.models import POItemReceipt, POReceipt
from vehicle_management.models import Transporter, Vehicle


class VehicleEntryDashboardQCTests(APITestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Raw Materials Co", code="RAW_CO")
        self.role = UserRole.objects.create(name="Gate User")
        self.user = User.objects.create_user(
            email="raw-dashboard@example.com",
            password="password",
            full_name="Raw Dashboard User",
            employee_code="RDU001",
        )
        UserCompany.objects.create(
            user=self.user,
            company=self.company,
            role=self.role,
            is_default=True,
            is_active=True,
        )
        permissions = Permission.objects.filter(
            content_type__app_label="raw_material_gatein",
            codename__in=["can_receive_po", "can_complete_raw_material_entry"],
        )
        self.user.user_permissions.add(*permissions)
        self.client.force_authenticate(self.user)
        self.client.credentials(HTTP_COMPANY_CODE=self.company.code)

        self.transporter = Transporter.objects.create(name="Raw Transporter")
        self.vehicle = Vehicle.objects.create(
            vehicle_number="HR55RM0002",
            transporter=self.transporter,
        )
        self.driver = Driver.objects.create(
            name="Raw Driver",
            mobile_no="9777777777",
            license_no="RAW-DL-002",
        )

    def create_item_with_inspection(self, po_receipt, suffix, final_status):
        item = POItemReceipt.objects.create(
            po_receipt=po_receipt,
            po_item_code=f"RM-DASH-{suffix}",
            item_name=f"Dashboard Raw Material {suffix}",
            sap_line_num=int(suffix),
            ordered_qty=Decimal("10.000"),
            received_qty=Decimal("10.000"),
            uom="KG",
            created_by=self.user,
        )
        arrival_slip = MaterialArrivalSlip.objects.create(
            po_item_receipt=item,
            particulars=item.item_name,
            arrival_datetime=timezone.now(),
            weighing_required=False,
            party_name=po_receipt.supplier_name,
            billing_qty=Decimal("10.000"),
            billing_uom=item.uom,
            truck_no_as_per_bill=self.vehicle.vehicle_number,
            status=ArrivalSlipStatus.SUBMITTED,
            is_submitted=True,
            submitted_at=timezone.now(),
            submitted_by=self.user,
            created_by=self.user,
        )
        RawMaterialInspection.objects.create(
            arrival_slip=arrival_slip,
            report_no=f"RPT-DASH-{suffix}",
            internal_lot_no=f"LOT-DASH-{suffix}",
            inspection_date=timezone.localdate(),
            description_of_material=item.item_name,
            sap_code=item.po_item_code,
            supplier_name=po_receipt.supplier_name,
            purchase_order_no=po_receipt.po_number,
            vehicle_no=self.vehicle.vehicle_number,
            workflow_status=InspectionWorkflowStatus.QAM_APPROVED,
            final_status=final_status,
            is_locked=True,
            created_by=self.user,
        )
        return item

    def test_raw_material_entry_list_shows_rejected_qc_final_status(self):
        entry = VehicleEntry.objects.create(
            entry_no="RM-DASH-001",
            company=self.company,
            vehicle=self.vehicle,
            driver=self.driver,
            entry_type="RAW_MATERIAL",
            status=GateEntryStatus.QC_COMPLETED,
            created_by=self.user,
            updated_by=self.user,
        )
        po_receipt = POReceipt.objects.create(
            vehicle_entry=entry,
            po_number="PO-DASH-001",
            supplier_code="SUP-DASH",
            supplier_name="Dashboard Supplier",
            created_by=self.user,
        )
        self.create_item_with_inspection(
            po_receipt,
            "1",
            InspectionStatus.ACCEPTED,
        )
        self.create_item_with_inspection(
            po_receipt,
            "2",
            InspectionStatus.REJECTED,
        )

        response = self.client.get(
            "/api/v1/vehicle-management/vehicle-entries/",
            {
                "entry_type": "RAW_MATERIAL",
                "from_date": timezone.localdate().isoformat(),
                "to_date": timezone.localdate().isoformat(),
            },
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        entry_data = response.data[0]
        self.assertEqual(entry_data["status"], GateEntryStatus.QC_COMPLETED)
        self.assertEqual(entry_data["qc_final_status"]["code"], InspectionStatus.REJECTED)
        self.assertEqual(entry_data["qc_final_status"]["display"], "QC Rejected")
        self.assertEqual(entry_data["qc_final_status"]["accepted_count"], 1)
        self.assertEqual(entry_data["qc_final_status"]["rejected_count"], 1)


class VehicleHistoryServiceTests(APITestCase):
    """The Previously-Registered-Vehicle aggregation."""

    def setUp(self):
        from vehicle_management.models.vehicle import VehicleType

        self.company = Company.objects.create(name="Hist Co", code="HIST_CO")
        self.transporter = Transporter.objects.create(
            name="ABC Transport", contact_person="Ram", mobile_no="9999", gstin="GST1",
        )
        self.vtype = VehicleType.objects.create(name="Truck 10T")
        self.vehicle = Vehicle.objects.create(
            vehicle_number="HR55AB1234", vehicle_type=self.vtype, transporter=self.transporter,
            capacity_ton=Decimal("10"), length_m=Decimal("6.5"),
            width_m=Decimal("2.4"), height_m=Decimal("3.0"),
        )
        self.driver = Driver.objects.create(name="Sohan", mobile_no="8888", license_no="DL-1")
        self.entry = VehicleEntry.objects.create(
            company=self.company, vehicle=self.vehicle, driver=self.driver,
            entry_type="SALES_DISPATCH", entry_no="GATE-1",
            status=GateEntryStatus.IN_PROGRESS,
        )

    def test_history_returns_vehicle_driver_and_visits(self):
        from vehicle_management.vehicle_history_service import build_vehicle_history

        data = build_vehicle_history("hr55ab1234")  # case-insensitive match
        self.assertTrue(data["found"])
        self.assertEqual(data["vehicle"]["length_m"], Decimal("6.5"))
        self.assertEqual(data["vehicle"]["transporter_name"], "ABC Transport")
        self.assertEqual(data["driver"]["name"], "Sohan")
        self.assertEqual(data["visit_count"], 1)
        self.assertEqual(len(data["visits"]), 1)
        self.assertEqual(data["visits"][0]["entry_no"], "GATE-1")
        self.assertIsNotNone(data["last_visit_date"])

    def test_history_not_found(self):
        from vehicle_management.vehicle_history_service import build_vehicle_history

        data = build_vehicle_history("NOPE999")
        self.assertFalse(data["found"])
        self.assertEqual(data["vehicle_number"], "NOPE999")
