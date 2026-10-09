"""
production_dispatch/tests.py

The service is driven with a fake reader -- no HANA -- so these pin what the
report does with SAP's rows: group companies left out of every figure but
reported, FAST / SLOW over the 90 days ending on the To date whatever range is
read, SAP's packing type with its misspelling corrected, and the range rules
on the API.
"""

from datetime import date
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from company.models import Company, UserCompany, UserRole
from sap_client.exceptions import SAPConnectionError, SAPDataError

from .services import FAST, SLOW, ProductionDispatchService, movement, packing_type

User = get_user_model()


def item_row(code, packing="PET BOTTLE", pieces=16, litres=1.0):
    return {
        "ItemCode": code,
        "ItemName": f"Item {code}",
        "Variety": "OLIVE",
        "Subgroup": "POMACE",
        "Sku": "1 LTR",
        "PackingType": packing,
        "PiecesPerBox": pieces,
        "LitresPerUnit": litres,
    }


def row(kind, day, code, qty, group=0):
    return {"Kind": kind, "Day": day, "ItemCode": code, "IsGroup": group, "Qty": qty}


class FakeReader:
    def __init__(self, items=(), daily=(), documents=()):
        self._items = list(items)
        self._daily = list(daily)
        self._documents = list(documents)
        self.calls = []

    def items(self):
        return self._items

    def daily(self, date_from, date_to, codes):
        self.calls.append(("daily", date_from, date_to, list(codes)))
        return [r for r in self._daily if date_from <= r["Day"] <= date_to]

    def documents(self, date_from, date_to, codes, item_code=None):
        self.calls.append(("documents", date_from, date_to, item_code))
        return self._documents


def build(reader, date_from, date_to):
    service = ProductionDispatchService(reader_factory=lambda: reader, today=date(2026, 10, 9))
    return service.build(date_from, date_to)


class MovementRuleTests(SimpleTestCase):
    def test_a_month_of_production_cleared_in_a_month_is_fast(self):
        self.assertEqual(movement(900, 900), {"movement": FAST, "days_to_dispatch": 30.0})

    def test_making_more_than_was_sold_is_slow(self):
        self.assertEqual(movement(1000, 500), {"movement": SLOW, "days_to_dispatch": 60.0})

    def test_sold_but_not_made_is_fast_from_opening_stock(self):
        self.assertEqual(movement(0, 10), {"movement": FAST, "days_to_dispatch": 0.0})

    def test_not_sold_at_all_is_slow(self):
        self.assertEqual(movement(10, 0), {"movement": SLOW, "days_to_dispatch": None})
        self.assertEqual(movement(0, 0), {"movement": SLOW, "days_to_dispatch": None})


class PackingTypeTests(SimpleTestCase):
    def test_saps_misspelling_reads_as_hdpe(self):
        self.assertEqual(packing_type("HDFPE BOTTLE"), "HDPE BOTTLE")
        self.assertEqual(packing_type(" hdfpe  bottle "), "HDPE BOTTLE")

    def test_blank_is_not_set_rather_than_guessed(self):
        self.assertIsNone(packing_type(""))
        self.assertIsNone(packing_type(None))

    def test_other_types_pass_through(self):
        self.assertEqual(packing_type("Tin"), "TIN")


