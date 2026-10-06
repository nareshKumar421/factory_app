"""
amounts_board/tests.py

The service is driven with fake readers -- no HANA -- so these pin what the
board does with SAP's rows: the RM / PM / FG split, the godown order, a section
that fails alone, the debtors Total naming what it could not read, and the
non-moving tile keeping to the page's own scope.
"""

from datetime import date
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from company.models import Company, UserCompany, UserRole
from sap_client.exceptions import SAPConnectionError, SAPDataError
from warehouse.models_manager import UserWarehouse

from .models import StockOwner
from .services import AmountsBoardService

User = get_user_model()


class FakeReader:
    """Rows shaped as AmountsReader returns them."""

    def __init__(
        self,
        stock=None,
        balances=None,
        debtors=None,
        items=None,
        customers=None,
        debits=None,
        fail=None,
    ):
        self.stock = stock or []
        self.balances = balances or []
        self.debtors = debtors or []
        self.items = items or []
        self.customers = customers or {}
        self.debits = debits or []
        self.fail = fail
        self.calls = []

    def _maybe_fail(self):
        if self.fail:
            raise self.fail

    def stock_by_godown(self, item_groups):
        self._maybe_fail()
        self.calls.append(("stock", tuple(item_groups)))
        return self.stock

    def godown_items(self, warehouse, item_group):
        self._maybe_fail()
        self.calls.append(("items", warehouse, item_group))
        return self.items

    def debtor_balances(self, group_card_codes):
        self._maybe_fail()
        self.calls.append(("balances", tuple(group_card_codes)))
        return self.balances

    def debtor_list(self, group_card_codes):
        self._maybe_fail()
        self.calls.append(("debtors", tuple(group_card_codes)))
        return self.debtors

    def customer(self, card_code):
        self._maybe_fail()
        return self.customers.get(card_code)

    def unpaid_debits(self, card_code, balance):
        self._maybe_fail()
        self.calls.append(("debits", card_code, balance))
        return self.debits


class FakeNonMoving:
    def __init__(self, rows=None, fail=None):
        self.rows = rows or []
        self.fail = fail
        self.asked = None

    def get_report(self, age, item_group, count_production=True):
        if self.fail:
            raise self.fail
        self.asked = (age, item_group, count_production)
        return {"data": self.rows}


def debtor_row(code, name, balance, since):
    return {"CardCode": code, "CardName": name, "Balance": balance, "Since": since}


def stock_row(warehouse, group, value, items=1, name=None):
    return {
        "Warehouse": warehouse,
        "WarehouseName": name or warehouse,
        "ItemGroup": group,
        "Items": items,
        "Value": value,
    }


def service(readers, non_moving=None):
    non_moving = non_moving or {}
    return AmountsBoardService(
        reader_factory=lambda code: readers[code],
        non_moving_factory=lambda code: non_moving.get(code, FakeNonMoving()),
    )


def make_company(code, name):
    return Company.objects.create(name=name, code=code)


def make_user(email, name, *companies):
    user = User.objects.create_user(email=email, full_name=name, password="x")
    role, _ = UserRole.objects.get_or_create(name="Staff")
    for i, company in enumerate(companies):
        UserCompany.objects.create(
            user=user, company=company, role=role, is_default=(i == 0), is_active=True
        )
    return user


def healthy_readers():
    return {
        "JIVO_OIL": FakeReader(
            stock=[
                stock_row("BH-LO", 106, 1000.0, 3, "Loose Oil"),
                stock_row("BH-PC", 106, 300.0, 2),
                stock_row("BH-PC", 105, 200.0, 5),
                stock_row("BH-BT", 102, 800.0, 4),
            ],
            balances=[
                {"Kind": "C", "Customers": 10, "Amount": 500.0},
                {"Kind": "G", "Customers": 2, "Amount": 9000.0},
            ],
            debtors=[
                debtor_row("C9", "Big Recent", 5000.0, date(2026, 9, 1)),
                debtor_row("C1", "Old Customer", 1200.0, date(2024, 9, 30)),
                # Older still, but Rs 7: a rounding residue, which must not
                # set the oldest date.
                debtor_row("C7", "Rounding Residue", 7.0, date(2023, 1, 1)),
            ],
        ),
        "JIVO_BEVERAGES": FakeReader(stock=[stock_row("BH-PM", 105, 50.0, 1)]),
        "JIVO_MART": FakeReader(
            balances=[{"Kind": "C", "Customers": 3, "Amount": 70.0}],
            debtors=[debtor_row("M1", "Mart Customer", 7000.0, date(2025, 10, 22))],
        ),
    }


