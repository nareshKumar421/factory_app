"""Tests for a run somebody types in: "can we make this, and if not how much?"

    python manage.py test planning_purchase --settings=config.sqlite_test_settings

The fixture is built around one trap on purpose: **every** product in the
request clears its own standalone check, and the request still cannot be made.
Three SKUs each ask for less than the warehouse could give them alone, but two
of them drink the same oil and together they want 70,000 litres of the 60,000
there are. A screen that only reported the standalone maxima would tell the
floor to go ahead, so the allocated quantity is the number that matters and
these tests are mostly about it.
"""

from decimal import Decimal

from django.test import TestCase, override_settings

from .services.producible import (
    ALLOC_FAIR_SHARE,
    ALLOC_PRIORITY,
    BASIS_FREE,
    MAX_REQUEST_LINES,
)
from .tests import FakeReader, make_plan_service

ZERO = Decimal(0)

# Per piece: FG0000004 takes 1 bottle, 0.25 carton and 5 L of oil (its BOM is
# written per 4, and the fixture carries the divided figure the SQL returns).
# FG0000900 takes 1 bottle and 1 L of the SAME oil. FG0000950 shares nothing.
FG_A = "FG0000004"
FG_B = "FG0000900"
FG_C = "FG0000950"
OIL = "RM0000002"

EXTRA_BOM = [
    {
        "ParentCode": FG_B, "BomBaseQty": Decimal("1"), "ChildNum": 0, "LineType": 4,
        "ComponentCode": "PM0000700", "ComponentName": "PET BOTTLE 1 LTR",
        "BomQty": Decimal("1"), "QtyPerUnit": Decimal("1"),
        "IssueWarehouse": "BH-PC", "Uom": "PCS",
        "ItemGroup": "PACKAGING MATERIAL", "PurchaseItem": "Y",
        "LastPurchasePrice": Decimal("6"), "HasOwnBom": 0,
    },
    {
        "ParentCode": FG_B, "BomBaseQty": Decimal("1"), "ChildNum": 1, "LineType": 4,
        "ComponentCode": OIL, "ComponentName": "CANOLA COLD PRESS LOOSE OIL",
        "BomQty": Decimal("1"), "QtyPerUnit": Decimal("1"),
        "IssueWarehouse": "BH-PC", "Uom": "LTR",
        "ItemGroup": "RAW MATERIAL", "PurchaseItem": "Y",
        "LastPurchasePrice": Decimal("120"), "HasOwnBom": 1,
    },
    {
        "ParentCode": FG_C, "BomBaseQty": Decimal("1"), "ChildNum": 0, "LineType": 4,
        "ComponentCode": "PM0000800", "ComponentName": "POUCH 500 ML",
        "BomQty": Decimal("1"), "QtyPerUnit": Decimal("1"),
        "IssueWarehouse": "BH-PC", "Uom": "PCS",
        "ItemGroup": "PACKAGING MATERIAL", "PurchaseItem": "Y",
        "LastPurchasePrice": Decimal("3"), "HasOwnBom": 0,
    },
]

# Packaging has to sit in a packaging store and oil in the tank farm: stock is
# scoped per material type, so a row in the wrong warehouse reads as zero.
STOCK_LEVELS = {
    "PM0000053": ("BH-PM", "10000"),
    "PM0000013": ("BH-PM", "10000"),
    "PM0000700": ("BH-PM", "50000"),
    "PM0000800": ("BH-PM", "5000"),
    OIL: ("BH-LO", "60000"),
}

