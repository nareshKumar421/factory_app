"""Tests for the Returnable Gate Pass lifecycle.

Every request carries the ``Company-Code`` header, as ``HasCompanyContext``
requires. Notifications are patched out — they are fire-and-forget and hitting
Firebase from a test run is neither possible nor useful.
"""

from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from company.models import Company, UserCompany, UserRole

from .constants import ReturnableStatus
from .models import ReturnableGatePass, ReturnableGatePassItem, ReturnableGatePassSequence

User = get_user_model()

ALL_PERMS = [
    "can_view_returnable_module",
    "can_view_returnable_gatepass",
    "can_manage_returnable_gatepass",
    "can_submit_returnable_gatepass",
    "can_approve_returnable_gatepass",
    "can_gate_out_returnable",
    "can_gate_in_returnable",
    "can_reject_returnable_at_gate",
    "can_acknowledge_returnable",
    "can_close_returnable",
    "can_short_close_returnable",
    "can_cancel_returnable",
    "can_view_returnable_reports",
]


@patch("returnable_items.views.notify", autospec=True)
class ReturnableGatePassFlowTests(APITestCase):
    maxDiff = None

    def setUp(self):
        self.company = Company.objects.create(name="Acme Foods", code="ACME")
        self.role = UserRole.objects.create(name="Maintenance")
        self.user = User.objects.create_user(
            email="dept@acme.test", password="pw", full_name="Dept User", employee_code="E001"
        )
        UserCompany.objects.create(
            user=self.user, company=self.company, role=self.role, is_default=True
        )
        self._grant(self.user, ALL_PERMS)

        # Approval is a separate pair of eyes — a user cannot approve their own
        # submission, so every test that needs a pass past approval uses this one.
        self.approver = User.objects.create_user(
            email="head@acme.test", password="pw", full_name="Plant Head", employee_code="E005"
        )
        UserCompany.objects.create(
            user=self.approver, company=self.company, role=self.role, is_default=True
        )
        self._grant(self.approver, ALL_PERMS)

        self.client.force_authenticate(self.user)
        self.client.credentials(HTTP_COMPANY_CODE=self.company.code)

    def _grant(self, user, codenames):
        permissions = Permission.objects.filter(
            content_type__app_label="returnable_items", codename__in=codenames
        )
        user.user_permissions.add(*permissions)
        # has_perm caches per instance
        user = User.objects.get(pk=user.pk)

    def _payload(self, **overrides):
        payload = {
            "party_name": "Sharma Motors",
            "purpose": "REPAIR",
            "expected_return_date": (timezone.localdate() + timedelta(days=7)).isoformat(),
            "items_input": [
                {"item_name": "Gear Motor 3HP", "quantity_out": "2.000", "uom": "NOS"},
                {"item_name": "Bearing 6205", "quantity_out": "4.000", "uom": "NOS"},
            ],
        }
        payload.update(overrides)
        return payload

    def _create_pass(self):
        url = reverse("returnable-gatepass-list")
        response = self.client.post(url, self._payload(), format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        return ReturnableGatePass.objects.get(pk=response.data["id"])

    def _action_url(self, gate_pass, name):
        return reverse(f"returnable-gatepass-{name}", args=[gate_pass.pk])

    def _as(self, user):
        """Re-authenticate; permission caching lives on the instance."""
        self.client.force_authenticate(User.objects.get(pk=user.pk))

    def _approve(self, gate_pass):
        """Approve as the head, then hand the client back to the department user."""
        self._as(self.approver)
        response = self.client.post(self._action_url(gate_pass, "approve"), {}, format="json")
        self._as(self.user)
        return response

    def _submit_and_approve(self, gate_pass):
        self.client.post(self._action_url(gate_pass, "submit"))
        return self._approve(gate_pass)

    def _gate_out(self, gate_pass):
        return self.client.post(
            self._action_url(gate_pass, "gate-out"),
            {"vehicle_number_manual": "GJ01AB1234", "driver_name_manual": "Ramesh"},
            format="json",
        )

    # -- creation ---------------------------------------------------------

    def test_create_assigns_sequential_pass_numbers(self, _notify):
        first = self._create_pass()
        second = self._create_pass()
        year = ReturnableGatePassSequence.current_financial_year()
        self.assertEqual(first.pass_no, f"RGP/{year}/000001")
        self.assertEqual(second.pass_no, f"RGP/{year}/000002")
        self.assertEqual(first.items.count(), 2)
        self.assertEqual(first.status, ReturnableStatus.DRAFT)

    def test_create_requires_at_least_one_item(self, _notify):
        response = self.client.post(
            reverse("returnable-gatepass-list"), self._payload(items_input=[]), format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    # -- happy path -------------------------------------------------------

    def test_full_lifecycle_draft_to_closed(self, notify):
        gate_pass = self._create_pass()

        self.assertEqual(
            self.client.post(self._action_url(gate_pass, "submit")).status_code, status.HTTP_200_OK
        )
        gate_pass.refresh_from_db()
        self.assertEqual(gate_pass.status, ReturnableStatus.PENDING_APPROVAL)
        notify.notify_submitted.assert_called_once()

        # The gate cannot act on a pass that has not been approved.
        premature = self._gate_out(gate_pass)
        self.assertEqual(premature.status_code, status.HTTP_400_BAD_REQUEST)

        self.assertEqual(self._approve(gate_pass).status_code, status.HTTP_200_OK)
        gate_pass.refresh_from_db()
        self.assertEqual(gate_pass.status, ReturnableStatus.PENDING_GATE_OUT)
        self.assertEqual(gate_pass.approved_by_id, self.approver.id)
        self.assertIsNotNone(gate_pass.approved_at)
        notify.notify_approved.assert_called_once()

        self.assertEqual(self._gate_out(gate_pass).status_code, status.HTTP_200_OK)
        gate_pass.refresh_from_db()
        self.assertEqual(gate_pass.status, ReturnableStatus.OUT)
        self.assertIsNotNone(gate_pass.gate_out_at)
        notify.notify_gate_out.assert_called_once()

        lines = [
            {"pass_item": item.id, "quantity_returned": str(item.quantity_out)}
            for item in gate_pass.items.all()
        ]
        response = self.client.post(
            self._action_url(gate_pass, "record-return"),
            {"vehicle_number_manual": "GJ05XY9999", "lines": lines},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        gate_pass.refresh_from_db()
        self.assertEqual(gate_pass.status, ReturnableStatus.RETURNED)
        notify.notify_return_recorded.assert_called_once()

        # Close is blocked until the department acknowledges collection.
        blocked = self.client.post(self._action_url(gate_pass, "close"))
        self.assertEqual(blocked.status_code, status.HTTP_400_BAD_REQUEST)

        self.client.post(self._action_url(gate_pass, "acknowledge"), {}, format="json")
        notify.notify_acknowledged.assert_called_once()

        self.assertEqual(
            self.client.post(self._action_url(gate_pass, "close")).status_code, status.HTTP_200_OK
        )
        gate_pass.refresh_from_db()
        self.assertEqual(gate_pass.status, ReturnableStatus.CLOSED)
        self.assertIsNotNone(gate_pass.closed_at)

    # -- partial returns --------------------------------------------------

    def test_partial_return_across_two_events(self, _notify):
        gate_pass = self._create_pass()
        self._submit_and_approve(gate_pass)
        self._gate_out(gate_pass)
        motor = gate_pass.items.get(item_name="Gear Motor 3HP")
        bearing = gate_pass.items.get(item_name="Bearing 6205")

        first = self.client.post(
            self._action_url(gate_pass, "record-return"),
            {"lines": [{"pass_item": motor.id, "quantity_returned": "1.000"}]},
            format="json",
        )
        self.assertEqual(first.status_code, status.HTTP_200_OK, first.data)
        gate_pass.refresh_from_db()
        motor.refresh_from_db()
        self.assertEqual(gate_pass.status, ReturnableStatus.PARTIALLY_RETURNED)
        self.assertEqual(motor.quantity_returned, Decimal("1.000"))
        self.assertEqual(motor.pending_return_qty, Decimal("1.000"))

        second = self.client.post(
            self._action_url(gate_pass, "record-return"),
            {
                "lines": [
                    {"pass_item": motor.id, "quantity_returned": "1.000"},
                    {"pass_item": bearing.id, "quantity_returned": "4.000"},
                ]
            },
            format="json",
        )
        self.assertEqual(second.status_code, status.HTTP_200_OK, second.data)
        gate_pass.refresh_from_db()
        self.assertEqual(gate_pass.status, ReturnableStatus.RETURNED)
        self.assertEqual(gate_pass.return_events.count(), 2)
        self.assertEqual(gate_pass.pending_return_qty, Decimal("0.000"))

    def test_over_return_is_rejected(self, _notify):
        gate_pass = self._create_pass()
        self._submit_and_approve(gate_pass)
        self._gate_out(gate_pass)
        motor = gate_pass.items.get(item_name="Gear Motor 3HP")

        response = self.client.post(
            self._action_url(gate_pass, "record-return"),
            {"lines": [{"pass_item": motor.id, "quantity_returned": "3.000"}]},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        motor.refresh_from_db()
        self.assertEqual(motor.quantity_returned, Decimal("0.000"))

    # -- approval stage ---------------------------------------------------

    def test_submit_parks_the_pass_with_the_approver_not_the_gate(self, notify):
        gate_pass = self._create_pass()
        self.client.post(self._action_url(gate_pass, "submit"))
        gate_pass.refresh_from_db()

        self.assertEqual(gate_pass.status, ReturnableStatus.PENDING_APPROVAL)
        self.assertEqual(gate_pass.submitted_by_id, self.user.id)
        self.assertIsNone(gate_pass.approved_at)
        notify.notify_submitted.assert_called_once()
        notify.notify_approved.assert_not_called()

    def test_approver_rejection_sends_the_pass_back_to_draft(self, notify):
        gate_pass = self._create_pass()
        self.client.post(self._action_url(gate_pass, "submit"))

        self._as(self.approver)
        response = self.client.post(
            self._action_url(gate_pass, "reject"),
            {"reason": "Estimated value looks wrong, recheck with stores."},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        gate_pass.refresh_from_db()
        self.assertEqual(gate_pass.status, ReturnableStatus.DRAFT)
        self.assertIn("Estimated value", gate_pass.approval_rejected_reason)
        self.assertIsNone(gate_pass.submitted_at)
        notify.notify_approval_rejected.assert_called_once()

    def test_approver_rejection_requires_a_reason(self, _notify):
        gate_pass = self._create_pass()
        self.client.post(self._action_url(gate_pass, "submit"))

        self._as(self.approver)
        response = self.client.post(self._action_url(gate_pass, "reject"), {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_nobody_can_approve_their_own_submission(self, _notify):
        gate_pass = self._create_pass()
        self.client.post(self._action_url(gate_pass, "submit"))

        # self.user submitted it and holds every permission, including approval.
        response = self.client.post(self._action_url(gate_pass, "approve"), {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        gate_pass.refresh_from_db()
        self.assertEqual(gate_pass.status, ReturnableStatus.PENDING_APPROVAL)

    def test_approve_denied_without_permission(self, _notify):
        gate_pass = self._create_pass()
        self.client.post(self._action_url(gate_pass, "submit"))

        clerk = User.objects.create_user(
            email="clerk@acme.test", password="pw", full_name="Clerk", employee_code="E006"
        )
        UserCompany.objects.create(user=clerk, company=self.company, role=self.role, is_default=True)
        self._grant(clerk, ["can_view_returnable_gatepass"])
        self._as(clerk)

        response = self.client.post(self._action_url(gate_pass, "approve"), {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_a_draft_pass_cannot_be_approved(self, _notify):
        gate_pass = self._create_pass()
        self._as(self.approver)
        response = self.client.post(self._action_url(gate_pass, "approve"), {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    # -- hand-carried gate out --------------------------------------------

    def test_hand_carried_gate_out_needs_no_vehicle(self, _notify):
        gate_pass = self._create_pass()
        self._submit_and_approve(gate_pass)

        response = self.client.post(
            self._action_url(gate_pass, "gate-out"),
            {"is_hand_carried": True, "carried_by_name": "Ramesh Kumar"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        gate_pass.refresh_from_db()
        self.assertEqual(gate_pass.status, ReturnableStatus.OUT)
        self.assertTrue(gate_pass.is_hand_carried)
        self.assertEqual(gate_pass.carried_by_name, "Ramesh Kumar")
        self.assertIsNone(gate_pass.vehicle_id)
        self.assertEqual(gate_pass.vehicle_number_manual, "")

    def test_hand_carried_gate_out_requires_the_carrier_name(self, _notify):
        gate_pass = self._create_pass()
        self._submit_and_approve(gate_pass)

        response = self.client.post(
            self._action_url(gate_pass, "gate-out"),
            {"is_hand_carried": True},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("carried_by_name", response.data)

    def test_hand_carried_gate_out_discards_any_vehicle_sent_with_it(self, _notify):
        gate_pass = self._create_pass()
        self._submit_and_approve(gate_pass)

        response = self.client.post(
            self._action_url(gate_pass, "gate-out"),
            {
                "is_hand_carried": True,
                "carried_by_name": "Ramesh Kumar",
                "vehicle_number_manual": "GJ01AB1234",
                "driver_name_manual": "Someone",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        gate_pass.refresh_from_db()
        self.assertEqual(gate_pass.vehicle_number_manual, "")
        self.assertEqual(gate_pass.driver_name_manual, "")

    def test_vehicle_gate_out_still_requires_a_vehicle(self, _notify):
        gate_pass = self._create_pass()
        self._submit_and_approve(gate_pass)

        response = self.client.post(
            self._action_url(gate_pass, "gate-out"),
            {"is_hand_carried": False, "driver_name_manual": "Ramesh"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("vehicle", response.data)

    def test_gate_out_writes_a_timeline_entry_naming_the_carrier(self, _notify):
        gate_pass = self._create_pass()
        self._submit_and_approve(gate_pass)
        self.client.post(
            self._action_url(gate_pass, "gate-out"),
            {"is_hand_carried": True, "carried_by_name": "Ramesh Kumar"},
            format="json",
        )

        timeline = self.client.get(self._action_url(gate_pass, "timeline"))
        self.assertEqual(timeline.status_code, status.HTTP_200_OK)
        gate_out_rows = [row for row in timeline.data if row["action"] == "GATE_OUT"]
        self.assertEqual(len(gate_out_rows), 1)
        self.assertIn("Hand-carried out by Ramesh Kumar", gate_out_rows[0]["note"])

    # -- gate rejection ---------------------------------------------------

    def test_reject_at_gate_returns_pass_to_draft(self, notify):
        gate_pass = self._create_pass()
        self._submit_and_approve(gate_pass)

        response = self.client.post(
            self._action_url(gate_pass, "reject-at-gate"),
            {"reason": "Only 1 motor physically presented, pass says 2."},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        gate_pass.refresh_from_db()
        self.assertEqual(gate_pass.status, ReturnableStatus.DRAFT)
        self.assertIn("Only 1 motor", gate_pass.rejected_reason)
        self.assertIsNone(gate_pass.submitted_at)
        notify.notify_rejected_at_gate.assert_called_once()

    # -- short close ------------------------------------------------------

    def test_short_close_requires_reason_and_keeps_pending_quantity(self, _notify):
        gate_pass = self._create_pass()
        self._submit_and_approve(gate_pass)
        self._gate_out(gate_pass)

        missing_reason = self.client.post(self._action_url(gate_pass, "short-close"), {}, format="json")
        self.assertEqual(missing_reason.status_code, status.HTTP_400_BAD_REQUEST)

        response = self.client.post(
            self._action_url(gate_pass, "short-close"),
            {"reason": "Vendor scrapped the motor."},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        gate_pass.refresh_from_db()
        self.assertEqual(gate_pass.status, ReturnableStatus.CLOSED)
        self.assertEqual(gate_pass.pending_return_qty, Decimal("6.000"))
        self.assertFalse(gate_pass.is_overdue)

    def test_short_close_denied_without_permission(self, _notify):
        gate_pass = self._create_pass()
        self._submit_and_approve(gate_pass)
        self._gate_out(gate_pass)

        weak = User.objects.create_user(
            email="weak@acme.test", password="pw", full_name="Weak", employee_code="E002"
        )
        UserCompany.objects.create(user=weak, company=self.company, role=self.role, is_default=True)
        self._grant(weak, ["can_view_returnable_gatepass"])
        self.client.force_authenticate(User.objects.get(pk=weak.pk))

        response = self.client.post(
            self._action_url(gate_pass, "short-close"), {"reason": "nope"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    # -- edit guards ------------------------------------------------------

    def test_cannot_edit_pass_once_submitted(self, _notify):
        gate_pass = self._create_pass()
        self.client.post(self._action_url(gate_pass, "submit"))

        response = self.client.patch(
            reverse("returnable-gatepass-detail", args=[gate_pass.pk]),
            {"party_name": "Someone Else"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_cannot_cancel_once_items_started_returning(self, _notify):
        gate_pass = self._create_pass()
        self._submit_and_approve(gate_pass)
        self._gate_out(gate_pass)
        motor = gate_pass.items.get(item_name="Gear Motor 3HP")
        self.client.post(
            self._action_url(gate_pass, "record-return"),
            {"lines": [{"pass_item": motor.id, "quantity_returned": "1.000"}]},
            format="json",
        )

        response = self.client.post(
            self._action_url(gate_pass, "cancel"), {"reason": "changed mind"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    # -- queues -----------------------------------------------------------

    def test_gate_queues_only_show_relevant_passes(self, _notify):
        awaiting_approval = self._create_pass()
        self.client.post(self._action_url(awaiting_approval, "submit"))

        waiting = self._create_pass()
        self._submit_and_approve(waiting)

        gone = self._create_pass()
        self._submit_and_approve(gone)
        self._gate_out(gone)

        approval_queue = self.client.get(reverse("returnable-gatepass-pending-approval"))
        self.assertEqual([row["id"] for row in approval_queue.data], [awaiting_approval.id])

        # An unapproved pass must never reach the gate's queue.
        out_queue = self.client.get(reverse("returnable-gatepass-pending-gate-out"))
        self.assertEqual([row["id"] for row in out_queue.data], [waiting.id])

        in_queue = self.client.get(reverse("returnable-gatepass-pending-gate-in"))
        self.assertEqual([row["id"] for row in in_queue.data], [gone.id])

    # -- non-returnable passes --------------------------------------------

    def _non_returnable_payload(self, **overrides):
        payload = {
            "is_returnable": False,
            "purpose": "OTHER",
            "recipient_name": "Suresh Patel",
            "recipient_contact": "9876543210",
            "recipient_department": "Production",
            "items_input": [
                {"item_code": "RM0001", "item_name": "Crude Palm Oil", "quantity_out": "5.000", "uom": "KG"},
            ],
        }
        payload.update(overrides)
        return payload

    def _create_non_returnable(self):
        response = self.client.post(
            reverse("returnable-gatepass-list"), self._non_returnable_payload(), format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        return ReturnableGatePass.objects.get(pk=response.data["id"])

    def test_non_returnable_gets_its_own_number_series(self, _notify):
        non_returnable = self._create_non_returnable()
        returnable = self._create_pass()
        year = ReturnableGatePassSequence.current_financial_year()

        self.assertEqual(non_returnable.pass_no, f"NRGP/{year}/000001")
        self.assertEqual(returnable.pass_no, f"RGP/{year}/000001")

    def test_non_returnable_requires_a_recipient(self, _notify):
        response = self.client.post(
            reverse("returnable-gatepass-list"),
            self._non_returnable_payload(recipient_name=""),
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("recipient_name", response.data)

    def test_non_returnable_never_stores_a_return_date(self, _notify):
        response = self.client.post(
            reverse("returnable-gatepass-list"),
            self._non_returnable_payload(expected_return_date="2099-01-01"),
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        gate_pass = ReturnableGatePass.objects.get(pk=response.data["id"])
        self.assertIsNone(gate_pass.expected_return_date)
        self.assertEqual(gate_pass.days_overdue, 0)

    def test_returnable_still_requires_party_and_return_date(self, _notify):
        response = self.client.post(
            reverse("returnable-gatepass-list"),
            self._payload(expected_return_date="", party_name=""),
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_non_returnable_closes_the_moment_it_is_gated_out(self, notify):
        gate_pass = self._create_non_returnable()
        self._submit_and_approve(gate_pass)

        self.assertEqual(self._gate_out(gate_pass).status_code, status.HTTP_200_OK)
        gate_pass.refresh_from_db()

        self.assertEqual(gate_pass.status, ReturnableStatus.CLOSED)
        self.assertIsNotNone(gate_pass.gate_out_at)
        self.assertIsNotNone(gate_pass.closed_at)
        notify.notify_gate_out.assert_called_once()
        notify.notify_closed.assert_called_once()

    def test_non_returnable_stays_out_of_the_gate_in_queue(self, _notify):
        non_returnable = self._create_non_returnable()
        self._submit_and_approve(non_returnable)
        self._gate_out(non_returnable)

        returnable = self._create_pass()
        self._submit_and_approve(returnable)
        self._gate_out(returnable)

        in_queue = self.client.get(reverse("returnable-gatepass-pending-gate-in"))
        self.assertEqual([row["id"] for row in in_queue.data], [returnable.id])

    def test_non_returnable_rejects_return_and_short_close(self, _notify):
        gate_pass = self._create_non_returnable()
        self._submit_and_approve(gate_pass)
        self._gate_out(gate_pass)
        item = gate_pass.items.first()

        recorded = self.client.post(
            self._action_url(gate_pass, "record-return"),
            {"lines": [{"pass_item": item.id, "quantity_returned": "1.000"}]},
            format="json",
        )
        self.assertEqual(recorded.status_code, status.HTTP_400_BAD_REQUEST)

        short_closed = self.client.post(
            self._action_url(gate_pass, "short-close"), {"reason": "no reason"}, format="json"
        )
        self.assertEqual(short_closed.status_code, status.HTTP_400_BAD_REQUEST)

    def test_non_returnable_keeps_issued_by_and_drops_requester(self, _notify):
        """A non-returnable pass is issued, not requested — the two are mutually
        exclusive, and the serializer must clear whichever does not apply."""
        response = self.client.post(
            reverse("returnable-gatepass-list"),
            self._non_returnable_payload(
                issued_by_name="Storekeeper Raju",
                requested_by_name="Should Be Cleared",
                contact_no="9999999999",
            ),
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        gate_pass = ReturnableGatePass.objects.get(pk=response.data["id"])
        self.assertEqual(gate_pass.issued_by_name, "Storekeeper Raju")
        self.assertEqual(gate_pass.requested_by_name, "")
        self.assertEqual(gate_pass.contact_no, "")

    def test_returnable_keeps_requester_and_drops_issued_by(self, _notify):
        response = self.client.post(
            reverse("returnable-gatepass-list"),
            self._payload(
                requested_by_name="Anil Kumar",
                contact_no="9876543210",
                issued_by_name="Should Be Cleared",
            ),
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        gate_pass = ReturnableGatePass.objects.get(pk=response.data["id"])
        self.assertEqual(gate_pass.requested_by_name, "Anil Kumar")
        self.assertEqual(gate_pass.contact_no, "9876543210")
        self.assertEqual(gate_pass.issued_by_name, "")

    def test_a_pass_cannot_switch_type_after_creation(self, _notify):
        gate_pass = self._create_non_returnable()
        response = self.client.patch(
            reverse("returnable-gatepass-detail", args=[gate_pass.pk]),
            {"is_returnable": True},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_list_filters_by_pass_type(self, _notify):
        non_returnable = self._create_non_returnable()
        returnable = self._create_pass()

        only_non = self.client.get(reverse("returnable-gatepass-list"), {"is_returnable": "false"})
        self.assertEqual([row["id"] for row in only_non.data], [non_returnable.id])

        only_returnable = self.client.get(
            reverse("returnable-gatepass-list"), {"is_returnable": "true"}
        )
        self.assertEqual([row["id"] for row in only_returnable.data], [returnable.id])

    # -- company scoping --------------------------------------------------

    def test_company_scoping_hides_other_companies_passes(self, _notify):
        mine = self._create_pass()

        other_company = Company.objects.create(name="Beta Foods", code="BETA")
        other_user = User.objects.create_user(
            email="beta@x.test", password="pw", full_name="Beta", employee_code="E003"
        )
        UserCompany.objects.create(
            user=other_user, company=other_company, role=self.role, is_default=True
        )
        self._grant(other_user, ALL_PERMS)

        self.client.force_authenticate(User.objects.get(pk=other_user.pk))
        self.client.credentials(HTTP_COMPANY_CODE=other_company.code)

        listing = self.client.get(reverse("returnable-gatepass-list"))
        self.assertEqual(listing.data, []) if isinstance(listing.data, list) else None
        self.assertNotIn(mine.id, [row["id"] for row in (listing.data or [])])

        detail = self.client.get(reverse("returnable-gatepass-detail", args=[mine.pk]))
        self.assertEqual(detail.status_code, status.HTTP_404_NOT_FOUND)


class ReturnableJobTests(APITestCase):
    """The two scheduled nudges: due-today and overdue. Both must be idempotent."""

    def setUp(self):
        self.company = Company.objects.create(name="Acme Foods", code="ACME")
        self.role = UserRole.objects.create(name="Maintenance")
        self.user = User.objects.create_user(
            email="dept@acme.test", password="pw", full_name="Dept", employee_code="E010"
        )
        UserCompany.objects.create(
            user=self.user, company=self.company, role=self.role, is_default=True
        )

    def _outstanding_pass(self, expected_return_date):
        gate_pass = ReturnableGatePass.objects.create(
            company=self.company,
            party_name="Sharma Motors",
            expected_return_date=expected_return_date,
            status=ReturnableStatus.OUT,
            created_by=self.user,
            submitted_by=self.user,
        )
        ReturnableGatePassItem.objects.create(
            company=self.company,
            gate_pass=gate_pass,
            line_num=1,
            item_name="Gear Motor",
            quantity_out=Decimal("2.000"),
        )
        return gate_pass

    @patch("returnable_items.jobs.notify", autospec=True)
    def test_due_today_notifies_once(self, notify):
        from .jobs import notify_due_returnables

        gate_pass = self._outstanding_pass(timezone.localdate())

        self.assertEqual(notify_due_returnables(), 1)
        notify.notify_due_today.assert_called_once()
        gate_pass.refresh_from_db()
        self.assertIsNotNone(gate_pass.due_notified_at)

        # Second run is a no-op.
        self.assertEqual(notify_due_returnables(), 0)
        notify.notify_due_today.assert_called_once()

    @patch("returnable_items.jobs.notify", autospec=True)
    def test_overdue_flags_once(self, notify):
        from .jobs import flag_overdue_returnables

        gate_pass = self._outstanding_pass(timezone.localdate() - timedelta(days=3))

        self.assertEqual(flag_overdue_returnables(), 1)
        gate_pass.refresh_from_db()
        self.assertTrue(gate_pass.is_overdue)
        self.assertEqual(gate_pass.days_overdue, 3)
        notify.notify_overdue.assert_called_once()

        self.assertEqual(flag_overdue_returnables(), 0)
        notify.notify_overdue.assert_called_once()

    @patch("returnable_items.jobs.notify", autospec=True)
    def test_returned_pass_is_never_flagged_overdue(self, _notify):
        from .jobs import flag_overdue_returnables

        gate_pass = self._outstanding_pass(timezone.localdate() - timedelta(days=3))
        gate_pass.status = ReturnableStatus.RETURNED
        gate_pass.save(update_fields=["status"])

        self.assertEqual(flag_overdue_returnables(), 0)
        gate_pass.refresh_from_db()
        self.assertFalse(gate_pass.is_overdue)
