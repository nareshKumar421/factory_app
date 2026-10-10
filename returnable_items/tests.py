"""Tests for the Returnable Gate Pass lifecycle.

Every request carries the ``Company-Code`` header, as ``HasCompanyContext``
requires. Notifications are patched out — they are fire-and-forget and hitting
Firebase from a test run is neither possible nor useful.
"""

import json
import tempfile
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from company.models import Company, UserCompany, UserRole
from maintenance.models import MaintenanceSpare, SpareCategory, SpareMovement

from .constants import ReturnableStatus
from .models import ReturnableGatePass, ReturnableGatePassItem, ReturnableGatePassSequence

User = get_user_model()

#: Uploads land here, never in the repo's shared media/ folder.
TEMP_MEDIA = tempfile.mkdtemp(prefix="returnable-test-media-")

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


def _upload(name):
    """A small file under ``name``. The server judges a photo by its file name."""
    content_type = "application/pdf" if name.lower().endswith(".pdf") else "image/jpeg"
    return SimpleUploadedFile(name, b"not really an image", content_type=content_type)


@override_settings(MEDIA_ROOT=TEMP_MEDIA)
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

    def _create_pass(self, photo=True):
        url = reverse("returnable-gatepass-list")
        response = self.client.post(url, self._payload(), format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        gate_pass = ReturnableGatePass.objects.get(pk=response.data["id"])
        if photo:
            self._attach(gate_pass)
        return gate_pass

    def _attach(self, gate_pass, name="gear-motor.jpg", doc_type="PHOTO"):
        """Upload a file to the pass the way the form does, right after saving it."""
        response = self.client.post(
            reverse("returnable-attachment-list"),
            {"gate_pass": gate_pass.pk, "file": _upload(name), "doc_type": doc_type},
            format="multipart",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        return response

    def _record_return(self, gate_pass, body, photos=("back-at-gate.jpg",)):
        """Record a return trip the way the gate screen does: JSON in "data", photos beside it."""
        return self.client.post(
            self._action_url(gate_pass, "record-return"),
            {"data": json.dumps(body), "attachments": [_upload(name) for name in photos]},
            format="multipart",
        )

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
        response = self._record_return(
            gate_pass,
            {"vehicle_number_manual": "GJ05XY9999", "lines": lines},
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

        first = self._record_return(
            gate_pass,
            {"lines": [{"pass_item": motor.id, "quantity_returned": "1.000"}]},
        )
        self.assertEqual(first.status_code, status.HTTP_200_OK, first.data)
        gate_pass.refresh_from_db()
        motor.refresh_from_db()
        self.assertEqual(gate_pass.status, ReturnableStatus.PARTIALLY_RETURNED)
        self.assertEqual(motor.quantity_returned, Decimal("1.000"))
        self.assertEqual(motor.pending_return_qty, Decimal("1.000"))

        second = self._record_return(
            gate_pass,
            {
                "lines": [
                    {"pass_item": motor.id, "quantity_returned": "1.000"},
                    {"pass_item": bearing.id, "quantity_returned": "4.000"},
                ]
            },
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

        response = self._record_return(
            gate_pass,
            {"lines": [{"pass_item": motor.id, "quantity_returned": "3.000"}]},
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

    # -- photos: the gate identifies the material by its picture ----------

    def _out_pass(self):
        gate_pass = self._create_pass()
        self._submit_and_approve(gate_pass)
        self._gate_out(gate_pass)
        return gate_pass

    def test_submit_requires_a_photo_of_the_material(self, notify):
        gate_pass = self._create_pass(photo=False)

        response = self.client.post(self._action_url(gate_pass, "submit"))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("photo", response.data["detail"])
        gate_pass.refresh_from_db()
        self.assertEqual(gate_pass.status, ReturnableStatus.DRAFT)
        notify.notify_submitted.assert_not_called()

    def test_a_non_returnable_pass_needs_a_photo_too(self, _notify):
        gate_pass = self._create_non_returnable(photo=False)

        response = self.client.post(self._action_url(gate_pass, "submit"))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_challan_on_its_own_is_not_a_photo(self, _notify):
        gate_pass = self._create_pass(photo=False)
        self._attach(gate_pass, "challan.pdf", doc_type="CHALLAN")

        response = self.client.post(self._action_url(gate_pass, "submit"))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_photo_filed_as_a_challan_still_counts(self, _notify):
        """The form files everything as "Delivery Challan" by default; the file decides."""
        gate_pass = self._create_pass(photo=False)
        self._attach(gate_pass, "IMG_2041.JPG", doc_type="CHALLAN")

        response = self.client.post(self._action_url(gate_pass, "submit"))
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_approver_cannot_sign_off_a_pass_without_a_photo(self, _notify):
        """A pass submitted before photos were required can still be in the queue."""
        gate_pass = self._create_pass()
        self.client.post(self._action_url(gate_pass, "submit"))
        gate_pass.attachments.all().delete()

        self.assertEqual(self._approve(gate_pass).status_code, status.HTTP_400_BAD_REQUEST)
        gate_pass.refresh_from_db()
        self.assertEqual(gate_pass.status, ReturnableStatus.PENDING_APPROVAL)

        # The approver may edit a pass waiting on them, so they can add it themselves.
        self._as(self.approver)
        self._attach(gate_pass)
        self.assertEqual(self._approve(gate_pass).status_code, status.HTTP_200_OK)

    def test_recording_a_return_requires_a_photo(self, notify):
        gate_pass = self._out_pass()
        motor = gate_pass.items.get(item_name="Gear Motor 3HP")
        lines = [{"pass_item": motor.id, "quantity_returned": "1.000"}]

        for response in (
            self._record_return(gate_pass, {"lines": lines}, photos=()),
            # What the gate screen sent before photos were required.
            self.client.post(
                self._action_url(gate_pass, "record-return"), {"lines": lines}, format="json"
            ),
        ):
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
            self.assertIn("photo", response.data["detail"])

        motor.refresh_from_db()
        self.assertEqual(motor.quantity_returned, Decimal("0.000"))
        self.assertFalse(gate_pass.return_events.exists())
        notify.notify_return_recorded.assert_not_called()

    def test_a_return_takes_photos_only(self, _notify):
        gate_pass = self._out_pass()
        motor = gate_pass.items.get(item_name="Gear Motor 3HP")

        response = self._record_return(
            gate_pass,
            {"lines": [{"pass_item": motor.id, "quantity_returned": "1.000"}]},
            photos=("back-at-gate.jpg", "vendor-challan.pdf"),
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("vendor-challan.pdf", response.data["detail"])
        self.assertFalse(gate_pass.return_events.exists())

    def test_return_photos_are_kept_on_their_trip(self, _notify):
        gate_pass = self._out_pass()
        motor = gate_pass.items.get(item_name="Gear Motor 3HP")

        response = self._record_return(
            gate_pass,
            {"lines": [{"pass_item": motor.id, "quantity_returned": "1.000"}]},
            photos=("front.jpg", "nameplate.png"),
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        event = gate_pass.return_events.get()
        self.assertEqual(event.attachments.count(), 2)
        trip = response.data["return_events"][0]
        self.assertEqual(len(trip["attachments"]), 2)
        self.assertTrue(trip["attachments"][0]["file"].startswith("http://testserver/media/"))
        self.assertIn("/returnable-items/returns/", trip["attachments"][0]["file"])
        # The department's photo of what went out stays separate from these.
        self.assertEqual(len(response.data["attachments"]), 1)
        self.assertIn("2 photo(s)", gate_pass.logs.get(action="RETURN_RECORDED").note)

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

    # -- draft / send split ----------------------------------------------

    def _in_group(self, email, code, group_name):
        """A user whose only rights come from one auth role group."""
        user = User.objects.create_user(
            email=email, password="pw", full_name=group_name, employee_code=code
        )
        UserCompany.objects.create(user=user, company=self.company, role=self.role, is_default=True)
        user.groups.add(Group.objects.get(name=group_name))
        return user

    def test_drafter_group_raises_a_pass_but_cannot_send_it_for_approval(self, _notify):
        self._as(self._in_group("drafter@acme.test", "E020", "returnable_drafter"))

        created = self.client.post(reverse("returnable-gatepass-list"), self._payload(), format="json")
        self.assertEqual(created.status_code, status.HTTP_201_CREATED, created.data)
        gate_pass = ReturnableGatePass.objects.get(pk=created.data["id"])

        submit = self.client.post(self._action_url(gate_pass, "submit"))
        self.assertEqual(submit.status_code, status.HTTP_403_FORBIDDEN)
        gate_pass.refresh_from_db()
        self.assertEqual(gate_pass.status, ReturnableStatus.DRAFT)

    def test_sender_group_submits_a_draft_but_cannot_raise_one(self, _notify):
        gate_pass = self._create_pass()
        self._as(self._in_group("sender@acme.test", "E021", "returnable_sender"))

        blocked = self.client.post(reverse("returnable-gatepass-list"), self._payload(), format="json")
        self.assertEqual(blocked.status_code, status.HTTP_403_FORBIDDEN)

        submit = self.client.post(self._action_url(gate_pass, "submit"))
        self.assertEqual(submit.status_code, status.HTTP_200_OK, submit.data)
        gate_pass.refresh_from_db()
        self.assertEqual(gate_pass.status, ReturnableStatus.PENDING_APPROVAL)

    def test_drafter_and_sender_work_the_same_on_a_non_returnable_pass(self, _notify):
        """One model, one permission set — the RGP/NRGP flag changes nothing here."""
        self._as(self._in_group("nrgp-draft@acme.test", "E022", "returnable_drafter"))
        created = self.client.post(
            reverse("returnable-gatepass-list"),
            self._payload(is_returnable=False, expected_return_date=None, recipient_name="Ravi"),
            format="json",
        )
        self.assertEqual(created.status_code, status.HTTP_201_CREATED, created.data)
        gate_pass = ReturnableGatePass.objects.get(pk=created.data["id"])
        self.assertFalse(gate_pass.is_returnable)
        # The drafter attaches the photo; the sender cannot, but needs it there.
        self._attach(gate_pass)
        self.assertEqual(
            self.client.post(self._action_url(gate_pass, "submit")).status_code,
            status.HTTP_403_FORBIDDEN,
        )

        self._as(self._in_group("nrgp-send@acme.test", "E023", "returnable_sender"))
        submit = self.client.post(self._action_url(gate_pass, "submit"))
        self.assertEqual(submit.status_code, status.HTTP_200_OK, submit.data)
        gate_pass.refresh_from_db()
        self.assertEqual(gate_pass.status, ReturnableStatus.PENDING_APPROVAL)

    def test_existing_requester_group_still_does_both(self, _notify):
        """The split must not narrow the roles that already shipped."""
        self._as(self._in_group("req@acme.test", "E024", "returnable_requester"))

        created = self.client.post(reverse("returnable-gatepass-list"), self._payload(), format="json")
        self.assertEqual(created.status_code, status.HTTP_201_CREATED, created.data)
        gate_pass = ReturnableGatePass.objects.get(pk=created.data["id"])
        self._attach(gate_pass)

        submit = self.client.post(self._action_url(gate_pass, "submit"))
        self.assertEqual(submit.status_code, status.HTTP_200_OK, submit.data)

    # -- edit guards ------------------------------------------------------

    def _department_only_user(self):
        """A clerk who can raise and submit passes but not approve them."""
        clerk = User.objects.create_user(
            email="clerk@acme.test", password="pw", full_name="Clerk", employee_code="E007"
        )
        UserCompany.objects.create(user=clerk, company=self.company, role=self.role, is_default=True)
        self._grant(
            clerk,
            [
                "can_view_returnable_gatepass",
                "can_manage_returnable_gatepass",
                "can_submit_returnable_gatepass",
            ],
        )
        return clerk

    def test_department_cannot_edit_pass_once_submitted(self, _notify):
        gate_pass = self._create_pass()
        self.client.post(self._action_url(gate_pass, "submit"))
        self._as(self._department_only_user())

        response = self.client.patch(
            reverse("returnable-gatepass-detail", args=[gate_pass.pk]),
            {"party_name": "Someone Else"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        gate_pass.refresh_from_db()
        self.assertEqual(gate_pass.party_name, "Sharma Motors")

    def test_approver_can_edit_a_pass_waiting_on_them(self, _notify):
        gate_pass = self._create_pass()
        self.client.post(self._action_url(gate_pass, "submit"))
        self._as(self.approver)

        response = self.client.patch(
            reverse("returnable-gatepass-detail", args=[gate_pass.pk]),
            {
                "party_name": "Verma Engineering",
                "purpose": "JOB_WORK",
                "items_input": [
                    {"item_name": "Gear Motor 3HP", "quantity_out": "1.000", "uom": "NOS"},
                ],
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        gate_pass.refresh_from_db()
        self.assertEqual(gate_pass.party_name, "Verma Engineering")
        self.assertEqual(gate_pass.purpose, "JOB_WORK")
        self.assertEqual(gate_pass.items.count(), 1)
        # Editing must not move the pass out of the approver's own queue.
        self.assertEqual(gate_pass.status, ReturnableStatus.PENDING_APPROVAL)

    def test_approver_edit_is_written_to_the_timeline(self, _notify):
        gate_pass = self._create_pass()
        self.client.post(self._action_url(gate_pass, "submit"))
        self._as(self.approver)

        self.client.patch(
            reverse("returnable-gatepass-detail", args=[gate_pass.pk]),
            {"party_name": "Verma Engineering"},
            format="json",
        )

        entry = gate_pass.logs.filter(action="UPDATED").first()
        self.assertIsNotNone(entry)
        self.assertEqual(entry.actor_id, self.approver.id)

    def test_approver_switching_pass_type_renumbers_it(self, _notify):
        gate_pass = self._create_pass()
        self.client.post(self._action_url(gate_pass, "submit"))
        original_pass_no = gate_pass.pass_no
        self._as(self.approver)

        response = self.client.patch(
            reverse("returnable-gatepass-detail", args=[gate_pass.pk]),
            {"is_returnable": False, "recipient_name": "Suresh Patel"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        gate_pass.refresh_from_db()
        year = ReturnableGatePassSequence.current_financial_year()
        self.assertFalse(gate_pass.is_returnable)
        self.assertEqual(gate_pass.pass_no, f"NRGP/{year}/000001")
        self.assertNotEqual(gate_pass.pass_no, original_pass_no)
        # Nothing is coming back now, so the returnable half must be blank.
        self.assertIsNone(gate_pass.expected_return_date)
        self.assertEqual(gate_pass.recipient_name, "Suresh Patel")
        self.assertIn(original_pass_no, gate_pass.logs.filter(action="UPDATED").first().note)

    def test_approver_switching_to_returnable_clears_the_recipient(self, _notify):
        gate_pass = self._create_non_returnable()
        self.client.post(self._action_url(gate_pass, "submit"))
        self._as(self.approver)

        response = self.client.patch(
            reverse("returnable-gatepass-detail", args=[gate_pass.pk]),
            {
                "is_returnable": True,
                "party_name": "Sharma Motors",
                "expected_return_date": (timezone.localdate() + timedelta(days=5)).isoformat(),
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        gate_pass.refresh_from_db()
        year = ReturnableGatePassSequence.current_financial_year()
        self.assertTrue(gate_pass.is_returnable)
        self.assertEqual(gate_pass.pass_no, f"RGP/{year}/000001")
        self.assertEqual(gate_pass.recipient_name, "")
        self.assertEqual(gate_pass.recipient_department, "")

    def test_dropping_the_first_line_renumbers_the_rest(self, _notify):
        """The survivor moves from line 2 to line 1, which the (pass, line_num)
        unique constraint rejects unless the removal is written first."""
        gate_pass = self._create_pass()
        bearing = gate_pass.items.get(item_name="Bearing 6205")

        response = self.client.patch(
            reverse("returnable-gatepass-detail", args=[gate_pass.pk]),
            {
                "items_input": [
                    {"id": bearing.id, "item_name": "Bearing 6205", "quantity_out": "4.000"},
                ]
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        lines = list(gate_pass.items.all())
        self.assertEqual([line.id for line in lines], [bearing.id])
        self.assertEqual(lines[0].line_num, 1)

    def test_approver_cannot_edit_a_pass_already_sent_to_the_gate(self, _notify):
        gate_pass = self._create_pass()
        self._submit_and_approve(gate_pass)
        self._as(self.approver)

        response = self.client.patch(
            reverse("returnable-gatepass-detail", args=[gate_pass.pk]),
            {"party_name": "Too Late Motors"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_approver_can_take_a_file_off_the_pass_when_saving_it(self, _notify):
        gate_pass = self._create_pass()
        self._attach(gate_pass, name="better-photo.jpg")
        wrong = gate_pass.attachments.get(file__contains="gear-motor")
        storage, name = wrong.file.storage, wrong.file.name
        self.client.post(self._action_url(gate_pass, "submit"))
        self._as(self.approver)

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.patch(
                reverse("returnable-gatepass-detail", args=[gate_pass.pk]),
                {"party_name": "Sharma Motors", "remove_attachments": [wrong.pk]},
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            [attachment["id"] for attachment in response.data["attachments"]],
            [gate_pass.attachments.get().pk],
        )
        self.assertFalse(storage.exists(name))
        # The department finds out from the timeline, so it names the file.
        self.assertIn(
            name.rsplit("/", 1)[-1], gate_pass.logs.filter(action="UPDATED").first().note
        )

    def test_department_can_take_a_file_off_its_own_draft(self, _notify):
        gate_pass = self._create_pass()
        attachment = gate_pass.attachments.get()

        response = self.client.patch(
            reverse("returnable-gatepass-detail", args=[gate_pass.pk]),
            {"remove_attachments": [attachment.pk]},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertFalse(gate_pass.attachments.exists())
        self.assertFalse(gate_pass.logs.filter(action="UPDATED").exists())

    def test_removing_files_leaves_another_pass_alone(self, _notify):
        gate_pass = self._create_pass()
        other = self._create_pass()
        theirs = other.attachments.get()

        response = self.client.patch(
            reverse("returnable-gatepass-detail", args=[gate_pass.pk]),
            {"remove_attachments": [theirs.pk]},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertTrue(other.attachments.filter(pk=theirs.pk).exists())

    def test_files_stay_on_a_pass_already_sent_to_the_gate(self, _notify):
        gate_pass = self._create_pass()
        attachment = gate_pass.attachments.get()
        self._submit_and_approve(gate_pass)
        self._as(self.approver)

        through_the_form = self.client.patch(
            reverse("returnable-gatepass-detail", args=[gate_pass.pk]),
            {"remove_attachments": [attachment.pk]},
            format="json",
        )
        on_its_own = self.client.delete(
            reverse("returnable-attachment-detail", args=[attachment.pk])
        )

        self.assertEqual(through_the_form.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(on_its_own.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue(gate_pass.attachments.filter(pk=attachment.pk).exists())

    def test_cannot_cancel_once_items_started_returning(self, _notify):
        gate_pass = self._create_pass()
        self._submit_and_approve(gate_pass)
        self._gate_out(gate_pass)
        motor = gate_pass.items.get(item_name="Gear Motor 3HP")
        self._record_return(
            gate_pass,
            {"lines": [{"pass_item": motor.id, "quantity_returned": "1.000"}]},
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

    def _create_non_returnable(self, photo=True):
        response = self.client.post(
            reverse("returnable-gatepass-list"), self._non_returnable_payload(), format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        gate_pass = ReturnableGatePass.objects.get(pk=response.data["id"])
        if photo:
            self._attach(gate_pass)
        return gate_pass

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

        recorded = self._record_return(
            gate_pass,
            {"lines": [{"pass_item": item.id, "quantity_returned": "1.000"}]},
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

    def test_the_department_cannot_switch_a_draft_pass_type(self, _notify):
        """Only the approver may change the type, and only while the pass is with
        them — see ``test_approver_switching_pass_type_renumbers_it``."""
        gate_pass = self._create_non_returnable()
        response = self.client.patch(
            reverse("returnable-gatepass-detail", args=[gate_pass.pk]),
            {"is_returnable": True},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        gate_pass.refresh_from_db()
        self.assertFalse(gate_pass.is_returnable)

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

    def test_two_companies_can_share_the_same_pass_number(self, _notify):
        """Regression: pass_no is unique per company, not globally. Before the
        fix, the second company's first pass collided on a global unique
        constraint and returned a 500."""
        mine = self._create_pass()

        other_company = Company.objects.create(name="Gamma Foods", code="GAMMA")
        other_user = User.objects.create_user(
            email="gamma@x.test", password="pw", full_name="Gamma", employee_code="E007"
        )
        UserCompany.objects.create(
            user=other_user, company=other_company, role=self.role, is_default=True
        )
        self._grant(other_user, ALL_PERMS)
        self.client.force_authenticate(User.objects.get(pk=other_user.pk))
        self.client.credentials(HTTP_COMPANY_CODE=other_company.code)

        response = self.client.post(
            reverse("returnable-gatepass-list"), self._payload(), format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        theirs = ReturnableGatePass.objects.get(pk=response.data["id"])
        # Same number string, different company, both valid.
        self.assertEqual(theirs.pass_no, mine.pass_no)
        self.assertNotEqual(theirs.company_id, mine.company_id)

    # -- lines picked from Store / Spares ---------------------------------

    def _store_item(self, name="Switch 6 amp", stock="5", company=None):
        company = company or self.company
        category, _ = SpareCategory.objects.get_or_create(company=company, name="General")
        return MaintenanceSpare.objects.create(
            company=company, category=category, name=name, part_number=name.upper(),
            uom="NOS", current_stock=Decimal(stock),
        )

    def _store_pass(self, spare, quantity, returnable=False):
        """A pass whose one line was picked from the store (sent in the wrong unit)."""
        payload = self._payload() if returnable else self._non_returnable_payload()
        payload["items_input"] = [
            {"item_name": spare.name, "spare": spare.id, "quantity_out": quantity, "uom": "BOX"}
        ]
        return self.client.post(reverse("returnable-gatepass-list"), payload, format="json")

    def _approved_store_pass(self, spare, quantity, returnable=False):
        response = self._store_pass(spare, quantity, returnable=returnable)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        gate_pass = ReturnableGatePass.objects.get(pk=response.data["id"])
        self._attach(gate_pass)
        self.assertEqual(self._submit_and_approve(gate_pass).status_code, status.HTTP_200_OK)
        return gate_pass

    def test_store_line_takes_the_store_unit_and_is_not_held_to_the_shelf(self, _notify):
        switch = self._store_item(stock="5")

        # More than the shelf shows is fine until the store's stock is entered...
        more = self._store_pass(switch, "6.000")
        self.assertEqual(more.status_code, status.HTTP_201_CREATED, more.data)
        # ...and refused again once the switch is turned off.
        with patch("maintenance.store_stock.ALLOW_NEGATIVE_STOCK", False):
            refused = self._store_pass(switch, "6.000")
        self.assertEqual(refused.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Only 5 NOS of Switch 6 amp in store.", str(refused.data))

        response = self._store_pass(switch, "3.000")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        line = ReturnableGatePassItem.objects.get(gate_pass_id=response.data["id"])
        self.assertEqual(line.spare_id, switch.id)
        self.assertEqual(line.uom, "NOS")
        # Saving the pass takes nothing: stock moves when it leaves the gate.
        switch.refresh_from_db()
        self.assertEqual(switch.current_stock, Decimal("5.000"))

    def test_store_line_must_be_this_companys_item(self, _notify):
        other = Company.objects.create(name="Other Co", code="OTHER")
        theirs = self._store_item(company=other)
        response = self._store_pass(theirs, "1.000")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_gate_out_takes_store_lines_off_the_shelf(self, _notify):
        switch = self._store_item(stock="5")
        gate_pass = self._approved_store_pass(switch, "3.000")

        self.assertEqual(self._gate_out(gate_pass).status_code, status.HTTP_200_OK)
        switch.refresh_from_db()
        self.assertEqual(switch.current_stock, Decimal("2.000"))
        movement = SpareMovement.objects.get(spare=switch)
        self.assertEqual(movement.movement_type, "ISSUE")
        self.assertEqual(movement.quantity, Decimal("3.000"))
        self.assertEqual(movement.remarks, f"Gate pass {gate_pass.pass_no} (Suresh Patel)")

    def test_gate_out_takes_the_store_below_zero_rather_than_stop_the_gate(self, _notify):
        switch = self._store_item(stock="5")
        first = self._approved_store_pass(switch, "3.000")
        second = self._approved_store_pass(switch, "3.000")
        self.assertEqual(self._gate_out(first).status_code, status.HTTP_200_OK)
        self.assertEqual(self._gate_out(second).status_code, status.HTTP_200_OK)
        switch.refresh_from_db()
        self.assertEqual(switch.current_stock, Decimal("-1.000"))

    def test_gate_out_is_refused_when_short_once_stock_may_not_go_below_zero(self, _notify):
        switch = self._store_item(stock="5")
        # Both passes were raised while 5 were on the shelf.
        first = self._approved_store_pass(switch, "3.000")
        second = self._approved_store_pass(switch, "3.000")
        self.assertEqual(self._gate_out(first).status_code, status.HTTP_200_OK)

        with patch("maintenance.store_stock.ALLOW_NEGATIVE_STOCK", False):
            refused = self._gate_out(second)
        self.assertEqual(refused.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            refused.data["detail"],
            "The store shows only 2 NOS of Switch 6 amp. Ask the store to count it again.",
        )
        second.refresh_from_db()
        self.assertEqual(second.status, ReturnableStatus.PENDING_GATE_OUT)
        switch.refresh_from_db()
        self.assertEqual(switch.current_stock, Decimal("2.000"))
        self.assertEqual(SpareMovement.objects.filter(spare=switch).count(), 1)

    def test_a_returned_store_line_goes_back_on_the_shelf_unless_it_is_scrap(self, _notify):
        tool = self._store_item(name="Welding machine set", stock="2")
        gate_pass = self._approved_store_pass(tool, "2.000", returnable=True)
        self._gate_out(gate_pass)
        line = gate_pass.items.get()

        back = self._record_return(
            gate_pass,
            {"lines": [{"pass_item": line.id, "quantity_returned": "1.000", "return_condition": "REPAIRED"}]},
        )
        self.assertEqual(back.status_code, status.HTTP_200_OK, back.data)
        tool.refresh_from_db()
        self.assertEqual(tool.current_stock, Decimal("1.000"))
        returned = SpareMovement.objects.get(spare=tool, movement_type="RETURN")
        self.assertEqual(returned.remarks, f"Back from gate pass {gate_pass.pass_no} (Repaired)")

        scrap = self._record_return(
            gate_pass,
            {"lines": [{"pass_item": line.id, "quantity_returned": "1.000", "return_condition": "SCRAP"}]},
        )
        self.assertEqual(scrap.status_code, status.HTTP_200_OK, scrap.data)
        tool.refresh_from_db()
        self.assertEqual(tool.current_stock, Decimal("1.000"))

    def test_cancelling_after_gate_out_puts_store_lines_back(self, _notify):
        tool = self._store_item(name="Welding machine set", stock="2")
        gate_pass = self._approved_store_pass(tool, "1.000", returnable=True)
        self._gate_out(gate_pass)

        response = self.client.post(
            self._action_url(gate_pass, "cancel"), {"reason": "raised by mistake"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        tool.refresh_from_db()
        self.assertEqual(tool.current_stock, Decimal("2.000"))
        self.assertEqual(
            SpareMovement.objects.get(spare=tool, movement_type="RETURN").remarks,
            f"Gate pass {gate_pass.pass_no} cancelled",
        )

    def test_store_picker_lists_this_companys_items_whatever_their_stock(self, _notify):
        self._store_item(name="Switch 6 amp", stock="5")
        self._store_item(name="Switch 16 amp", stock="0")
        self._store_item(name="Switch 32 amp", company=Company.objects.create(name="O", code="O"))
        # The department clerk has the gate pass permissions only, none of the Store's.
        response = self.client.get(reverse("returnable-store-items"), {"search": "switch"})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual([row["name"] for row in response.data], ["Switch 16 amp", "Switch 6 amp"])
        self.assertEqual(response.data[1]["current_stock"], "5.000")

    def test_store_picker_adds_an_item_the_store_does_not_have(self, _notify):
        url = reverse("returnable-store-items")
        added = self.client.post(url, {"name": "MCB single pole", "uom": "nos"}, format="json")
        self.assertEqual(added.status_code, status.HTTP_201_CREATED, added.data)
        self.assertEqual(added.data["current_stock"], "0.000")
        self.assertEqual(added.data["uom"], "NOS")
        item = MaintenanceSpare.objects.get(pk=added.data["id"])
        self.assertEqual(item.company, self.company)
        self.assertEqual(item.category.name, "General")

        # The same name again is the same item, not a second one.
        again = self.client.post(url, {"name": "mcb single POLE"}, format="json")
        self.assertEqual(again.status_code, status.HTTP_200_OK)
        self.assertEqual(again.data["id"], item.id)

        # A line on it takes the store below zero at gate out.
        gate_pass = self._approved_store_pass(item, "2.000")
        self.assertEqual(self._gate_out(gate_pass).status_code, status.HTTP_200_OK)
        item.refresh_from_db()
        self.assertEqual(item.current_stock, Decimal("-2.000"))

    def test_a_line_cannot_be_edited_directly_once_approved(self, _notify):
        gate_pass = self._create_pass()
        self._submit_and_approve(gate_pass)
        line = gate_pass.items.first()
        response = self.client.patch(
            reverse("returnable-gatepass-item-detail", args=[line.pk]),
            {"quantity_out": "9.000"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        line.refresh_from_db()
        self.assertNotEqual(line.quantity_out, Decimal("9.000"))


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