ITEMS = [
    {
        "ItemCode": FG_A, "ItemName": "COLD PRESS 5 LTR 4 PCS", "Uom": "PCS",
        "PiecesPerCase": 4, "LitresPerUnit": Decimal("5"),
        "ItemGroup": "FINISHED", "TreeType": "P", "HasBom": 1,
        "BomBaseQty": Decimal("4"),
    },
    {
        "ItemCode": FG_B, "ItemName": "COLD PRESS 1 LTR 12 PCS", "Uom": "PCS",
        "PiecesPerCase": 12, "LitresPerUnit": Decimal("1"),
        "ItemGroup": "FINISHED", "TreeType": "P", "HasBom": 1,
        "BomBaseQty": Decimal("1"),
    },
    {
        "ItemCode": FG_C, "ItemName": "POUCH BLEND 500 ML 24 PCS", "Uom": "PCS",
        "PiecesPerCase": 24, "LitresPerUnit": Decimal("0.5"),
        "ItemGroup": "FINISHED", "TreeType": "P", "HasBom": 1,
        "BomBaseQty": Decimal("1"),
    },
    {
        # SAP holds no recipe for this one. It must never be given a quantity.
        "ItemCode": "FG0000451", "ItemName": "COLD PRESS SUNFLOWER 200 ML",
        "Uom": "PCS", "PiecesPerCase": 70, "LitresPerUnit": Decimal("0.2"),
        "ItemGroup": "FINISHED", "TreeType": "N", "HasBom": 0,
        "BomBaseQty": Decimal("0"),
    },
]


def simulate_reader(**overrides):
    reader = FakeReader()
    reader.data["items"] = ITEMS
    reader.data["bom"] = reader.data["bom"] + EXTRA_BOM
    levels = dict(STOCK_LEVELS)
    levels.update(overrides)
    reader.data["stock"] = [
        {
            "ItemCode": code,
            "WhsCode": warehouse,
            "OnHand": Decimal(on_hand),
            "MinStock": ZERO,
            "Committed": ZERO,
            "OnOrder": ZERO,
            "Uom": "PCS",
            "LastPurchasePrice": ZERO,
            "ItemGroup": "",
            "LastConsumptionDate": None,
            "DaysSinceLastConsumption": None,
        }
        for code, (warehouse, on_hand) in levels.items()
    ]
    return reader


@override_settings(
    PLANNING_PURCHASE_PM_WAREHOUSES=["BH-PS", "BH-PC", "BH-PM"],
    PLANNING_PURCHASE_RM_WAREHOUSES=["BH-LO", "BH-OT"],
)
class SimulateBaseTests(TestCase):
    REQUEST = [
        {"item_code": FG_A, "quantity": Decimal("10000")},
        {"item_code": FG_B, "quantity": Decimal("20000")},
        {"item_code": FG_C, "quantity": Decimal("1000")},
    ]

    def run_request(self, lines=None, **kwargs):
        service = make_plan_service(kwargs.pop("reader", None) or simulate_reader())
        result = service.simulate_producible(lines or self.REQUEST, **kwargs)
        result["by_code"] = {row["item_code"]: row for row in result["items"]}
        result["by_component"] = {
            row["component_code"]: row for row in result["components"]
        }
        return result


class StandaloneVersusAllocatedTests(SimulateBaseTests):
    """The whole reason the allocated column exists."""

    def setUp(self):
        self.result = self.run_request()

    def test_every_product_clears_its_own_standalone_check(self):
        # Given the warehouse to itself, each of the three could be made in the
        # quantity asked for. This is the answer that would mislead on its own.
        for code in (FG_A, FG_B, FG_C):
            self.assertTrue(
                self.result["by_code"][code]["covers_plan"],
                f"{code} should clear its standalone check",
            )

    def test_the_run_as_a_whole_still_cannot_be_made(self):
        # 10,000 x 5 L + 20,000 x 1 L = 70,000 litres of oil against 60,000.
        self.assertFalse(self.result["meta"]["request_runs_in_full"])
        self.assertEqual(self.result["by_component"][OIL]["needed_qty"], Decimal("70000"))
        self.assertEqual(self.result["by_component"][OIL]["shortage_qty"], Decimal("10000"))
        self.assertTrue(self.result["by_component"][OIL]["is_blocking"])

    def test_a_component_nobody_is_short_of_is_not_blocking(self):
        self.assertFalse(self.result["by_component"]["PM0000800"]["is_blocking"])


