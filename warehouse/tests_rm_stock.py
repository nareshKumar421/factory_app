"""Tests for the raw-material stock register.

Three things here are worth a test more than the CRUD is:

* setting a quantity twice **updates** the row and **appends** history — the
  whole point of the register is that the previous figure is recoverable;
* only a manager of that warehouse may set a quantity, and the permission alone
  is not enough (the `UserWarehouse` assignment is the second half);
* reads are NOT warehouse-scoped, on purpose — a register whose totals depend on
  who is looking cannot be reconciled against anything.
"""

from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import Permission
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.test import APIClient

from accounts.models import User
from company.models import Company, UserCompany, UserRole
from warehouse.models_manager import UserWarehouse
from warehouse.models_rm_stock import RawMaterialStock, RawMaterialStockEntry
from warehouse.services import rm_stock_service

LIST_URL = "/api/v1/warehouse/rm-stock/"
ITEMS_URL = "/api/v1/warehouse/rm-stock/items/"


# The register covers exactly one warehouse (BH-LO in production). These tests
# are about the register's mechanics, not which warehouse it is, so they point
# the setting at BH-PM and keep their existing fixtures.
@override_settings(RM_STOCK_WAREHOUSE="BH-PM")
class RMStockTestBase(TestCase):
    def setUp(self):
        self.company = Company.objects.create(code="JIVO_OIL", name="Jivo Oil")
        self.role = UserRole.objects.create(name="Store")

        # Keeper of BH-PM: holds both permissions and the assignment.
        self.keeper = self._user("pm@example.com", "PM Keeper", "E-PM")
        self._grant(self.keeper, "can_view_rm_stock", "can_set_rm_stock")
        UserWarehouse.objects.create(
            user=self.keeper, company=self.company, warehouse_code="BH-PM"
        )

        # Planner: may read the whole register, may set nothing.
        self.viewer = self._user("plan@example.com", "Planner", "E-PL")
        self._grant(self.viewer, "can_view_rm_stock")

    def _user(self, email, name, code):
        user = User.objects.create_user(
            email=email, full_name=name, employee_code=code, password="x"
        )
        UserCompany.objects.create(user=user, company=self.company, role=self.role)
        return user

    def _grant(self, user, *codenames):
        for codename in codenames:
            user.user_permissions.add(
                Permission.objects.get(
                    content_type__app_label="warehouse", codename=codename
                )
            )

    def _client(self, user):
        client = APIClient()
        client.force_authenticate(user=user)
        client.credentials(HTTP_COMPANY_CODE=self.company.code)
        return client

    def _set(self, user=None, **kwargs):
        payload = {
            "user": user or self.keeper,
            "company": self.company,
            "warehouse_code": "BH-PM",
            "item_code": "RM0001",
            "qty": Decimal("100.000"),
        }
        payload.update(kwargs)
        return rm_stock_service.set_quantity(**payload)