class PlantStockTests(TestCase):
    def setUp(self):
        self.oil = make_company("JIVO_OIL", "Jivo Oil")
        make_company("JIVO_BEVERAGES", "Jivo Beverages")
        make_company("JIVO_MART", "Jivo Mart")

    def test_splits_by_item_group_and_lists_godowns_largest_first(self):
        stock = service(healthy_readers()).plant_stock("JIVO_OIL")

        self.assertEqual(stock["total"], 2300.0)
        rm, pm, fg = stock["categories"]
        self.assertEqual([rm["key"], pm["key"], fg["key"]], ["RM", "PM", "FG"])
        self.assertEqual(rm["value"], 1300.0)
        self.assertEqual([g["code"] for g in rm["godowns"]], ["BH-LO", "BH-PC"])
        self.assertEqual(rm["godowns"][0]["name"], "Loose Oil")
        # A mixed godown appears under every category it holds.
        self.assertEqual([g["code"] for g in pm["godowns"]], ["BH-PC"])
        self.assertEqual(fg["value"], 800.0)

    def test_godowns_carry_their_active_managers(self):
        boss = make_user("boss@example.com", "Gautam", self.oil)
        gone = make_user("gone@example.com", "Former", self.oil)
        gone.is_active = False
        gone.save()
        UserWarehouse.objects.create(user=boss, company=self.oil, warehouse_code="BH-LO")
        UserWarehouse.objects.create(user=gone, company=self.oil, warehouse_code="BH-LO")

        stock = service(healthy_readers()).plant_stock("JIVO_OIL")

        self.assertEqual(stock["categories"][0]["godowns"][0]["managers"], ["Gautam"])
        self.assertEqual(stock["categories"][0]["godowns"][1]["managers"], [])

    def test_reads_only_the_three_item_groups(self):
        readers = healthy_readers()
        service(readers).plant_stock("JIVO_OIL")
        self.assertEqual(readers["JIVO_OIL"].calls[0], ("stock", (102, 105, 106)))

    def test_godown_items_reads_the_category_group(self):
        readers = healthy_readers()
        readers["JIVO_OIL"].items = [
            {"ItemCode": "RM1", "ItemName": "Oil", "Uom": "KG", "Quantity": 10, "Value": 99.5}
        ]
        payload = service(readers).godown_items("JIVO_OIL", "RM", "BH-LO")
        self.assertEqual(readers["JIVO_OIL"].calls[-1], ("items", "BH-LO", 106))
        self.assertEqual(payload["value"], 99.5)
        self.assertEqual(payload["items"][0]["item_code"], "RM1")


