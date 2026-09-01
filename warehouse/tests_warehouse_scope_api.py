"""API tests for the warehouse-manager endpoints.

The config page is only as good as these four endpoints, and two of them carry
security decisions that a service-layer test cannot reach:

* `my-warehouses/` must NOT require the admin permission — every warehouse screen
  calls it to decide what to enable, and a screen cannot correctly disable an
  action it is not allowed to ask about. It must also only ever answer about the
  caller.
* the write endpoints MUST require it, or a warehouse manager could widen their
  own scope and the whole restriction is decorative.
"""

from django.contrib.auth.models import Permission
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import User
from company.models import Company, UserCompany, UserRole
from warehouse.models_manager import UserWarehouse


class WarehouseManagerAPITests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(code="JIVO_OIL", name="Jivo Oil")
        self.role = UserRole.objects.create(name="Store")

        self.admin = self._user("admin@example.com", "Admin", "E-ADM")
        self.admin.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="warehouse",
                codename="can_manage_user_warehouses",
            )
        )
        self.manager = self._user("bh@example.com", "BH Manager", "E-BH")
        UserWarehouse.objects.create(
            user=self.manager, company=self.company, warehouse_code="BH-PF"
        )

    def _user(self, email, name, code):
        user = User.objects.create_user(
            email=email, full_name=name, employee_code=code, password="x"
        )
        UserCompany.objects.create(user=user, company=self.company, role=self.role)
        return user

    def _client(self, user):
        client = APIClient()
        client.force_authenticate(user=user)
        client.credentials(HTTP_COMPANY_CODE=self.company.code)
        return client

    # ---- my-warehouses ----------------------------------------------------

    def test_any_user_can_read_their_own_warehouses(self):
        response = self._client(self.manager).get("/api/v1/warehouse/my-warehouses/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["warehouse_codes"], ["BH-PF"])
        self.assertFalse(response.data["unrestricted"])

    def test_my_warehouses_needs_no_admin_permission(self):
        """The manager has no admin permission and must still get an answer."""
        self.assertFalse(self.manager.has_perm("warehouse.can_manage_user_warehouses"))
        response = self._client(self.manager).get("/api/v1/warehouse/my-warehouses/")
        self.assertEqual(response.status_code, 200)

    def test_my_warehouses_answers_only_about_the_caller(self):
        other = self._user("dl@example.com", "DL", "E-DL")
        UserWarehouse.objects.create(
            user=other, company=self.company, warehouse_code="DL-FG"
        )
        response = self._client(self.manager).get("/api/v1/warehouse/my-warehouses/")
        self.assertEqual(response.data["warehouse_codes"], ["BH-PF"])

    # ---- the write endpoints are admin-only ------------------------------

    def test_a_manager_cannot_widen_their_own_scope(self):
        response = self._client(self.manager).post(
            "/api/v1/warehouse/user-warehouses/",
            {"user": self.manager.id, "warehouse_codes": ["DL-FG"]},
            format="json",
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(
            UserWarehouse.objects.filter(
                user=self.manager, warehouse_code="DL-FG"
            ).exists()
        )

    def test_a_manager_cannot_even_list_assignments(self):
        response = self._client(self.manager).get("/api/v1/warehouse/user-warehouses/")
        self.assertEqual(response.status_code, 403)

    # ---- assigning --------------------------------------------------------

    def test_admin_assigns_several_warehouses_at_once(self):
        response = self._client(self.admin).post(
            "/api/v1/warehouse/user-warehouses/",
            {"user": self.manager.id, "warehouse_codes": ["BH-FG", "bh-rm"]},
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(sorted(response.data["created"]), ["BH-FG", "BH-RM"])
        codes = set(
            UserWarehouse.objects.filter(
                user=self.manager, is_active=True
            ).values_list("warehouse_code", flat=True)
        )
        self.assertEqual(codes, {"BH-PF", "BH-FG", "BH-RM"})

    def test_reassigning_an_existing_warehouse_is_not_an_error(self):
        """The unique constraint would refuse a duplicate; the API must not 500."""
        response = self._client(self.admin).post(
            "/api/v1/warehouse/user-warehouses/",
            {"user": self.manager.id, "warehouse_codes": ["BH-PF"]},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["already_assigned"], ["BH-PF"])
        self.assertEqual(
            UserWarehouse.objects.filter(
                user=self.manager, warehouse_code="BH-PF"
            ).count(),
            1,
        )

    def test_removing_deactivates_rather_than_deletes(self):
        row = UserWarehouse.objects.get(user=self.manager, warehouse_code="BH-PF")
        response = self._client(self.admin).delete(
            f"/api/v1/warehouse/user-warehouses/{row.id}/"
        )
        self.assertEqual(response.status_code, 204)
        row.refresh_from_db()
        self.assertFalse(row.is_active)

    def test_re_adding_a_removed_manager_reactivates_the_row(self):
        row = UserWarehouse.objects.get(user=self.manager, warehouse_code="BH-PF")
        row.is_active = False
        row.save(update_fields=["is_active"])

        response = self._client(self.admin).post(
            "/api/v1/warehouse/user-warehouses/",
            {"user": self.manager.id, "warehouse_codes": ["BH-PF"]},
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["reactivated"], ["BH-PF"])
        row.refresh_from_db()
        self.assertTrue(row.is_active)

    def test_empty_warehouse_list_is_rejected(self):
        response = self._client(self.admin).post(
            "/api/v1/warehouse/user-warehouses/",
            {"user": self.manager.id, "warehouse_codes": []},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    # ---- the lockout report ----------------------------------------------

    def test_gaps_lists_a_permission_holder_with_no_warehouse(self):
        stranded = self._user("nowh@example.com", "Stranded", "E-STR")
        stranded.user_permissions.add(
            Permission.objects.get(
                content_type__app_label="warehouse",
                codename="can_create_transfer_request",
            )
        )
        response = self._client(self.admin).get(
            "/api/v1/warehouse/user-warehouses/gaps/"
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(stranded.id, [row["id"] for row in response.data])
        # The already-assigned manager is not a gap.
        self.assertNotIn(self.manager.id, [row["id"] for row in response.data])
