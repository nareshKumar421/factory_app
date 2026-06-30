from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from rest_framework import status
from rest_framework.test import APITestCase, APIClient

from accounts.models import Department
from company.models import Company, UserCompany, UserRole
from person_gatein.models import Contractor

from .models import LabourGateEntry


WORK_DATE = "2026-06-29"
BASE = "/api/v1/labour-gate/"

PERMISSION_CODENAMES = [
    "view_labourgateentry",
    "can_record_labour_in",
    "can_record_labour_out",
]


class LabourGateAPITests(APITestCase):
    def setUp(self):
        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            email="labour.gate@example.com",
            password="testpass",
            full_name="Labour Gate User",
            employee_code="LGU001",
        )
        self.company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        self.role = UserRole.objects.create(name="Gate")
        UserCompany.objects.create(
            user=self.user, company=self.company, role=self.role, is_default=True
        )
        self.user.user_permissions.add(
            *Permission.objects.filter(
                content_type__app_label="labour_gate",
                codename__in=PERMISSION_CODENAMES,
            )
        )
        self.department = Department.objects.create(name="Production")
        self.contractor = Contractor.objects.create(contractor_name="Gaurav")
        self.other = Contractor.objects.create(contractor_name="SVK")

        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.headers = {"HTTP_COMPANY_CODE": self.company.code}

    def _record_in(self, contractor, count):
        return self.client.post(
            f"{BASE}in/",
            {
                "department": self.department.id,
                "contractor": contractor.id,
                "work_date": WORK_DATE,
                "count_in": count,
            },
            format="json",
            **self.headers,
        )

    def test_record_in_creates_then_upserts(self):
        res = self._record_in(self.contractor, 20)
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertEqual(res.data["count_in"], 20)
        self.assertEqual(res.data["remaining"], 20)

        # Same contractor/date again updates in place (one row per day).
        res = self._record_in(self.contractor, 25)
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.data["count_in"], 25)
        self.assertEqual(LabourGateEntry.objects.count(), 1)

    def test_day_list_returns_entries(self):
        self._record_in(self.contractor, 10)
        self._record_in(self.other, 5)
        res = self.client.get(f"{BASE}?date={WORK_DATE}", **self.headers)
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(len(res.data), 2)

    def test_out_batches_reduce_remaining_and_reject_overcount(self):
        entry_id = self._record_in(self.contractor, 20).data["id"]

        res = self.client.post(f"{BASE}{entry_id}/out/", {"count": 8}, format="json", **self.headers)
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.data["total_out"], 8)
        self.assertEqual(res.data["remaining"], 12)

        res = self.client.post(f"{BASE}{entry_id}/out/", {"count": 7}, format="json", **self.headers)
        self.assertEqual(res.data["total_out"], 15)
        self.assertEqual(res.data["remaining"], 5)

        # Over-count is rejected.
        res = self.client.post(f"{BASE}{entry_id}/out/", {"count": 6}, format="json", **self.headers)
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_undo_out_removes_last_batch(self):
        entry_id = self._record_in(self.contractor, 20).data["id"]
        self.client.post(f"{BASE}{entry_id}/out/", {"count": 8}, format="json", **self.headers)
        self.client.post(f"{BASE}{entry_id}/out/", {"count": 3}, format="json", **self.headers)

        res = self.client.post(f"{BASE}{entry_id}/out/undo/", **self.headers)
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.data["total_out"], 8)
        self.assertEqual(len(res.data["out_batches"]), 1)

    def test_update_in_below_total_out_rejected(self):
        entry_id = self._record_in(self.contractor, 20).data["id"]
        self.client.post(f"{BASE}{entry_id}/out/", {"count": 12}, format="json", **self.headers)

        res = self.client.patch(f"{BASE}{entry_id}/", {"count_in": 10}, format="json", **self.headers)
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

        res = self.client.patch(f"{BASE}{entry_id}/", {"count_in": 15}, format="json", **self.headers)
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.data["count_in"], 15)

    def test_delete_blocked_after_out(self):
        entry_id = self._record_in(self.contractor, 20).data["id"]
        self.client.post(f"{BASE}{entry_id}/out/", {"count": 5}, format="json", **self.headers)

        res = self.client.delete(f"{BASE}{entry_id}/", **self.headers)
        self.assertEqual(res.status_code, status.HTTP_409_CONFLICT)

        # A clean entry can be deleted.
        clean_id = self._record_in(self.other, 4).data["id"]
        res = self.client.delete(f"{BASE}{clean_id}/", **self.headers)
        self.assertEqual(res.status_code, status.HTTP_204_NO_CONTENT)

    def test_permission_required(self):
        self.user.user_permissions.clear()
        self.user = get_user_model().objects.get(pk=self.user.pk)  # refresh perm cache
        client = APIClient()
        client.force_authenticate(self.user)
        res = client.post(
            f"{BASE}in/",
            {"contractor": self.contractor.id, "work_date": WORK_DATE, "count_in": 5},
            format="json",
            **self.headers,
        )
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
