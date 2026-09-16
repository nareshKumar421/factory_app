"""A dispatch truck arriving after the evening cutoff needs an approval.

Covers the rule itself (which of the two clocks decides lateness), the gate
endpoint that refuses the entry, and the request/approve/reject cycle that lets
it through. Reasons other than DISPATCH are never time-bound.
"""

import datetime as dt

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from company.models import Company, UserCompany, UserRole
from dispatch_plans.models import DispatchPlan, DispatchPlanStatus
from driver_management.models import Driver
from gate_core.models import (
    EmptyVehicleGateIn,
    LateDispatchGateInApproval,
    LateDispatchGateInApprovalStatus,
)
from gate_core.services.late_dispatch_gate_in import is_late_dispatch_gate_in
from vehicle_management.models import Transporter, Vehicle

GATE_IN_URL = "/api/v1/gate-core/empty-vehicle-ins/"
APPROVALS_URL = "/api/v1/gate-core/late-dispatch-approvals/"

# A cutoff far enough into the night that the wall clock can never make an
# otherwise-early entry late, whatever time the suite is run at.
NEVER_LATE_CUTOFF = dt.time(23, 59, 59)


class LateDispatchCutoffRuleTests(TestCase):
    """The rule reads the later of the typed arrival time and the wall clock."""

    def _now(self, hour, minute=0, day_offset=0):
        today = timezone.localdate() + dt.timedelta(days=day_offset)
        return dt.datetime.combine(today, dt.time(hour, minute))

    def test_typed_time_after_cutoff_is_late(self):
        self.assertTrue(
            is_late_dispatch_gate_in(
                timezone.localdate(), dt.time(18, 30), now=self._now(18, 35)
            )
        )

    def test_typed_time_before_cutoff_and_early_clock_is_not_late(self):
        self.assertFalse(
            is_late_dispatch_gate_in(
                timezone.localdate(), dt.time(10, 0), now=self._now(10, 5)
            )
        )

    def test_exactly_at_cutoff_is_late(self):
        self.assertTrue(
            is_late_dispatch_gate_in(
                timezone.localdate(), dt.time(17, 0), now=self._now(17, 0)
            )
        )

    def test_todays_entry_back_typed_to_the_morning_is_still_late(self):
        # The in_time field is editable: an entry started at 8 PM with "10:00"
        # typed into it would otherwise walk straight past the rule.
        self.assertTrue(
            is_late_dispatch_gate_in(
                timezone.localdate(), dt.time(10, 0), now=self._now(20, 0)
            )
        )

    def test_back_dated_entry_rests_on_its_typed_time(self):
        # Yesterday's entry has no live clock to appeal to.
        self.assertFalse(
            is_late_dispatch_gate_in(
                timezone.localdate() - dt.timedelta(days=1),
                dt.time(10, 0),
                now=self._now(20, 0),
            )
        )

    @override_settings(LATE_DISPATCH_GATE_IN_CUTOFF="20:00")
    def test_cutoff_is_configurable(self):
        self.assertFalse(
            is_late_dispatch_gate_in(
                timezone.localdate(), dt.time(18, 0), now=self._now(18, 0)
            )
        )
        self.assertTrue(
            is_late_dispatch_gate_in(
                timezone.localdate(), dt.time(20, 30), now=self._now(20, 30)
            )
        )

    @override_settings(LATE_DISPATCH_GATE_IN_CUTOFF="not-a-time")
    def test_unparseable_cutoff_falls_back_to_five_pm(self):
        self.assertTrue(
            is_late_dispatch_gate_in(
                timezone.localdate(), dt.time(17, 30), now=self._now(17, 30)
            )
        )


class LateDispatchGateInBaseTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        self.role = UserRole.objects.create(name="Gate")
        self.user = self._make_user("gate@example.com", "GATE001", "Gate User")
        self.approver = self._make_user("boss@example.com", "BOSS001", "Approver User")
        self.transporter = Transporter.objects.create(name="Test Transporter")
        self.vehicle = Vehicle.objects.create(
            vehicle_number="PB01AA1111", transporter=self.transporter
        )
        self.driver = Driver.objects.create(
            name="Test Driver", mobile_no="9000000000", license_no="DL-LATE-1"
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.headers = {"HTTP_COMPANY_CODE": self.company.code}
        self.today = timezone.localdate()

    def _make_user(self, email, code, name):
        user = get_user_model().objects.create_user(
            email=email, password="testpass123", full_name=name, employee_code=code
        )
        UserCompany.objects.create(
            user=user, company=self.company, role=self.role, is_default=True
        )
        return user

    def _grant_approver_permissions(self):
        ct = ContentType.objects.get_for_model(LateDispatchGateInApproval)
        self.approver.user_permissions.add(
            *Permission.objects.filter(
                content_type=ct,
                codename__in=[
                    "can_view_late_dispatch_gate_in",
                    "can_approve_late_dispatch_gate_in",
                ],
            )
        )
        # Permission caching is per-instance; re-fetch so the grant is visible.
        self.approver = get_user_model().objects.get(pk=self.approver.pk)

    def _approver_client(self):
        self._grant_approver_permissions()
        client = APIClient()
        client.force_authenticate(self.approver)
        return client

    def _book_plan(self, doc_entry=5001, doc_num="626090001", customer="Acme Foods"):
        return DispatchPlan.objects.create(
            company=self.company,
            sap_invoice_doc_entry=doc_entry,
            sap_invoice_doc_num=doc_num,
            customer_name=customer,
            vehicle=self.vehicle,
            booking_status=DispatchPlanStatus.BOOKED,
            dispatch_date=self.today,
            created_by=self.user,
            updated_by=self.user,
        )

    def _gate_in_payload(self, *, reason="DISPATCH", in_time="18:30", gate_in_date=None):
        return {
            "vehicle_id": self.vehicle.id,
            "driver_id": self.driver.id,
            "reason": reason,
            "gate_in_date": (gate_in_date or self.today).isoformat(),
            "in_time": in_time,
        }

    def _post_gate_in(self, **kwargs):
        return self.client.post(
            GATE_IN_URL, self._gate_in_payload(**kwargs), format="json", **self.headers
        )

    def _request_approval(self, in_time="18:30", reason="Truck reached the gate late."):
        return self.client.post(
            APPROVALS_URL,
            {
                "vehicle_id": self.vehicle.id,
                "gate_in_date": self.today.isoformat(),
                "in_time": in_time,
                "reason": reason,
            },
            format="json",
            **self.headers,
        )

    def _approved_approval(self, **kwargs):
        approval = LateDispatchGateInApproval.objects.create(
            company=self.company,
            vehicle=self.vehicle,
            gate_in_date=kwargs.get("gate_in_date", self.today),
            in_time=dt.time(18, 30),
            reason="Late but the load is ready.",
            status=LateDispatchGateInApprovalStatus.APPROVED,
            requested_by=self.user,
            reviewed_by=self.approver,
            reviewed_at=timezone.now(),
            created_by=self.user,
            updated_by=self.approver,
        )
        return approval


class LateDispatchGateInGateTests(LateDispatchGateInBaseTests):
    """What the gate can and cannot start once the cutoff has passed."""

    def test_dispatch_after_cutoff_without_approval_is_refused(self):
        response = self._post_gate_in()

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "LATE_DISPATCH_APPROVAL_REQUIRED")
        self.assertEqual(response.data["approval_status"], "NONE")
        self.assertIn("PB01AA1111", response.data["detail"])
        self.assertFalse(EmptyVehicleGateIn.objects.exists())

    @override_settings(LATE_DISPATCH_GATE_IN_CUTOFF=NEVER_LATE_CUTOFF)
    def test_dispatch_before_cutoff_needs_no_approval(self):
        response = self._post_gate_in(in_time="10:00")

        self.assertEqual(response.status_code, 201)
        self.assertEqual(EmptyVehicleGateIn.objects.count(), 1)

    def test_non_dispatch_reason_is_never_time_bound(self):
        # A repair movement is not loading anything; it has never had a cutoff.
        response = self._post_gate_in(reason="REPAIR_MOVEMENT")

        self.assertEqual(response.status_code, 201)
        self.assertEqual(EmptyVehicleGateIn.objects.count(), 1)

    def test_pending_approval_does_not_let_the_truck_in(self):
        self._request_approval()

        response = self._post_gate_in()

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["approval_status"], "PENDING")
        self.assertIn("waiting with the approver", response.data["detail"])

    def test_rejected_approval_reports_the_approver_note(self):
        request_response = self._request_approval()
        approval_id = request_response.data["id"]
        self._approver_client().post(
            f"{APPROVALS_URL}{approval_id}/reject/",
            {"notes": "Load it tomorrow morning."},
            format="json",
            **self.headers,
        )

        response = self._post_gate_in()

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["approval_status"], "REJECTED")
        self.assertIn("Load it tomorrow morning.", response.data["detail"])

    def test_approved_request_lets_the_truck_in_and_is_spent(self):
        approval = self._approved_approval()

        response = self._post_gate_in()

        self.assertEqual(response.status_code, 201)
        approval.refresh_from_db()
        self.assertIsNotNone(approval.consumed_at)
        self.assertEqual(
            approval.empty_vehicle_gate_in_id,
            EmptyVehicleGateIn.objects.get().id,
        )

    def test_an_approval_is_good_for_one_entry_only(self):
        self._approved_approval()
        self.assertEqual(self._post_gate_in().status_code, 201)

        # Retire the first entry so the already-inside guard is not what refuses
        # the second one -- the spent approval must be.
        EmptyVehicleGateIn.objects.update(
            retired_at=timezone.now(), retired_reason="DISPATCHED"
        )

        response = self._post_gate_in()

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "LATE_DISPATCH_APPROVAL_REQUIRED")

    def test_approval_does_not_carry_to_another_day(self):
        self._approved_approval(gate_in_date=self.today - dt.timedelta(days=1))

        response = self._post_gate_in()

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["approval_status"], "NONE")