class ReportTests(SimpleTestCase):
    def setUp(self):
        self.reader = FakeReader(
            items=[
                item_row("FG1"),
                item_row("FG2", packing="HDFPE BOTTLE", pieces=4, litres=5.0),
                item_row("FG3", packing="", pieces=0, litres=15.0),
            ],
            daily=[
                # Before the range, inside the movement window.
                row("PRODUCTION", date(2026, 8, 1), "FG1", 100),
                row("DISPATCH", date(2026, 8, 2), "FG1", 50),
                # Inside the range.
                row("PRODUCTION", date(2026, 9, 29), "FG1", 20),
                row("DISPATCH", date(2026, 9, 29), "FG1", 30),
                row("DISPATCH", date(2026, 9, 29), "FG1", 400, group=1),
                row("DISPATCH", date(2026, 9, 30), "FG2", 8),
                # Sold only to a group company: listed, but its counted dispatch is nil.
                row("DISPATCH", date(2026, 9, 30), "FG3", 5, group=1),
                # Before the movement window: read for nothing.
                row("DISPATCH", date(2026, 6, 1), "FG2", 999),
            ],
        )

    def test_reads_back_to_the_start_of_the_movement_window(self):
        report = build(self.reader, date(2026, 9, 29), date(2026, 9, 30))
        _, read_from, read_to, codes = self.reader.calls[0]
        self.assertEqual(read_from, date(2026, 7, 3))  # 90 days ending 30 Sep
        self.assertEqual(read_to, date(2026, 9, 30))
        self.assertIn("CUSTA000606", codes)  # Jivo Mart
        self.assertEqual(report["movement_window"], {"from": "2026-07-03", "to": "2026-09-30"})

    def test_reads_the_whole_range_when_it_is_longer_than_the_window(self):
        build(self.reader, date(2026, 1, 1), date(2026, 9, 30))
        self.assertEqual(self.reader.calls[0][1], date(2026, 1, 1))

    def test_days_hold_only_the_range_with_group_sales_apart(self):
        report = build(self.reader, date(2026, 9, 29), date(2026, 9, 30))
        days = {(d["date"], d["item_code"]): d for d in report["days"]}
        self.assertEqual(
            days[("2026-09-29", "FG1")],
            {
                "date": "2026-09-29",
                "item_code": "FG1",
                "production": 20.0,
                "dispatch": 30.0,
                "group_dispatch": 400.0,
            },
        )
        self.assertEqual(days[("2026-09-30", "FG3")]["group_dispatch"], 5.0)
        self.assertNotIn(("2026-08-01", "FG1"), days)

    def test_movement_is_judged_over_the_window_without_group_sales(self):
        report = build(self.reader, date(2026, 9, 29), date(2026, 9, 30))
        items = {i["item_code"]: i for i in report["items"]}
        # FG1: made 120, sold 80 to outside customers in the window -> 45 days.
        self.assertEqual(items["FG1"]["window_production"], 120.0)
        self.assertEqual(items["FG1"]["window_dispatch"], 80.0)
        self.assertEqual(items["FG1"]["movement"], SLOW)
        self.assertEqual(items["FG1"]["days_to_dispatch"], 45.0)
        # FG2: sold, not made -> FAST; the June sale is outside the window.
        self.assertEqual(items["FG2"]["window_dispatch"], 8.0)
        self.assertEqual(items["FG2"]["movement"], FAST)
        # FG3: only a group company bought it -> SLOW.
        self.assertEqual(items["FG3"]["movement"], SLOW)

    def test_items_carry_saps_factors(self):
        report = build(self.reader, date(2026, 9, 29), date(2026, 9, 30))
        items = {i["item_code"]: i for i in report["items"]}
        self.assertEqual(items["FG2"]["packing_type"], "HDPE BOTTLE")
        self.assertEqual(items["FG2"]["pieces_per_box"], 4.0)
        self.assertEqual(items["FG2"]["litres_per_unit"], 5.0)
        # No pack size: a single unit is its own box. No packing type: None.
        self.assertEqual(items["FG3"]["pieces_per_box"], 1.0)
        self.assertIsNone(items["FG3"]["packing_type"])

    def test_settings_are_the_workbooks(self):
        report = build(self.reader, date(2026, 9, 29), date(2026, 9, 30))
        self.assertEqual(
            report["settings"],
            {
                "pallet_litres": 800,
                "oil_density": 0.91,
                "fast_days": 30,
                "month_days": 30,
                "movement_window_days": 90,
            },
        )
        self.assertEqual(report["company"]["code"], "JIVO_OIL")

    def test_documents_flag_group_customers(self):
        reader = FakeReader(
            documents=[
                {
                    "Kind": "DISPATCH",
                    "DocType": "A/R Invoice",
                    "Day": date(2026, 9, 30),
                    "DocNum": 626090301,
                    "CardCode": "CUSTA000606",
                    "CardName": "JIVO MART PVT LTD",
                    "IsGroup": 1,
                    "Warehouse": "BH-PF",
                    "ItemCode": "FG1",
                    "ItemName": "Item FG1",
                    "Qty": 40,
                }
            ]
        )
        service = ProductionDispatchService(reader_factory=lambda: reader)
        payload = service.documents(date(2026, 9, 30), date(2026, 9, 30), item_code="FG1")
        self.assertEqual(reader.calls[0], ("documents", date(2026, 9, 30), date(2026, 9, 30), "FG1"))
        self.assertTrue(payload["lines"][0]["is_group"])
        self.assertEqual(payload["lines"][0]["date"], "2026-09-30")
        self.assertEqual(payload["lines"][0]["quantity"], 40.0)


