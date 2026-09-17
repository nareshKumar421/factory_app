from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from accounts.models import Department
from company.models import Company, UserCompany, UserRole

from .models import LabourRequest, LabourRequestStatus

WORK_DATE = "2026-09-17"
BASE = "/api/v1/labour-request/"

ALL_PERMISSIONS = [
    "can_view_labour_request",
    "can_raise_labour_request",
    "can_decide_labour_request",
]


class LabourRequestTestCase(APITestCase):
    """Shared fixture: one company, two departments, and a user whose rights
    each test grants explicitly."""

    permissions = ALL_PERMISSIONS

    def setUp(self):
        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            email="labour.request@example.com",
            password="testpass",
            full_name="Labour Request User",
            employee_code="LRU001",
        )
        self.company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        self.role = UserRole.objects.create(name="HOD")
        UserCompany.objects.create(
            user=self.user, company=self.company, role=self.role, is_default=True
        )
        if self.permissions:
            self.user.user_permissions.add(
                *Permission.objects.filter(
                    content_type__app_label="labour_request",
                    codename__in=self.permissions,
                )
            )
        self.production = Department.objects.create(name="Production")
        self.packing = Department.objects.create(name="Packing")

        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.headers = {"HTTP_COMPANY_CODE": self.company.code}

    # -- helpers -------------------------------------------------------------

    def raise_request(self, department, count, shift="DAY", note="", date=WORK_DATE):
        return self.client.post(
            f"{BASE}raise/",
            {
                "department": department.id,
                "work_date": date,
                "shift": shift,
                "requested_count": count,
                "note": note,
            },
            format="json",
            **self.headers,
        )

    def decide(self, request_id, decision, **extra):
        return self.client.post(
            f"{BASE}{request_id}/decision/",
            {"decision": decision, **extra},
            format="json",
            **self.headers,
        )


