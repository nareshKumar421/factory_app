"""Tests for per-user warehouse scoping.

The rules are short but every one of them fails dangerously in a different
direction, so each gets its own test: an unassigned user must be refused (not
waved through), a superuser must not be refused (or the first deploy locks out
the people who would fix it), and a partly-managed set must be refused as a
whole (or a manager ships another site's stock by attaching one of their own
documents).
"""

from django.test import TestCase
from rest_framework.exceptions import PermissionDenied

from accounts.models import User
from company.models import Company
from warehouse.models_manager import UserWarehouse
from warehouse.services import warehouse_scope


class WarehouseScopeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.oil = Company.objects.create(code="JIVO_OIL", name="Jivo Oil")
        cls.mart = Company.objects.create(code="JIVO_MART", name="Jivo Mart")

        cls.manager = User.objects.create_user(
            email="bh@example.com", full_name="BH Manager",
            employee_code="E-BH", password="x",
        )
        cls.other = User.objects.create_user(
            email="dl@example.com", full_name="DL Manager",
            employee_code="E-DL", password="x",
        )
        cls.unassigned = User.objects.create_user(
            email="none@example.com", full_name="No Warehouse",
            employee_code="E-NONE", password="x",
        )
        cls.admin = User.objects.create_superuser(
            email="admin@example.com", full_name="Admin",
            employee_code="E-ADM", password="x",
        )

        UserWarehouse.objects.create(
            user=cls.manager, company=cls.oil, warehouse_code="BH-PF"
        )
        UserWarehouse.objects.create(
            user=cls.manager, company=cls.oil, warehouse_code="BH-FG"
        )
        UserWarehouse.objects.create(
            user=cls.other, company=cls.oil, warehouse_code="DL-FG"
        )

    # ---- the mapping itself ------------------------------------------------

    def test_managed_warehouses_lists_only_that_users_own(self):
        self.assertEqual(
            warehouse_scope.managed_warehouses(self.manager, "JIVO_OIL"),
            frozenset({"BH-PF", "BH-FG"}),
        )

    def test_assignment_does_not_leak_across_companies(self):
        """A BH-PF manager in Oil manages nothing in Mart."""
        self.assertEqual(
            warehouse_scope.managed_warehouses(self.manager, "JIVO_MART"),
            frozenset(),
        )

    def test_deactivated_assignment_stops_counting(self):
        UserWarehouse.objects.filter(
            user=self.manager, warehouse_code="BH-FG"
        ).update(is_active=False)
        self.assertEqual(
            warehouse_scope.managed_warehouses(self.manager, "JIVO_OIL"),
            frozenset({"BH-PF"}),
        )

    def test_codes_are_stored_upper_case(self):
        """A lower-case code would never match sap_from_warehouse."""
        row = UserWarehouse.objects.create(
            user=self.unassigned, company=self.oil, warehouse_code=" bh-rm "
        )
        row.refresh_from_db()
        self.assertEqual(row.warehouse_code, "BH-RM")

    # ---- the send / receive assertions ------------------------------------

    def test_manager_may_send_from_own_warehouse(self):
        warehouse_scope.assert_can_send_from(self.manager, "JIVO_OIL", ["BH-PF"])

    def test_manager_may_not_send_from_another_warehouse(self):
        with self.assertRaises(PermissionDenied) as ctx:
            warehouse_scope.assert_can_send_from(self.manager, "JIVO_OIL", ["DL-FG"])
        message = str(ctx.exception)
        self.assertIn("DL-FG", message)
        # The message must say what they CAN do, or a 403 reads as a bug.
        self.assertIn("BH-PF", message)

    def test_unassigned_user_is_refused(self):
        """The chosen rule: no assignment means no access, not free rein."""
        with self.assertRaises(PermissionDenied) as ctx:
            warehouse_scope.assert_can_send_from(self.unassigned, "JIVO_OIL", ["BH-PF"])
        self.assertIn("not set as the manager of any warehouse", str(ctx.exception))

    def test_superuser_is_never_refused(self):
        """Deadlock guard: without this the first deploy locks admins out too."""
        warehouse_scope.assert_can_send_from(self.admin, "JIVO_OIL", ["ANY-WH"])
        warehouse_scope.assert_can_receive_into(self.admin, "JIVO_MART", ["OTHER"])

    def test_a_partly_managed_set_is_refused_as_a_whole(self):
        """One managed document must not authorise another site's goods."""
        with self.assertRaises(PermissionDenied) as ctx:
            warehouse_scope.assert_can_send_from(
                self.manager, "JIVO_OIL", ["BH-PF", "DL-FG"]
            )
        self.assertIn("DL-FG", str(ctx.exception))
        self.assertNotIn("you do not manage BH-PF", str(ctx.exception))

    def test_case_and_whitespace_do_not_defeat_the_check(self):
        warehouse_scope.assert_can_send_from(self.manager, "JIVO_OIL", [" bh-pf "])

    def test_receive_uses_the_same_mapping(self):
        warehouse_scope.assert_can_receive_into(self.manager, "JIVO_OIL", ["BH-FG"])
        with self.assertRaises(PermissionDenied):
            warehouse_scope.assert_can_receive_into(self.manager, "JIVO_OIL", ["DL-FG"])

    # ---- the blank-warehouse case ----------------------------------------

    def test_blank_warehouse_is_refused_by_default(self):
        with self.assertRaises(PermissionDenied) as ctx:
            warehouse_scope.assert_can_send_from(self.manager, "JIVO_OIL", [""])
        self.assertIn("no warehouse is named", str(ctx.exception))

    def test_blank_warehouse_passes_when_explicitly_allowed(self):
        """An INVOICE BST has no destination warehouse -- it settles to a company.

        Refusing on the blank would break every cross-company receipt, so those
        callers opt in to allowing it. This test pins that the opt-in is
        deliberate rather than accidental.
        """
        warehouse_scope.assert_can_receive_into(
            self.manager, "JIVO_OIL", [""], blank_ok=True
        )

    def test_blank_ok_still_refuses_a_user_who_manages_nothing(self):
        """`blank_ok` must not become a hole for unassigned users.

        Otherwise missing SAP data would be a way past the whole restriction:
        no warehouse named, nothing to compare, everyone allowed.
        """
        with self.assertRaises(PermissionDenied):
            warehouse_scope.assert_can_receive_into(
                self.unassigned, "JIVO_OIL", [""], blank_ok=True
            )
        with self.assertRaises(PermissionDenied):
            warehouse_scope.assert_can_send_from(
                self.unassigned, "JIVO_OIL", [], blank_ok=True
            )

    # ---- the predicate used for hiding buttons ---------------------------

    def test_manages_predicate(self):
        self.assertTrue(warehouse_scope.manages(self.manager, "JIVO_OIL", "BH-PF"))
        self.assertFalse(warehouse_scope.manages(self.manager, "JIVO_OIL", "DL-FG"))
        self.assertFalse(warehouse_scope.manages(self.unassigned, "JIVO_OIL", "BH-PF"))
        self.assertTrue(warehouse_scope.manages(self.admin, "JIVO_OIL", "ANYTHING"))
        self.assertFalse(warehouse_scope.manages(self.manager, "JIVO_OIL", ""))

    def test_unrestricted_flag(self):
        self.assertTrue(warehouse_scope.is_unrestricted(self.admin))
        self.assertFalse(warehouse_scope.is_unrestricted(self.manager))
        self.assertFalse(warehouse_scope.is_unrestricted(None))
