"""Warehouse Inventory: oil in SAP's warehouses, in litres, by category.

  - litres add up per warehouse and category, raw material and finished goods
    apart, and a negative balance stays negative and is counted;
  - EXIM's warehouses come first and are the default selection;
  - only EXIM's inventory right opens it; SAP down is a 503.
"""

from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase
from rest_framework.test import APIClient, APITestCase

from company.models import Company, UserCompany, UserRole
from sap_client.exceptions import SAPConnectionError

from . import services_inventory

D = Decimal


def row(whs, code, category, litres):
    return {"warehouse": whs, "warehouse_name": whs, "item_code": code, "item_name": code,
            "kind": "FG" if code.startswith("FG") else "RM", "category": category,
            "on_hand": D(litres), "litres": D(litres)}


ROWS = [
    row("BH-LO", "RM1", "SOYABEAN", "1000"), row("BH-LO", "RM2", "SOYABEAN", "500"),
    row("BH-LO", "RM3", "MUSTARD", "-20"), row("01", "RM1", "SOYABEAN", "5"),
    row("BH-EC", "FG1", "SOYABEAN", "300"),
]


class InventoryTests(TestCase):
    def test_litres_by_warehouse_and_category_keep_their_sign(self):
        company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        with mock.patch("exim.services_inventory.warehouse_litres", return_value=ROWS):
            data = services_inventory.warehouse_inventory(company, warehouse="BH-LO")
        self.assertEqual(data["default_warehouses"], ["BH-LO", "BH-EC"])
        self.assertEqual([w["warehouse"] for w in data["warehouses"]], ["BH-LO", "BH-EC", "01"])
        lo = data["warehouses"][0]
        self.assertEqual((lo["litres"], lo["negative_items"]), (D("1480"), 1))
        self.assertEqual([(c["category"], c["litres"]) for c in lo["categories"]],
                         [("SOYABEAN", D("1500")), ("MUSTARD", D("-20"))])
        self.assertEqual(len(data["items"]), 3)


class InventoryAPITests(APITestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        self.role = UserRole.objects.create(name="Import / Export")

    def client_for(self, *codenames):
        user = get_user_model().objects.create_user(email=f"{len(codenames)}i@example.com", password="x",
                                                    full_name="I", employee_code=f"I{len(codenames)}")
        UserCompany.objects.create(user=user, company=self.company, role=self.role, is_default=True)
        user.user_permissions.add(*Permission.objects.filter(content_type__app_label="exim", codename__in=codenames))
        client = APIClient()
        client.force_authenticate(user)
        return client

    def test_exims_inventory_right_opens_it_and_sap_down_is_a_503(self):
        url, headers = "/api/v1/exim/warehouse-inventory/", {"HTTP_COMPANY_CODE": "JIVO_OIL"}
        self.assertEqual(self.client_for().get(url, **headers).status_code, 403)
        client = self.client_for("sync_inventory")
        with mock.patch("exim.services_inventory.warehouse_litres", return_value=ROWS):
            self.assertEqual(client.get(url, **headers).status_code, 200)
        with mock.patch("exim.services_inventory.warehouse_litres", side_effect=SAPConnectionError("down")):
            self.assertEqual(client.get(url, **headers).status_code, 503)
