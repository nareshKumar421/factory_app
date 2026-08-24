"""planning_purchase tests.

Run on SQLite — the default database is the live production Postgres:

    python manage.py test planning_purchase --settings=config.sqlite_test_settings

No test here touches SAP. The bucketing maths, the BOM/shortage arithmetic and
the purchase-order state machine are all exercised against fixtures, with the
HANA reader stubbed. The one thing that must never be tested against a live
company is posting a purchase order, so `PLANNING_PURCHASE_SIMULATE_SAP` is
forced on wherever a post is exercised.
"""

from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from .models import PurchaseOrder, PurchaseOrderLine, PurchaseOrderStatus
from .services import calendar as cal
from .services.errors import PlanningError, PurchaseOrderStateError
from .services.plan_service import PlanService
from .services.purchase_service import PurchaseOrderService

User = get_user_model()
COMPANY = "JIVO_OIL"


# ---------------------------------------------------------------------------
# Bucketing
# ---------------------------------------------------------------------------


@override_settings(PLANNING_NON_WORKING_WEEKDAYS=[6], PLANNING_WEEK_START_DAY=0)
class CalendarTests(TestCase):
    """August 2026: 31 days, starts on a Saturday, 5 Sundays -> 26 working days.

    It also spans SIX partial weeks, which is exactly the case the old
    "divide the month into 4 weeks" design silently got wrong.
    """

    PERIOD_START = date(2026, 8, 1)
    PERIOD_END = date(2026, 8, 31)

    def test_working_days_exclude_sunday(self):
        days = cal.working_days(self.PERIOD_START, self.PERIOD_END)
        self.assertEqual(len(days), 26)
        self.assertFalse(any(day.weekday() == 6 for day in days))

    def test_working_days_falls_back_when_range_is_all_non_working(self):
        # A single Sunday. Returning nothing would divide by zero and silently
        # lose the quantity, which is worse than putting plan on a day off.
        sunday = date(2026, 8, 2)
        self.assertEqual(cal.working_days(sunday, sunday), [sunday])

    def test_spread_sums_back_exactly(self):
        days = cal.working_days(self.PERIOD_START, self.PERIOD_END)
        total = Decimal("690000")
        allocation = cal.spread_even(total, days)
        self.assertEqual(sum(allocation.values()), total)

    def test_spread_sums_back_exactly_for_an_indivisible_total(self):
        days = cal.working_days(self.PERIOD_START, self.PERIOD_END)
        total = Decimal("6667")  # 6667 / 26 does not divide cleanly
        self.assertEqual(sum(cal.spread_even(total, days).values()), total)

    def test_day_week_month_all_sum_to_the_planned_quantity(self):
        """The invariant that stops a five-week month losing three days of plan."""
        total = Decimal("100000")
        buckets = cal.build_buckets(
            total, self.PERIOD_START, self.PERIOD_START, self.PERIOD_END
        )
        for bucket_type in (cal.DAY, cal.WEEK, cal.MONTH):
            with self.subTest(bucket_type=bucket_type):
                self.assertEqual(
                    sum(row["planned_qty"] for row in buckets[bucket_type]), total
                )

    def test_august_2026_produces_six_week_buckets_not_four(self):
        buckets = cal.build_buckets(
            Decimal("1000"), self.PERIOD_START, self.PERIOD_START, self.PERIOD_END
        )
        self.assertEqual(len(buckets[cal.WEEK]), 6)
        self.assertEqual(len(buckets[cal.MONTH]), 1)

    def test_spread_buckets_are_flagged_derived(self):
        buckets = cal.build_buckets(
            Decimal("1000"), self.PERIOD_START, self.PERIOD_START, self.PERIOD_END
        )
        self.assertTrue(all(row["derived"] for row in buckets[cal.DAY]))
        # The month total is the number SAP actually stated, so it is not derived.
        self.assertFalse(buckets[cal.MONTH][0]["derived"])

    def test_period_start_policy_invents_nothing(self):
        buckets = cal.build_buckets(
            Decimal("1000"), self.PERIOD_START, self.PERIOD_START, self.PERIOD_END,
            policy=cal.POLICY_PERIOD_START,
        )
        self.assertEqual(len(buckets[cal.DAY]), 1)
        self.assertEqual(buckets[cal.DAY][0]["bucket_start"], self.PERIOD_START)
        self.assertFalse(buckets[cal.DAY][0]["derived"])

    def test_no_day_bucket_lands_on_a_non_working_day(self):
        buckets = cal.build_buckets(
            Decimal("1000"), self.PERIOD_START, self.PERIOD_START, self.PERIOD_END
        )
        self.assertFalse(
            any(row["bucket_start"].weekday() == 6 for row in buckets[cal.DAY])
        )

    def test_week_start_is_configurable(self):
        # A Wednesday. With a Monday week start its week begins on the Monday.
        self.assertEqual(cal.week_start(date(2026, 8, 19)), date(2026, 8, 17))
        with override_settings(PLANNING_WEEK_START_DAY=6):  # Sunday
            self.assertEqual(cal.week_start(date(2026, 8, 19)), date(2026, 8, 16))


# ---------------------------------------------------------------------------
# Requirement: BOM explosion, availability, shortage
# ---------------------------------------------------------------------------