class RMStockServiceTests(RMStockTestBase):
    def test_setting_a_new_item_creates_the_row_and_a_created_entry(self):
        row = self._set(item_name="Mustard Seed", uom="KG")

        self.assertEqual(row.qty, Decimal("100.000"))
        self.assertEqual(row.warehouse_code, "BH-PM")
        self.assertEqual(row.as_of_date, timezone.localdate())
        self.assertEqual(row.set_by, self.keeper)

        entry = RawMaterialStockEntry.objects.get()
        self.assertEqual(entry.action, RawMaterialStockEntry.Action.CREATED)
        # Null, not zero: there was no figure before, which is a different fact.
        self.assertIsNone(entry.previous_qty)
        self.assertEqual(entry.qty, Decimal("100.000"))
        self.assertIsNone(entry.qty_delta)

    def test_setting_it_again_updates_the_row_and_keeps_the_old_figure(self):
        self._set(qty=Decimal("100"))
        row = self._set(qty=Decimal("40.500"))

        self.assertEqual(RawMaterialStock.objects.count(), 1)
        self.assertEqual(row.qty, Decimal("40.500"))

        entry = RawMaterialStockEntry.objects.order_by("-id").first()
        self.assertEqual(entry.action, RawMaterialStockEntry.Action.UPDATED)
        self.assertEqual(entry.previous_qty, Decimal("100.000"))
        self.assertEqual(entry.qty_delta, Decimal("-59.500"))
        self.assertEqual(RawMaterialStockEntry.objects.count(), 2)

    def test_lower_case_codes_do_not_create_a_second_row(self):
        self._set(item_code="RM0001", warehouse_code="BH-PM")
        self._set(item_code="rm0001", warehouse_code="bh-pm", qty=Decimal("7"))

        self.assertEqual(RawMaterialStock.objects.count(), 1)
        self.assertEqual(RawMaterialStock.objects.get().qty, Decimal("7.000"))

    def test_a_save_without_item_text_does_not_blank_what_is_stored(self):
        self._set(item_name="Mustard Seed", uom="KG")
        row = self._set(qty=Decimal("5"))

        self.assertEqual(row.item_name, "Mustard Seed")
        self.assertEqual(row.uom, "KG")

    @override_settings(RM_STOCK_WAREHOUSE="BH-PC")
    def test_a_user_who_does_not_manage_the_warehouse_is_refused(self):
        with self.assertRaises(PermissionDenied):
            self._set(warehouse_code="BH-PC")
        self.assertFalse(RawMaterialStock.objects.exists())

    def test_a_warehouse_that_is_not_the_registers_is_refused(self):
        """Even for someone who manages it — the register covers one store."""
        UserWarehouse.objects.create(
            user=self.keeper, company=self.company, warehouse_code="BH-PC"
        )
        with self.assertRaises(ValidationError):
            self._set(warehouse_code="BH-PC")
        self.assertFalse(RawMaterialStock.objects.exists())

    def test_an_omitted_warehouse_takes_the_registers_own(self):
        row = self._set(warehouse_code="")
        self.assertEqual(row.warehouse_code, "BH-PM")

    def test_a_user_who_manages_nothing_is_refused(self):
        with self.assertRaises(PermissionDenied):
            self._set(user=self.viewer)

    @override_settings(RM_STOCK_WAREHOUSE="BH-PC")
    def test_a_superuser_is_not_warehouse_scoped(self):
        boss = User.objects.create_superuser(
            email="boss@example.com", full_name="Boss", password="x"
        )
        row = self._set(user=boss, warehouse_code="BH-PC")
        self.assertEqual(row.warehouse_code, "BH-PC")

    def test_a_negative_quantity_is_refused(self):
        with self.assertRaises(ValidationError):
            self._set(qty=Decimal("-1"))

    def test_a_future_as_of_date_is_refused(self):
        with self.assertRaises(ValidationError):
            self._set(as_of_date=timezone.localdate() + timedelta(days=1))

    def test_removing_deactivates_and_keeps_the_history(self):
        row = self._set()
        rm_stock_service.remove_row(
            user=self.keeper, company=self.company, row=row, remarks="not stocked here"
        )

        row.refresh_from_db()
        self.assertFalse(row.is_active)
        self.assertTrue(RawMaterialStock.objects.filter(pk=row.pk).exists())
        self.assertEqual(
            RawMaterialStockEntry.objects.order_by("-id").first().action,
            RawMaterialStockEntry.Action.REMOVED,
        )

    def test_setting_a_removed_item_brings_it_back(self):
        row = self._set()
        rm_stock_service.remove_row(user=self.keeper, company=self.company, row=row)

        restored = self._set(qty=Decimal("12"))

        self.assertTrue(restored.is_active)
        self.assertEqual(restored.pk, row.pk)
        self.assertEqual(
            RawMaterialStockEntry.objects.order_by("-id").first().action,
            RawMaterialStockEntry.Action.RESTORED,
        )

    def test_history_survives_the_row_being_removed_and_replaced(self):
        """The denormalised columns are what makes the trail continuous."""
        row = self._set(qty=Decimal("100"))
        rm_stock_service.remove_row(user=self.keeper, company=self.company, row=row)
        row.delete()  # the one case the FK is allowed to go null

        self._set(qty=Decimal("3"))
        trail = rm_stock_service.history_for(
            company_code=self.company.code, row=RawMaterialStock.objects.get()
        )
        self.assertEqual(trail.count(), 3)

    def test_the_register_is_per_company(self):
        other = Company.objects.create(code="JIVO_MART", name="Jivo Mart")
        UserWarehouse.objects.create(
            user=self.keeper, company=other, warehouse_code="BH-PM"
        )
        self._set(qty=Decimal("100"))
        self._set(company=other, qty=Decimal("55"))

        self.assertEqual(RawMaterialStock.objects.count(), 2)
        oil = rm_stock_service.list_stock(company_code=self.company.code)
        self.assertEqual([r.qty for r in oil], [Decimal("100.000")])


