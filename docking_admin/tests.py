from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase
from rest_framework.test import APIClient

from company.models import Company, UserCompany, UserRole
from driver_management.models import Driver, VehicleEntry
from gate_core.models import SalesDispatchDocumentType, SalesDispatchGateOut
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