class FakeReader:
    """Stands in for HANA, shaped exactly like the live rows the probes returned."""

    def __init__(self, **overrides):
        self.calls = []
        self.data = {
            "header": {
                "AbsID": 43,
                "Code": "AUG PLANNING 26",
                "Name": "OIL Monthly Production Planning for the Aug Month 2026",
                "StartDate": date(2026, 8, 1),
                "EndDate": date(2026, 8, 31),
                "FormView": "M",
            },
            "lines": [
                {
                    "LineID": 0, "ItemCode": "FG0000004",
                    "ItemName": "COLD PRESS 5 LTR 4 PCS",
                    "BucketDate": date(2026, 8, 1), "PlannedQty": Decimal("45000"),
                    "WhsCode": "", "Uom": "PCS", "PiecesPerCase": 4,
                    "LitresPerUnit": Decimal("5"),
                    "ItemGroup": "FINISHED", "TreeType": "P",
                    "HasBom": 1, "BomBaseQty": Decimal("4"),
                },
                {
                    # No BOM in SAP — must be reported, never treated as zero need.
                    "LineID": 1, "ItemCode": "FG0000451",
                    "ItemName": "COLD PRESS SUNFLOWER OIL 200 ML 70 PCS",
                    "BucketDate": date(2026, 8, 1), "PlannedQty": Decimal("25000"),
                    "WhsCode": "", "Uom": "PCS", "PiecesPerCase": 70,
                    "LitresPerUnit": Decimal("0.2"),
                    "ItemGroup": "FINISHED", "TreeType": "N",
                    "HasBom": 0, "BomBaseQty": Decimal("0"),
                },
            ],
            # BOM is written per 4 PCS, so QtyPerUnit is the SQL-side division.
            "bom": [
                {
                    "ParentCode": "FG0000004", "BomBaseQty": Decimal("4"), "ChildNum": 0, "LineType": 4,
                    "ComponentCode": "PM0000053", "ComponentName": "HDPE BOTTLE 5 LTR",
                    "BomQty": Decimal("4"), "QtyPerUnit": Decimal("1"),
                    "IssueWarehouse": "BH-PC", "Uom": "PCS",
                    "ItemGroup": "PACKAGING MATERIAL", "PurchaseItem": "Y",
                    "LastPurchasePrice": Decimal("18.5"), "HasOwnBom": 0,
                },
                {
                    "ParentCode": "FG0000004", "BomBaseQty": Decimal("4"), "ChildNum": 1, "LineType": 4,
                    "ComponentCode": "PM0000013", "ComponentName": "CARTON 5 LTR 4 PCS",
                    "BomQty": Decimal("1"), "QtyPerUnit": Decimal("0.25"),
                    "IssueWarehouse": "BH-PC", "Uom": "PCS",
                    "ItemGroup": "PACKAGING MATERIAL", "PurchaseItem": "Y",
                    "LastPurchasePrice": Decimal("22"), "HasOwnBom": 0,
                },
                {
                    "ParentCode": "FG0000004", "BomBaseQty": Decimal("4"), "ChildNum": 2, "LineType": 4,
                    "ComponentCode": "RM0000002", "ComponentName": "CANOLA COLD PRESS LOOSE OIL",
                    "BomQty": Decimal("20"), "QtyPerUnit": Decimal("5"),
                    "IssueWarehouse": "BH-PC", "Uom": "LTR",
                    "ItemGroup": "RAW MATERIAL", "PurchaseItem": "Y",
                    "LastPurchasePrice": Decimal("120"), "HasOwnBom": 1,
                },
                {
                    # Corrupt master data: OITT."Qauntity" was zero.
                    "ParentCode": "FG0000004", "BomBaseQty": Decimal("0"), "ChildNum": 3, "LineType": 4,
                    "ComponentCode": "PM0000884", "ComponentName": "SHRINK POF ROLL KG",
                    "BomQty": Decimal("0.024"), "QtyPerUnit": None,
                    "IssueWarehouse": "BH-PC", "Uom": "KGS",
                    "ItemGroup": "PACKAGING MATERIAL", "PurchaseItem": "Y",
                    "LastPurchasePrice": Decimal("300"), "HasOwnBom": 0,
                },
                {
                    # ITT1.Type 290 = a resource, not a material. Live data has
                    # JWPL09240002 "Filling Cost Commodities" against 99 parents.
                    "ParentCode": "FG0000004", "BomBaseQty": Decimal("4"), "ChildNum": 4,
                    "LineType": 290,
                    "ComponentCode": "JWPL09240002",
                    "ComponentName": "Filling Cost Commodities",
                    "BomQty": Decimal("20"), "QtyPerUnit": Decimal("5"),
                    "IssueWarehouse": "BH-PC", "Uom": "",
                    "ItemGroup": "", "PurchaseItem": "N",
                    "LastPurchasePrice": Decimal("0"), "HasOwnBom": 0,
                },
            ],
            "stock": [
                {
                    "ItemCode": "PM0000053", "WhsCode": "BH-PM",
                    "OnHand": Decimal("30000"), "MinStock": Decimal("5000"),
                    "Committed": Decimal("2000"), "OnOrder": Decimal("0"),
                    "Uom": "PCS", "LastPurchasePrice": Decimal("18.5"),
                    "ItemGroup": "PACKAGING MATERIAL",
                    "LastConsumptionDate": date(2026, 8, 20),
                    "DaysSinceLastConsumption": 4,
                },
                {
                    "ItemCode": "PM0000013", "WhsCode": "BH-PM",
                    "OnHand": Decimal("20000"), "MinStock": Decimal("0"),
                    "Committed": Decimal("0"), "OnOrder": Decimal("0"),
                    "Uom": "PCS", "LastPurchasePrice": Decimal("22"),
                    "ItemGroup": "PACKAGING MATERIAL",
                    "LastConsumptionDate": None, "DaysSinceLastConsumption": None,
                },
                {
                    "ItemCode": "RM0000002", "WhsCode": "BH-LO",
                    "OnHand": Decimal("100000"), "MinStock": Decimal("0"),
                    "Committed": Decimal("0"), "OnOrder": Decimal("0"),
                    "Uom": "LTR", "LastPurchasePrice": Decimal("120"),
                    "ItemGroup": "RAW MATERIAL",
                    "LastConsumptionDate": date(2026, 8, 22),
                    "DaysSinceLastConsumption": 2,
                },
            ],
            "open_po": [
                {
                    "ItemCode": "PM0000053", "OpenQty": Decimal("10000"),
                    "EarliestDue": date(2026, 8, 28), "LatestDue": date(2026, 9, 5),
                    "OpenLines": 2,
                },
            ],
            "vendors_for_items": [
                {
                    "ItemCode": "PM0000053", "CardCode": "VENDA000021",
                    "CardName": "AMD INDUSTRIES LTD", "Price": Decimal("18.5"),
                    "Currency": "INR", "DocDate": date(2026, 6, 1),
                },
            ],
            "produced": [{"ItemCode": "FG0000004", "ProducedQty": Decimal("20000"),
                          "Receipts": 4, "FirstReceipt": None, "LastReceipt": None}],
        }
        self.data.update(overrides)

    def get_plan_header(self, abs_id):
        return self.data["header"]

    def get_plan_lines(self, abs_id):
        return self.data["lines"]

    def get_bom_components(self, item_codes):
        return [row for row in self.data["bom"] if row["ParentCode"] in set(item_codes)]

    def get_item_stock(self, item_codes, warehouses=None):
        rows = [r for r in self.data["stock"] if r["ItemCode"] in set(item_codes)]
        if warehouses:
            rows = [r for r in rows if r["WhsCode"] in set(warehouses)]
        return rows

    def get_open_purchase_qty(self, item_codes):
        return [r for r in self.data["open_po"] if r["ItemCode"] in set(item_codes)]

    def get_last_vendors(self, item_codes):
        return [
            r for r in self.data["vendors_for_items"] if r["ItemCode"] in set(item_codes)
        ]

    def get_produced_quantities(self, item_codes, date_from, date_to):
        return [r for r in self.data["produced"] if r["ItemCode"] in set(item_codes)]

    def get_vendors(self, search="", limit=100):
        return [{"CardCode": "VENDA000021", "CardName": "AMD INDUSTRIES LTD",
                 "Currency": "INR"}]

    def get_warehouses(self):
        return [{"WhsCode": "BH-PC", "WhsName": "Production Component"}]

    def get_branch_for_warehouse(self, warehouse_code):
        return 1