class RMStockAPITests(RMStockTestBase):
    def test_the_register_is_readable_by_anyone_who_may_view_it(self):
        self._set(item_name="Mustard Seed", uom="KG")

        response = self._client(self.viewer).get(LIST_URL)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data["rows"]), 1)
        self.assertEqual(response.data["rows"][0]["item_code"], "RM0001")
        self.assertEqual(response.data["rows"][0]["set_by_name"], "PM Keeper")

    def test_reads_are_not_warehouse_scoped(self):
        """A planner manages nothing and must still see every warehouse's rows.

        The second row is written straight to the model: only one warehouse can
        be *set* through the service now, but rows banked under another code
        before that must stay readable.
        """
        self._set(warehouse_code="BH-PM")
        RawMaterialStock.objects.create(
            company=self.company, warehouse_code="BH-PC", item_code="RM0002",
            qty=Decimal("5"), as_of_date=timezone.localdate(),
        )

        response = self._client(self.viewer).get(LIST_URL)

        self.assertEqual(len(response.data["rows"]), 2)
        # …but it is told it may edit nothing, so it offers no Set button.
        self.assertEqual(response.data["managed_warehouse_codes"], [])
        self.assertFalse(response.data["unrestricted"])

    def test_the_list_reports_which_warehouses_the_caller_runs(self):
        response = self._client(self.keeper).get(LIST_URL)
        self.assertEqual(response.data["managed_warehouse_codes"], ["BH-PM"])

    def test_removed_rows_are_hidden_unless_asked_for(self):
        row = self._set()
        rm_stock_service.remove_row(user=self.keeper, company=self.company, row=row)

        self.assertEqual(len(self._client(self.viewer).get(LIST_URL).data["rows"]), 0)
        with_inactive = self._client(self.viewer).get(
            LIST_URL, {"include_inactive": "true"}
        )
        self.assertEqual(len(with_inactive.data["rows"]), 1)

    def test_filtering_by_warehouse_and_search(self):
        self._set(item_code="RM0001", item_name="Mustard Seed")
        RawMaterialStock.objects.create(
            company=self.company, warehouse_code="BH-PC", item_code="RM0002",
            item_name="Canola Oil", qty=Decimal("5"), as_of_date=timezone.localdate(),
        )

        by_whs = self._client(self.viewer).get(LIST_URL, {"warehouse_code": "bh-pc"})
        self.assertEqual([r["item_code"] for r in by_whs.data["rows"]], ["RM0002"])

        by_name = self._client(self.viewer).get(LIST_URL, {"search": "mustard"})
        self.assertEqual([r["item_code"] for r in by_name.data["rows"]], ["RM0001"])

    def test_a_keeper_can_set_a_quantity_through_the_api(self):
        response = self._client(self.keeper).post(
            LIST_URL,
            {
                "warehouse_code": "BH-PM",
                "item_code": "RM0001",
                "item_name": "Mustard Seed",
                "uom": "KG",
                "qty": "42.250",
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(RawMaterialStock.objects.get().qty, Decimal("42.250"))

    def test_a_viewer_cannot_set_a_quantity(self):
        response = self._client(self.viewer).post(
            LIST_URL,
            {"warehouse_code": "BH-PM", "item_code": "RM0001", "qty": "1"},
            format="json",
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(RawMaterialStock.objects.exists())

    @override_settings(RM_STOCK_WAREHOUSE="BH-PC")
    def test_the_permission_alone_does_not_let_a_keeper_set_another_warehouse(self):
        response = self._client(self.keeper).post(
            LIST_URL,
            {"warehouse_code": "BH-PC", "item_code": "RM0001", "qty": "1"},
            format="json",
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn("BH-PM", str(response.data))

    def test_the_list_names_the_warehouse_the_register_covers(self):
        response = self._client(self.viewer).get(LIST_URL)
        self.assertEqual(response.data["register_warehouse"], "BH-PM")

    def test_detail_returns_the_row_with_its_history(self):
        row = self._set(qty=Decimal("100"))
        self._set(qty=Decimal("60"))

        response = self._client(self.viewer).get(f"{LIST_URL}{row.pk}/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["row"]["qty"], "60.000")
        self.assertEqual(len(response.data["history"]), 2)
        self.assertEqual(response.data["history"][0]["previous_qty"], "100.000")

    def test_delete_takes_the_row_off_the_register(self):
        row = self._set()
        response = self._client(self.keeper).delete(f"{LIST_URL}{row.pk}/")

        self.assertEqual(response.status_code, 204)
        row.refresh_from_db()
        self.assertFalse(row.is_active)

    def test_another_company_row_is_not_reachable_by_id(self):
        other = Company.objects.create(code="JIVO_MART", name="Jivo Mart")
        UserWarehouse.objects.create(
            user=self.keeper, company=other, warehouse_code="BH-PM"
        )
        row = self._set(company=other)

        response = self._client(self.keeper).get(f"{LIST_URL}{row.pk}/")
        self.assertEqual(response.status_code, 404)

    @patch("warehouse.services.rm_stock_service.WMSHanaReader")
    def test_the_item_picker_asks_sap_for_the_raw_material_group(self, reader_cls):
        reader_cls.return_value.search_items_in_group.return_value = [
            {"item_code": "RM0001", "item_name": "Mustard Seed",
             "uom": "KG", "sap_on_hand": 1200.0}
        ]

        response = self._client(self.keeper).get(
            ITEMS_URL, {"search": "must", "warehouse_code": "BH-PM"}
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["items"][0]["item_code"], "RM0001")
        reader_cls.return_value.search_items_in_group.assert_called_once_with(
            item_group_code=rm_stock_service.RM_ITEM_GROUP_CODE,
            search="must",
            warehouse_code="BH-PM",
            limit=50,
        )

    @patch("warehouse.services.rm_stock_service.WMSHanaReader")
    def test_a_hana_outage_answers_503_and_does_not_take_the_register_down(
        self, reader_cls
    ):
        from sap_client.exceptions import SAPConnectionError

        reader_cls.return_value.search_items_in_group.side_effect = SAPConnectionError(
            "HANA is down"
        )

        items = self._client(self.keeper).get(ITEMS_URL)
        self.assertEqual(items.status_code, 503)

        # The register itself never touches HANA, so it still answers.
        self.assertEqual(self._client(self.keeper).get(LIST_URL).status_code, 200)
