"""Tests for the buildable-from-stock analysis.

    python manage.py test planning_purchase --settings=config.sqlite_test_settings

The trap these guard is the mutual exclusivity of per-SKU maxima: the same caps
appear in the buildable figure for every SKU that uses them, so a column of those
numbers must never be totalled. The component table is the additive answer, and
the two are kept apart deliberately.
"""

from datetime import date
from decimal import Decimal

from django.test import TestCase, override_settings

from .services.producible import next_working_day
from .tests import FakeReader, make_plan_service

ZERO = Decimal(0)


@override_settings(PLANNING_NON_WORKING_WEEKDAYS=[6], PLANNING_WEEK_START_DAY=0)
class NextWorkingDayTests(TestCase):
    def test_skips_a_non_working_day(self):
        # 1 Aug 2026 is a Saturday, so "tomorrow" is Monday the 3rd, not Sunday.
        self.assertEqual(next_working_day(date(2026, 8, 1)), date(2026, 8, 3))

    def test_an_ordinary_day_is_just_the_next_one(self):
        self.assertEqual(next_working_day(date(2026, 8, 3)), date(2026, 8, 4))


@override_settings(
    PLANNING_NON_WORKING_WEEKDAYS=[6],
    PLANNING_WEEK_START_DAY=0,
    PLANNING_PURCHASE_PM_WAREHOUSES=["BH-PS", "BH-PC", "BH-PM"],
    PLANNING_PURCHASE_RM_WAREHOUSES=["BH-LO", "BH-OT"],
)
class ProducibleTests(TestCase):
    """FG0000004 needs, per piece: 1 bottle, 0.25 carton, 5 L of oil."""

    TARGET = date(2026, 8, 3)

    DEFAULT_STOCK = {
        "PM0000053": ("10000", "0"),
        "PM0000013": ("10000", "0"),
        "RM0000002": ("100000", "0"),
    }

    # Stock is scoped per material type, so each component has to sit in a store
    # its own type is actually counted in: packaging in the packaging warehouse,
    # oil in the tank farm. Putting the oil in BH-PM would read as zero, which is
    # the rule working rather than a fixture quirk.
    HOME = {
        "PM0000053": ("BH-PM", "PACKAGING MATERIAL"),
        "PM0000013": ("BH-PM", "PACKAGING MATERIAL"),
        "RM0000002": ("BH-LO", "RAW MATERIAL"),
    }

    def _service(self, warehouse=None, **overrides):
        """`warehouse` forces every row into one store, for the wastage test."""
        levels = dict(self.DEFAULT_STOCK)
        levels.update(overrides)
        reader = FakeReader()
        reader.data["stock"] = [
            {
                "ItemCode": code,
                "WhsCode": warehouse or self.HOME[code][0],
                "OnHand": Decimal(on_hand),
                "MinStock": Decimal("0"),
                "Committed": Decimal(committed),
                "OnOrder": Decimal("0"),
                "Uom": "PCS",
                "LastPurchasePrice": Decimal("1"),
                "ItemGroup": self.HOME[code][1],
                "LastConsumptionDate": None,
                "DaysSinceLastConsumption": None,
            }
            for code, (on_hand, committed) in levels.items()
        ]
        return make_plan_service(reader)

    def _skus(self, result):
        return {row["item_code"]: row for row in result["skus"]}

    def _run(self, **kwargs):
        service = kwargs.pop("service", None) or self._service()
        return service.get_producible(43, target_date=self.TARGET, **kwargs)

    # -- the core calculation -------------------------------------------

    def test_the_scarcest_component_sets_the_buildable_quantity(self):
        """Bottles allow 10,000, cartons 40,000, oil 20,000 — so 10,000."""
        row = self._skus(self._run())["FG0000004"]
        self.assertEqual(row["buildable_qty"], Decimal("10000"))
        self.assertEqual(row["limited_by"], "PM0000053")

    def test_a_different_scarcity_moves_the_limiter(self):
        result = self._run(service=self._service(RM0000002=("25000", "0")))
        row = self._skus(result)["FG0000004"]
        # 25,000 L at 5 L a piece = 5,000 pieces.
        self.assertEqual(row["buildable_qty"], Decimal("5000"))
        self.assertEqual(row["limited_by"], "RM0000002")

    def test_buildable_is_reported_in_litres_and_cases_too(self):
        row = self._skus(self._run())["FG0000004"]
        self.assertEqual(row["buildable_litres"], Decimal("50000.000"))  # x 5 L
        self.assertEqual(row["buildable_cases"], Decimal("2500.00"))     # / 4

    def test_a_fractional_result_rounds_down(self):
        """Nine tenths of a bottle cannot be filled."""
        result = self._run(service=self._service(RM0000002=("12", "0")))
        # 12 L / 5 = 2.4 pieces, so 2.
        self.assertEqual(self._skus(result)["FG0000004"]["buildable_qty"], Decimal("2"))

    def test_the_limiter_is_named_with_its_numbers(self):
        detail = self._skus(self._run())["FG0000004"]["limited_by_detail"]
        self.assertEqual(detail["component_code"], "PM0000053")
        self.assertEqual(detail["available_qty"], Decimal("10000"))
        self.assertEqual(detail["qty_per_unit"], Decimal("1"))

    # -- honest absences -----------------------------------------------

    def test_no_bom_gives_no_answer_rather_than_zero(self):
        """No recipe in SAP must not read as out of material."""
        row = self._skus(self._run())["FG0000451"]
        self.assertFalse(row["has_bom"])
        self.assertIsNone(row["buildable_qty"])
        self.assertIsNone(row["covers_plan"])

    def test_resource_lines_cannot_starve_the_line(self):
        """A filling cost is not a material."""
        result = self._run()
        codes = {c["component_code"] for c in result["components"]}
        self.assertNotIn("JWPL09240002", codes)
        self.assertNotEqual(
            self._skus(result)["FG0000004"]["limited_by"], "JWPL09240002"
        )

    # -- stock basis ---------------------------------------------------

    def test_on_hand_is_the_default_and_free_is_opt_in(self):
        """Over-committed stock must not silently read as no stock.

        Most components on the live company are over-committed, so a FREE default
        would report a factory that shipped a million pieces as able to make
        nothing at all.
        """
        service = self._service(PM0000053=("10000", "40000"))

        on_hand = self._skus(self._run(service=service))
        self.assertEqual(on_hand["FG0000004"]["buildable_qty"], Decimal("10000"))

        free = self._skus(self._run(service=service, stock_basis="FREE"))
        self.assertEqual(free["FG0000004"]["buildable_qty"], Decimal("0"))

    def test_over_commitment_is_flagged_even_when_not_deducted(self):
        result = self._run(service=self._service(PM0000053=("10000", "40000")))
        component = next(
            c for c in result["components"] if c["component_code"] == "PM0000053"
        )
        self.assertTrue(component["over_committed"])
        self.assertEqual(component["on_hand_qty"], Decimal("10000"))
        self.assertEqual(component["free_qty"], Decimal("-30000"))

    def test_wastage_stock_is_never_counted_as_usable(self):
        """BH-WST holds scrap; filling from it is a promise nobody can keep."""
        result = self._run(service=self._service(warehouse="BH-WST"))
        self.assertEqual(self._skus(result)["FG0000004"]["buildable_qty"], Decimal("0"))
        self.assertEqual(result["meta"]["excluded_warehouses"], ["BH-WST"])

    # -- the additive half ---------------------------------------------

    def test_the_component_table_adds_demand_across_skus(self):
        """Two SKUs sharing a bottle add their demands on it, never take a max."""
        reader = FakeReader()
        reader.data["lines"].append({
            "LineID": 2, "ItemCode": "FG0000032",
            "ItemName": "COLD PRESS 1 LTR 20 PCS",
            "BucketDate": date(2026, 8, 1), "PlannedQty": Decimal("20000"),
            "WhsCode": "", "Uom": "PCS", "PiecesPerCase": 20,
            "LitresPerUnit": Decimal("1"),
            "ItemGroup": "FINISHED", "TreeType": "P",
            "HasBom": 1, "BomBaseQty": Decimal("20"),
        })
        reader.data["bom"].append({
            "ParentCode": "FG0000032", "BomBaseQty": Decimal("20"), "ChildNum": 0,
            "LineType": 4,
            "ComponentCode": "PM0000053", "ComponentName": "HDPE BOTTLE 5 LTR",
            "BomQty": Decimal("20"), "QtyPerUnit": Decimal("1"),
            "IssueWarehouse": "BH-PC", "Uom": "PCS",
            "ItemGroup": "PACKAGING MATERIAL", "PurchaseItem": "Y",
            "LastPurchasePrice": Decimal("18.5"), "HasOwnBom": 0,
        })
        reader.data["stock"] = [
            {
                "ItemCode": "PM0000053", "WhsCode": "BH-PM",
                "OnHand": Decimal("10000"), "MinStock": Decimal("0"),
                "Committed": Decimal("0"), "OnOrder": Decimal("0"),
                "Uom": "PCS", "LastPurchasePrice": Decimal("1"),
                "ItemGroup": "PACKAGING MATERIAL",
                "LastConsumptionDate": None, "DaysSinceLastConsumption": None,
            }
        ]
        result = make_plan_service(reader).get_producible(43, target_date=self.TARGET)

        bottle = next(
            c for c in result["components"] if c["component_code"] == "PM0000053"
        )
        self.assertEqual(len(bottle["drawn_by"]), 2)
        self.assertEqual(
            bottle["needed_qty"], sum(d["needed_qty"] for d in bottle["drawn_by"])
        )

    def test_a_blocked_sku_is_named_with_its_shortfall(self):
        result = self._run(service=self._service(PM0000053=("100", "0")))
        row = self._skus(result)["FG0000004"]
        self.assertFalse(row["covers_plan"])
        self.assertGreater(row["shortfall_qty"], ZERO)
        self.assertEqual(
            [b["item_code"] for b in result["meta"]["blocked_skus"]], ["FG0000004"]
        )

    def test_at_risk_litres_measure_the_day_not_the_worst_component(self):
        """One starved minor component must not read as "the day is 8% possible"."""
        result = self._run(service=self._service(PM0000053=("1", "0")))
        meta = result["meta"]
        self.assertEqual(meta["blocked_sku_count"], 1)
        self.assertGreater(meta["at_risk_litres"], ZERO)
        self.assertLessEqual(meta["at_risk_pct"], Decimal("100"))

    def test_a_fully_stocked_day_reports_nothing_blocking(self):
        meta = self._run()["meta"]
        self.assertTrue(meta["plan_runs_in_full"])
        self.assertEqual(meta["blocking_component_count"], 0)
        self.assertEqual(meta["at_risk_litres"], ZERO)
        self.assertEqual(meta["worst_component_coverage_pct"], Decimal("100.0"))

    def test_coverage_says_how_far_the_stock_goes(self):
        """A component with half of what the day needs reports 50%."""
        result = self._run(service=self._service(PM0000053=("100", "0")))
        bottle = next(
            c for c in result["components"] if c["component_code"] == "PM0000053"
        )
        expected = (
            Decimal("100") / bottle["needed_qty"] * 100
        ).quantize(Decimal("0.1"))
        self.assertEqual(bottle["coverage_pct"], expected)
        self.assertTrue(bottle["is_blocking"])