def make_plan_service(reader=None) -> PlanService:
    with patch("planning_purchase.services.plan_service.CompanyContext"), \
         patch("planning_purchase.services.plan_service.HanaProductionPlanReader"):
        service = PlanService(COMPANY)
    service.reader = reader or FakeReader()
    return service


class RequirementTests(TestCase):
    def setUp(self):
        self.service = make_plan_service()
        self.result = self.service.get_requirement(43)
        self.rows = {row["component_code"]: row for row in self.result["data"]}

    def test_bom_base_quantity_is_divided_out(self):
        """45,000 PCS x (1 carton / 4 PCS) = 11,250 cartons, not 45,000.

        Skipping the OITT."Qauntity" division is the single easiest way to
        overstate a requirement by the case factor, so it gets its own test.
        """
        self.assertEqual(self.rows["PM0000013"]["required_qty"], Decimal("11250"))
        self.assertEqual(self.rows["PM0000053"]["required_qty"], Decimal("45000"))
        self.assertEqual(self.rows["RM0000002"]["required_qty"], Decimal("225000"))

    def test_shortage_nets_committed_stock_and_open_purchase_orders(self):
        row = self.rows["PM0000053"]
        # 30,000 on hand - 2,000 committed = 28,000 available.
        self.assertEqual(row["net_available_qty"], Decimal("28000"))
        # 45,000 - 28,000 = 17,000 before netting the open PO.
        self.assertEqual(row["shortage_before_po_qty"], Decimal("17000"))
        # ... and 10,000 is already on order, so only 7,000 needs buying.
        self.assertEqual(row["on_order_qty"], Decimal("10000"))
        self.assertEqual(row["shortage_qty"], Decimal("7000"))

    def test_covered_component_reports_zero_shortage_not_a_negative(self):
        self.assertEqual(self.rows["PM0000013"]["shortage_qty"], Decimal("0"))
        self.assertEqual(self.rows["PM0000013"]["urgency"], "COVERED")

    def test_component_with_no_stock_row_is_short_by_the_whole_requirement(self):
        service = make_plan_service(FakeReader(stock=[]))
        rows = {r["component_code"]: r for r in service.get_requirement(43)["data"]}
        self.assertEqual(rows["PM0000053"]["on_hand_qty"], Decimal("0"))
        self.assertEqual(rows["PM0000053"]["shortage_before_po_qty"], Decimal("45000"))

    def test_packaging_and_raw_are_split_by_sap_item_group(self):
        self.assertEqual(self.rows["PM0000053"]["material_type"], "PACKAGING")
        self.assertEqual(self.rows["RM0000002"]["material_type"], "RAW")

    def test_manufactured_component_is_flagged_not_exploded_further(self):
        self.assertTrue(self.rows["RM0000002"]["has_own_bom"])
        self.assertEqual(self.result["meta"]["sub_assembly_count"], 1)

    def test_zero_base_quantity_bom_is_reported_not_silently_dropped(self):
        self.assertNotIn("PM0000884", self.rows)
        unusable = self.result["meta"]["unusable_boms"]
        self.assertEqual(len(unusable), 1)
        self.assertEqual(unusable[0]["component_code"], "PM0000884")

    def test_plan_item_without_a_bom_is_named(self):
        missing = self.result["meta"]["items_without_bom"]
        self.assertEqual([row["item_code"] for row in missing], ["FG0000451"])

    def test_used_by_carries_the_evidence_for_the_number(self):
        used_by = self.rows["PM0000013"]["used_by"]
        self.assertEqual(len(used_by), 1)
        self.assertEqual(used_by[0]["item_code"], "FG0000004")
        self.assertEqual(used_by[0]["qty_per_unit"], Decimal("0.25"))

    def test_shared_component_aggregates_once_across_skus(self):
        reader = FakeReader()
        reader.data["lines"].append({
            "LineID": 2, "ItemCode": "FG0000032", "ItemName": "COLD PRESS 1 LTR 20 PCS",
            "BucketDate": date(2026, 8, 1), "PlannedQty": Decimal("100000"),
            "WhsCode": "", "Uom": "PCS", "PiecesPerCase": 20,
            "LitresPerUnit": Decimal("1"),
            "ItemGroup": "FINISHED", "TreeType": "P",
            "HasBom": 1, "BomBaseQty": Decimal("20"),
        })
        reader.data["bom"].append({
            "ParentCode": "FG0000032", "BomBaseQty": Decimal("20"), "ChildNum": 0, "LineType": 4,
            "ComponentCode": "PM0000053", "ComponentName": "HDPE BOTTLE 5 LTR",
            "BomQty": Decimal("20"), "QtyPerUnit": Decimal("1"),
            "IssueWarehouse": "BH-PC", "Uom": "PCS",
            "ItemGroup": "PACKAGING MATERIAL", "PurchaseItem": "Y",
            "LastPurchasePrice": Decimal("18.5"), "HasOwnBom": 0,
        })

        rows = {
            r["component_code"]: r
            for r in make_plan_service(reader).get_requirement(43)["data"]
        }
        bottle = rows["PM0000053"]
        self.assertEqual(bottle["required_qty"], Decimal("145000"))
        self.assertEqual(len(bottle["used_by"]), 1 + 1)

    def test_moq_rounds_the_order_up_and_says_so(self):
        with patch.object(PlanService, "_lead_time_map", return_value={
            "PM0000053": {
                "lead_time_days": 21, "moq": Decimal("20000"),
                "supplier_name": "AMD INDUSTRIES LTD",
            }
        }):
            rows = {
                r["component_code"]: r
                for r in make_plan_service().get_requirement(43)["data"]
            }
        row = rows["PM0000053"]
        self.assertEqual(row["shortage_qty"], Decimal("7000"))
        self.assertEqual(row["suggested_order_qty"], Decimal("20000"))
        self.assertEqual(row["moq_applied"], Decimal("20000"))
        # 1 Aug need-by minus 21 days is 11 July — already gone.
        self.assertEqual(row["order_by_date"], date(2026, 7, 11))
        self.assertEqual(row["urgency"], "OVERDUE")

    def test_missing_lead_time_outranks_scheduled(self):
        row = self.rows["PM0000053"]
        self.assertIsNone(row["lead_time_days"])
        self.assertEqual(row["urgency"], "NO_LEAD_TIME")
        self.assertEqual(row["lead_time_source"], "NONE")
        self.assertEqual(self.result["meta"]["no_lead_time_count"], 2)

    def test_shortage_rows_sort_worst_first_with_covered_last(self):
        codes = [row["component_code"] for row in self.result["data"]]
        self.assertEqual(codes[-1], "PM0000013")  # the only covered one

    def test_filters_narrow_the_rows(self):
        raw_only = self.service.get_requirement(43, material_type="RAW")
        self.assertEqual(
            [row["component_code"] for row in raw_only["data"]], ["RM0000002"]
        )

        shortages = self.service.get_requirement(43, include_covered=False)
        self.assertNotIn(
            "PM0000013", [row["component_code"] for row in shortages["data"]]
        )

    def test_warehouse_scope_is_reported_on_the_response(self):
        scoped = self.service.get_requirement(43, warehouses=["BH-PM"])
        self.assertEqual(scoped["meta"]["warehouse_scope"], ["BH-PM"])
        rows = {r["component_code"]: r for r in scoped["data"]}
        # RM stock lives in BH-LO, so scoping to BH-PM must show it as unstocked
        # rather than quietly counting stock from a warehouse the user excluded.
        self.assertEqual(rows["RM0000002"]["on_hand_qty"], Decimal("0"))

    def test_benchmark_absence_is_explicit(self):
        self.assertTrue(self.rows["PM0000053"]["has_benchmark"])
        self.assertFalse(self.rows["PM0000013"]["has_benchmark"])

    def test_resource_lines_never_appear_as_purchasable_material(self):
        """A filling cost must not top the purchase list.

        This guards a bug the live company actually produced: JWPL09240002
        "Filling Cost Commodities" is an `ITT1` resource against 99 parents, and
        treating it as a component put a 2.5-million-unit shortage of a cost
        centre above every real material.
        """
        self.assertNotIn("JWPL09240002", self.rows)

    def test_resource_lines_are_reported_not_dropped(self):
        resources = {row["resource_code"]: row for row in self.result["resources"]}
        self.assertIn("JWPL09240002", resources)
        # 45,000 PCS x 5 per unit
        self.assertEqual(resources["JWPL09240002"]["required_qty"], Decimal("225000"))
        self.assertEqual(
            resources["JWPL09240002"]["resource_name"], "Filling Cost Commodities"
        )
        self.assertEqual(self.result["meta"]["resource_line_count"], 1)

    def test_price_comes_from_the_item_master_not_the_last_purchase_order(self):
        """The thousandfold costing bug.

        The fake vendor row prices PM0000053 at 250 in its purchase unit while
        the item master says 18.5 per piece. Costing off the purchase-order price
        is how a live run produced an estimated spend of 710 billion rupees: bulk
        oil is bought by the metric ton and consumed by the litre.
        """
        reader = FakeReader()
        reader.data["vendors_for_items"][0]["Price"] = Decimal("250")
        rows = {
            r["component_code"]: r
            for r in make_plan_service(reader).get_requirement(43)["data"]
        }
        row = rows["PM0000053"]
        self.assertEqual(row["unit_price"], Decimal("18.5"))
        self.assertEqual(row["price_source"], "ITEM_MASTER")
        self.assertEqual(row["last_po_price"], Decimal("250"))
        self.assertEqual(row["estimated_value"], Decimal("7000") * Decimal("18.5"))

    def test_vendor_still_comes_from_the_last_purchase_order(self):
        row = self.rows["PM0000053"]
        self.assertEqual(row["vendor_code"], "VENDA000021")
        self.assertEqual(row["vendor_name"], "AMD INDUSTRIES LTD")

    def test_over_committed_stock_is_flagged(self):
        """Committed above on-hand is real, and worth showing rather than hiding."""
        reader = FakeReader()
        reader.data["stock"][0]["Committed"] = Decimal("50000")  # > 30,000 on hand
        rows = {
            r["component_code"]: r
            for r in make_plan_service(reader).get_requirement(43)["data"]
        }
        row = rows["PM0000053"]
        self.assertEqual(row["net_available_qty"], Decimal("-20000"))
        self.assertTrue(row["is_over_committed"])
        # The shortage grows to cover the over-promise, and never goes negative.
        self.assertEqual(row["shortage_before_po_qty"], Decimal("65000"))
        self.assertEqual(row["shortage_qty"], Decimal("55000"))

    def test_components_with_no_price_are_counted(self):
        self.assertEqual(self.result["meta"]["no_price_count"], 0)

        reader = FakeReader()
        for row in reader.data["bom"]:
            row["LastPurchasePrice"] = Decimal("0")
        for row in reader.data["stock"]:
            row["LastPurchasePrice"] = Decimal("0")
        result = make_plan_service(reader).get_requirement(43)
        self.assertEqual(result["meta"]["no_price_count"], 2)


