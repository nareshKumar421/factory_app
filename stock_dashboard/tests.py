from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

from django.test import SimpleTestCase
from django.utils import timezone

from .hana_reader import HanaStockDashboardReader
from .serializers import (
    StockDashboardAsOfFilterSerializer,
    StockDashboardExportFilterSerializer,
    StockDashboardFilterSerializer,
)
from .services import EXPORT_MAX_ROWS, StockDashboardService


class StockDashboardFilterSerializerTests(SimpleTestCase):
    def test_accepts_item_group_filter(self):
        serializer = StockDashboardFilterSerializer(
            data={"item_group": "  PACKAGING MATERIAL  "}
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(
            serializer.validated_data["item_group"], "PACKAGING MATERIAL"
        )

    def test_accepts_movement_status_filter(self):
        serializer = StockDashboardFilterSerializer(
            data={"movement_status": "recent,slow"}
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data["movement_status"], ["recent", "slow"])

    def test_rejects_invalid_movement_status_filter(self):
        serializer = StockDashboardFilterSerializer(
            data={"movement_status": "planned,stale"}
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("movement_status", serializer.errors)

    def test_as_of_filter_accepts_historical_date(self):
        historical = timezone.localdate() - timedelta(days=1)
        serializer = StockDashboardAsOfFilterSerializer(
            data={"as_of_date": historical.isoformat(), "movement_status": "recent,slow"}
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data["as_of_date"], historical)
        self.assertEqual(serializer.validated_data["movement_status"], ["recent", "slow"])

    def test_as_of_filter_rejects_future_date(self):
        future = timezone.localdate() + timedelta(days=1)
        serializer = StockDashboardAsOfFilterSerializer(
            data={"as_of_date": future.isoformat()}
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("as_of_date", serializer.errors)

    def test_export_filter_as_of_date_is_optional(self):
        serializer = StockDashboardExportFilterSerializer(data={"status": "low,critical"})

        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertNotIn("as_of_date", serializer.validated_data)
        self.assertEqual(serializer.validated_data["status"], ["low", "critical"])

    def test_export_filter_rejects_future_as_of_date(self):
        future = timezone.localdate() + timedelta(days=1)
        serializer = StockDashboardExportFilterSerializer(
            data={"as_of_date": future.isoformat()}
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("as_of_date", serializer.errors)


class HanaStockDashboardReaderQueryTests(SimpleTestCase):
    def setUp(self):
        self.reader = HanaStockDashboardReader.__new__(HanaStockDashboardReader)
        self.reader.connection = SimpleNamespace(schema="SAP_SCHEMA")

    def test_stock_query_filters_by_item_group_name(self):
        query, params = self.reader._build_query(
            {"item_group": "PACKAGING MATERIAL"}
        )

        self.assertIn('"OITB" grp', query)
        self.assertIn('UPPER(IFNULL(grp."ItmsGrpNam", \'\')) = UPPER(?)', query)
        self.assertEqual(params, ["PACKAGING MATERIAL"])

    def test_stock_query_includes_movement_signal(self):
        query, _ = self.reader._build_query({})

        self.assertIn('"OINM" n', query)
        self.assertIn('"LastConsumptionDate"', query)
        self.assertNotIn('"OWOR" po', query)
        self.assertNotIn('"WOR1" c', query)
        self.assertNotIn('"OpenPlanCount"', query)

    def test_stock_movement_is_item_level_not_selected_warehouse_level(self):
        query, _ = self.reader._build_query({"warehouse": ["BH-BS", "BH-PM"]})

        self.assertIn('GROUP BY n."ItemCode"', query)
        self.assertNotIn('GROUP BY n."ItemCode", n."Warehouse"', query)
        self.assertNotIn('mov."Warehouse" = w."WhsCode"', query)

    def test_stock_query_filters_by_movement_status(self):
        query, _ = self.reader._build_query(
            {"movement_status": ["recent", "slow"]}
        )

        self.assertIn('DAYS_BETWEEN(mov."LastConsumptionDate", CURRENT_DATE) <= 30', query)
        self.assertIn('DAYS_BETWEEN(mov."LastConsumptionDate", CURRENT_DATE) > 30', query)
        self.assertNotIn("WHERE WHERE", query)

    def test_single_status_filter_excludes_slow_moving_rows(self):
        query, _ = self.reader._build_query({"status": ["healthy"]})

        self.assertIn('NOT (mov."LastConsumptionDate" IS NULL', query)
        self.assertIn('mov."LastConsumptionDate" IS NULL', query)
        self.assertIn('DAYS_BETWEEN(mov."LastConsumptionDate", CURRENT_DATE) > 30', query)
        self.assertNotIn('OR (w."MinStock" > 0', query)

    def test_default_operational_status_filter_keeps_benchmarked_no_status_slow_rows(self):
        query, _ = self.reader._build_query({"status": ["healthy", "low", "critical"]})

        self.assertIn(
            'OR (w."MinStock" > 0 AND (mov."LastConsumptionDate" IS NULL OR '
            'DAYS_BETWEEN(mov."LastConsumptionDate", CURRENT_DATE) > 30))',
            query,
        )

    def test_critical_status_uses_required_quantity(self):
        query, _ = self.reader._build_query({"status": ["critical"]})

        self.assertIn(
            'w."OnHand" < w."MinStock" * 0.6',
            query,
        )

    def test_unset_status_requires_no_benchmark(self):
        query, _ = self.reader._build_query({"status": ["unset"]})

        self.assertIn(
            'w."MinStock" = 0',
            query,
        )

    def test_stock_stats_count_statuses_by_required_quantity(self):
        query, _ = self.reader._build_stats_query({})

        self.assertIn(
            'w."OnHand" >= w."MinStock"',
            query,
        )
        self.assertIn(
            'w."OnHand" < w."MinStock" * 0.6',
            query,
        )
        self.assertIn('AND NOT (mov."LastConsumptionDate" IS NULL', query)

    def test_as_of_query_reconstructs_on_hand_from_future_movements(self):
        as_of_date = date(2026, 5, 1)
        query, params = self.reader._build_as_of_query(
            {"item_group": "PACKAGING MATERIAL", "movement_status": ["slow"]},
            as_of_date,
        )

        self.assertIn('n."DocDate" > ?', query)
        self.assertIn('n."DocDate" <= ?', query)
        self.assertIn('IFNULL(w."OnHand", 0)', query)
        self.assertIn('IFNULL(future_mov."FutureNetQty", 0)', query)
        self.assertIn('DAYS_BETWEEN(mov."LastConsumptionDate", ?)', query)
        self.assertIn("days_since_last_consumption > 30", query)
        self.assertEqual(params[:3], [as_of_date, as_of_date, as_of_date])
        self.assertEqual(params[3:], ["PACKAGING MATERIAL"])

    def test_as_of_stats_query_uses_reconstructed_aliases(self):
        query, _ = self.reader._build_as_of_stats_query(
            {"status": ["critical"]},
            date(2026, 5, 1),
        )

        self.assertIn("on_hand < min_stock * 0.6", query)
        self.assertIn("NOT (days_since_last_consumption IS NULL", query)
        self.assertIn("FROM (", query)

    def test_grouped_stats_query_filters_by_item_group_name(self):
        query, params = self.reader._build_grouped_stats_query(
            {"item_group": "PACKAGING MATERIAL"}
        )

        self.assertIn('"OITB" grp', query)
        self.assertIn('UPPER(IFNULL(grp."ItmsGrpNam", \'\')) = UPPER(?)', query)
        self.assertEqual(params, ["PACKAGING MATERIAL"])

    def test_grouped_stats_query_filters_by_movement_status(self):
        query, _ = self.reader._build_grouped_stats_query(
            {"movement_status": ["slow"]}
        )

        self.assertIn("days_since_last_consumption", query)
        self.assertIn("days_since_last_consumption > 30", query)
        self.assertNotIn("WHERE WHERE", query)

    def test_grouped_critical_status_uses_required_quantity(self):
        query, _ = self.reader._build_grouped_query({"status": ["critical"]})

        self.assertIn("on_hand < min_stock * 0.6", query)
        self.assertIn("NOT (days_since_last_consumption IS NULL", query)
        self.assertNotIn("planned_without_benchmark", query)

    def test_grouped_default_operational_status_filter_keeps_benchmarked_no_status_slow_rows(self):
        query, _ = self.reader._build_grouped_query(
            {"status": ["healthy", "low", "critical"]}
        )

        self.assertIn(
            "OR (min_stock > 0 AND (days_since_last_consumption IS NULL OR "
            "days_since_last_consumption > 30))",
            query,
        )

    def test_row_mapper_excludes_planned_fields(self):
        row = (
            "PM0001",
            "Bottle",
            "BH-PM",
            10,
            20,
            "PCS",
            None,
            None,
        )

        mapped = self.reader._map_row(row)

        self.assertNotIn("planned_qty", mapped)
        self.assertNotIn("has_open_plan", mapped)

    def test_grouped_row_mapper_excludes_planned_fields(self):
        row = (
            "PM0001",
            "Bottle",
            10,
            20,
            "PCS",
            2,
            1,
            0,
            None,
            None,
        )

        mapped = self.reader._map_grouped_row(row)

        self.assertNotIn("planned_qty", mapped)
        self.assertNotIn("has_open_plan", mapped)


class StockDashboardServiceTests(SimpleTestCase):
    def make_service(self, reader):
        service = StockDashboardService.__new__(StockDashboardService)
        service.reader = reader
        return service

    def test_meta_cards_use_filtered_stats_for_ungrouped_table(self):
        reader = Mock()
        reader.get_warehouses.return_value = ["BH-PM"]
        reader.get_stock_stats.return_value = {
            "total_items": 12,
            "healthy_count": 4,
            "low_count": 3,
            "critical_count": 5,
        }
        reader.get_stock_levels.return_value = [
            {
                "item_code": "PM0001",
                "item_name": "Bottle",
                "warehouse": "BH-PM",
                "on_hand": 10,
                "min_stock": 20,
                "uom": "PCS",
            }
        ]
        service = self.make_service(reader)
        filters = {"item_group": "PACKAGING MATERIAL", "page": 1, "page_size": 50}

        result = service.get_stock_levels(filters)

        reader.get_stock_stats.assert_called_once_with(filters)
        self.assertEqual(result["meta"]["total_items"], 12)
        self.assertEqual(result["meta"]["healthy_count"], 4)
        self.assertEqual(result["meta"]["low_stock_count"], 3)
        self.assertEqual(result["meta"]["critical_stock_count"], 5)

    def test_meta_cards_use_grouped_stats_for_multi_warehouse_table(self):
        reader = Mock()
        reader.get_warehouses.return_value = ["BH-PM", "GP-FG"]
        reader.get_grouped_stock_stats.return_value = {
            "total_items": 7,
            "healthy_count": 2,
            "low_count": 1,
            "critical_count": 4,
        }
        reader.get_grouped_stock_levels.return_value = [
            {
                "item_code": "PM0001",
                "item_name": "Bottle",
                "on_hand": 10,
                "min_stock": 20,
                "uom": "PCS",
                "warehouse_count": 2,
                "critical_warehouses": 1,
                "low_warehouses": 0,
            }
        ]
        service = self.make_service(reader)
        filters = {
            "warehouse": ["BH-PM", "GP-FG"],
            "item_group": "PACKAGING MATERIAL",
            "page": 1,
            "page_size": 50,
        }

        result = service.get_stock_levels(filters)

        reader.get_grouped_stock_stats.assert_called_once_with(filters)
        reader.get_stock_stats.assert_not_called()
        self.assertEqual(result["meta"]["total_items"], 7)
        self.assertEqual(result["meta"]["healthy_count"], 2)
        self.assertEqual(result["meta"]["low_stock_count"], 1)
        self.assertEqual(result["meta"]["critical_stock_count"], 4)

    def test_as_of_stock_levels_use_reconstructed_reader_methods(self):
        as_of_date = date(2026, 5, 1)
        reader = Mock()
        reader.get_warehouses.return_value = ["BH-PM"]
        reader.get_as_of_stock_stats.return_value = {
            "total_items": 1,
            "healthy_count": 0,
            "low_count": 1,
            "critical_count": 0,
        }
        reader.get_as_of_stock_levels.return_value = [
            {
                "item_code": "PM0001",
                "item_name": "Bottle",
                "warehouse": "BH-PM",
                "on_hand": 75,
                "min_stock": 100,
                "uom": "PCS",
                "days_since_last_consumption": 12,
            }
        ]
        service = self.make_service(reader)
        filters = {"as_of_date": as_of_date, "page": 1, "page_size": 50}

        result = service.get_as_of_stock_levels(filters)

        reader.get_as_of_stock_stats.assert_called_once_with(filters, as_of_date)
        reader.get_as_of_stock_levels.assert_called_once_with(
            filters,
            as_of_date=as_of_date,
            page=1,
            page_size=50,
        )
        self.assertEqual(result["meta"]["as_of_date"], "2026-05-01")
        self.assertEqual(result["data"][0]["stock_status"], "low")
        self.assertEqual(result["data"][0]["movement_status"], "recent")

    def test_item_without_benchmark_is_unset(self):
        service = self.make_service(Mock())

        status = service._stock_status(0, 0)

        self.assertEqual(status, "unset")

    def test_benchmark_covered_item_is_healthy(self):
        service = self.make_service(Mock())

        status = service._stock_status(100, 50)

        self.assertEqual(status, "healthy")

    def test_benchmark_shortfall_can_be_critical(self):
        service = self.make_service(Mock())

        status = service._stock_status(10, 100)

        self.assertEqual(status, "critical")

    def test_health_ratio_uses_benchmark_only(self):
        service = self.make_service(Mock())

        ratio = service._health_ratio({"on_hand": 75, "min_stock": 50})

        self.assertEqual(ratio, 1.5)

    def test_slow_moving_item_has_no_stock_status(self):
        service = self.make_service(Mock())

        status = service._stock_status(
            0,
            100,
            movement_status="slow",
        )

        self.assertEqual(status, "none")

    def test_slow_moving_healthy_item_has_no_stock_status(self):
        service = self.make_service(Mock())

        status = service._stock_status(
            200,
            100,
            movement_status="slow",
        )

        self.assertEqual(status, "none")

    def test_movement_status_ignores_open_plan_flag(self):
        service = self.make_service(Mock())

        status = service._movement_status(
            {"has_open_plan": True, "days_since_last_consumption": 999}
        )

        self.assertEqual(status, "slow")

    def test_movement_status_marks_recent_consumption_active(self):
        service = self.make_service(Mock())

        status = service._movement_status(
            {"days_since_last_consumption": 30}
        )

        self.assertEqual(status, "recent")

    def test_movement_status_marks_no_recent_consumption_slow(self):
        service = self.make_service(Mock())

        status = service._movement_status(
            {"days_since_last_consumption": 31}
        )

        self.assertEqual(status, "slow")

    def test_export_fetches_all_rows_ungrouped(self):
        reader = Mock()
        reader.get_stock_levels.return_value = [
            {
                "item_code": "PM0001",
                "item_name": "CAP",
                "warehouse": "BH-PM",
                "on_hand": 10,
                "min_stock": 100,
                "uom": "PCS",
                "days_since_last_consumption": 2,
            }
        ]
        service = self.make_service(reader)

        rows = service.get_stock_levels_for_export({"warehouse": ["BH-PM"]})

        reader.get_stock_levels.assert_called_once_with(
            {"warehouse": ["BH-PM"]}, page=1, page_size=EXPORT_MAX_ROWS
        )
        reader.get_grouped_stock_levels.assert_not_called()
        self.assertEqual(rows[0]["stock_status"], "critical")
        self.assertEqual(rows[0]["movement_status"], "recent")

    def test_export_uses_grouped_rows_for_multi_warehouse(self):
        reader = Mock()
        reader.get_grouped_stock_levels.return_value = [
            {
                "item_code": "PM0001",
                "item_name": "CAP",
                "on_hand": 200,
                "min_stock": 100,
                "uom": "PCS",
                "warehouse_count": 2,
                "days_since_last_consumption": 2,
                "critical_warehouses": 1,
                "low_warehouses": 0,
            }
        ]
        service = self.make_service(reader)

        rows = service.get_stock_levels_for_export({"warehouse": ["BH-PM", "GP-FG"]})

        reader.get_grouped_stock_levels.assert_called_once()
        reader.get_stock_levels.assert_not_called()
        self.assertEqual(rows[0]["warehouse"], "2 warehouses")
        self.assertTrue(rows[0]["has_warning"])

    def test_export_uses_as_of_reader_when_date_given(self):
        as_of_date = date(2026, 5, 1)
        reader = Mock()
        reader.get_as_of_stock_levels.return_value = []
        service = self.make_service(reader)

        filters = {"warehouse": ["BH-PM", "GP-FG"], "as_of_date": as_of_date}
        service.get_stock_levels_for_export(filters)

        reader.get_as_of_stock_levels.assert_called_once_with(
            filters, as_of_date=as_of_date, page=1, page_size=EXPORT_MAX_ROWS
        )
        reader.get_grouped_stock_levels.assert_not_called()
