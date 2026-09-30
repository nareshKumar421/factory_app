"""Oil prices: reading the price sheet, keeping one set a day, reading days back.

The promises worth pinning down:
  - the sheet's two tables are found by their labels, read to their last row,
    and each commodity of the JIVO RATE block is taken from its first column;
  - a day read twice keeps one set, the later; other days are never touched;
  - a day comes back beside the previous day the sheet was read;
  - the sheet not answering is a 503 with its reason, and EXIM's rights gate
    reading, previewing and saving separately;
  - EXIM's history fills only the days missing here.
"""

import csv
import io
from datetime import date
from decimal import Decimal
from io import StringIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase
from rest_framework.test import APIClient, APITestCase

from company.models import Company, UserCompany, UserRole

from . import price_sheet
from . import services_price as sp
from .models_price import CommodityPrice, PackRate, PriceSource
from .price_import import import_prices
from .services_licence import EximError

D = Decimal
BASE = "/api/v1/exim/"


def sheet_rows():
    """The price sheet's shape, cut down: the JIVO RATE block top right with a
    second SOYA further along, the Commodities table below with its GST column."""
    grid = [[""] * 26 for _ in range(12)]

    def put(r, c, v):
        grid[r][c] = v

    put(0, 1, "SOYA"), put(0, 11, "JIVO RATE"), put(0, 12, "SOYA"), put(0, 14, "Mustard"),
    put(0, 18, "Cotton\nRefined"), put(0, 23, "SOYA")
    put(1, 11, "Pouch 1 Ltr"), put(1, 12, "151"), put(1, 14, "181"), put(1, 23, "9999")
    put(2, 11, "15 Kg Tin"), put(2, 12, "2,562"), put(2, 14, "3050"), put(2, 18, "2752")
    put(4, 11, "Not a pack: after the blank row")
    put(5, 1, "Commodities"), put(5, 3, "Factory (In Kg)")
    put(6, 1, "Soya DO"), put(6, 3, "141.50"), put(6, 5, "155.50"), put(6, 6, "7.775"), put(6, 7, "163.28"),
    put(6, 9, "148.58")
    put(7, 1, "Mustard Kachi Ghani"), put(7, 3, "176.10"), put(7, 5, "190.10"), put(7, 7, "199.61"),
    put(7, 9, "181.64")
    put(9, 1, "A note under the table")
    out = io.StringIO()
    csv.writer(out).writerows(grid)
    return list(csv.reader(io.StringIO(out.getvalue())))


class SheetTests(SimpleTestCase):
    def test_the_commodities_table_is_read_to_its_last_row(self):
        prices = price_sheet.commodity_prices(sheet_rows())
        self.assertEqual([p["commodity"] for p in prices], ["Soya DO", "Mustard Kachi Ghani"])
        self.assertEqual(
            (prices[0]["factory_price_kg"], prices[0]["packed_price_kg"], prices[0]["with_gst_kg"],
             prices[0]["with_gst_litre"]),
            (D("141.50"), D("155.50"), D("163.28"), D("148.58")),
        )

    def test_jivo_rates_take_each_commodity_from_its_first_column(self):
        rates = {(r["pack_type"], r["commodity"]): r["rate"] for r in price_sheet.pack_rates(sheet_rows())}
        self.assertEqual(rates, {
            ("Pouch 1 Ltr", "SOYA"): D("151"), ("Pouch 1 Ltr", "Mustard"): D("181"),
            ("15 Kg Tin", "SOYA"): D("2562"), ("15 Kg Tin", "Mustard"): D("3050"),
            ("15 Kg Tin", "Cotton Refined"): D("2752"),
        })

    def test_a_missing_table_says_which(self):
        with self.assertRaisesMessage(price_sheet.PriceSheetError, "Commodities"):
            price_sheet.commodity_prices([["nothing here"]])
        with self.assertRaisesMessage(price_sheet.PriceSheetError, "JIVO RATE"):
            price_sheet.pack_rates([["nothing here"]])

    def test_google_not_answering_is_a_sheet_error(self):
        import requests

        with mock.patch("exim.price_sheet.requests.get", side_effect=requests.ConnectionError("down")):
            with self.assertRaises(price_sheet.PriceSheetError):
                price_sheet.read_sheet()