class BoardTests(TestCase):
    def setUp(self):
        self.oil = make_company("JIVO_OIL", "Jivo Oil")
        make_company("JIVO_BEVERAGES", "Jivo Beverages")
        make_company("JIVO_MART", "Jivo Mart")

    def test_debtors_total_sums_and_takes_the_oldest(self):
        board = service(healthy_readers()).build()
        debtors = board["debtors"]

        self.assertEqual([c["key"] for c in debtors["companies"]], ["JWPL", "MART", "BEVERAGES"])
        jwpl = debtors["companies"][0]["figures"]
        self.assertEqual(jwpl["amount"], 500.0)
        self.assertEqual(jwpl["group_amount"], 9000.0)
        # Not the Rs 7 residue from 2023: it is under the floor.
        self.assertEqual(jwpl["oldest"]["date"], "2024-09-30")
        self.assertEqual(jwpl["oldest"]["card_code"], "C1")
        # Beverages has no debtor in debit: zero, not missing.
        self.assertEqual(debtors["companies"][2]["figures"]["amount"], 0.0)
        self.assertIsNone(debtors["companies"][2]["figures"]["oldest"])

        total = debtors["total"]
        self.assertEqual(total["amount"], 570.0)
        self.assertEqual(total["customers"], 13)
        self.assertEqual(total["oldest"]["company"], "JWPL")
        self.assertEqual(total["missing"], [])

    def test_debtors_exclude_each_companys_own_group_codes(self):
        readers = healthy_readers()
        service(readers).build()
        oil_codes = [c for c in readers["JIVO_OIL"].calls if c[0] == "balances"][0][1]
        mart_codes = [c for c in readers["JIVO_MART"].calls if c[0] == "balances"][0][1]
        self.assertIn("CUSTA000906", oil_codes)
        # CUSTA000906 is a real customer to Mart; the lists are never merged.
        self.assertNotIn("CUSTA000906", mart_codes)

    def test_one_company_failing_degrades_only_its_sections(self):
        readers = healthy_readers()
        readers["JIVO_MART"] = FakeReader(fail=SAPDataError("bad"))
        board = service(readers).build()

        self.assertIsNotNone(board["plants"][0]["stock"])
        self.assertIsNone(board["debtors"]["companies"][1]["figures"])
        self.assertEqual(board["meta"]["degraded"], ["debtors_mart"])
        self.assertEqual(board["debtors"]["total"]["missing"], ["MART"])
        self.assertEqual(board["debtors"]["total"]["amount"], 500.0)

    def test_owners_show_while_sap_is_down(self):
        boss = make_user("boss@example.com", "Gautam", self.oil)
        StockOwner.objects.create(company=self.oil, category="RM", user=boss)
        down = SAPConnectionError("down")
        readers = {code: FakeReader(fail=down) for code in ("JIVO_OIL", "JIVO_BEVERAGES", "JIVO_MART")}

        board = service(readers, {"JIVO_OIL": FakeNonMoving(fail=down)}).build()

        oil = board["plants"][0]
        self.assertIsNone(oil["stock"])
        self.assertEqual(oil["owners"]["RM"]["name"], "Gautam")
        self.assertIsNone(oil["owners"]["PM"])
        self.assertIsNone(board["debtors"]["total"])
        self.assertTrue(board["meta"]["warnings"])

    def test_non_moving_keeps_to_the_pages_opening_scope(self):
        nm = FakeNonMoving(
            rows=[
                {"item_code": "PM1", "warehouse": "BH-NM", "value": 100.0},
                {"item_code": "PM1", "warehouse": "BH-PM", "value": 50.0},
                {"item_code": "PM2", "warehouse": "BH-PC", "value": 999.0},
            ]
        )
        board = service(healthy_readers(), {"JIVO_OIL": nm}).build()

        tile = board["plants"][0]["non_moving"]
        self.assertEqual(nm.asked, (45, 105, True))
        self.assertEqual(tile["value"], 150.0)
        self.assertEqual(tile["item_count"], 1)
        self.assertEqual(tile["warehouses"], ["BH-NM", "BH-PM"])

    def test_payload_carries_every_key_the_screen_reads(self):
        """FactoryFlow's amounts types dereference these unguarded."""
        board = service(healthy_readers()).build()

        self.assertEqual(set(board), {"plants", "debtors", "meta"})
        plant = board["plants"][0]
        self.assertEqual(set(plant), {"company_code", "label", "owners", "stock", "non_moving"})
        self.assertEqual(set(plant["owners"]), {"RM", "PM", "FG"})
        self.assertEqual(set(plant["stock"]), {"total", "categories"})
        self.assertEqual(
            set(plant["stock"]["categories"][0]),
            {"key", "label", "item_group", "value", "items", "godowns"},
        )
        self.assertEqual(
            set(plant["stock"]["categories"][0]["godowns"][0]),
            {"code", "name", "value", "items", "managers"},
        )
        self.assertEqual(
            set(plant["non_moving"]), {"value", "item_count", "warehouses", "age_days", "item_group"}
        )
        self.assertEqual(set(board["debtors"]), {"companies", "total", "oldest_floor"})
        self.assertEqual(
            set(board["debtors"]["companies"][0]["figures"]),
            {"amount", "customers", "group_amount", "oldest"},
        )
        self.assertEqual(
            set(board["debtors"]["total"]),
            {"amount", "customers", "group_amount", "oldest", "missing"},
        )
        self.assertTrue({"generated_at", "refresh_seconds", "degraded", "withheld", "warnings"} <= set(board["meta"]))