class PlanDetailTests(TestCase):
    def setUp(self):
        self.service = make_plan_service()

    def test_cases_are_derived_from_pieces_per_case(self):
        result = self.service.get_plan(43, bucket_type=cal.MONTH)
        line = next(l for l in result["lines"] if l["item_code"] == "FG0000004")
        self.assertEqual(line["planned_qty"], Decimal("45000"))
        self.assertEqual(line["planned_cases"], Decimal("11250.00"))

    def test_actuals_come_from_sap_movements_in_the_same_unit(self):
        result = self.service.get_plan(43)
        line = next(l for l in result["lines"] if l["item_code"] == "FG0000004")
        self.assertEqual(line["produced_qty"], Decimal("20000"))
        self.assertEqual(line["variance_qty"], Decimal("-25000"))
        self.assertEqual(line["attainment_pct"], Decimal("44.4"))
        self.assertEqual(line["produced_cases"], Decimal("5000.00"))

    def test_item_with_no_production_reports_zero_not_missing(self):
        result = self.service.get_plan(43)
        line = next(l for l in result["lines"] if l["item_code"] == "FG0000451")
        self.assertEqual(line["produced_qty"], Decimal("0"))
        self.assertEqual(line["attainment_pct"], Decimal("0"))

    def test_bucket_totals_match_the_plan_total_at_every_grain(self):
        planned = Decimal("45000") + Decimal("25000")
        for bucket_type in (cal.DAY, cal.WEEK, cal.MONTH):
            with self.subTest(bucket_type=bucket_type):
                result = self.service.get_plan(43, bucket_type=bucket_type)
                self.assertEqual(
                    sum(b["planned_qty"] for b in result["buckets"]), planned
                )

    def test_sap_actual_failure_does_not_take_the_plan_down(self):
        reader = FakeReader()
        reader.get_produced_quantities = lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("HANA gone")
        )
        result = make_plan_service(reader).get_plan(43)
        self.assertEqual(len(result["lines"]), 2)
        self.assertEqual(result["plan"]["produced_qty"], Decimal("0"))

    def test_unknown_plan_is_a_404_not_an_empty_page(self):
        reader = FakeReader()
        reader.get_plan_header = lambda abs_id: None
        with self.assertRaises(PlanningError) as ctx:
            make_plan_service(reader).get_plan(999)
        self.assertEqual(ctx.exception.status_code, 404)

    # -- litres ---------------------------------------------------------

    def test_litres_come_from_salpackun_not_the_item_name(self):
        """FG0000004 reads "COLD PRESS 5 LTR 4 PCS" and is 5 L a piece.

        45,000 pieces x 5 L = 225,000 L. Parsing the name would also have to know
        the trailing "4 PCS" is the carton size and not a divisor -- the trap
        SalPackUn exists to avoid.
        """
        result = self.service.get_plan(43, bucket_type=cal.MONTH)
        line = next(l for l in result["lines"] if l["item_code"] == "FG0000004")
        self.assertEqual(line["litres_per_unit"], Decimal("5"))
        self.assertTrue(line["is_litre_item"])
        self.assertEqual(line["planned_litres"], Decimal("225000.000"))

    def test_fractional_litres_per_piece_are_not_rounded_away(self):
        """A 200 ML piece is 0.2 L, so 25,000 of them is 5,000 L, not 25,000."""
        result = self.service.get_plan(43)
        line = next(l for l in result["lines"] if l["item_code"] == "FG0000451")
        self.assertEqual(line["litres_per_unit"], Decimal("0.2"))
        self.assertEqual(line["planned_litres"], Decimal("5000.000"))

    def test_plan_totals_are_carried_in_all_three_units(self):
        plan = self.service.get_plan(43, bucket_type=cal.MONTH)["plan"]
        self.assertEqual(plan["planned_qty"], Decimal("70000"))          # pieces
        self.assertEqual(plan["planned_litres"], Decimal("230000.000"))  # 225k + 5k
        self.assertEqual(plan["planned_cases"], Decimal("11607.14"))     # /4 and /70

    def test_produced_litres_and_variance_use_the_same_factor(self):
        result = self.service.get_plan(43)
        line = next(l for l in result["lines"] if l["item_code"] == "FG0000004")
        self.assertEqual(line["produced_litres"], Decimal("100000.000"))  # 20,000 x 5
        self.assertEqual(line["variance_litres"], Decimal("-125000.000"))

    def test_bucket_litres_sum_to_the_plan_litre_total(self):
        """The pieces invariant, again in litres, at every grain."""
        for bucket_type in (cal.DAY, cal.WEEK, cal.MONTH):
            with self.subTest(bucket_type=bucket_type):
                result = self.service.get_plan(43, bucket_type=bucket_type)
                total = sum(b["planned_litres"] for b in result["buckets"])
                self.assertEqual(total, result["plan"]["planned_litres"])

    def test_a_non_litre_item_reports_zero_and_is_named(self):
        """Zero litres must be distinguishable from "not a litre item".

        Packaging carries a SalPackUn but is never flagged, so such a line would
        otherwise contribute a confident 0 L to the total with nothing saying why.
        """
        reader = FakeReader()
        reader.data["lines"][1]["LitresPerUnit"] = Decimal("0")
        result = make_plan_service(reader).get_plan(43)

        line = next(l for l in result["lines"] if l["item_code"] == "FG0000451")
        self.assertFalse(line["is_litre_item"])
        self.assertEqual(line["planned_litres"], Decimal("0"))

        self.assertEqual(result["plan"]["non_litre_item_count"], 1)
        self.assertEqual(
            [i["item_code"] for i in result["plan"]["non_litre_items"]], ["FG0000451"]
        )
        # The litre total drops that SKU, which is exactly why it gets named.
        self.assertEqual(result["plan"]["planned_litres"], Decimal("225000.000"))