def price(commodity="Soya DO", factory="141.50"):
    f = D(factory)
    return {"commodity": commodity, "factory_price_kg": f, "packed_price_kg": f + 14,
            "with_gst_kg": (f + 14) * D("1.05"), "with_gst_litre": D("1")}


class DayTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")

    def test_a_day_read_twice_keeps_the_later_and_other_days_alone(self):
        sp.save_prices(self.company, [price(factory="140")], day=date(2026, 9, 28))
        sp.save_prices(self.company, [price(factory="141")], day=date(2026, 9, 29))
        counts = sp.save_prices(self.company, [price(factory="142")], day=date(2026, 9, 29))
        self.assertEqual(counts, {"created": 0, "updated": 1})
        self.assertEqual(
            list(CommodityPrice.objects.order_by("date").values_list("date", "factory_price_kg")),
            [(date(2026, 9, 28), D("140.00")), (date(2026, 9, 29), D("142.00"))],
        )

    def test_a_day_comes_back_beside_the_previous_day_read(self):
        sp.save_prices(self.company, [price(factory="140"), price("Sunflower", "160")], day=date(2026, 9, 26))
        sp.save_prices(self.company, [price(factory="141"), price("Mustard Deo", "164")], day=date(2026, 9, 29))
        day = sp.price_day(self.company, date(2026, 9, 30))  # nothing that day: the latest before it
        self.assertEqual((day["date"], day["previous_date"]), (date(2026, 9, 29), date(2026, 9, 26)))
        by = {p["commodity"]: p for p in day["prices"]}
        self.assertEqual(by["Soya DO"]["previous"]["factory_price_kg"], D("140.00"))
        self.assertIsNone(by["Mustard Deo"]["previous"])
        self.assertEqual((day["first_date"], day["last_date"]), (date(2026, 9, 26), date(2026, 9, 29)))
        earlier = sp.price_day(self.company, date(2026, 9, 26))
        self.assertEqual((earlier["previous_date"], earlier["next_date"]), (None, date(2026, 9, 29)))

    def test_no_prices_yet_is_an_empty_day(self):
        self.assertEqual(sp.price_day(self.company)["prices"], [])

    def test_pack_rates_come_with_the_previous_days(self):
        sp.save_rates(self.company, [{"pack_type": "Pouch 1 Ltr", "commodity": "SOYA", "rate": D("150")}],
                      day=date(2026, 9, 28))
        sp.save_rates(self.company, [{"pack_type": "Pouch 1 Ltr", "commodity": "SOYA", "rate": D("151")}],
                      day=date(2026, 9, 29))
        day = sp.rate_day(self.company)
        self.assertEqual(day["rates"][0]["rate"], D("151.000"))
        self.assertEqual(day["rates"][0]["previous"], D("150.000"))
        self.assertEqual(day["rates"][0]["source"], PriceSource.SHEET)
        self.assertEqual((day["packs"], day["commodities"]), (["Pouch 1 Ltr"], ["SOYA"]))

    def test_a_range_must_run_forwards_and_not_too_far(self):
        with self.assertRaises(EximError):
            sp.price_range(self.company, date(2026, 9, 2), date(2026, 9, 1))
        with self.assertRaises(EximError):
            sp.price_range(self.company, date(2024, 1, 1), date(2026, 9, 1))


class ImportTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")

    def test_exims_history_fills_only_the_days_missing(self):
        sp.save_prices(self.company, [price(factory="141")], day=date(2026, 9, 29))
        snapshot = {
            "prices": [
                {"commodity_name": "Soya DO", "date": date(2026, 9, 29), "factory_price": D("999"),
                 "packing_cost_kg": D("1"), "with_gst_kg": D("1"), "with_gst_ltr": D("1")},
                {"commodity_name": "Soya DO", "date": date(2026, 9, 28), "factory_price": D("140"),
                 "packing_cost_kg": D("154"), "with_gst_kg": D("161.70"), "with_gst_ltr": D("147.15")},
            ],
            "rates": [{"pack_type": "Pouch 1 Ltr", "commodity": "SOYA", "rate": D("150"), "date": date(2026, 9, 28)}],
        }
        report = import_prices(snapshot, company=self.company)
        self.assertEqual((report.counts["prices"]["create"], report.counts["prices"]["kept"]), (1, 1))
        self.assertEqual(CommodityPrice.objects.get(date=date(2026, 9, 29)).factory_price_kg, D("141.00"))
        self.assertEqual(CommodityPrice.objects.get(date=date(2026, 9, 28)).source, PriceSource.EXIM)
        self.assertEqual(PackRate.objects.count(), 1)
        again = import_prices(snapshot, company=self.company)
        self.assertEqual(again.counts["prices"]["create"], 0)


