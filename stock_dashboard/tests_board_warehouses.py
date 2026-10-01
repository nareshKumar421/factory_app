"""
The operations board's warehouse band as a ticked list rather than one floor.

What has to hold: a tick round-trips and defaults off, the board's own read of
the ticked list never touches SAP, the settings screen's list offers every
finished-goods warehouse without creating a row for each, a ticked warehouse
that has emptied stays listed so it can be unticked, and the stock read takes
several warehouses in one query with each bound rather than inlined.
"""

import importlib
from unittest.mock import MagicMock, patch

from django.apps import apps
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from company.models import Company, UserCompany, UserRole
from sap_client.exceptions import SAPConnectionError

from .hana_reader import HanaStockDashboardReader
from .models import WarehouseBoardSettings
from .serializers import WarehouseOccupancyFilterSerializer
from .services import StockDashboardService, summarise_warehouse_stock


def _stock_row(**overrides):
    row = {
        "item_code": "FG001",
        "item_name": "Mustard 1L",
        "on_hand": 120.0,
        "pieces_per_box": 12.0,
        "litres_per_piece": 1.0,
        "stock_value": 0.0,
        "sub_group": "MUSTARD",
        "uom": "PCS",
        "gross_weight_per_case": 11.0,
        "warehouse": "BH-BT",
        "warehouse_name": "Bhakharpur New Basement",
    }
    row.update(overrides)
    return row