class LateDispatchApprovalRequestTests(LateDispatchGateInBaseTests):
    """Raising the request from the gate, and reading it back."""

    def test_request_snapshots_the_booked_load(self):
        self._book_plan(doc_entry=5001, doc_num="626090001", customer="Acme Foods")
        self._book_plan(doc_entry=5002, doc_num="626090002", customer="Beta Traders")

        response = self._request_approval()

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["status"], "PENDING")
        self.assertEqual(response.data["bill_count"], 2)
        self.assertEqual(response.data["bill_doc_nums"], "626090001, 626090002")
        self.assertEqual(response.data["customer_names"], "Acme Foods, Beta Traders")
        self.assertEqual(response.data["vehicle_no"], "PB01AA1111")

    def test_a_second_request_returns_the_one_already_waiting(self):
        first = self._request_approval()

        second = self._request_approval(reason="Sent again by mistake.")

        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.data["id"], first.data["id"])
        self.assertEqual(LateDispatchGateInApproval.objects.count(), 1)

    def test_an_already_cleared_truck_raises_nothing_new(self):
        # The board would not offer to send it, but a stale tab might: an unspent
        # clearance means there is nothing left to ask.
        cleared = self._approved_approval()

        response = self._request_approval()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["id"], cleared.id)
        self.assertEqual(LateDispatchGateInApproval.objects.count(), 1)

    @override_settings(LATE_DISPATCH_GATE_IN_CUTOFF=NEVER_LATE_CUTOFF)
    def test_request_before_the_cutoff_is_refused(self):
        response = self._request_approval(in_time="10:00")

        self.assertEqual(response.status_code, 400)
        self.assertIn("no approval is needed", response.data["detail"])

    def test_reason_is_required(self):
        response = self._request_approval(reason="   ")

        self.assertEqual(response.status_code, 400)

    def _by_vehicle(self):
        return self.client.get(
            f"{APPROVALS_URL}by-vehicle/{self.vehicle.id}/",
            {"gate_in_date": self.today.isoformat()},
            **self.headers,
        )

    def test_by_vehicle_reads_back_the_trucks_request(self):
        created = self._request_approval()

        response = self._by_vehicle()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["approval"]["id"], created.data["id"])

    def test_by_vehicle_has_no_approval_when_nothing_was_asked(self):
        response = self._by_vehicle()

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.data["approval"])

    @override_settings(LATE_DISPATCH_GATE_IN_CUTOFF=NEVER_LATE_CUTOFF)
    def test_by_vehicle_says_nothing_is_needed_before_the_cutoff(self):
        response = self._by_vehicle()

        self.assertFalse(response.data["is_late"])
        self.assertFalse(response.data["requires_approval"])

    @override_settings(LATE_DISPATCH_GATE_IN_CUTOFF=dt.time(0, 0))
    def test_by_vehicle_demands_approval_after_the_cutoff(self):
        response = self._by_vehicle()

        self.assertTrue(response.data["is_late"])
        self.assertTrue(response.data["requires_approval"])
        self.assertEqual(response.data["cutoff"], "00:00")

    @override_settings(LATE_DISPATCH_GATE_IN_CUTOFF=dt.time(0, 0))
    def test_by_vehicle_stops_demanding_once_cleared(self):
        self._approved_approval()

        response = self._by_vehicle()

        self.assertTrue(response.data["is_late"])
        self.assertFalse(response.data["requires_approval"])
        self.assertEqual(response.data["approval"]["status"], "APPROVED")


