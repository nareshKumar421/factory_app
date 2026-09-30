"""The SAP copies: taken once a night, kept through a failed night, and served
only while HANA does not answer.

No HANA: the live reads are patched at ``HanaConnection.connect``, which both
readers go through, and the copy's own fetches at ``sap_mirror.services``.
"""

from datetime import datetime
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone
from hdbcli import dbapi
from rest_framework.test import APIClient

from barcode.services.oitm_item_service import OitmItemReadError, OitmItemService
from company.models import Company, UserCompany, UserRole
from sap_client.exceptions import SAPConnectionError
from sap_client.hana.connection import HanaConnection

from . import services
from .models import MirrorDataset, MirrorRow


def at(day, hour, minute=0):
    return timezone.make_aware(datetime(2026, 9, day, hour, minute))


def item(code, name, sal_factor2=12):
    return {
        "item_code": code, "item_name": name, "inventory_uom": "PCS", "sales_uom": "BOX",
        "purchase_uom": "", "item_group_code": 102, "manage_batch_numbers": True,
        "manage_serial_numbers": False, "is_inventory_item": True, "is_sales_item": True,
        "is_purchase_item": False, "sal_factor2": sal_factor2, "pieces_per_box": sal_factor2,
        "pieces_per_box_source": "sap", "valid_for": True, "frozen_for": False,
    }


OIL_ITEMS = [item("FG0000151", "Olive Oil 1 LTR"), item("FG0000329", "Mustard Oil 5 LTR", 4)]
OIL_WAREHOUSES = [{"code": "BH-PC", "name": "Panchkula FG"}, {"code": "BH-BT", "name": "Barotiwala FG"}]


class FakeSAP:
    """What each company's SAP answers the copy's fetches; ``down`` for an outage."""

    def __init__(self):
        self.items = {"JIVO_OIL": list(OIL_ITEMS), "JIVO_MART": [item("FG0000900", "Mart Pack")]}
        self.warehouses = {"JIVO_OIL": list(OIL_WAREHOUSES), "JIVO_MART": [{"code": "MRT", "name": "Mart"}]}
        self.down = False

    def fetch_items(self, code):
        if self.down:
            raise SAPConnectionError("Unable to connect to SAP HANA.")
        return self.items[code]

    def fetch_warehouses(self, code):
        if self.down:
            raise SAPConnectionError("Unable to connect to SAP HANA.")
        return self.warehouses[code]


class CopyTestCase(TestCase):
    def setUp(self):
        self.oil = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        self.mart = Company.objects.create(name="Jivo Mart", code="JIVO_MART")
        self.sap = FakeSAP()
        for target, fake in (
            ("sap_mirror.services._fetch_fg_items", self.sap.fetch_items),
            ("sap_mirror.services._fetch_warehouses", self.sap.fetch_warehouses),
        ):
            patcher = patch(target, side_effect=fake)
            patcher.start()
            self.addCleanup(patcher.stop)
        # The master lists only; the bill copy has its own tests (tests_bills).
        lists_only = patch.dict(
            services.DATASETS,
            {name: spec for name, spec in services.DATASETS.items() if name != services.BILLS},
            clear=True,
        )
        lists_only.start()
        self.addCleanup(lists_only.stop)

    def copy_of(self, company, name):
        return MirrorDataset.objects.get(company=company, name=name)


class TakingTheCopiesTests(CopyTestCase):
    def test_the_first_run_copies_every_list_for_every_sap_company(self):
        Company.objects.create(name="No SAP here", code="NOSAP")

        services.sync_due(at(30, 14))

        self.assertEqual(MirrorDataset.objects.count(), 4)
        items = self.copy_of(self.oil, services.FG_ITEMS)
        self.assertEqual(items.row_count, 2)
        self.assertEqual(items.synced_at, at(30, 14))
        self.assertEqual(
            list(items.rows.values_list("key", flat=True)), ["FG0000151", "FG0000329"]
        )
        self.assertEqual(items.rows.get(key="FG0000329").data["pieces_per_box"], 4)
        self.assertEqual(self.copy_of(self.mart, services.WAREHOUSES).rows.get().key, "MRT")

    def test_a_copy_is_taken_once_a_night(self):
        services.sync_due(at(29, 14))

        self.assertEqual(services.sync_due(at(29, 23, 45)), [])
        self.assertEqual(services.sync_due(at(30, 0, 45)), [])
        # The first run after 01:00 takes the night's copy.
        self.assertEqual(len(services.sync_due(at(30, 1, 5))), 4)
        self.assertEqual(services.sync_due(at(30, 1, 20)), [])

    def test_sap_not_answering_keeps_the_last_copy_and_tries_again(self):
        services.sync_due(at(29, 14))
        self.sap.down = True

        services.sync_due(at(30, 1, 5))

        items = self.copy_of(self.oil, services.FG_ITEMS)
        self.assertEqual(items.synced_at, at(29, 14))
        self.assertEqual(items.rows.count(), 2)
        self.assertIn("the copy was kept", items.last_error)
        # Still due: the next run tries again, and a good answer clears the error.
        self.sap.down = False
        self.assertEqual(len(services.sync_due(at(30, 1, 20))), 4)
        items.refresh_from_db()
        self.assertEqual(items.synced_at, at(30, 1, 20))
        self.assertEqual(items.last_error, "")

    def test_an_empty_answer_keeps_the_last_copy(self):
        services.sync_due(at(29, 14))
        self.sap.items["JIVO_OIL"] = []

        services.sync_due(at(30, 1, 5))

        items = self.copy_of(self.oil, services.FG_ITEMS)
        self.assertEqual(items.rows.count(), 2)
        self.assertIn("no rows", items.last_error)

    def test_an_item_sap_no_longer_offers_leaves_the_copy(self):
        services.sync_due(at(29, 14))
        self.sap.items["JIVO_OIL"] = [OIL_ITEMS[0]]  # FG0000329 frozen in SAP

        services.sync_due(at(30, 1, 5))

        items = self.copy_of(self.oil, services.FG_ITEMS)
        self.assertEqual(list(items.rows.values_list("key", flat=True)), ["FG0000151"])
        self.assertEqual(items.row_count, 1)

    def test_the_command_can_take_every_copy_now(self):
        services.sync_due(timezone.now())
        MirrorRow.objects.all().delete()

        call_command("sync_sap_copy", "--force", stdout=StringIO())

        self.assertEqual(self.copy_of(self.oil, services.FG_ITEMS).rows.count(), 2)