class BoardWarehousesAPITests(TestCase):
    def setUp(self):
        self.settings_url = reverse("warehouse-board-settings")
        self.list_url = reverse("board-warehouses")

        self.oil = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        self.mart = Company.objects.create(name="Jivo Mart", code="JIVO_MART")
        role = UserRole.objects.create(name="StockViewer")

        self.user = get_user_model().objects.create_user(
            email="ticks@example.com",
            password="testpass123",
            full_name="Tick Setter",
            employee_code="TICK01",
        )
        for company in (self.oil, self.mart):
            UserCompany.objects.create(
                user=self.user, company=company, role=role, is_active=True
            )
        self.user.user_permissions.add(
            Permission.objects.get(codename="can_view_stock_dashboard")
        )

        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.client.credentials(HTTP_COMPANY_CODE=self.oil.code)

    def _tick(self, warehouse, on_board=True):
        return self.client.put(
            f"{self.settings_url}?warehouse={warehouse}", {"on_board": on_board}, format="json"
        )

    # ------------------------------------------------------------------ tick

    def test_a_warehouse_is_off_the_board_until_ticked(self):
        response = self.client.get(self.settings_url, {"warehouse": "BH-PF"})

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["on_board"])

    def test_the_tick_round_trips(self):
        self.assertEqual(self._tick("BH-PF").status_code, 200)
        self.assertTrue(
            self.client.get(self.settings_url, {"warehouse": "BH-PF"}).data["on_board"]
        )

        self._tick("BH-PF", on_board=False)
        self.assertFalse(
            self.client.get(self.settings_url, {"warehouse": "BH-PF"}).data["on_board"]
        )

    def test_ticking_leaves_the_capacity_alone(self):
        self.client.put(
            f"{self.settings_url}?warehouse=BH-BT", {"capacity_tonnes": 502}, format="json"
        )
        self._tick("BH-BT")

        row = WarehouseBoardSettings.objects.get(company_code="JIVO_OIL", warehouse="BH-BT")
        self.assertEqual(float(row.capacity_tonnes), 502)
        self.assertTrue(row.on_board)

    # ------------------------------------------------------ the board's read

    @patch.object(StockDashboardService, "get_finished_goods_by_warehouse")
    def test_the_boards_read_is_the_ticked_list_and_never_sap(self, sap):
        sap.side_effect = AssertionError("the board's read must not reach SAP")
        self._tick("BH-BT")
        self._tick("BH-PF")
        self._tick("BH-PF", on_board=False)

        response = self.client.get(self.list_url, {"on_board": "true"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual([row["warehouse"] for row in response.data["warehouses"]], ["BH-BT"])
        sap.assert_not_called()

    def test_ticks_do_not_leak_between_companies(self):
        """Oil's BH-GR and Mart's BH-GR are different floors."""
        self._tick("BH-GR")

        self.client.credentials(HTTP_COMPANY_CODE=self.mart.code)
        response = self.client.get(self.list_url, {"on_board": "true"})

        self.assertEqual(response.data["company_code"], "JIVO_MART")
        self.assertEqual(response.data["warehouses"], [])

    # ------------------------------------------------ the settings screen's

    @patch.object(StockDashboardService, "get_finished_goods_by_warehouse")
    def test_the_list_offers_every_warehouse_sap_holds_stock_in(self, sap):
        sap.return_value = {
            "BH-PF": {"warehouse": "BH-PF", "name": "Finished 1st Floor", "items": 26,
                      "tonnes": 182.4, "unweighed_items": 3},
            "BH-BT": {"warehouse": "BH-BT", "name": "New Basement", "items": 117,
                      "tonnes": 437.1, "unweighed_items": 8},
        }
        self._tick("BH-PF")

        response = self.client.get(self.list_url, {"item_groups": "102"})

        self.assertEqual(response.status_code, 200)
        rows = response.data["warehouses"]
        # Ticked first, then heaviest.
        self.assertEqual([row["warehouse"] for row in rows], ["BH-PF", "BH-BT"])
        self.assertEqual(rows[1]["tonnes"], 437.1)
        self.assertEqual(rows[1]["name"], "New Basement")
        self.assertFalse(rows[1]["on_board"])
        sap.assert_called_once_with([102])

    @patch.object(StockDashboardService, "get_finished_goods_by_warehouse")
    def test_listing_does_not_create_a_row_per_warehouse(self, sap):
        sap.return_value = {
            code: {"warehouse": code, "name": "", "items": 1, "tonnes": 1.0, "unweighed_items": 0}
            for code in ("BH-BT", "BH-PF", "BH-GR")
        }

        self.client.get(self.list_url)

        self.assertFalse(WarehouseBoardSettings.objects.filter(company_code="JIVO_OIL").exists())

    @patch.object(StockDashboardService, "get_finished_goods_by_warehouse")
    def test_a_ticked_warehouse_that_has_emptied_stays_listed(self, sap):
        """Otherwise it could never be unticked."""
        sap.return_value = {}
        self._tick("GP-FG")

        rows = self.client.get(self.list_url).data["warehouses"]

        self.assertEqual([row["warehouse"] for row in rows], ["GP-FG"])
        # SAP was read and holds nothing there: zero, not unknown.
        self.assertEqual(rows[0]["tonnes"], 0)
        self.assertEqual(rows[0]["items"], 0)

    @patch.object(StockDashboardService, "get_finished_goods_by_warehouse")
    def test_an_sap_outage_keeps_the_ticks_editable(self, sap):
        sap.side_effect = SAPConnectionError("down")
        self._tick("BH-BT")

        response = self.client.get(self.list_url)

        self.assertEqual(response.status_code, 200)
        self.assertIn("SAP", response.data["stock_error"])
        rows = response.data["warehouses"]
        self.assertEqual([row["warehouse"] for row in rows], ["BH-BT"])
        # Not read is unknown, never zero.
        self.assertIsNone(rows[0]["tonnes"])

    # ------------------------------------------------------ the stock read

    @patch.object(StockDashboardService, "get_warehouse_occupancy")
    def test_the_stock_read_takes_several_warehouses(self, occupancy):
        occupancy.return_value = {
            "data": [],
            "meta": {
                "warehouse": "BH-BT,BH-PF", "item_groups": [102], "item_count": 0,
                "total_on_hand": 0, "total_value": 0, "loose_items": 0,
                "unconfigured_items": 0, "unweighed_items": 0, "non_piece_items": 0,
                "fetched_at": "",
            },
        }

        response = self.client.get(
            reverse("warehouse-occupancy"), {"warehouse": "bh-bt,BH-PF", "item_groups": "102"}
        )

        self.assertEqual(response.status_code, 200)
        occupancy.assert_called_once_with(["BH-BT", "BH-PF"], item_groups=[102])


class TickExistingFloorsMigrationTests(TestCase):
    """Neither wall may change the day the tick ships."""

    def test_the_floors_the_boards_were_pinned_to_come_ticked(self):
        migration = importlib.import_module(
            "stock_dashboard.migrations.0010_warehouseboardsettings_on_board"
        )
        WarehouseBoardSettings.objects.create(
            company_code="JIVO_MART", warehouse="GP-FGM", capacity_tonnes=1120
        )

        migration.tick_existing_floors(apps, None)

        ticked = set(
            WarehouseBoardSettings.objects.filter(on_board=True).values_list(
                "company_code", "warehouse"
            )
        )
        self.assertEqual(
            ticked,
            {("JIVO_OIL", "BH-BT"), ("JIVO_MART", "GP-FGM"), ("JIVO_BEVERAGES", "BH-FG")},
        )
        # A capacity somebody already typed survives the tick.
        self.assertEqual(
            float(
                WarehouseBoardSettings.objects.get(
                    company_code="JIVO_MART", warehouse="GP-FGM"
                ).capacity_tonnes
            ),
            1120,
        )


class SummariseWarehouseStockTests(SimpleTestCase):
    def test_weighs_piece_rows_by_case(self):
        # 120 pieces at 12 a case is 10 cases of 11 kg.
        summary = summarise_warehouse_stock([_stock_row()])

        self.assertAlmostEqual(summary["BH-BT"]["tonnes"], 0.11)
        self.assertEqual(summary["BH-BT"]["items"], 1)
        self.assertEqual(summary["BH-BT"]["name"], "Bhakharpur New Basement")

    def test_counts_what_it_cannot_weigh_rather_than_dropping_it(self):
        summary = summarise_warehouse_stock(
            [
                _stock_row(gross_weight_per_case=None),
                _stock_row(pieces_per_box=None),
                _stock_row(uom="KG"),
                _stock_row(uom=""),
            ]
        )

        self.assertEqual(summary["BH-BT"]["tonnes"], 0)
        self.assertEqual(summary["BH-BT"]["items"], 4)
        self.assertEqual(summary["BH-BT"]["unweighed_items"], 4)

    def test_keeps_warehouses_apart(self):
        summary = summarise_warehouse_stock(
            [_stock_row(), _stock_row(warehouse="GP-FG", warehouse_name="Gupta")]
        )

        self.assertEqual(set(summary), {"BH-BT", "GP-FG"})


class OccupancyWarehouseListTests(SimpleTestCase):
    def _reader(self):
        reader = HanaStockDashboardReader.__new__(HanaStockDashboardReader)
        reader.connection = MagicMock(schema="JIVO_OIL_HANADB")
        reader._columns_cache = {"OITM": {"U_Gross_Weight"}}
        reader._execute = MagicMock(return_value=[])
        return reader

    def test_warehouses_are_bound_one_placeholder_each(self):
        reader = self._reader()

        reader.get_warehouse_occupancy(["BH-BT", "GP-FG"], item_groups=[102])

        query, params = reader._execute.call_args.args
        self.assertIn('w."WhsCode" IN (?, ?)', query)
        self.assertEqual(params, ["BH-BT", "GP-FG"])
        self.assertNotIn("'BH-BT'", query)

    def test_one_code_still_reads_one_warehouse(self):
        """Production Control and the plant board still pass a single code."""
        reader = self._reader()

        reader.get_warehouse_occupancy("BH-PF")

        query, params = reader._execute.call_args.args
        self.assertIn('w."WhsCode" IN (?)', query)
        self.assertEqual(params, ["BH-PF"])

    def test_none_reads_every_warehouse(self):
        reader = self._reader()

        reader.get_warehouse_occupancy(None, item_groups=[102])

        query, params = reader._execute.call_args.args
        self.assertNotIn('w."WhsCode" IN', query)
        self.assertEqual(params, [])

    def test_an_empty_list_reads_nothing(self):
        reader = self._reader()

        self.assertEqual(reader.get_warehouse_occupancy([]), [])
        reader._execute.assert_not_called()

    def test_rows_say_which_warehouse_they_stand_in(self):
        reader = self._reader()
        reader._execute.return_value = [
            ("FG001", "Mustard 1L", 120, 12, 1, 5000, "MUSTARD", "PCS", 11, "GP-FG ", "Gupta")
        ]

        [row] = reader.get_warehouse_occupancy(["GP-FG"])

        self.assertEqual(row["warehouse"], "GP-FG")
        self.assertEqual(row["warehouse_name"], "Gupta")


class OccupancyFilterTests(SimpleTestCase):
    def test_a_comma_list_is_cleaned_and_deduplicated(self):
        serializer = WarehouseOccupancyFilterSerializer(
            data={"warehouse": "bh-bt, GP-FGM,bh-bt,"}
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data["warehouse"], ["BH-BT", "GP-FGM"])

    def test_a_code_longer_than_sap_allows_is_refused(self):
        serializer = WarehouseOccupancyFilterSerializer(data={"warehouse": "BH-BT,NOTAWHSCODE"})

        self.assertFalse(serializer.is_valid())

    def test_nothing_but_commas_is_refused(self):
        serializer = WarehouseOccupancyFilterSerializer(data={"warehouse": " , "})

        self.assertFalse(serializer.is_valid())
