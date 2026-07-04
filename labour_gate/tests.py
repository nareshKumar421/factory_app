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
    "can_allocate_labour_department",
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
        # Gate Labour In (no department) — the primary row that holds out batches.
        return self.client.post(
            f"{BASE}in/",
            {"contractor": contractor.id, "work_date": WORK_DATE, "count_in": count},
            format="json",
            **self.headers,
        )

    def _record_dept(self, contractor, department, count):
        return self.client.post(
            f"{BASE}in/",
            {
                "department": department.id,
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

    def test_gate_entry_without_department_coexists_with_department_entry(self):
        # Gate Labour In: contractor total, no department.
        res = self.client.post(
            f"{BASE}in/",
            {"contractor": self.contractor.id, "work_date": WORK_DATE, "count_in": 15},
            format="json",
            **self.headers,
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertIsNone(res.data["department"])

        # Labour module: same contractor/date but with a department -> separate row.
        res2 = self.client.post(
            f"{BASE}in/",
            {
                "department": self.department.id,
                "contractor": self.contractor.id,
                "work_date": WORK_DATE,
                "count_in": 10,
            },
            format="json",
            **self.headers,
        )
        self.assertEqual(res2.status_code, status.HTTP_201_CREATED)
        self.assertEqual(res2.data["department"], self.department.id)
        self.assertEqual(
            LabourGateEntry.objects.filter(
                contractor=self.contractor, work_date=WORK_DATE
            ).count(),
            2,
        )

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

        # A clean entry can be (soft) deleted.
        clean_id = self._record_in(self.other, 4).data["id"]
        res = self.client.delete(f"{BASE}{clean_id}/", **self.headers)
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertFalse(LabourGateEntry.objects.get(id=clean_id).is_active)

    def test_soft_delete_preserves_row_and_reactivates_on_readd(self):
        # Gate intake first (a department split requires it).
        self._record_in(self.contractor, 10)
        res = self._record_dept(self.contractor, self.department, 10)
        entry_id = res.data["id"]

        # Soft delete keeps the row, records who/when, flags it.
        res = self.client.delete(f"{BASE}{entry_id}/", **self.headers)
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertTrue(res.data["is_deleted"])
        self.assertIsNotNone(res.data["deleted_at"])
        obj = LabourGateEntry.objects.get(id=entry_id)
        self.assertFalse(obj.is_active)
        self.assertIsNotNone(obj.deleted_by)

        # The day-list still returns the deleted row, flagged.
        day = self.client.get(f"{BASE}?date={WORK_DATE}", **self.headers).data
        deleted = [d for d in day if d["id"] == entry_id]
        self.assertEqual(len(deleted), 1)
        self.assertTrue(deleted[0]["is_deleted"])

        # Re-adding the same dept+contractor+date reactivates the row in place.
        res = self._record_dept(self.contractor, self.department, 7)
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertFalse(res.data["is_deleted"])
        self.assertEqual(res.data["count_in"], 7)
        self.assertEqual(
            LabourGateEntry.objects.filter(
                department=self.department, contractor=self.contractor, work_date=WORK_DATE
            ).count(),
            1,
        )

    def test_restore_within_window_then_blocked_after(self):
        from datetime import timedelta

        from django.utils import timezone

        self._record_in(self.contractor, 10)
        res = self._record_dept(self.contractor, self.department, 10)
        entry_id = res.data["id"]
        self.client.delete(f"{BASE}{entry_id}/", **self.headers)

        # Restore within the grace window reactivates the row.
        res = self.client.post(f"{BASE}{entry_id}/restore/", **self.headers)
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertFalse(res.data["is_deleted"])
        self.assertTrue(LabourGateEntry.objects.get(id=entry_id).is_active)

        # Delete again, age it past the window: restore is rejected.
        self.client.delete(f"{BASE}{entry_id}/", **self.headers)
        LabourGateEntry.objects.filter(id=entry_id).update(
            deleted_at=timezone.now() - timedelta(minutes=11)
        )
        res = self.client.post(f"{BASE}{entry_id}/restore/", **self.headers)
        self.assertEqual(res.status_code, status.HTTP_409_CONFLICT)
        self.assertFalse(LabourGateEntry.objects.get(id=entry_id).is_active)

    def test_department_split_cannot_exceed_gate_intake(self):
        # Gate intake of 10 for the contractor.
        self.client.post(
            f"{BASE}in/",
            {"contractor": self.contractor.id, "work_date": WORK_DATE, "count_in": 10},
            format="json",
            **self.headers,
        )
        # Allocate 8 to one department.
        res = self.client.post(
            f"{BASE}in/",
            {
                "department": self.department.id,
                "contractor": self.contractor.id,
                "work_date": WORK_DATE,
                "count_in": 8,
            },
            format="json",
            **self.headers,
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)

        # A second department wants 3 — only 2 left — blocked with the breakdown.
        other = Department.objects.create(name="Packing")
        res = self.client.post(
            f"{BASE}in/",
            {
                "department": other.id,
                "contractor": self.contractor.id,
                "work_date": WORK_DATE,
                "count_in": 3,
            },
            format="json",
            **self.headers,
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(res.data["entered"], 10)
        self.assertEqual(res.data["used"], 8)
        self.assertEqual(res.data["left"], 2)

        # Exactly the 2 that are left is allowed.
        res = self.client.post(
            f"{BASE}in/",
            {
                "department": other.id,
                "contractor": self.contractor.id,
                "work_date": WORK_DATE,
                "count_in": 2,
            },
            format="json",
            **self.headers,
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)

    def test_undo_blocked_after_grace_window(self):
        from datetime import timedelta

        from django.utils import timezone

        from .models import LabourGateOutBatch

        entry_id = self._record_in(self.contractor, 20).data["id"]
        self.client.post(f"{BASE}{entry_id}/out/", {"count": 5}, format="json", **self.headers)

        # Age the batch beyond the 10-minute window (bypass auto_now_add via update).
        batch = LabourGateOutBatch.objects.filter(entry_id=entry_id).latest("created_at")
        LabourGateOutBatch.objects.filter(id=batch.id).update(
            created_at=timezone.now() - timedelta(minutes=11)
        )

        res = self.client.post(f"{BASE}{entry_id}/out/undo/", **self.headers)
        self.assertEqual(res.status_code, status.HTTP_409_CONFLICT)
        # The batch is untouched.
        self.assertTrue(LabourGateOutBatch.objects.filter(id=batch.id).exists())

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

    def _grant(self, *codenames):
        """Reset the user's labour_gate perms to exactly the given codenames."""
        self.user.user_permissions.clear()
        self.user.user_permissions.add(
            *Permission.objects.filter(
                content_type__app_label="labour_gate", codename__in=codenames
            ),
            *Permission.objects.filter(
                content_type__app_label="person_gatein", codename="view_contractor"
            ),
        )
        user = get_user_model().objects.get(pk=self.user.pk)  # refresh perm cache
        client = APIClient()
        client.force_authenticate(user)
        return client

    def test_gate_person_cannot_allocate_to_department(self):
        """A gate person (can_record_labour_in only) may record a raw gate-in but
        must NOT be able to split labour across departments."""
        client = self._grant("view_labourgateentry", "can_record_labour_in")
        gate_in = client.post(
            f"{BASE}in/",
            {"contractor": self.contractor.id, "work_date": WORK_DATE, "count_in": 5},
            format="json",
            **self.headers,
        )
        self.assertEqual(gate_in.status_code, status.HTTP_201_CREATED)

        split = client.post(
            f"{BASE}in/",
            {
                "department": self.department.id,
                "contractor": self.contractor.id,
                "work_date": WORK_DATE,
                "count_in": 2,
            },
            format="json",
            **self.headers,
        )
        self.assertEqual(split.status_code, status.HTTP_403_FORBIDDEN)

    def test_hod_cannot_record_raw_gate_in(self):
        """An HOD (can_allocate_labour_department only) may split labour across
        departments but must NOT be able to record a raw gate-in."""
        client = self._grant("view_labourgateentry", "can_allocate_labour_department")
        gate_in = client.post(
            f"{BASE}in/",
            {"contractor": self.contractor.id, "work_date": WORK_DATE, "count_in": 5},
            format="json",
            **self.headers,
        )
        self.assertEqual(gate_in.status_code, status.HTTP_403_FORBIDDEN)