class DebtorDrillTests(TestCase):
    def test_one_company_lists_its_customers(self):
        drill = service(healthy_readers()).debtor_drill("JWPL")

        self.assertEqual([c["card_code"] for c in drill["customers"]], ["C9", "C1", "C7"])
        self.assertEqual(drill["customers"][1]["since"], "2024-09-30")
        self.assertEqual(drill["customers"][0]["company_label"], "JWPL")
        self.assertEqual(drill["amount"], 6207.0)
        self.assertEqual(drill["missing"], [])

    def test_total_merges_every_company_largest_first(self):
        drill = service(healthy_readers()).debtor_drill("TOTAL")

        self.assertEqual(
            [(c["company_label"], c["card_code"]) for c in drill["customers"]],
            [("MART", "M1"), ("JWPL", "C9"), ("JWPL", "C1"), ("JWPL", "C7")],
        )

    def test_total_names_a_company_it_could_not_read(self):
        readers = healthy_readers()
        readers["JIVO_MART"] = FakeReader(fail=SAPDataError("bad"))
        drill = service(readers).debtor_drill("TOTAL")

        self.assertEqual(drill["missing"], ["MART"])
        self.assertEqual(len(drill["customers"]), 3)

    def test_one_company_that_cannot_be_read_is_an_error(self):
        readers = healthy_readers()
        readers["JIVO_MART"] = FakeReader(fail=SAPConnectionError("down"))
        with self.assertRaises(SAPConnectionError):
            service(readers).debtor_drill("MART")

    def test_bills_are_the_balance_with_only_the_oldest_part_paid(self):
        readers = healthy_readers()
        readers["JIVO_OIL"].customers = {
            "C1": {"CardCode": "C1", "CardName": "Old Customer", "Balance": 1200.0}
        }
        # Newest-first running totals, as SAP returns them oldest first: the
        # newest bill is 800, the older one 1000 of which 400 is still owed.
        readers["JIVO_OIL"].debits = [
            {
                "TransId": 10,
                "LineId": 0,
                "RefDate": date(2024, 9, 30),
                "DueDate": date(2020, 1, 11),
                "TransType": "13",
                "BaseRef": "1601201044",
                "LineMemo": "A/R Invoices - C1",
                "Debit": 1000.0,
                "Cum": 1800.0,
            },
            {
                "TransId": 20,
                "LineId": 0,
                "RefDate": date(2026, 9, 1),
                "DueDate": date(2026, 10, 1),
                "TransType": "30",
                "BaseRef": "99",
                "LineMemo": "",
                "Debit": 800.0,
                "Cum": 800.0,
            },
        ]

        bills = service(readers).debtor_bills("JIVO_OIL", "C1")

        self.assertEqual(readers["JIVO_OIL"].calls[-1], ("debits", "C1", 1200.0))
        self.assertEqual([b["unpaid"] for b in bills["bills"]], [400.0, 800.0])
        self.assertEqual(sum(b["unpaid"] for b in bills["bills"]), bills["balance"])
        self.assertEqual(bills["bills"][0]["type"], "A/R Invoice")
        self.assertEqual(bills["bills"][0]["due_date"], "2020-01-11")
        self.assertEqual(bills["bills"][1]["type"], "Journal Entry")

    def test_no_bills_for_a_customer_in_credit_and_none_for_a_stranger(self):
        readers = healthy_readers()
        readers["JIVO_OIL"].customers = {
            "C2": {"CardCode": "C2", "CardName": "Paid Ahead", "Balance": -500.0}
        }
        svc = service(readers)

        self.assertEqual(svc.debtor_bills("JIVO_OIL", "C2")["bills"], [])
        self.assertNotIn("debits", [c[0] for c in readers["JIVO_OIL"].calls])
        self.assertIsNone(svc.debtor_bills("JIVO_OIL", "NOBODY"))


