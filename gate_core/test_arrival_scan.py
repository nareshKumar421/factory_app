"""Arrival-level (whole-truck) scan + photo: one operator action fans across every
company's docking on the physical truck."""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from company.models import Company, UserCompany, UserRole
from driver_management.models import Driver, VehicleEntry
from gate_core.models import (
    SalesDispatchAttachment,
    SalesDispatchAttachmentType,
    SalesDispatchDocumentType,
    SalesDispatchGateOut,
    SalesDispatchGateOutStatus,
    VehicleArrival,
    VehicleArrivalStatus,
)
from vehicle_management.models import Vehicle, VehicleType


class ArrivalTruckPhotoTests(TestCase):
    def setUp(self):
        self.beverages = Company.objects.create(name="Jivo Beverages", code="JIVO_BEVERAGES")
        self.oil = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        role = UserRole.objects.create(name="Gate")
        self.user = get_user_model().objects.create_user(
            email="atp@example.com", password="p", full_name="ATP", employee_code="ATP1",
        )
        UserCompany.objects.create(user=self.user, company=self.beverages, role=role, is_active=True)
        UserCompany.objects.create(user=self.user, company=self.oil, role=role, is_active=True)
        vt = VehicleType.objects.create(name="TRUCK-ATP")
        self.vehicle = Vehicle.objects.create(vehicle_number="DL01ATP0001", vehicle_type=vt)
        self.driver = Driver.objects.create(name="D", mobile_no="9111111111", license_no="DL-ATP")
        self.arrival = VehicleArrival.objects.create(
            arrival_no="ARV-ATP-0001", vehicle=self.vehicle, driver=self.driver,
            gate_in_date=timezone.localdate(), in_time=timezone.now().time(),
            tare_weight=Decimal("250.000"), status=VehicleArrivalStatus.LOADING,
            created_by=self.user, updated_by=self.user,
        )

    def _docked(self, company, suffix):
        ve = VehicleEntry.objects.create(
            entry_no=f"DOCKV-{suffix}", company=company, vehicle=self.vehicle,
            driver=self.driver, entry_type="SALES_DISPATCH", status="IN_PROGRESS",
            created_by=self.user, updated_by=self.user,
        )
        return SalesDispatchGateOut.objects.create(
            company=company, entry_no=f"DOCK-{suffix}", arrival=self.arrival,
            vehicle_entry=ve, vehicle=self.vehicle, driver=self.driver,
            document_type=SalesDispatchDocumentType.INVOICE,
            sap_doc_entry=int(suffix), sap_doc_num=f"INV-{suffix}",
            status=SalesDispatchGateOutStatus.DOCKED,
            created_by=self.user, updated_by=self.user,
        )

    def _client(self):
        client = APIClient()
        client.force_authenticate(self.user)
        return client

    def _photo(self):
        return SimpleUploadedFile("truck.jpg", b"img-bytes", content_type="image/jpeg")

    def _url(self):
        return f"/api/v1/gate-core/arrivals/{self.arrival.id}/truck-photo/"

    def test_one_photo_attaches_to_every_company_docking(self):
        bev = self._docked(self.beverages, "111")
        oil = self._docked(self.oil, "222")

        resp = self._client().post(
            self._url(),
            {"file": self._photo(), "latitude": "28.6139", "longitude": "77.2090"},
            format="multipart",
        )

        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(resp.data["photographed_dockings"], 2)
        for dock in (bev, oil):
            dock.refresh_from_db()
            self.assertEqual(dock.status, SalesDispatchGateOutStatus.PHOTO_ATTACHED)
            self.assertTrue(dock.truck_photo)
            self.assertIsNotNone(dock.photo_latitude)
            self.assertIsNotNone(dock.photo_uploaded_at)
            self.assertTrue(
                SalesDispatchAttachment.objects.filter(
                    sales_dispatch=dock,
                    attachment_type=SalesDispatchAttachmentType.TRUCK_PHOTO,
                ).exists()
            )

    def test_geo_is_required_like_the_per_docking_upload(self):
        self._docked(self.beverages, "111")
        resp = self._client().post(self._url(), {"file": self._photo()}, format="multipart")
        self.assertEqual(resp.status_code, 400, resp.content)

    def test_scoped_out_company_is_rejected(self):
        # A third company the user does NOT belong to, docked on the same arrival.
        mart = Company.objects.create(name="Jivo Mart", code="JIVO_MART")
        self._docked(self.beverages, "111")
        self._docked(mart, "333")
        resp = self._client().post(
            self._url(),
            {"file": self._photo(), "latitude": "28.6", "longitude": "77.2"},
            format="multipart",
        )
        self.assertEqual(resp.status_code, 403, resp.content)

    def test_dispatch_gross_reads_from_sibling_docking(self):
        # One weighbridge gross for the truck: a docking with no gross of its own
        # reconciles against a sibling docking's recorded gross, so recording it
        # once lets the whole truck dispatch.
        from weighment.models import Weighment

        from gate_core.services.sales_dispatch_dispatch import (
            dispatch_gross_weight,
            get_dispatch_weight_error,
        )

        self.arrival.tare_weight = Decimal("4000.000")
        self.arrival.save(update_fields=["tare_weight"])
        bev = self._docked(self.beverages, "111")
        oil = self._docked(self.oil, "222")
        # Only Oil's weighment carries the loaded gross; Beverages' has none.
        Weighment.objects.create(
            vehicle_entry=oil.vehicle_entry, gross_weight=Decimal("5000.000"),
            tare_weight=Decimal("4000.000"), created_by=self.user, updated_by=self.user,
        )
        Weighment.objects.create(
            vehicle_entry=bev.vehicle_entry, tare_weight=Decimal("4000.000"),
            created_by=self.user, updated_by=self.user,
        )
        bev.refresh_from_db()

        self.assertEqual(dispatch_gross_weight(bev), Decimal("5000.000"))
        self.assertEqual(get_dispatch_weight_error(bev), "")