class RaiseRequestTests(LabourRequestTestCase):
    def test_raise_creates_a_pending_request(self):
        response = self.raise_request(self.production, 12, note="Bottling line")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["requested_count"], 12)
        self.assertEqual(response.data["status"], LabourRequestStatus.PENDING)
        self.assertEqual(response.data["department_name"], "Production")
        self.assertEqual(response.data["note"], "Bottling line")
        # Nothing is decided yet, so the effective figure is the ask.
        self.assertEqual(response.data["effective_count"], 12)

    def test_re_raising_the_same_day_and_shift_revises_in_place(self):
        self.raise_request(self.production, 12)
        response = self.raise_request(self.production, 18)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["requested_count"], 18)
        self.assertEqual(LabourRequest.objects.count(), 1)

    def test_day_and_night_are_separate_rows(self):
        self.raise_request(self.production, 12, shift="DAY")
        self.raise_request(self.production, 5, shift="NIGHT")
        self.assertEqual(LabourRequest.objects.count(), 2)

    def test_zero_is_rejected(self):
        response = self.raise_request(self.production, 0)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_day_listing_returns_both_shifts(self):
        self.raise_request(self.production, 12, shift="DAY")
        self.raise_request(self.packing, 5, shift="NIGHT")
        response = self.client.get(f"{BASE}?date={WORK_DATE}", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)

    def test_listing_without_a_date_is_rejected(self):
        response = self.client.get(BASE, **self.headers)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class EditAndDeleteTests(LabourRequestTestCase):
    def test_patch_edits_the_count(self):
        created = self.raise_request(self.production, 12)
        response = self.client.patch(
            f"{BASE}{created.data['id']}/",
            {"requested_count": 20},
            format="json",
            **self.headers,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["requested_count"], 20)

    def test_delete_is_soft_and_restorable(self):
        created = self.raise_request(self.production, 12)
        request_id = created.data["id"]

        deleted = self.client.delete(f"{BASE}{request_id}/", **self.headers)
        self.assertEqual(deleted.status_code, status.HTTP_200_OK)
        self.assertTrue(deleted.data["is_deleted"])
        self.assertTrue(deleted.data["can_restore"])
        self.assertTrue(LabourRequest.objects.filter(id=request_id).exists())

        restored = self.client.post(f"{BASE}{request_id}/restore/", **self.headers)
        self.assertEqual(restored.status_code, status.HTTP_200_OK)
        self.assertFalse(restored.data["is_deleted"])

    def test_re_raising_a_deleted_request_revives_it(self):
        created = self.raise_request(self.production, 12)
        self.client.delete(f"{BASE}{created.data['id']}/", **self.headers)

        response = self.raise_request(self.production, 15)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data["is_deleted"])
        self.assertEqual(response.data["requested_count"], 15)
        self.assertEqual(LabourRequest.objects.count(), 1)

    def test_audit_trail_records_every_action(self):
        created = self.raise_request(self.production, 12)
        request_id = created.data["id"]
        self.client.patch(
            f"{BASE}{request_id}/", {"requested_count": 20}, format="json", **self.headers
        )
        self.client.delete(f"{BASE}{request_id}/", **self.headers)

        response = self.client.get(f"{BASE}{request_id}/audit/", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        actions = [log["action"] for log in response.data]
        self.assertEqual(sorted(actions), ["CREATE", "DELETE", "UPDATE"])


class DecisionTests(LabourRequestTestCase):
    def test_approve_grants_the_full_ask_by_default(self):
        created = self.raise_request(self.production, 12)
        response = self.decide(created.data["id"], LabourRequestStatus.APPROVED)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], LabourRequestStatus.APPROVED)
        self.assertEqual(response.data["approved_count"], 12)
        self.assertEqual(response.data["effective_count"], 12)
        self.assertEqual(response.data["decided_by_name"], "Labour Request User")

    def test_partial_approval_keeps_both_numbers(self):
        created = self.raise_request(self.production, 12)
        response = self.decide(
            created.data["id"], LabourRequestStatus.APPROVED, approved_count=8
        )
        self.assertEqual(response.data["requested_count"], 12)
        self.assertEqual(response.data["approved_count"], 8)
        self.assertEqual(response.data["effective_count"], 8)

    def test_cannot_approve_more_than_was_asked(self):
        created = self.raise_request(self.production, 12)
        response = self.decide(
            created.data["id"], LabourRequestStatus.APPROVED, approved_count=20
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["requested_count"], 12)

    def test_reject_zeroes_the_effective_count(self):
        created = self.raise_request(self.production, 12)
        response = self.decide(
            created.data["id"], LabourRequestStatus.REJECTED, note="No shift running"
        )
        self.assertEqual(response.data["status"], LabourRequestStatus.REJECTED)
        self.assertEqual(response.data["effective_count"], 0)
        self.assertEqual(response.data["decision_note"], "No shift running")

    def test_revising_a_decided_request_sends_it_back_to_pending(self):
        created = self.raise_request(self.production, 12)
        self.decide(created.data["id"], LabourRequestStatus.APPROVED)

        response = self.raise_request(self.production, 15)
        self.assertEqual(response.data["status"], LabourRequestStatus.PENDING)
        self.assertIsNone(response.data["approved_count"])

    def test_patching_the_count_of_a_decided_request_reopens_it(self):
        created = self.raise_request(self.production, 12)
        self.decide(created.data["id"], LabourRequestStatus.APPROVED)

        response = self.client.patch(
            f"{BASE}{created.data['id']}/",
            {"requested_count": 9},
            format="json",
            **self.headers,
        )
        self.assertEqual(response.data["status"], LabourRequestStatus.PENDING)

    def test_patching_only_the_note_keeps_the_decision(self):
        created = self.raise_request(self.production, 12)
        self.decide(created.data["id"], LabourRequestStatus.APPROVED)

        response = self.client.patch(
            f"{BASE}{created.data['id']}/",
            {"note": "Line 2 instead of line 1"},
            format="json",
            **self.headers,
        )
        self.assertEqual(response.data["status"], LabourRequestStatus.APPROVED)
        self.assertEqual(response.data["approved_count"], 12)

    def test_reopen_clears_the_decision(self):
        created = self.raise_request(self.production, 12)
        self.decide(created.data["id"], LabourRequestStatus.APPROVED, approved_count=8)

        response = self.client.post(
            f"{BASE}{created.data['id']}/reopen/", **self.headers
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], LabourRequestStatus.PENDING)
        self.assertIsNone(response.data["approved_count"])
        self.assertIsNone(response.data["decided_by_name"])

    def test_reopen_is_rejected_on_a_pending_request(self):
        created = self.raise_request(self.production, 12)
        response = self.client.post(
            f"{BASE}{created.data['id']}/reopen/", **self.headers
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_deleted_request_cannot_be_decided(self):
        created = self.raise_request(self.production, 12)
        self.client.delete(f"{BASE}{created.data['id']}/", **self.headers)
        response = self.decide(created.data["id"], LabourRequestStatus.APPROVED)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class PermissionTests(LabourRequestTestCase):
    """Each right stands on its own: viewing does not raise, raising does not
    decide, and deciding does not raise."""

    permissions = []

    def grant(self, *codenames):
        self.user.user_permissions.add(
            *Permission.objects.filter(
                content_type__app_label="labour_request", codename__in=codenames
            )
        )
        self.user = get_user_model().objects.get(pk=self.user.pk)
        self.client.force_authenticate(self.user)

    def test_no_rights_cannot_read_the_board(self):
        response = self.client.get(f"{BASE}?date={WORK_DATE}", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_viewer_can_read_but_not_raise(self):
        self.grant("can_view_labour_request")
        self.assertEqual(
            self.client.get(f"{BASE}?date={WORK_DATE}", **self.headers).status_code,
            status.HTTP_200_OK,
        )
        self.assertEqual(
            self.raise_request(self.production, 5).status_code,
            status.HTTP_403_FORBIDDEN,
        )

    def test_requester_cannot_decide(self):
        self.grant("can_raise_labour_request")
        created = self.raise_request(self.production, 5)
        self.assertEqual(created.status_code, status.HTTP_201_CREATED)
        self.assertEqual(
            self.decide(created.data["id"], LabourRequestStatus.APPROVED).status_code,
            status.HTTP_403_FORBIDDEN,
        )

    def test_approver_cannot_raise(self):
        self.grant("can_decide_labour_request")
        self.assertEqual(
            self.raise_request(self.production, 5).status_code,
            status.HTTP_403_FORBIDDEN,
        )

    def test_another_companys_request_is_not_reachable(self):
        self.grant(*ALL_PERMISSIONS)
        created = self.raise_request(self.production, 5)
        other = Company.objects.create(name="Jivo Mart", code="JIVO_MART")
        UserCompany.objects.create(user=self.user, company=other, role=self.role)
        response = self.client.get(
            f"{BASE}{created.data['id']}/audit/", HTTP_COMPANY_CODE=other.code
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