class ApiTests(TestCase):
    def setUp(self):
        self.oil = make_company("JIVO_OIL", "Jivo Oil")
        self.bev = make_company("JIVO_BEVERAGES", "Jivo Beverages")
        make_company("JIVO_MART", "Jivo Mart")
        self.user = make_user("viewer@example.com", "Viewer", self.oil)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.client.credentials(HTTP_COMPANY_CODE="JIVO_OIL")

    def grant(self, codename):
        self.user.user_permissions.add(Permission.objects.get(codename=codename))
        self.user = User.objects.get(pk=self.user.pk)  # drop the permission cache
        self.client.force_authenticate(user=self.user)

    def test_board_refuses_a_reader_without_the_right(self):
        response = self.client.get(reverse("amounts_board:amounts-board"))
        self.assertEqual(response.status_code, 403)

    @mock.patch("amounts_board.views.AmountsBoardService")
    def test_board_opens_with_the_right(self, service_cls):
        service_cls.return_value.build.return_value = {"plants": [], "debtors": {}, "meta": {}}
        self.grant("can_view_amounts_board")
        response = self.client.get(reverse("amounts_board:amounts-board"))
        self.assertEqual(response.status_code, 200)

    def test_godown_items_validates_its_query(self):
        self.grant("can_view_amounts_board")
        url = reverse("amounts_board:amounts-board-godown-items")
        self.assertEqual(self.client.get(url, {"company": "JIVO_MART", "category": "RM", "warehouse": "X"}).status_code, 400)
        self.assertEqual(self.client.get(url, {"company": "JIVO_OIL", "category": "XX", "warehouse": "X"}).status_code, 400)
        self.assertEqual(self.client.get(url, {"company": "JIVO_OIL", "category": "RM"}).status_code, 400)

    @mock.patch("amounts_board.views.AmountsBoardService")
    def test_godown_items_maps_sap_failures(self, service_cls):
        self.grant("can_view_amounts_board")
        service_cls.return_value.godown_items.side_effect = SAPConnectionError("down")
        url = reverse("amounts_board:amounts-board-godown-items")
        response = self.client.get(url, {"company": "jivo_oil", "category": "rm", "warehouse": "bh-lo"})
        self.assertEqual(response.status_code, 503)
        service_cls.return_value.godown_items.assert_called_once_with("JIVO_OIL", "RM", "BH-LO")

    def test_debtors_validates_its_key(self):
        self.grant("can_view_amounts_board")
        url = reverse("amounts_board:amounts-board-debtors")
        self.assertEqual(self.client.get(url, {"debtor": "JIVO_OIL"}).status_code, 400)
        self.assertEqual(self.client.get(url).status_code, 400)

    @mock.patch("amounts_board.views.AmountsBoardService")
    def test_debtors_reads_the_tile_asked_for(self, service_cls):
        self.grant("can_view_amounts_board")
        service_cls.return_value.debtor_drill.return_value = {"customers": []}
        url = reverse("amounts_board:amounts-board-debtors")
        self.assertEqual(self.client.get(url, {"debtor": "total"}).status_code, 200)
        service_cls.return_value.debtor_drill.assert_called_once_with("TOTAL")

        service_cls.return_value.debtor_drill.side_effect = SAPDataError("bad")
        self.assertEqual(self.client.get(url, {"debtor": "MART"}).status_code, 502)

    @mock.patch("amounts_board.views.AmountsBoardService")
    def test_debtor_bills_validates_and_404s_a_stranger(self, service_cls):
        self.grant("can_view_amounts_board")
        url = reverse("amounts_board:amounts-board-debtor-bills")
        self.assertEqual(self.client.get(url, {"company": "X", "customer": "C1"}).status_code, 400)
        self.assertEqual(self.client.get(url, {"company": "JIVO_MART"}).status_code, 400)

        service_cls.return_value.debtor_bills.return_value = None
        response = self.client.get(url, {"company": "jivo_mart", "customer": "c1"})
        self.assertEqual(response.status_code, 404)
        service_cls.return_value.debtor_bills.assert_called_once_with("JIVO_MART", "C1")

    def test_debtor_reads_need_the_board_right(self):
        self.assertEqual(
            self.client.get(reverse("amounts_board:amounts-board-debtors"), {"debtor": "MART"}).status_code,
            403,
        )

    def test_owners_need_the_manage_right(self):
        self.grant("can_view_amounts_board")
        self.assertEqual(self.client.get(reverse("amounts_board:amounts-board-owners")).status_code, 403)

    def test_owner_is_set_replaced_and_cleared(self):
        self.grant("can_manage_stock_owners")
        url = reverse("amounts_board:amounts-board-owners")
        first = make_user("a@example.com", "Asha", self.bev)
        second = make_user("b@example.com", "Bilal", self.bev)

        listing = self.client.get(url).json()
        bev = [p for p in listing["plants"] if p["company_code"] == "JIVO_BEVERAGES"][0]
        self.assertEqual({c["name"] for c in bev["candidates"]}, {"Asha", "Bilal"})

        response = self.client.put(url, {"company": "JIVO_BEVERAGES", "category": "PM", "user": first.id}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["owner"]["name"], "Asha")

        self.client.put(url, {"company": "JIVO_BEVERAGES", "category": "PM", "user": second.id}, format="json")
        owner = StockOwner.objects.get(company=self.bev, category="PM")
        self.assertEqual(owner.user, second)
        self.assertEqual(owner.updated_by, self.user)

        response = self.client.put(url, {"company": "JIVO_BEVERAGES", "category": "PM", "user": None}, format="json")
        self.assertEqual(response.json()["owner"], None)
        self.assertFalse(StockOwner.objects.exists())

    def test_owner_must_be_staff_of_that_plant(self):
        self.grant("can_manage_stock_owners")
        oil_only = make_user("o@example.com", "Oil Only", self.oil)
        response = self.client.put(
            reverse("amounts_board:amounts-board-owners"),
            {"company": "JIVO_BEVERAGES", "category": "FG", "user": oil_only.id},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(StockOwner.objects.exists())