class ApiTests(TestCase):
    def setUp(self):
        oil = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        self.user = User.objects.create_user(email="viewer@example.com", full_name="Viewer", password="x")
        role, _ = UserRole.objects.get_or_create(name="Staff")
        UserCompany.objects.create(user=self.user, company=oil, role=role, is_default=True, is_active=True)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.client.credentials(HTTP_COMPANY_CODE="JIVO_OIL")
        self.url = reverse("production_dispatch:production-dispatch-report")

    def grant(self):
        self.user.user_permissions.add(Permission.objects.get(codename="can_view_production_dispatch"))
        self.user = User.objects.get(pk=self.user.pk)  # drop the permission cache
        self.client.force_authenticate(user=self.user)

    def test_refuses_a_reader_without_the_right(self):
        response = self.client.get(self.url, {"from": "2026-09-01", "to": "2026-09-30"})
        self.assertEqual(response.status_code, 403)

    @mock.patch("production_dispatch.views.ProductionDispatchService")
    def test_opens_with_the_right(self, service_cls):
        service_cls.return_value.build.return_value = {"items": [], "days": []}
        self.grant()
        response = self.client.get(self.url, {"from": "2026-09-01", "to": "2026-09-30"})
        self.assertEqual(response.status_code, 200)
        service_cls.return_value.build.assert_called_once_with(date(2026, 9, 1), date(2026, 9, 30))

    @mock.patch("production_dispatch.views.timezone.localdate", return_value=date(2026, 10, 9))
    def test_validates_the_range(self, _today):
        self.grant()
        for params in (
            {"from": "2026-09-01"},
            {"from": "yesterday", "to": "2026-09-30"},
            {"from": "2026-09-30", "to": "2026-09-01"},
            {"from": "2026-10-01", "to": "2026-10-10"},  # a day not yet happened
            {"from": "2025-01-01", "to": "2026-09-30"},  # longer than a year
        ):
            self.assertEqual(self.client.get(self.url, params).status_code, 400, params)

    @mock.patch("production_dispatch.views.ProductionDispatchService")
    def test_maps_sap_failures(self, service_cls):
        self.grant()
        params = {"from": "2026-09-01", "to": "2026-09-30"}
        service_cls.return_value.build.side_effect = SAPConnectionError("down")
        self.assertEqual(self.client.get(self.url, params).status_code, 503)
        service_cls.return_value.build.side_effect = SAPDataError("bad")
        self.assertEqual(self.client.get(self.url, params).status_code, 502)

    @mock.patch("production_dispatch.views.ProductionDispatchService")
    def test_documents_pass_the_item(self, service_cls):
        service_cls.return_value.documents.return_value = {"lines": []}
        self.grant()
        url = reverse("production_dispatch:production-dispatch-documents")
        response = self.client.get(url, {"from": "2026-09-01", "to": "2026-09-30", "item": "fg0000005"})
        self.assertEqual(response.status_code, 200)
        service_cls.return_value.documents.assert_called_once_with(
            date(2026, 9, 1), date(2026, 9, 30), item_code="FG0000005"
        )
