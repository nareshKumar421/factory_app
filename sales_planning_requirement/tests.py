from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase

from .hana_reader import ForecastSelection, ProcedureResult
from .models import SalesPlanningRequirementRefreshRun, SalesPlanningRequirementRow
from .services import SalesPlanningRequirementService


class SalesPlanningRequirementServiceTests(TestCase):
    @patch("sales_planning_requirement.services.CompanyContext")
    @patch("sales_planning_requirement.services.HanaSalesPlanningRequirementReader")
    def test_refresh_loads_beverage_procedure_rows_and_replaces_existing_data(
        self,
        reader_cls,
        _context_cls,
    ):
        old_run = SalesPlanningRequirementRefreshRun.objects.create(
            company_code="JIVO_BEVERAGES",
            source_schema="JIVO_BEVERAGES_HANADB",
            status=SalesPlanningRequirementRefreshRun.Status.SUCCESS,
        )
        SalesPlanningRequirementRow.objects.create(
            company_code="JIVO_BEVERAGES",
            source_schema="JIVO_BEVERAGES_HANADB",
            item_code="OLD",
            item_name="Old row",
            required_qty=Decimal("10"),
            refresh_run=old_run,
        )

        reader = reader_cls.return_value
        reader.source_schema = "JIVO_BEVERAGES_HANADB"
        reader.received_quantities.return_value = {"FG0000323": Decimal("12500")}
        reader.execute_procedure.return_value = ProcedureResult(
            source_schema="JIVO_BEVERAGES_HANADB",
            forecast=ForecastSelection(
                forecast_id=4,
                forecast_name="PLANNING OF MAY MONTH",
                start_date=date(2026, 5, 1),
                end_date=date(2026, 5, 31),
                line_count=7,
            ),
            parameter_name="forecast_id",
            parameter_value=4,
            column_metadata=[{"name": "ItemCode", "hana_type": "NVARCHAR"}],
            rows=[
                {
                    "Planning Month": "PLANNING OF MAY MONTH",
                    "ItemCode": "FG0000323",
                    "ItemName": "PET BOTTLE 1000 ML",
                    "Planned Qty": Decimal("300000"),
                    "Min Stock": Decimal("0"),
                    "Stock In Hand": Decimal("61129"),
                    "Required Qty": Decimal("238871"),
                    "Open PO Quantity": Decimal("38871"),
                    "Report Execution Date & Time": "2026-06-04 17:24:28.7380000",
                }
            ],
        )

        result = SalesPlanningRequirementService("JIVO_BEVERAGES").refresh(
            triggered_by=SalesPlanningRequirementRefreshRun.TriggeredBy.COMMAND
        )

        self.assertEqual(result["refresh"]["rows_loaded"], 1)
        self.assertFalse(
            SalesPlanningRequirementRow.objects.filter(item_code="OLD").exists()
        )
        row = SalesPlanningRequirementRow.objects.get(item_code="FG0000323")
        self.assertEqual(row.planned_qty, Decimal("300000"))
        self.assertEqual(row.required_qty, Decimal("238871"))
        self.assertEqual(row.open_po_qty, Decimal("38871"))
        self.assertEqual(row.net_shortage_qty, Decimal("200000"))
        self.assertEqual(row.received_qty, Decimal("12500"))
        reader.received_quantities.assert_called_once_with(
            date(2026, 5, 1),
            date(2026, 5, 31),
        )
        self.assertEqual(row.raw_payload["ItemCode"], "FG0000323")

    def test_report_summarizes_postgres_rows(self):
        run = SalesPlanningRequirementRefreshRun.objects.create(
            company_code="JIVO_OIL",
            source_schema="JIVO_OIL_HANADB",
            forecast_id=24,
            forecast_name="OIL Monthly Production Planning for the June Month 2026",
            status=SalesPlanningRequirementRefreshRun.Status.SUCCESS,
            rows_loaded=2,
        )
        SalesPlanningRequirementRow.objects.create(
            company_code="JIVO_OIL",
            source_schema="JIVO_OIL_HANADB",
            forecast_id=24,
            forecast_name=run.forecast_name,
            item_code="PM0000077",
            item_name="TIN 5 LTR",
            base_required_qty=Decimal("14880"),
            min_stock=Decimal("1243.5625"),
            stock_in_hand=Decimal("10515"),
            required_qty=Decimal("5608.5625"),
            open_po_qty=Decimal("1000"),
            net_shortage_qty=Decimal("4608.5625"),
            refresh_run=run,
        )
        SalesPlanningRequirementRow.objects.create(
            company_code="JIVO_OIL",
            source_schema="JIVO_OIL_HANADB",
            forecast_id=24,
            forecast_name=run.forecast_name,
            item_code="FG0000030",
            item_name="MUSTARD KACHI GHANI",
            base_required_qty=Decimal("442419"),
            min_stock=Decimal("0"),
            stock_in_hand=Decimal("7581"),
            required_qty=Decimal("434838"),
            open_po_qty=Decimal("434838"),
            net_shortage_qty=Decimal("0"),
            refresh_run=run,
        )

        report = SalesPlanningRequirementService("JIVO_OIL").get_report(
            {"page": 1, "page_size": 50}
        )

        self.assertEqual(report["summary"]["total_items"], 2)
        self.assertEqual(report["summary"]["shortage_items"], 1)
        self.assertEqual(report["summary"]["po_covered_items"], 1)
        self.assertEqual(report["summary"]["total_net_shortage_qty"], 4608.5625)
        self.assertEqual(report["refresh"]["last_success"]["forecast_id"], 24)