class PriorityAllocationTests(SimulateBaseTests):
    """The default: fill the lines in the order they were entered."""

    def setUp(self):
        self.result = self.run_request(allocation=ALLOC_PRIORITY)

    def test_the_first_line_is_filled_before_the_second_gets_a_look(self):
        row = self.result["by_code"][FG_A]
        self.assertEqual(row["achievable_qty"], Decimal("10000"))
        self.assertTrue(row["runs_in_full"])
        self.assertEqual(row["unmet_qty"], ZERO)

    def test_the_second_line_gets_only_what_the_first_left(self):
        # 50,000 of the 60,000 litres went to FG0000004, so 10,000 remain and
        # FG0000900 takes 1 L a piece.
        row = self.result["by_code"][FG_B]
        self.assertEqual(row["achievable_qty"], Decimal("10000"))
        self.assertEqual(row["unmet_qty"], Decimal("10000"))
        self.assertFalse(row["runs_in_full"])

    def test_a_line_sharing_nothing_is_untouched_by_the_contention(self):
        row = self.result["by_code"][FG_C]
        self.assertEqual(row["achievable_qty"], Decimal("1000"))
        self.assertTrue(row["runs_in_full"])

    def test_the_shortfall_names_what_is_actually_stopping_it_now(self):
        detail = self.result["by_code"][FG_B]["allocation_limited_by_detail"]
        self.assertEqual(detail["component_code"], OIL)
        self.assertEqual(detail["qty_per_unit"], Decimal("1"))
        # Nothing left of it once the run is split, which is the honest figure:
        # the standalone view would have shown 60,000 litres available.
        self.assertEqual(detail["remaining_qty"], ZERO)
        self.assertEqual(detail["available_qty"], Decimal("60000"))

    def test_a_line_that_runs_in_full_names_no_limiter(self):
        self.assertIsNone(self.result["by_code"][FG_A]["allocation_limited_by"])
        self.assertIsNone(
            self.result["by_code"][FG_A]["allocation_limited_by_detail"]
        )

    def test_the_rows_stay_in_the_order_they_were_asked_for(self):
        # That order IS the priority the allocation honoured, so a table in any
        # other order would disagree with the split beside it.
        self.assertEqual(
            [row["item_code"] for row in self.result["items"]], [FG_A, FG_B, FG_C]
        )

    def test_the_allocated_quantities_fit_in_stock_all_at_once(self):
        """The invariant that lets this column, unlike the maxima, be totalled."""
        recipes = {
            FG_A: {"PM0000053": Decimal("1"), "PM0000013": Decimal("0.25"),
                   OIL: Decimal("5")},
            FG_B: {"PM0000700": Decimal("1"), OIL: Decimal("1")},
            FG_C: {"PM0000800": Decimal("1")},
        }
        drawn = {}
        for code, recipe in recipes.items():
            achieved = self.result["by_code"][code]["achievable_qty"]
            for component, per_unit in recipe.items():
                drawn[component] = drawn.get(component, ZERO) + achieved * per_unit

        for component, used in drawn.items():
            available = self.result["by_component"][component]["available_qty"]
            self.assertLessEqual(
                used, available,
                f"{component}: the split spends {used} of {available}",
            )

    def test_the_headline_measures_litres_not_a_count_of_lines(self):
        meta = self.result["meta"]
        self.assertEqual(meta["requested_litres"], Decimal("70500"))
        self.assertEqual(meta["achievable_litres"], Decimal("60500"))
        self.assertEqual(meta["achievable_pct"], Decimal("85.8"))
        self.assertEqual(meta["short_item_count"], 1)
        self.assertEqual(meta["runnable_item_count"], 2)
        self.assertEqual(meta["allocation"], ALLOC_PRIORITY)

    def test_cases_and_litres_come_back_for_the_allocated_quantity_too(self):
        row = self.result["by_code"][FG_B]
        self.assertEqual(row["achievable_litres"], Decimal("10000"))
        # 10,000 pieces at 12 a case.
        self.assertEqual(row["achievable_cases"], Decimal("833.33"))