# ---------------------------------------------------------------------------
# Purchase orders
# ---------------------------------------------------------------------------


def requirement_line(**overrides):
    line = {
        "item_code": "PM0000053",
        "item_name": "HDPE BOTTLE 5 LTR",
        "item_group": "PACKAGING MATERIAL",
        "material_type": "PACKAGING",
        "uom": "PCS",
        "vendor_code": "VENDA000021",
        "quantity": Decimal("7000"),
        "unit_price": Decimal("18.5"),
        "warehouse_code": "BH-PM",
        "required_date": date(2026, 9, 1),
        "required_qty": Decimal("45000"),
        "available_qty": Decimal("28000"),
        "on_order_qty": Decimal("10000"),
        "shortage_qty": Decimal("7000"),
        "moq_applied": None,
    }
    line.update(overrides)
    return line


@override_settings(PLANNING_PURCHASE_SIMULATE_SAP=True)
class PurchaseOrderServiceTests(TestCase):
    def setUp(self):
        self.buyer = User.objects.create_user(
            email="buyer@test.local", password="x",
            full_name="Buyer", employee_code="EMP-BUY",
        )
        self.hod = User.objects.create_user(
            email="hod@test.local", password="x",
            full_name="Procurement HOD", employee_code="EMP-HOD",
        )
        self.service = self._service(self.buyer)

    def _service(self, user) -> PurchaseOrderService:
        with patch("planning_purchase.services.purchase_service.CompanyContext"), \
             patch("planning_purchase.services.purchase_service.HanaProductionPlanReader"):
            service = PurchaseOrderService(COMPANY, user=user)
        service.reader = FakeReader()
        return service

    def _create(self, lines=None, **payload):
        body = {
            "plan_abs_id": 43,
            "plan_code": "AUG PLANNING 26",
            "lines": lines or [requirement_line()],
        }
        body.update(payload)
        return self.service.create_from_requirement(body)

    # -- create --------------------------------------------------------

    def test_creates_one_order_per_vendor(self):
        orders = self._create([
            requirement_line(),
            requirement_line(item_code="PM0000013", vendor_code="VENDA000913"),
        ])
        self.assertEqual(len(orders), 2)
        self.assertEqual(
            {order.vendor_code for order in orders},
            {"VENDA000021", "VENDA000913"},
        )

    def test_lines_for_one_vendor_land_on_one_order(self):
        orders = self._create([
            requirement_line(),
            requirement_line(item_code="PM0000013"),
        ])
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0].lines.count(), 2)

    def test_total_value_is_computed_from_the_lines(self):
        order = self._create()[0]
        self.assertEqual(order.total_value, Decimal("129500.000000"))

    def test_evidence_for_the_quantity_is_snapshotted(self):
        line = self._create()[0].lines.first()
        self.assertEqual(line.required_qty, Decimal("45000"))
        self.assertEqual(line.available_qty, Decimal("28000"))
        self.assertEqual(line.on_order_qty, Decimal("10000"))
        self.assertEqual(line.shortage_qty, Decimal("7000"))

    def test_a_line_without_a_supplier_is_refused_with_the_item_named(self):
        with self.assertRaises(PlanningError) as ctx:
            self._create([requirement_line(vendor_code="")])
        self.assertIn("PM0000053", str(ctx.exception))

    def test_no_lines_is_refused(self):
        with self.assertRaises(PlanningError):
            self.service.create_from_requirement({"lines": []})

    def test_repeated_item_is_merged_rather_than_duplicated(self):
        """A double-click must not become two SAP lines for the same item."""
        orders = self._create([
            requirement_line(quantity=Decimal("1000")),
            requirement_line(quantity=Decimal("500")),
        ])
        self.assertEqual(orders[0].lines.count(), 1)
        self.assertEqual(orders[0].lines.first().quantity, Decimal("1500"))

    def test_material_type_falls_back_to_the_item_group(self):
        order = self._create([
            requirement_line(material_type="", item_group="RAW MATERIAL")
        ])[0]
        self.assertEqual(order.lines.first().material_type, "RAW")

    # -- approve -------------------------------------------------------

    def test_author_cannot_approve_their_own_order(self):
        order = self._create()[0]
        with self.assertRaises(PurchaseOrderStateError) as ctx:
            self.service.approve(order)
        self.assertIn("someone other than", str(ctx.exception))
        order.refresh_from_db()
        self.assertEqual(order.status, PurchaseOrderStatus.DRAFT)

    def test_a_second_person_can_approve(self):
        order = self._create()[0]
        approved = self._service(self.hod).approve(order)
        self.assertEqual(approved.status, PurchaseOrderStatus.APPROVED)
        self.assertEqual(approved.approved_by, self.hod)
        self.assertIsNotNone(approved.approved_at)

    def test_approving_twice_is_refused(self):
        order = self._create()[0]
        hod_service = self._service(self.hod)
        hod_service.approve(order)
        with self.assertRaises(PurchaseOrderStateError):
            hod_service.approve(order)

    def test_an_empty_order_cannot_be_approved(self):
        order = self._create()[0]
        order.lines.all().delete()
        with self.assertRaises(PlanningError):
            self._service(self.hod).approve(order)

    # -- edit / cancel -------------------------------------------------

    def test_an_approved_order_can_no_longer_be_edited(self):
        order = self._create()[0]
        self._service(self.hod).approve(order)
        with self.assertRaises(PurchaseOrderStateError):
            self.service.update_draft(order, {"remarks": "late change"})

    def test_replacing_lines_recomputes_the_total(self):
        order = self._create()[0]
        self.service.update_draft(order, {
            "lines": [requirement_line(quantity=Decimal("100"),
                                       unit_price=Decimal("10"))],
        })
        order.refresh_from_db()
        self.assertEqual(order.lines.count(), 1)
        self.assertEqual(order.total_value, Decimal("1000.000000"))

    def test_a_posted_order_cannot_be_cancelled_here(self):
        order = self._create()[0]
        self._service(self.hod).approve(order)
        self._service(self.hod).post_to_sap(order.pk)
        order.refresh_from_db()
        with self.assertRaises(PurchaseOrderStateError) as ctx:
            self.service.cancel(order)
        self.assertIn("SAP", str(ctx.exception))

    # -- post ----------------------------------------------------------

    def test_a_draft_cannot_be_posted(self):
        order = self._create()[0]
        with self.assertRaises(PurchaseOrderStateError):
            self.service.post_to_sap(order.pk)

    def test_simulate_mode_posts_nothing_and_records_that_it_did_not(self):
        order = self._create()[0]
        self._service(self.hod).approve(order)

        with patch(
            "sap_client.service_layer.purchase_order_writer.PurchaseOrderWriter"
        ) as writer:
            posted = self._service(self.hod).post_to_sap(order.pk)

        writer.assert_not_called()
        self.assertEqual(posted.status, PurchaseOrderStatus.POSTED)
        self.assertTrue(posted.simulated)
        self.assertIsNone(posted.sap_doc_entry)

    def test_posting_twice_is_refused(self):
        order = self._create()[0]
        hod_service = self._service(self.hod)
        hod_service.approve(order)
        hod_service.post_to_sap(order.pk)
        with self.assertRaises(PurchaseOrderStateError) as ctx:
            hod_service.post_to_sap(order.pk)
        self.assertIn("Already posted", str(ctx.exception))

    @override_settings(PLANNING_PURCHASE_SIMULATE_SAP=False)
    def test_a_real_post_records_the_sap_document(self):
        order = self._create()[0]
        hod_service = self._service(self.hod)
        hod_service.approve(order)

        with patch(
            "sap_client.service_layer.purchase_order_writer.PurchaseOrderWriter"
        ) as writer_cls:
            writer_cls.return_value.create.return_value = {
                "DocEntry": 5555, "DocNum": 1026227777, "raw": {},
            }
            posted = hod_service.post_to_sap(order.pk)
            payload = writer_cls.return_value.create.call_args.args[0]

        self.assertEqual(posted.status, PurchaseOrderStatus.POSTED)
        self.assertEqual(posted.sap_doc_entry, 5555)
        self.assertEqual(posted.sap_doc_num, 1026227777)
        self.assertFalse(posted.simulated)

        self.assertEqual(payload["CardCode"], "VENDA000021")
        self.assertEqual(len(payload["DocumentLines"]), 1)
        self.assertEqual(payload["DocumentLines"][0]["ItemCode"], "PM0000053")
        self.assertEqual(payload["DocumentLines"][0]["Quantity"], Decimal("7000"))
        self.assertEqual(payload["DocumentLines"][0]["ShipDate"], "2026-09-01")
        self.assertIn("AUG PLANNING 26", payload["Comments"])

    @override_settings(PLANNING_PURCHASE_SIMULATE_SAP=False)
    def test_a_rejected_post_leaves_the_order_failed_and_retryable(self):
        from sap_client.exceptions import SAPValidationError

        order = self._create()[0]
        hod_service = self._service(self.hod)
        hod_service.approve(order)

        with patch(
            "sap_client.service_layer.purchase_order_writer.PurchaseOrderWriter"
        ) as writer_cls:
            writer_cls.return_value.create.side_effect = SAPValidationError(
                "Item PM0000053 is not defined for this vendor"
            )
            with self.assertRaises(PlanningError):
                hod_service.post_to_sap(order.pk)

        order.refresh_from_db()
        self.assertEqual(order.status, PurchaseOrderStatus.FAILED)
        self.assertIn("not defined for this vendor", order.sap_error_message)
        self.assertIsNone(order.sap_doc_entry)

        # FAILED must stay postable, or a fixable rejection strands the order.
        with patch(
            "sap_client.service_layer.purchase_order_writer.PurchaseOrderWriter"
        ) as writer_cls:
            writer_cls.return_value.create.return_value = {
                "DocEntry": 1, "DocNum": 2, "raw": {},
            }
            hod_service.post_to_sap(order.pk)
        order.refresh_from_db()
        self.assertEqual(order.status, PurchaseOrderStatus.POSTED)

    def test_idempotency_keys_are_unique_per_order(self):
        orders = self._create([
            requirement_line(),
            requirement_line(item_code="PM0000013", vendor_code="VENDA000913"),
        ])
        keys = {order.idempotency_key for order in orders}
        self.assertEqual(len(keys), 2)
        self.assertTrue(all(keys))