class SyncCommandTests(TestCase):
    def setUp(self):
        Company.objects.create(name="Jivo Oil", code="JIVO_OIL")

    def test_a_dry_run_saves_nothing_and_a_missed_night_fails_loudly(self):
        with mock.patch("exim.price_sheet.fetch_rows", return_value=sheet_rows()):
            call_command("sync_oil_prices", "--dry-run", stdout=StringIO())
        self.assertEqual(CommodityPrice.objects.count(), 0)
        with mock.patch("exim.price_sheet.fetch_rows", return_value=sheet_rows()):
            call_command("sync_oil_prices", stdout=StringIO())
        self.assertEqual((CommodityPrice.objects.count(), PackRate.objects.count()), (2, 5))
        with mock.patch("exim.price_sheet.fetch_rows", side_effect=price_sheet.PriceSheetError("down")):
            with self.assertRaises(CommandError):
                call_command("sync_oil_prices", stdout=StringIO())


class PriceAPITests(APITestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        self.role = UserRole.objects.create(name="Import / Export")
        self.headers = {"HTTP_COMPANY_CODE": "JIVO_OIL"}

    def client_for(self, *rights):
        n = get_user_model().objects.count() + 1
        user = get_user_model().objects.create_user(email=f"p{n}@example.com", password="x", full_name="P",
                                                    employee_code=f"P{n}")
        UserCompany.objects.create(user=user, company=self.company, role=self.role, is_default=True)
        user.user_permissions.add(*Permission.objects.filter(content_type__app_label="exim", codename__in=rights))
        client = APIClient()
        client.force_authenticate(user)
        return client

    def test_viewing_previewing_and_saving_are_separate_rights(self):
        viewer = self.client_for("view_dailyprice")
        self.assertEqual(viewer.get(f"{BASE}prices/", **self.headers).status_code, 200)
        self.assertEqual(viewer.get(f"{BASE}prices/sheet/", **self.headers).status_code, 403)
        fetcher = self.client_for("fetch_daily_price")
        with mock.patch("exim.price_sheet.fetch_rows", return_value=sheet_rows()):
            preview = fetcher.get(f"{BASE}prices/sheet/", **self.headers)
            self.assertEqual(preview.status_code, 200)
            self.assertEqual(len(preview.data["prices"]), 2)
            self.assertEqual(fetcher.post(f"{BASE}prices/sheet/", **self.headers).status_code, 403)
            saved = self.client_for("add_dailyprice").post(f"{BASE}prices/sheet/", **self.headers)
        self.assertEqual(saved.status_code, 200)
        self.assertEqual((saved.data["created"], len(saved.data["prices"])), (2, 2))
        self.assertEqual(self.client_for().get(f"{BASE}prices/", **self.headers).status_code, 403)

    def test_the_sheet_not_answering_is_a_503_with_its_reason(self):
        with mock.patch("exim.price_sheet.fetch_rows", side_effect=price_sheet.PriceSheetError("Google is down")):
            response = self.client_for("add_jivorates").post(f"{BASE}pack-rates/sheet/", **self.headers)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.data["code"], "price_sheet_unavailable")

    def test_a_range_needs_both_ends(self):
        client = self.client_for("view_jivorates")
        self.assertEqual(client.get(f"{BASE}pack-rates/range/", {"from": "2026-09-01"}, **self.headers).status_code,
                         400)
        response = client.get(f"{BASE}pack-rates/range/", {"from": "2026-09-01", "to": "2026-09-30"}, **self.headers)
        self.assertEqual(response.status_code, 200)
