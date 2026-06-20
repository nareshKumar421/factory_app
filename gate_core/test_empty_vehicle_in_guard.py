"""A vehicle can be inside only once: starting a new empty-vehicle gate-in is
blocked while the truck still has a live one that has not left the gate."""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from company.models import Company, UserCompany, UserRole
from driver_management.models import Driver, VehicleEntry
from gate_core.models import EmptyVehicleGateIn, EmptyVehicleGateOut
from vehicle_management.models import Transporter, Vehicle

CREATE_URL = "/api/v1/gate-core/empty-vehicle-ins/"


class EmptyVehicleInInsideGuardTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        self.role = UserRole.objects.create(name="Gate")
        self.user = get_user_model().objects.create_user(
            email="guard@example.com",
            password="testpass123",
            full_name="Guard User",
            employee_code="GRD001",
        )
        UserCompany.objects.create(
            user=self.user, company=self.company, role=self.role, is_default=True
        )
        self.transporter = Transporter.objects.create(name="Test Transporter")
        self.vehicle = Vehicle.objects.create(
            vehicle_number="DL01LAC9967", transporter=self.transporter
        )
        self.driver = Driver.objects.create(
            name="Test Driver", mobile_no="9000000000", license_no="DL-GRD-1"
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.company_header = {"HTTP_COMPANY_CODE": self.company.code}

    def _gate_in(self, *, entry_no, status="COMPLETED", retired=False):
        vehicle_entry = VehicleEntry.objects.create(
            entry_no=entry_no,
            company=self.company,
            vehicle=self.vehicle,
            driver=self.driver,
            entry_type="EMPTY_VEHICLE",
            status=status,
            created_by=self.user,
            updated_by=self.user,
        )
        return EmptyVehicleGateIn.objects.create(
            company=self.company,
            entry_no=entry_no,
            vehicle_entry=vehicle_entry,
            vehicle=self.vehicle,
            driver=self.driver,
            reason="DISPATCH",
            gate_in_date=timezone.localdate(),
            in_time=timezone.now().time(),
            retired_at=timezone.now() if retired else None,
            retired_reason="DISPATCHED" if retired else "",
            created_by=self.user,
            updated_by=self.user,
        )

    def _create_payload(self):
        return {
            "vehicle_id": self.vehicle.id,
            "driver_id": self.driver.id,
            "reason": "DISPATCH",
            "gate_in_date": timezone.localdate().isoformat(),
            "in_time": "10:00",
        }

    def _post(self):
        return self.client.post(
            CREATE_URL, self._create_payload(), format="json", **self.company_header
        )

    def test_blocked_when_vehicle_completed_and_still_inside(self):
        self._gate_in(entry_no="EVGI-INSIDE-1", status="COMPLETED")

        response = self._post()

        self.assertEqual(response.status_code, 400)
        self.assertIn("already inside", response.data["detail"].lower())
        self.assertIn("EVGI-INSIDE-1", response.data["detail"])

    def test_blocked_when_vehicle_in_progress(self):
        self._gate_in(entry_no="EVGI-INPROG-1", status="IN_PROGRESS")

        self.assertEqual(self._post().status_code, 400)

    def test_allowed_after_vehicle_retired_dispatched(self):
        self._gate_in(entry_no="EVGI-GONE-1", status="COMPLETED", retired=True)

        # Retired (dispatched / emptied out) => the truck has left; a new entry is fine.
        self.assertEqual(self._post().status_code, 201)

    def test_allowed_after_empty_vehicle_out(self):
        gate_in = self._gate_in(entry_no="EVGI-OUT-1", status="COMPLETED")
        EmptyVehicleGateOut.objects.create(
            company=self.company,
            entry_no="EVGO-OUT-1",
            vehicle_entry=gate_in.vehicle_entry,
            vehicle=self.vehicle,
            driver=self.driver,
            gate_out_date=timezone.localdate(),
            out_time=timezone.now().time(),
            status="COMPLETED",
            created_by=self.user,
            updated_by=self.user,
        )

        self.assertEqual(self._post().status_code, 201)

    def test_cancelled_gate_in_does_not_block(self):
        self._gate_in(entry_no="EVGI-CANCEL-1", status="CANCELLED")

        self.assertEqual(self._post().status_code, 201)

    # ----- inside_only list filter (feeds the "Already inside" flag) -------

    def _inside_entry_nos(self):
        response = self.client.get(
            CREATE_URL,
            {"reason": "DISPATCH", "inside_only": "true"},
            **self.company_header,
        )
        self.assertEqual(response.status_code, 200)
        return [entry["entry_no"] for entry in response.data]

    def test_inside_only_includes_completed_not_departed(self):
        self._gate_in(entry_no="EVGI-IN-1", status="COMPLETED")
        self._gate_in(entry_no="EVGI-IN-2", status="IN_PROGRESS")

        entry_nos = self._inside_entry_nos()
        self.assertIn("EVGI-IN-1", entry_nos)
        self.assertIn("EVGI-IN-2", entry_nos)

    def test_inside_only_excludes_retired_and_departed(self):
        self._gate_in(entry_no="EVGI-RETIRED", status="COMPLETED", retired=True)
        departed = self._gate_in(entry_no="EVGI-DEPARTED", status="COMPLETED")
        EmptyVehicleGateOut.objects.create(
            company=self.company,
            entry_no="EVGO-DEP-1",
            vehicle_entry=departed.vehicle_entry,
            vehicle=self.vehicle,
            driver=self.driver,
            gate_out_date=timezone.localdate(),
            out_time=timezone.now().time(),
            status="COMPLETED",
            created_by=self.user,
            updated_by=self.user,
        )

        entry_nos = self._inside_entry_nos()
        self.assertNotIn("EVGI-RETIRED", entry_nos)
        self.assertNotIn("EVGI-DEPARTED", entry_nos)