class LateDispatchApprovalReviewTests(LateDispatchGateInBaseTests):
    """The approver's side: the queue, and deciding on a request."""

    def test_queue_needs_the_view_permission(self):
        self._request_approval()

        response = self.client.get(APPROVALS_URL, **self.headers)

        self.assertEqual(response.status_code, 403)

    def test_queue_lists_pending_requests_for_an_approver(self):
        self._request_approval()

        response = self._approver_client().get(
            APPROVALS_URL, {"status": "PENDING"}, **self.headers
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["vehicle_no"], "PB01AA1111")

    def test_deciding_needs_the_approve_permission(self):
        approval_id = self._request_approval().data["id"]

        response = self.client.post(
            f"{APPROVALS_URL}{approval_id}/approve/", {}, format="json", **self.headers
        )

        self.assertEqual(response.status_code, 403)

    def test_approving_records_the_decision(self):
        approval_id = self._request_approval().data["id"]

        response = self._approver_client().post(
            f"{APPROVALS_URL}{approval_id}/approve/",
            {"notes": "Loading crew is on shift."},
            format="json",
            **self.headers,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], "APPROVED")
        self.assertEqual(response.data["reviewed_by_name"], "Approver User")

    def test_rejecting_requires_a_note(self):
        approval_id = self._request_approval().data["id"]

        response = self._approver_client().post(
            f"{APPROVALS_URL}{approval_id}/reject/", {}, format="json", **self.headers
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("notes", response.data)

    def test_a_decided_request_cannot_be_decided_again(self):
        approval_id = self._request_approval().data["id"]
        client = self._approver_client()
        client.post(f"{APPROVALS_URL}{approval_id}/approve/", {}, format="json", **self.headers)

        response = client.post(
            f"{APPROVALS_URL}{approval_id}/reject/",
            {"notes": "Changed my mind."},
            format="json",
            **self.headers,
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("already approved", response.data["detail"])

    def test_approving_unblocks_the_gate(self):
        approval_id = self._request_approval().data["id"]
        self._approver_client().post(
            f"{APPROVALS_URL}{approval_id}/approve/", {}, format="json", **self.headers
        )

        response = self._post_gate_in()

        self.assertEqual(response.status_code, 201)


class LateDispatchArrivalPathTests(LateDispatchGateInBaseTests):
    """The cross-company arrival is the other door into the yard, and it is gated too."""

    ARRIVALS_URL = "/api/v1/gate-core/arrivals/"

    def _post_arrival(self, in_time="18:30"):
        return self.client.post(
            self.ARRIVALS_URL,
            {
                "vehicle_id": self.vehicle.id,
                "driver_id": self.driver.id,
                "gate_in_date": self.today.isoformat(),
                "in_time": in_time,
            },
            format="json",
            **self.headers,
        )

    def test_arrival_after_cutoff_without_approval_is_refused(self):
        self._book_plan()

        response = self._post_arrival()

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "LATE_DISPATCH_APPROVAL_REQUIRED")
        self.assertFalse(EmptyVehicleGateIn.objects.exists())

    def test_arrival_after_cutoff_with_approval_goes_in_and_spends_it(self):
        self._book_plan()
        approval = self._approved_approval()

        response = self._post_arrival()

        self.assertEqual(response.status_code, 201, response.data)
        approval.refresh_from_db()
        self.assertIsNotNone(approval.consumed_at)
        self.assertEqual(
            approval.empty_vehicle_gate_in_id, EmptyVehicleGateIn.objects.get().id
        )

    @override_settings(LATE_DISPATCH_GATE_IN_CUTOFF=NEVER_LATE_CUTOFF)
    def test_arrival_before_cutoff_needs_no_approval(self):
        self._book_plan()

        response = self._post_arrival(in_time="10:00")

        self.assertEqual(response.status_code, 201, response.data)