class FairShareAllocationTests(SimulateBaseTests):
    """Scale every line by one factor instead of starving the later ones."""

    def setUp(self):
        self.result = self.run_request(allocation=ALLOC_FAIR_SHARE)

    def test_the_shared_factor_cuts_the_first_line_back(self):
        # 60,000 / 70,000 = 6/7 of everything, so 10,000 becomes 8,571 -- less
        # than PRIORITY gave it, which is the whole point of the policy.
        self.assertEqual(
            self.result["by_code"][FG_A]["achievable_qty"], Decimal("8571")
        )

    def test_the_later_line_gets_far_more_than_priority_would_give_it(self):
        self.assertEqual(
            self.result["by_code"][FG_B]["achievable_qty"], Decimal("17145")
        )

    def test_a_line_competing_for_nothing_is_not_held_back_by_the_factor(self):
        """The second pass exists for exactly this row.

        FG0000950 shares no component with anything, so scaling it to 6/7 would
        leave 143 pouches' worth of material idle for a shortage of an oil it
        does not use. Material sitting unused in the answer reads as a defect.
        """
        row = self.result["by_code"][FG_C]
        self.assertEqual(row["achievable_qty"], Decimal("1000"))
        self.assertTrue(row["runs_in_full"])

    def test_the_second_pass_spends_the_oil_down_to_nothing(self):
        # 8,571 x 5 + 17,145 x 1 = 60,000 exactly.
        used = (
            self.result["by_code"][FG_A]["achievable_qty"] * Decimal("5")
            + self.result["by_code"][FG_B]["achievable_qty"] * Decimal("1")
        )
        self.assertEqual(used, Decimal("60000"))

    def test_the_policy_is_named_on_the_response(self):
        self.assertEqual(self.result["meta"]["allocation"], ALLOC_FAIR_SHARE)
        self.assertIn("common factor", " ".join(self.result["meta"]["notes"]))


class RoundingTests(SimulateBaseTests):
    def test_a_part_made_unit_is_never_promised(self):
        """Whole units only, rounded down.

        1,999 litres of oil at 5 L a piece is 399.8 pieces. Rounding that up
        would hand out material that is not in the building.
        """
        result = self.run_request(
            lines=[{"item_code": FG_A, "quantity": Decimal("500")}],
            reader=simulate_reader(**{OIL: ("BH-LO", "1999")}),
        )
        self.assertEqual(result["by_code"][FG_A]["achievable_qty"], Decimal("399"))


class NoRecipeTests(SimulateBaseTests):
    """An item SAP holds no BOM for has no answer, which is not an answer of nil."""

    def setUp(self):
        self.result = self.run_request(lines=[
            {"item_code": FG_A, "quantity": Decimal("100")},
            {"item_code": "FG0000451", "quantity": Decimal("25000")},
        ])

    def test_no_quantity_is_invented_for_it(self):
        row = self.result["by_code"]["FG0000451"]
        self.assertFalse(row["has_bom"])
        self.assertIsNone(row["achievable_qty"])
        self.assertIsNone(row["achievable_litres"])
        self.assertIsNone(row["unmet_qty"])

    def test_it_is_never_reported_as_running_in_full(self):
        # The dangerous failure would be a confident "yes" for a product nothing
        # was actually checked against.
        self.assertIsNone(self.result["by_code"]["FG0000451"]["runs_in_full"])

    def test_the_verdict_is_flagged_as_incomplete(self):
        meta = self.result["meta"]
        self.assertEqual(meta["item_without_bom_count"], 1)
        self.assertFalse(meta["fully_checked"])

    def test_the_items_that_could_be_checked_still_answer(self):
        self.assertEqual(
            self.result["by_code"][FG_A]["achievable_qty"], Decimal("100")
        )
        self.assertEqual(self.result["meta"]["answerable_item_count"], 1)

    def test_an_unusable_bom_line_is_reported_rather_than_silently_dropped(self):
        # PM0000884 has a zero BOM base quantity in SAP, so no per-unit figure
        # exists for it. It must not quietly vanish from the analysis.
        codes = [row["component_code"] for row in self.result["meta"]["unusable_boms"]]
        self.assertIn("PM0000884", codes)