class PurchaseOrderModelTests(TestCase):
    def test_one_line_per_item_per_order_is_enforced_by_the_database(self):
        from django.db.utils import IntegrityError

        order = PurchaseOrder.objects.create(
            company_code=COMPANY, vendor_code="V1",
            doc_date=date(2026, 8, 24), doc_due_date=date(2026, 9, 1),
            idempotency_key="k1",
        )
        PurchaseOrderLine.objects.create(
            purchase_order=order, item_code="PM1", quantity=Decimal("1")
        )
        with self.assertRaises(IntegrityError):
            PurchaseOrderLine.objects.create(
                purchase_order=order, item_code="PM1", quantity=Decimal("2")
            )

    def test_idempotency_key_is_unique_per_company(self):
        from django.db.utils import IntegrityError

        common = dict(
            doc_date=date(2026, 8, 24), doc_due_date=date(2026, 9, 1),
            idempotency_key="same-key",
        )
        PurchaseOrder.objects.create(company_code=COMPANY, vendor_code="V1", **common)
        # A different company may reuse the key; the same company may not.
        PurchaseOrder.objects.create(
            company_code="JIVO_BEVERAGES", vendor_code="V1", **common
        )
        with self.assertRaises(IntegrityError):
            PurchaseOrder.objects.create(
                company_code=COMPANY, vendor_code="V2", **common
            )


