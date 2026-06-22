from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from company.models import Company, UserCompany, UserRole
from driver_management.models import Driver, VehicleEntry
from gate_core.models import (
    SalesDispatchBoxScan,
    SalesDispatchDocumentType,
    SalesDispatchGateOut,
)
from vehicle_management.models import Vehicle, VehicleType


class ScanSkipCompanyResolutionTests(TestCase):
    """Scan-skip endpoints resolve the docking by the user's companies, not the
    active Company-Code header.

    Regression: ``get_sales_dispatch_or_404`` was switched to take the request, but
    docking_admin still passed the header ``Company`` object, so ``user_company_ids``
    did ``company.user`` -> AttributeError -> 500.
    """

    def setUp(self):
        self.beverages = Company.objects.create(name="Jivo Beverages", code="JIVO_BEVERAGES")
        self.oil = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        role = UserRole.objects.create(name="Gate")
        self.user = get_user_model().objects.create_user(
            email="scanskip@example.com",
            password="testpass123",
            full_name="Scan Skip",
            employee_code="SS001",
        )
        UserCompany.objects.create(
            user=self.user, company=self.beverages, role=role, is_active=True
        )
        UserCompany.objects.create(user=self.user, company=self.oil, role=role, is_active=True)
        self.user.user_permissions.add(
            *Permission.objects.filter(content_type__app_label="docking_admin")
        )
        vehicle_type = VehicleType.objects.create(name="TRUCK-SS")
        self.vehicle = Vehicle.objects.create(
            vehicle_number="DL01SS0001", vehicle_type=vehicle_type
        )
        self.driver = Driver.objects.create(
            name="Scan Driver", mobile_no="9000000001", license_no="DL-SS-0001"
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def _docking(self, company, suffix):
        entry = VehicleEntry.objects.create(
            entry_no=f"SSV-{suffix}", company=company, vehicle=self.vehicle,
            driver=self.driver, entry_type="SALES_DISPATCH", status="IN_PROGRESS",
            created_by=self.user, updated_by=self.user,
        )
        return SalesDispatchGateOut.objects.create(
            company=company, entry_no=f"SSDOCK-{suffix}", vehicle_entry=entry,
            vehicle=self.vehicle, driver=self.driver,
            document_type=SalesDispatchDocumentType.INVOICE, sap_doc_entry=int(suffix),
            status="DOCKED", created_by=self.user, updated_by=self.user,
        )

    def test_by_sales_dispatch_resolves_across_companies(self):
        # Docking belongs to Beverages; active header is Oil. The user is in both,
        # so the lookup resolves the docking by record -> 200 (previously 500).
        dock = self._docking(self.beverages, "501")
        response = self.client.get(
            f"/api/v1/docking-admin/scan-skip-requests/by-sales-dispatch/{dock.id}/",
            HTTP_COMPANY_CODE=self.oil.code,
        )
        self.assertEqual(response.status_code, 200)

    def test_by_sales_dispatch_out_of_scope_company_404(self):
        mart = Company.objects.create(name="Jivo Mart", code="JIVO_MART")  # user NOT a member
        dock = self._docking(mart, "502")
        response = self.client.get(
            f"/api/v1/docking-admin/scan-skip-requests/by-sales-dispatch/{dock.id}/",
            HTTP_COMPANY_CODE=self.oil.code,
        )
        self.assertEqual(response.status_code, 404)


@override_settings(DOCKING_BOX_SCAN_OPTIONAL_COMPANY_CODES=[])
class PartialScanApprovalTests(TestCase):
    """Partial-dispatch approval: dispatch a docking with some-but-not-all boxes
    scanned needs an admin approval, mirroring the zero-scan scan-skip flow."""

    def setUp(self):
        self.oil = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        role = UserRole.objects.create(name="Gate")
        self.user = get_user_model().objects.create_user(
            email="partial@example.com",
            password="testpass123",
            full_name="Partial User",
            employee_code="PS001",
        )
        UserCompany.objects.create(user=self.user, company=self.oil, role=role, is_active=True)
        self.user.user_permissions.add(
            *Permission.objects.filter(content_type__app_label="docking_admin")
        )
        vehicle_type = VehicleType.objects.create(name="TRUCK-PS")
        self.vehicle = Vehicle.objects.create(
            vehicle_number="DL01PS0001", vehicle_type=vehicle_type
        )
        self.driver = Driver.objects.create(
            name="Partial Driver", mobile_no="9000000002", license_no="DL-PS-0001"
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def _docking(self, suffix, total_boxes):
        entry = VehicleEntry.objects.create(
            entry_no=f"PSV-{suffix}", company=self.oil, vehicle=self.vehicle,
            driver=self.driver, entry_type="SALES_DISPATCH", status="IN_PROGRESS",
            created_by=self.user, updated_by=self.user,
        )
        return SalesDispatchGateOut.objects.create(
            company=self.oil, entry_no=f"PSDOCK-{suffix}", vehicle_entry=entry,
            vehicle=self.vehicle, driver=self.driver,
            document_type=SalesDispatchDocumentType.INVOICE, sap_doc_entry=int(suffix),
            status="DOCKED", total_boxes=Decimal(total_boxes),
            created_by=self.user, updated_by=self.user,
        )

    def _scan(self, dock, count):
        for index in range(count):
            SalesDispatchBoxScan.objects.create(
                company=self.oil, sales_dispatch=dock, box_barcode=f"BOX-{dock.id}-{index}",
                created_by=self.user, updated_by=self.user,
            )

    def _create_partial(self, dock, reason="Short load, rest to follow"):
        return self.client.post(
            "/api/v1/docking-admin/partial-scan-requests/",
            {"sales_dispatch": dock.id, "reason": reason},
            format="json",
            HTTP_COMPANY_CODE=self.oil.code,
        )

    def test_created_when_partially_scanned(self):
        dock = self._docking("601", total_boxes=10)
        self._scan(dock, 3)
        response = self._create_partial(dock)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["scanned_boxes"], 3)
        self.assertEqual(response.data["expected_boxes"], 10)
        self.assertEqual(response.data["status"], "PENDING")

    def test_rejected_when_nothing_scanned(self):
        dock = self._docking("602", total_boxes=10)
        self.assertEqual(self._create_partial(dock).status_code, 400)

    def test_rejected_when_fully_scanned(self):
        dock = self._docking("603", total_boxes=3)
        self._scan(dock, 3)
        self.assertEqual(self._create_partial(dock).status_code, 400)

    def test_approved_partial_satisfies_gatepass_box_scans(self):
        from gate_core.services.sales_dispatch_gatepass import get_gatepass_readiness

        dock = self._docking("604", total_boxes=10)
        self._scan(dock, 4)
        create = self._create_partial(dock)
        self.assertEqual(create.status_code, 201)
        # Pending partial scan -> the box-scan gate still blocks gatepass.
        self.assertIn("box_scans", get_gatepass_readiness(dock)["missing"])

        approve = self.client.post(
            f"/api/v1/docking-admin/partial-scan-requests/{create.data['id']}/approve/",
            {}, format="json", HTTP_COMPANY_CODE=self.oil.code,
        )
        self.assertEqual(approve.status_code, 200)
        # Approved -> box-scan requirement satisfied.
        self.assertNotIn("box_scans", get_gatepass_readiness(dock)["missing"])