class _Cursor:
    def __init__(self, rows=(), description=(), refuse=False):
        self.rows, self.description, self.refuse = list(rows), description, refuse

    def execute(self, sql, params=None):
        if self.refuse:
            raise dbapi.ProgrammingError(260, "invalid column name")

    def fetchall(self):
        return self.rows

    def close(self):
        pass


class _Connection:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    def close(self):
        pass


HANA_DOWN = dbapi.OperationalError(-10709, "Connection failed (RTE:[89006] System call 'connect' failed)")


class ServingTheCopyTests(CopyTestCase):
    def setUp(self):
        super().setUp()
        services.sync_due(at(30, 1, 5))
        self.picker = OitmItemService("JIVO_OIL")

    def hana(self, **behaviour):
        if "down" in behaviour:
            return patch.object(HanaConnection, "connect", side_effect=HANA_DOWN)
        return patch.object(
            HanaConnection, "connect", return_value=_Connection(_Cursor(**behaviour))
        )

    def test_hana_down_serves_the_picker_from_the_copy_and_says_how_old_it_is(self):
        with self.hana(down=True):
            rows = self.picker.list_items(search="olive")

        self.assertEqual([row["item_code"] for row in rows], ["FG0000151"])
        self.assertEqual(rows[0]["pieces_per_box"], 12)
        self.assertEqual(rows[0]["sap_copy_as_of"], at(30, 1, 5).isoformat())

    def test_the_copy_honours_the_pickers_limit(self):
        with self.hana(down=True):
            self.assertEqual(len(self.picker.list_items(limit=1)), 1)

    def test_sap_answering_is_served_live(self):
        live = _Cursor(
            rows=[("FG0000777", "New Item", "PCS", "BOX", "", 102, "Y", "N", "Y", "Y", "N", 6, "Y", "N")],
            description=[(c,) for c in (
                "ItemCode", "ItemName", "InvntryUom", "SalUnitMsr", "BuyUnitMsr", "ItmsGrpCod",
                "ManBtchNum", "ManSerNum", "InvntItem", "SellItem", "PrchseItem", "SalFactor2",
                "validFor", "frozenFor",
            )],
        )
        with patch.object(HanaConnection, "connect", return_value=_Connection(live)):
            rows = self.picker.list_items()

        self.assertEqual([row["item_code"] for row in rows], ["FG0000777"])
        self.assertNotIn("sap_copy_as_of", rows[0])

    def test_a_refused_query_is_not_hidden_by_the_copy(self):
        with self.hana(refuse=True), self.assertRaises(OitmItemReadError):
            self.picker.list_items()

    def test_no_copy_yet_still_fails(self):
        MirrorDataset.objects.filter(company=self.oil).delete()

        with self.hana(down=True), self.assertRaises(OitmItemReadError):
            self.picker.list_items()


class WarehouseEndpointTests(CopyTestCase):
    URL = "/api/v1/warehouse/wms/warehouses/"

    def setUp(self):
        super().setUp()
        services.sync_due(at(30, 1, 5))
        user = get_user_model().objects.create(email="line@example.com", full_name="Line Lead")
        UserCompany.objects.create(
            user=user, company=self.oil, role=UserRole.objects.create(name="Production"),
            is_active=True,
        )
        self.api = APIClient()
        self.api.force_authenticate(user)

    def get(self):
        return self.api.get(self.URL, HTTP_COMPANY_CODE="JIVO_OIL")

    def test_hana_down_serves_the_copy(self):
        with patch.object(HanaConnection, "connect", side_effect=HANA_DOWN):
            response = self.get()

        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual([w["code"] for w in body["warehouses"]], ["BH-BT", "BH-PC"])
        self.assertEqual(body["sap_copy_as_of"], at(30, 1, 5).isoformat())

    def test_a_refused_query_still_fails(self):
        with patch.object(
            HanaConnection, "connect", return_value=_Connection(_Cursor(refuse=True))
        ):
            response = self.get()

        self.assertEqual(response.status_code, 500)


class HanaUnreachableTests(TestCase):
    def test_what_counts_as_unreachable(self):
        wrapped = OitmItemReadError("x")
        wrapped.__cause__ = HANA_DOWN
        self.assertTrue(services.hana_unreachable(wrapped))
        self.assertTrue(services.hana_unreachable(SAPConnectionError("down")))

        refused = OitmItemReadError("x")
        refused.__cause__ = dbapi.ProgrammingError(260, "invalid column name")
        self.assertFalse(services.hana_unreachable(refused))
        self.assertFalse(services.hana_unreachable(ValueError("bad input")))