class PurchaseOrderWriterTests(TestCase):
    """The payload guard, with no network involved."""

    def _writer(self):
        from sap_client.service_layer.purchase_order_writer import PurchaseOrderWriter

        class Context:
            service_layer = {
                "base_url": "https://sap.example", "company_db": "DB",
                "username": "u", "password": "p",
            }

        return PurchaseOrderWriter(Context())

    def test_a_payload_with_no_vendor_is_refused_before_any_request(self):
        from sap_client.exceptions import SAPValidationError

        with self.assertRaises(SAPValidationError):
            self._writer().create({"DocumentLines": [{"ItemCode": "X", "Quantity": 1}]})

    def test_a_payload_with_no_lines_is_refused(self):
        from sap_client.exceptions import SAPValidationError

        with self.assertRaises(SAPValidationError):
            self._writer().create({"CardCode": "V1", "DocumentLines": []})

    def test_a_zero_quantity_line_is_refused_and_names_the_item(self):
        from sap_client.exceptions import SAPValidationError

        with self.assertRaises(SAPValidationError) as ctx:
            self._writer().create({
                "CardCode": "V1",
                "DocumentLines": [{"ItemCode": "PM0000053", "Quantity": 0}],
            })
        self.assertIn("PM0000053", str(ctx.exception))