class RequestHygieneTests(SimulateBaseTests):
    def test_the_same_product_twice_is_one_product_to_make(self):
        """Left as two lines it would compete against itself.

        The first copy would eat the stock and the second would report a
        shortage of material it had just consumed.
        """
        result = self.run_request(lines=[
            {"item_code": FG_A, "quantity": Decimal("4000")},
            {"item_code": FG_A, "quantity": Decimal("6000")},
        ])
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["by_code"][FG_A]["planned_qty"], Decimal("10000"))
        self.assertEqual(result["by_code"][FG_A]["achievable_qty"], Decimal("10000"))
        self.assertEqual(result["meta"]["merged_item_codes"], [FG_A])

    def test_a_code_sap_does_not_know_is_named_not_swallowed(self):
        result = self.run_request(lines=[
            {"item_code": FG_A, "quantity": Decimal("100")},
            {"item_code": "FG9999999", "quantity": Decimal("500")},
        ])
        self.assertEqual(result["meta"]["unknown_item_codes"], ["FG9999999"])
        self.assertNotIn("FG9999999", result["by_code"])
        self.assertFalse(result["meta"]["fully_checked"])

    def test_a_clean_request_reports_itself_fully_checked(self):
        result = self.run_request(
            lines=[{"item_code": FG_A, "quantity": Decimal("100")}]
        )
        self.assertTrue(result["meta"]["fully_checked"])
        self.assertTrue(result["meta"]["request_runs_in_full"])


class StockBasisTests(SimulateBaseTests):
    def test_free_stock_nets_off_what_sap_has_committed(self):
        reader = simulate_reader()
        for row in reader.data["stock"]:
            if row["ItemCode"] == OIL:
                row["Committed"] = Decimal("59000")

        result = self.run_request(
            lines=[{"item_code": FG_A, "quantity": Decimal("10000")}],
            reader=reader,
            stock_basis=BASIS_FREE,
        )
        # 1,000 litres free at 5 L a piece.
        self.assertEqual(result["by_code"][FG_A]["achievable_qty"], Decimal("200"))
        self.assertEqual(result["meta"]["stock_basis"], BASIS_FREE)

    def test_on_hand_is_the_default_and_ignores_the_commitment(self):
        reader = simulate_reader()
        for row in reader.data["stock"]:
            if row["ItemCode"] == OIL:
                # More reserved than is physically in the tank -- the ordinary
                # state of this company, and the reason ON_HAND is the default.
                row["Committed"] = Decimal("61000")

        result = self.run_request(
            lines=[{"item_code": FG_A, "quantity": Decimal("2000")}], reader=reader
        )
        self.assertEqual(result["by_code"][FG_A]["achievable_qty"], Decimal("2000"))
        self.assertEqual(result["meta"]["over_committed_component_count"], 1)


class RequestValidationTests(TestCase):
    """The serializer, because a bad request must not reach a BOM explosion."""

    def _serializer(self, payload):
        from .serializers import SimulateRequestSerializer

        return SimulateRequestSerializer(data=payload)

    def test_a_quantity_of_zero_is_rejected(self):
        serializer = self._serializer(
            {"lines": [{"item_code": FG_A, "quantity": "0"}]}
        )
        self.assertFalse(serializer.is_valid())

    def test_an_empty_request_is_rejected(self):
        self.assertFalse(self._serializer({"lines": []}).is_valid())

    def test_a_pasted_spreadsheet_cannot_hold_the_sap_connection(self):
        payload = {
            "lines": [
                {"item_code": f"FG{index:07d}", "quantity": "1"}
                for index in range(MAX_REQUEST_LINES + 1)
            ]
        }
        self.assertFalse(self._serializer(payload).is_valid())

    def test_the_defaults_are_on_hand_stock_and_priority_order(self):
        serializer = self._serializer(
            {"lines": [{"item_code": FG_A, "quantity": "100"}]}
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data["allocation"], ALLOC_PRIORITY)
        self.assertEqual(serializer.validated_data["stock_basis"], "ON_HAND")


