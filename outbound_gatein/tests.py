"""Tests for the outbound_gatein app."""
from decimal import Decimal
from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from rest_framework.exceptions import ValidationError

from company.models import Company, UserCompany, UserRole
from vehicle_management.models import Vehicle, VehicleType
from driver_management.models import Driver, VehicleEntry
from security_checks.models import SecurityCheck

from .models import OutboundGateEntry, OutboundPurpose
from .services import complete_outbound_gate_entry

User = get_user_model()


class OutboundGateTestMixin:
    """Shared test data setup."""

    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user(
            email="gatetest@example.com",
            password="testpass123",
            full_name="Gate Test User",
            employee_code="EMP100",
        )
        from django.contrib.auth.models import Permission
        all_perms = Permission.objects.filter(
            content_type__app_label="outbound_gatein"
        )
        self.user.user_permissions.set(all_perms)
        self.user = User.objects.get(pk=self.user.pk)

        self.company = Company.objects.create(
            name="Test Co", code="TEST_CO", is_active=True
        )
        self.role, _ = UserRole.objects.get_or_create(
            name="Admin", defaults={"description": "Admin"}
        )
        UserCompany.objects.create(
            user=self.user, company=self.company,
            role=self.role, is_default=True, is_active=True,
        )

        self.vtype = VehicleType.objects.create(name="TRAILER")
        self.vehicle = Vehicle.objects.create(
            vehicle_number="MH12XY9999", vehicle_type=self.vtype,
        )
        self.driver = Driver.objects.create(
            name="Outbound Driver", mobile_no="7777777777", license_no="OB-DL-001",
        )

        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.client.credentials(HTTP_COMPANY_CODE="TEST_CO")

    def create_vehicle_entry(self, **kwargs):
        defaults = {
            "company": self.company,
            "vehicle": self.vehicle,
            "driver": self.driver,
            "entry_type": "OUTBOUND",
            "entry_no": "GE-OB-001",
        }
        defaults.update(kwargs)
        return VehicleEntry.objects.create(**defaults)


# =====================================================================
# MODEL TESTS
# =====================================================================

class OutboundGateEntryModelTest(OutboundGateTestMixin, TestCase):

    def test_create_outbound_entry(self):
        ve = self.create_vehicle_entry()
        entry = OutboundGateEntry.objects.create(
            vehicle_entry=ve,
            customer_name="Test Customer",
            vehicle_empty_confirmed=True,
        )
        self.assertIn("OUT-", str(entry))
        self.assertEqual(entry.assigned_zone, "YARD")

    def test_purpose_lookup(self):
        purpose = OutboundPurpose.objects.create(
            name="Finished Goods Dispatch"
        )
        ve = self.create_vehicle_entry()
        entry = OutboundGateEntry.objects.create(
            vehicle_entry=ve, purpose=purpose,
        )
        self.assertEqual(entry.purpose.name, "Finished Goods Dispatch")


# =====================================================================
# SERVICE TESTS
# =====================================================================

class OutboundGateCompletionTest(OutboundGateTestMixin, TestCase):

    def test_complete_success(self):
        ve = self.create_vehicle_entry()
        OutboundGateEntry.objects.create(
            vehicle_entry=ve, vehicle_empty_confirmed=True,
        )
        SecurityCheck.objects.create(
            vehicle_entry=ve,
            inspected_by_name="Inspector",
            is_submitted=True,
        )
        complete_outbound_gate_entry(ve)
        ve.refresh_from_db()
        self.assertEqual(ve.status, "COMPLETED")
        self.assertTrue(ve.is_locked)

    def test_complete_fails_not_outbound(self):
        ve = self.create_vehicle_entry(entry_type="RAW_MATERIAL", entry_no="GE-RM-001")
        with self.assertRaises(ValidationError):
            complete_outbound_gate_entry(ve)

    def test_complete_fails_no_security_check(self):
        ve = self.create_vehicle_entry()
        OutboundGateEntry.objects.create(
            vehicle_entry=ve, vehicle_empty_confirmed=True,
        )
        with self.assertRaises(ValidationError):
            complete_outbound_gate_entry(ve)

    def test_complete_fails_not_empty_confirmed(self):
        ve = self.create_vehicle_entry()
        OutboundGateEntry.objects.create(
            vehicle_entry=ve, vehicle_empty_confirmed=False,
        )
        SecurityCheck.objects.create(
            vehicle_entry=ve,
            inspected_by_name="Inspector",
            is_submitted=True,
        )
        with self.assertRaises(ValidationError):
            complete_outbound_gate_entry(ve)

    def test_complete_fails_already_locked(self):
        ve = self.create_vehicle_entry()
        ve.is_locked = True
        ve.save(update_fields=["is_locked"])
        with self.assertRaises(ValidationError):
            complete_outbound_gate_entry(ve)