class BomItemPickerTests(SimulateBaseTests):
    def test_only_items_with_a_real_recipe_are_offered(self):
        service = make_plan_service(simulate_reader())
        codes = [row["ItemCode"] for row in service.reader.search_bom_items()]
        self.assertIn(FG_A, codes)
        # Carries OITM."TreeType" = 'N' and has no BOM, so it must not be
        # pickable: it would answer "no BOM" to every quantity typed at it.
        self.assertNotIn("FG0000451", codes)

    def test_the_search_matches_a_name_as_well_as_a_code(self):
        service = make_plan_service(simulate_reader())
        codes = [
            row["ItemCode"] for row in service.reader.search_bom_items(search="1 ltr")
        ]
        self.assertEqual(codes, [FG_B])


class ResponseShapeTests(SimulateBaseTests):
    """The engine's dict, through the serializers, as the frontend receives it.

    This is the seam that fails silently. The engine works in `planned_qty`
    because it shares its maths with the plan screen, while the API renames that
    to `requested_qty` -- nothing about a request is "planned". A wrong `source`
    there type-checks, passes every test above, and 500s the moment somebody
    opens the page.
    """

    def setUp(self):
        from .serializers import ProducibleComponentSerializer, SimulateItemSerializer

        self.result = self.run_request()
        self.payload = SimulateItemSerializer(self.result["items"], many=True).data
        self.components = ProducibleComponentSerializer(
            self.result["components"], many=True
        ).data
        self.by_code = {row["item_code"]: row for row in self.payload}

    def test_the_requested_quantity_survives_the_rename(self):
        self.assertEqual(self.by_code[FG_A]["requested_qty"], "10000.000")
        self.assertNotIn("planned_qty", self.by_code[FG_A])

    def test_the_allocated_quantity_is_serialised(self):
        self.assertEqual(self.by_code[FG_B]["achievable_qty"], "10000.000")
        self.assertEqual(self.by_code[FG_B]["unmet_qty"], "10000.000")
        self.assertFalse(self.by_code[FG_B]["runs_in_full"])

    def test_the_limiter_carries_what_is_left_of_it(self):
        detail = self.by_code[FG_B]["allocation_limited_by_detail"]
        self.assertEqual(detail["component_code"], OIL)
        self.assertEqual(detail["remaining_qty"], "0.000")

    def test_a_line_that_runs_in_full_serialises_a_null_limiter(self):
        self.assertIsNone(self.by_code[FG_A]["allocation_limited_by"])
        self.assertIsNone(self.by_code[FG_A]["allocation_limited_by_detail"])

    def test_the_standalone_maximum_is_still_carried_alongside(self):
        # Both quantities reach the client, because the page's whole job is
        # showing that the standalone one said yes and the allocated one did not.
        row = self.by_code[FG_B]
        self.assertEqual(row["buildable_qty"], "50000.000")
        self.assertEqual(row["achievable_qty"], "10000.000")

    def test_the_component_table_serialises_with_the_shared_serializer(self):
        oil = next(r for r in self.components if r["component_code"] == OIL)
        self.assertEqual(oil["needed_qty"], "70000.000")
        self.assertEqual(oil["shortage_qty"], "10000.000")
        self.assertTrue(oil["is_blocking"])

    def test_an_item_with_no_recipe_serialises_nulls_not_zeroes(self):
        from .serializers import SimulateItemSerializer

        result = self.run_request(lines=[
            {"item_code": "FG0000451", "quantity": Decimal("25000")},
        ])
        row = SimulateItemSerializer(result["items"], many=True).data[0]
        self.assertIsNone(row["achievable_qty"])
        self.assertIsNone(row["buildable_qty"])
        self.assertIsNone(row["runs_in_full"])
        # The ask itself is still a real number -- only the answers are null.
        self.assertEqual(row["requested_qty"], "25000.000")


class RoutingTests(TestCase):
    def test_both_new_endpoints_resolve(self):
        from django.urls import reverse

        self.assertEqual(
            reverse("planning_purchase:pp-producible-simulate"),
            "/api/v1/planning-purchase/producible/simulate/",
        )
        self.assertEqual(
            reverse("planning_purchase:pp-bom-items"),
            "/api/v1/planning-purchase/bom-items/",
        )