# =====================================================================
# API TESTS
# =====================================================================

class OutboundGateAPITest(OutboundGateTestMixin, TestCase):

    def test_create_outbound_entry_api(self):
        ve = self.create_vehicle_entry()
        response = self.client.post(
            f"/api/v1/outbound-gatein/gate-entries/{ve.id}/outbound/",
            {
                "customer_name": "ACME Corp",
                "customer_code": "C10001",
                "vehicle_empty_confirmed": True,
                "transporter_name": "Fast Logistics",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertTrue(OutboundGateEntry.objects.filter(vehicle_entry=ve).exists())

    def test_create_rejects_non_outbound_type(self):
        ve = self.create_vehicle_entry(entry_type="RAW_MATERIAL", entry_no="GE-RM-002")
        response = self.client.post(
            f"/api/v1/outbound-gatein/gate-entries/{ve.id}/outbound/",
            {"customer_name": "Test"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_create_rejects_duplicate(self):
        ve = self.create_vehicle_entry()
        OutboundGateEntry.objects.create(vehicle_entry=ve)
        response = self.client.post(
            f"/api/v1/outbound-gatein/gate-entries/{ve.id}/outbound/",
            {"customer_name": "Test"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_get_outbound_entry(self):
        ve = self.create_vehicle_entry()
        OutboundGateEntry.objects.create(
            vehicle_entry=ve, customer_name="Fetch Me",
        )
        response = self.client.get(
            f"/api/v1/outbound-gatein/gate-entries/{ve.id}/outbound/"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["customer_name"], "Fetch Me")

    def test_get_returns_404_when_no_entry(self):
        ve = self.create_vehicle_entry()
        response = self.client.get(
            f"/api/v1/outbound-gatein/gate-entries/{ve.id}/outbound/"
        )
        self.assertEqual(response.status_code, 404)

    def test_update_outbound_entry(self):
        ve = self.create_vehicle_entry()
        OutboundGateEntry.objects.create(
            vehicle_entry=ve, customer_name="Old Name",
        )
        response = self.client.put(
            f"/api/v1/outbound-gatein/gate-entries/{ve.id}/outbound/update/",
            {"customer_name": "New Name"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["customer_name"], "New Name")

    def test_complete_api(self):
        ve = self.create_vehicle_entry()
        OutboundGateEntry.objects.create(
            vehicle_entry=ve, vehicle_empty_confirmed=True,
        )
        SecurityCheck.objects.create(
            vehicle_entry=ve,
            inspected_by_name="Inspector",
            is_submitted=True,
        )
        response = self.client.post(
            f"/api/v1/outbound-gatein/gate-entries/{ve.id}/complete/",
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        ve.refresh_from_db()
        self.assertTrue(ve.is_locked)

    def test_release_for_loading(self):
        ve = self.create_vehicle_entry()
        OutboundGateEntry.objects.create(
            vehicle_entry=ve, vehicle_empty_confirmed=True,
        )
        response = self.client.post(
            f"/api/v1/outbound-gatein/gate-entries/{ve.id}/release-for-loading/",
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(response.data["released_for_loading_at"])

    def test_release_fails_not_empty(self):
        ve = self.create_vehicle_entry()
        OutboundGateEntry.objects.create(
            vehicle_entry=ve, vehicle_empty_confirmed=False,
        )
        response = self.client.post(
            f"/api/v1/outbound-gatein/gate-entries/{ve.id}/release-for-loading/",
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_available_vehicles_list(self):
        ve = self.create_vehicle_entry()
        OutboundGateEntry.objects.create(
            vehicle_entry=ve, vehicle_empty_confirmed=True,
        )
        response = self.client.get(
            "/api/v1/outbound-gatein/available-vehicles/"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["vehicle_number"], "MH12XY9999")

    def test_available_vehicles_excludes_not_confirmed(self):
        ve = self.create_vehicle_entry()
        OutboundGateEntry.objects.create(
            vehicle_entry=ve, vehicle_empty_confirmed=False,
        )
        response = self.client.get(
            "/api/v1/outbound-gatein/available-vehicles/"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 0)

    def test_purposes_list(self):
        OutboundPurpose.objects.create(name="FG Dispatch")
        OutboundPurpose.objects.create(name="Sample Delivery")
        OutboundPurpose.objects.create(name="Inactive", is_active=False)
        response = self.client.get("/api/v1/outbound-gatein/purposes/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 2)

    def test_unauthenticated_access(self):
        client = APIClient()
        response = client.get("/api/v1/outbound-gatein/available-vehicles/")
        self.assertEqual(response.status_code, 401)

    def test_missing_company_header(self):
        client = APIClient()
        client.force_authenticate(user=self.user)
        response = client.get("/api/v1/outbound-gatein/available-vehicles/")
        self.assertEqual(response.status_code, 403)
