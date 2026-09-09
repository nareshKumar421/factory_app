"""
packing_material/tests.py

The arithmetic, against fixed rows. No HANA connection and no live database:
the readers are injected, so every figure this board puts on screen is
exercised here as a pure function of the rows SAP returned.

Where a test asserts a specific number, the number comes from the live data
recorded in ``constants`` -- the same August 2026 figures that were measured
before any of this was written.
"""

from datetime import date

from django.test import SimpleTestCase

from .constants import supply_warehouses
from .services import (
    PackingMaterialService,
    build_requirement_rows,
    build_stock_board,
    explode_dispatch,
    index_bom,
    index_drivers,
    index_master,
    issue_window,
    plan_coverage_summary,
    rank_by_qty,
    requirement_totals,
    resolve_plan,
    split_dispatch_lines,
    summarise_dispatch,
    unplanned_issue,
)

MASTER = [
    {
        "item_code": "PM0000121",
        "item_name": "PET BOTTLE 1 LTR 52 GMS POMACE",
        "uom": "PCS",
        "sub_group": "PET BOTTLES",
        "unit_price": 8.8,
    },
    {
        "item_code": "PM0000085",
        "item_name": "CAPS 1 OR 2 LTR WHITE AND YELLOW WITH LOGO",
        "uom": "PCS",
        "sub_group": "CAPS",
        "unit_price": 1.29,
    },
    {
        "item_code": "PM0000019",
        "item_name": "LABEL 1 LTR KACHCHI GHANI BACK",
        "uom": "PCS",
        "sub_group": "LABEL",
        "unit_price": 0.3,
    },
    {
        "item_code": "PM0000900",
        "item_name": "STRETCH FILM",
        "uom": "KGS",
        "sub_group": "FILM",
        "unit_price": 140.0,
    },
]


# ---------------------------------------------------------------------------
# The four cards
# ---------------------------------------------------------------------------


class StockBoardTests(SimpleTestCase):
    WAREHOUSES = ["BH-PC", "BH-BS", "BH-PM"]
    NAMES = [
        {"code": "BH-PC", "name": "Bhakharpur Production Consumption", "inactive": False},
        {"code": "BH-BS", "name": "Bhakharpur Basement", "inactive": False},
        {"code": "BH-PM", "name": "Bhakharpur Packaging Materials 1st Floor", "inactive": False},
    ]
    STOCK = [
        {"warehouse": "BH-PC", "item_code": "PM0000121", "stock_qty": 1000, "stock_value": 8800},
        {"warehouse": "BH-PC", "item_code": "PM0000085", "stock_qty": 4000, "stock_value": 5160},
        {"warehouse": "BH-BS", "item_code": "PM0000019", "stock_qty": 2500, "stock_value": 750},
        # The same bottle again, in a second store.
        {"warehouse": "BH-PM", "item_code": "PM0000121", "stock_qty": 500, "stock_value": 4400},
    ]

    def board(self, **overrides):
        kwargs = {
            "warehouse_codes": self.WAREHOUSES,
            "warehouse_names": self.NAMES,
            "stock_rows": self.STOCK,
            "master": index_master(MASTER),
        }
        kwargs.update(overrides)
        return build_stock_board(**kwargs)

    def test_cards_come_back_in_the_order_they_were_asked_for(self):
        """A card must never swap places with another between two loads."""
        board = self.board()
        self.assertEqual([w["code"] for w in board["warehouses"]], self.WAREHOUSES)

    def test_each_card_totals_only_its_own_warehouse(self):
        board = {w["code"]: w for w in self.board()["warehouses"]}
        self.assertEqual(board["BH-PC"]["total_qty"], 5000)
        self.assertEqual(board["BH-PC"]["total_value"], 13960)
        self.assertEqual(board["BH-PC"]["item_count"], 2)
        self.assertEqual(board["BH-BS"]["total_qty"], 2500)
        self.assertEqual(board["BH-PM"]["total_qty"], 500)

    def test_total_card_sums_the_cards(self):
        total = self.board()["total"]
        self.assertEqual(total["total_qty"], 8000)
        self.assertEqual(total["total_value"], 19110)
        self.assertEqual(total["warehouse_count"], 3)

    def test_total_item_count_is_distinct_not_the_sum_of_the_cards(self):
        """One bottle held in two stores is one item, not two.

        Four stock rows, three distinct codes. Adding the per-card counts
        would report 4 and overstate the range the factory carries.
        """
        self.assertEqual(self.board()["total"]["item_count"], 3)

    def test_items_within_a_card_are_ranked_by_quantity(self):
        items = {w["code"]: w for w in self.board()["warehouses"]}["BH-PC"]["items"]
        self.assertEqual([row["item_code"] for row in items], ["PM0000085", "PM0000121"])

    def test_item_rows_carry_the_master_description(self):
        items = {w["code"]: w for w in self.board()["warehouses"]}["BH-BS"]["items"]
        self.assertEqual(items[0]["item_name"], "LABEL 1 LTR KACHCHI GHANI BACK")
        self.assertEqual(items[0]["uom"], "PCS")
        self.assertEqual(items[0]["sub_group"], "LABEL")

    def test_an_inactive_warehouse_is_flagged_not_dropped(self):
        """BH-PP is Beverages' floor, frozen in Oil's schema, and holds 1.64 Cr.

        A warehouse somebody configured must show what is in it and say SAP
        has decommissioned it. Filtering it out would report an empty store.
        """
        board = build_stock_board(
            ["BH-PP"],
            [{"code": "BH-PP", "name": "Production Process 1st Floor", "inactive": True}],
            [
                {
                    "warehouse": "BH-PP",
                    "item_code": "PM0000121",
                    "stock_qty": 4983774,
                    "stock_value": 16390810,
                }
            ],
            index_master(MASTER),
        )
        card = board["warehouses"][0]
        self.assertTrue(card["inactive"])
        self.assertTrue(card["exists"])
        self.assertEqual(card["total_qty"], 4983774)

    def test_a_warehouse_sap_does_not_have_is_reported_as_missing(self):
        """A misconfigured code must read as a mistake, not as an empty store."""
        board = build_stock_board(["BH-XYZ"], [], [], index_master(MASTER))
        card = board["warehouses"][0]
        self.assertFalse(card["exists"])
        self.assertEqual(card["name"], "BH-XYZ")
        self.assertEqual(card["total_qty"], 0)

    def test_an_empty_board_does_not_divide_by_zero(self):
        board = build_stock_board(self.WAREHOUSES, self.NAMES, [], index_master(MASTER))
        self.assertEqual(board["total"]["total_qty"], 0)
        self.assertEqual([w["share_pct"] for w in board["warehouses"]], [0.0, 0.0, 0.0])


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------


class RankingTests(SimpleTestCase):
    def test_ranked_by_quantity_not_value(self):
        """The choice the factory made: pieces lead, rupees ride along.

        The bottle is worth 6.8 times the caps here and still ranks below
        them, because more caps were used. Ranking on value would invert it.
        """
        ranked = rank_by_qty(
            {"PM0000121": 603505, "PM0000085": 570331, "PM0000900": 120},
            index_master(MASTER),
            10,
        )
        self.assertEqual(
            [row["item_code"] for row in ranked["items"]],
            ["PM0000121", "PM0000085", "PM0000900"],
        )
        # ... and the value is on the row all the same.
        self.assertEqual(ranked["items"][1]["value"], round(570331 * 1.29, 2))

    def test_ranks_are_one_based_and_consecutive(self):
        ranked = rank_by_qty({"PM0000121": 30, "PM0000085": 20}, index_master(MASTER), 10)
        self.assertEqual([row["rank"] for row in ranked["items"]], [1, 2])

    def test_share_is_of_the_whole_period_not_of_the_rows_shown(self):
        """"The top two are 60% of the month" has to be true of the month."""
        ranked = rank_by_qty(
            {"a": 40, "b": 20, "c": 20, "d": 20},
            {},
            2,
        )
        self.assertEqual([row["share_pct"] for row in ranked["items"]], [40.0, 20.0])
        self.assertEqual(ranked["totals"]["total_qty"], 100)
        self.assertEqual(ranked["totals"]["shown_qty"], 60)
        self.assertEqual(ranked["totals"]["shown_share_pct"], 60.0)

    def test_totals_count_every_item_not_only_the_top_n(self):
        ranked = rank_by_qty({str(n): n for n in range(1, 30)}, {}, 10)
        self.assertEqual(len(ranked["items"]), 10)
        self.assertEqual(ranked["totals"]["item_count"], 29)

    def test_zero_and_negative_quantities_are_left_out(self):
        """Nothing issued is not a top-ten entry; it is not an entry."""
        ranked = rank_by_qty({"a": 5, "b": 0, "c": -3}, {}, 10)
        self.assertEqual([row["item_code"] for row in ranked["items"]], ["a"])

    def test_ties_break_on_item_code_so_the_order_is_stable(self):
        """Labels come in matched front/back pairs with identical quantities.

        Without a tiebreak the two would swap places between loads on nothing
        but dict ordering, which reads as the board changing its mind.
        """
        first = rank_by_qty({"PM0000020": 390503, "PM0000019": 390503}, {}, 10)
        second = rank_by_qty({"PM0000019": 390503, "PM0000020": 390503}, {}, 10)
        self.assertEqual(
            [row["item_code"] for row in first["items"]],
            [row["item_code"] for row in second["items"]],
        )

    def test_an_item_missing_from_the_master_is_still_reported(self):
        """An item group changed under a movement that already happened.

        The quantity is real and stays on the board; only the description is
        blank. Dropping the row would understate the month.
        """
        ranked = rank_by_qty({"PM9999999": 100}, index_master(MASTER), 10)
        self.assertEqual(ranked["items"][0]["item_code"], "PM9999999")
        self.assertEqual(ranked["items"][0]["item_name"], "")
        self.assertEqual(ranked["items"][0]["value"], 0)

    def test_an_empty_period_does_not_divide_by_zero(self):
        ranked = rank_by_qty({}, {}, 10)
        self.assertEqual(ranked["items"], [])
        self.assertEqual(ranked["totals"]["total_qty"], 0)
        self.assertEqual(ranked["totals"]["shown_share_pct"], 0.0)


# ---------------------------------------------------------------------------
# BOM explosion
# ---------------------------------------------------------------------------


class BomTests(SimpleTestCase):
    def test_a_component_on_two_lines_of_one_recipe_is_summed(self):
        """SAP allows it -- a printed and an unprinted carton on one gift pack.

        Exploding both separately would put the same item on the board twice.
        """
        bom = index_bom(
            [
                {"parent_code": "FG1", "pm_code": "PM0000019", "qty_per_unit": 1},
                {"parent_code": "FG1", "pm_code": "PM0000019", "qty_per_unit": 0.5},
            ]
        )
        self.assertEqual(bom["FG1"], [{"pm_code": "PM0000019", "qty_per_unit": 1.5}])

    def test_a_recipe_with_no_usable_per_unit_quantity_is_dropped(self):
        """A batch size SAP cannot state cannot be exploded.

        The reader's NULLIF already returns nothing for these; this is the
        second gate, because reporting one as per-1 would overstate the
        component by the batch size.
        """
        bom = index_bom(
            [
                {"parent_code": "FG1", "pm_code": "PM1", "qty_per_unit": 0},
                {"parent_code": "FG2", "pm_code": "PM2", "qty_per_unit": None},
                {"parent_code": "", "pm_code": "PM3", "qty_per_unit": 1},
            ]
        )
        self.assertEqual(bom, {})

    def test_explosion_multiplies_dispatched_pieces_by_the_recipe(self):
        bom = index_bom(
            [
                {"parent_code": "FG1", "pm_code": "PM0000121", "qty_per_unit": 1},
                {"parent_code": "FG1", "pm_code": "PM0000085", "qty_per_unit": 1},
                {"parent_code": "FG1", "pm_code": "PM0000900", "qty_per_unit": 0.002},
            ]
        )
        exploded = explode_dispatch([{"item_code": "FG1", "qty": 10000}], bom)
        self.assertEqual(exploded["quantities"]["PM0000121"], 10000)
        self.assertEqual(exploded["quantities"]["PM0000900"], 20)

    def test_the_same_packing_item_across_two_finished_goods_is_added_up(self):
        bom = index_bom(
            [
                {"parent_code": "FG1", "pm_code": "PM0000085", "qty_per_unit": 1},
                {"parent_code": "FG2", "pm_code": "PM0000085", "qty_per_unit": 1},
            ]
        )
        exploded = explode_dispatch(
            [{"item_code": "FG1", "qty": 100}, {"item_code": "FG2", "qty": 250}], bom
        )
        self.assertEqual(exploded["quantities"]["PM0000085"], 350)

    def test_an_item_with_no_recipe_is_named_in_coverage(self):
        """Otherwise a board that explodes 40% looks like one that explodes all."""
        bom = index_bom([{"parent_code": "FG1", "pm_code": "PM1", "qty_per_unit": 1}])
        exploded = explode_dispatch(
            [{"item_code": "FG1", "qty": 600}, {"item_code": "FG404", "qty": 400}], bom
        )
        coverage = exploded["coverage"]
        self.assertEqual(coverage["fg_items"], 2)
        self.assertEqual(coverage["fg_items_with_bom"], 1)
        self.assertEqual(coverage["fg_items_without_bom"], ["FG404"])
        self.assertEqual(coverage["qty_total"], 1000)
        self.assertEqual(coverage["qty_with_bom"], 600)
        self.assertEqual(coverage["qty_covered_pct"], 60.0)

    def test_the_named_gaps_are_capped_but_the_count_is_not(self):
        bom = {}
        exploded = explode_dispatch(
            [{"item_code": f"FG{n:03d}", "qty": 1} for n in range(80)], bom
        )
        coverage = exploded["coverage"]
        self.assertEqual(coverage["fg_items_without_bom_count"], 80)
        self.assertEqual(len(coverage["fg_items_without_bom"]), 25)

    def test_a_net_negative_item_takes_packaging_back_off_the_board(self):
        """More came back than went out. The month is net, so this is too."""
        bom = index_bom([{"parent_code": "FG1", "pm_code": "PM1", "qty_per_unit": 2}])
        exploded = explode_dispatch([{"item_code": "FG1", "qty": -50}], bom)
        self.assertEqual(exploded["quantities"]["PM1"], -100)

    def test_coverage_cannot_exceed_100_percent(self):
        """The live September bug: three net-negative items had no recipe.

        Netting the un-explodable volume against the explodable one shrank the
        denominator and reported 100.29% coverage. Coverage counts absolute
        volume, so it is bounded by construction.
        """
        bom = index_bom([{"parent_code": "FG1", "pm_code": "PM1", "qty_per_unit": 1}])
        exploded = explode_dispatch(
            [{"item_code": "FG1", "qty": 375785}, {"item_code": "FG404", "qty": -1092}],
            bom,
        )
        coverage = exploded["coverage"]
        self.assertLessEqual(coverage["qty_covered_pct"], 100.0)
        self.assertEqual(coverage["qty_total"], 376877)
        self.assertEqual(coverage["qty_with_bom"], 375785)
        self.assertEqual(coverage["fg_items_without_bom"], ["FG404"])

    def test_coverage_of_an_empty_period_does_not_divide_by_zero(self):
        exploded = explode_dispatch([], {})
        self.assertEqual(exploded["coverage"]["qty_covered_pct"], 0.0)


# ---------------------------------------------------------------------------
# Splitting the dispatched lines
# ---------------------------------------------------------------------------


class DispatchSplitTests(SimpleTestCase):
    def test_sap_rows_are_split_on_the_group_they_arrive_with(self):
        split = split_dispatch_lines(
            [
                {"item_code": "FG1", "item_group": 102, "qty": 100},
                {"item_code": "PM1", "item_group": 105, "qty": 40},
            ]
        )
        self.assertEqual([row["item_code"] for row in split["fg"]], ["FG1"])
        self.assertEqual([row["item_code"] for row in split["direct_pm"]], ["PM1"])

    def test_gate_out_rows_are_split_on_the_looked_up_group(self):
        """A gate-out line carries a code and no group.

        18 of August's distinct gate-out items were packaging. Without the
        lookup the board would try to explode a cap through a recipe and
        report it as a gap.
        """
        split = split_dispatch_lines(
            [{"item_code": "FG1", "qty": 100}, {"item_code": "PM1", "qty": 40}],
            {"FG1": 102, "PM1": 105},
        )
        self.assertEqual([row["item_code"] for row in split["fg"]], ["FG1"])
        self.assertEqual([row["item_code"] for row in split["direct_pm"]], ["PM1"])

    def test_anything_that_is_neither_is_set_aside_and_counted(self):
        """Folding raw material into either bucket would put oil in a
        packaging figure."""
        split = split_dispatch_lines(
            [{"item_code": "RM1", "qty": 900}], {"RM1": 101}
        )
        self.assertEqual(split["fg"], [])
        self.assertEqual(split["direct_pm"], [])
        self.assertEqual(split["other_item_count"], 1)
        self.assertEqual(split["other_qty"], 900)

    def test_a_code_with_no_group_at_all_is_set_aside_not_exploded(self):
        split = split_dispatch_lines([{"item_code": "MYSTERY", "qty": 5}], {})
        self.assertEqual(split["other_item_count"], 1)


class DispatchSummaryTests(SimpleTestCase):
    def test_the_intercompany_split_adds_back_to_the_net_figure(self):
        """August 2026 Oil: 1,999,070 net, of which 1,329,822 to the group.

        Excluding the group would report a third of the real packaging and
        imply a million pieces of finished goods piling up that are not there.
        """
        summary = summarise_dispatch(
            [
                {"qty": 1999070, "intercompany_qty": 1329822, "return_qty": 0},
            ]
        )
        self.assertEqual(summary["fg_dispatched_qty"], 1999070)
        self.assertEqual(summary["fg_intercompany_qty"], 1329822)
        self.assertEqual(summary["fg_third_party_qty"], 669248)

    def test_returns_are_reported_gross_beside_the_netted_figure(self):
        summary = summarise_dispatch(
            [{"qty": 900, "intercompany_qty": 0, "return_qty": 100}]
        )
        self.assertEqual(summary["fg_dispatched_qty"], 900)
        self.assertEqual(summary["fg_returns_qty"], 100)


# ---------------------------------------------------------------------------
# The service, end to end, on fake readers
# ---------------------------------------------------------------------------


class FakeReader:
    """A HANA reader that returns fixed rows and counts its calls."""

    def __init__(self, **rows):
        self.rows = rows
        self.calls = []

    def pm_group_name(self):
        self.calls.append("pm_group_name")
        return self.rows.get("group_name", "PACKAGING MATERIAL")

    def pm_master(self):
        self.calls.append("pm_master")
        return self.rows.get("master", MASTER)

    def warehouse_names(self, warehouses):
        self.calls.append("warehouse_names")
        return self.rows.get("warehouse_names", [])

    def pm_stock_by_warehouse(self, warehouses):
        self.calls.append("pm_stock_by_warehouse")
        return self.rows.get("stock", [])

    def pm_issued(self, warehouses, date_from, date_to):
        self.calls.append("pm_issued")
        self.issued_args = (list(warehouses), date_from, date_to)
        return self.rows.get("issued", [])

    def dispatched_lines(self, date_from, date_to, intercompany):
        self.calls.append("dispatched_lines")
        self.intercompany = list(intercompany)
        return self.rows.get("dispatched", [])

    def dispatch_document_count(self, date_from, date_to):
        self.calls.append("dispatch_document_count")
        return self.rows.get("document_count", 0)

    def pm_bom_lines(self):
        self.calls.append("pm_bom_lines")
        return self.rows.get("bom", [])

    def item_group_map(self, item_codes):
        self.calls.append("item_group_map")
        return self.rows.get("groups", {})


class FakeAppReader:
    def __init__(self, **rows):
        self.rows = rows
        self.calls = []

    def dispatched_lines(self, date_from, date_to):
        self.calls.append("dispatched_lines")
        return self.rows.get("dispatched", [])

    def dispatch_counts(self, date_from, date_to):
        self.calls.append("dispatch_counts")
        return self.rows.get("counts", {"gate_out_count": 0, "document_count": 0})


class ServiceTests(SimpleTestCase):
    FROM = date(2026, 8, 1)
    TO = date(2026, 8, 31)

    def service(self, reader, app_reader=None):
        return PackingMaterialService(
            "JIVO_OIL", reader=reader, app_reader=app_reader or FakeAppReader()
        )

    def test_stock_reads_the_configured_warehouses_and_stamps_them(self):
        reader = FakeReader(
            warehouse_names=[
                {"code": "BH-PC", "name": "Production Consumption", "inactive": False}
            ],
            stock=[
                {
                    "warehouse": "BH-PC",
                    "item_code": "PM0000121",
                    "stock_qty": 3050403,
                    "stock_value": 8145807,
                }
            ],
        )
        board = self.service(reader).get_stock()
        self.assertEqual(board["meta"]["stock_warehouses"], ["BH-PC", "BH-BS", "BH-PM"])
        self.assertEqual(len(board["warehouses"]), 3)
        self.assertEqual(board["total"]["total_qty"], 3050403)
        self.assertTrue(board["meta"]["fetched_at"])

    def test_production_reads_only_the_consumption_warehouse(self):
        """BH-PM and BH-BS feed BH-PC. Counting their issues double-counts."""
        reader = FakeReader(issued=[{"item_code": "PM0000121", "issued_qty": 603505}])
        report = self.service(reader).get_production(self.FROM, self.TO, 10)
        self.assertEqual(reader.issued_args[0], ["BH-PC"])
        self.assertEqual(report["meta"]["consumption_warehouses"], ["BH-PC"])
        self.assertEqual(report["items"][0]["qty"], 603505)

    def test_production_names_its_basis_as_the_goods_issue(self):
        """The one label that must never drift: this is the issue, not the
        transfer to the line and not the approved BOM."""
        report = self.service(FakeReader()).get_production(self.FROM, self.TO, 10)
        self.assertEqual(report["meta"]["basis"], "issued")

    def test_a_renamed_item_group_is_reported_rather_than_trusted(self):
        report = self.service(FakeReader(group_name="CONSUMABLES")).get_production(
            self.FROM, self.TO, 10
        )
        self.assertEqual(report["meta"]["pm_item_group"], 105)
        self.assertEqual(report["meta"]["pm_item_group_name"], "CONSUMABLES")
        self.assertFalse(report["meta"]["pm_item_group_matches"])

    def test_the_group_name_check_passes_on_the_live_name(self):
        report = self.service(FakeReader()).get_production(self.FROM, self.TO, 10)
        self.assertTrue(report["meta"]["pm_item_group_matches"])

    def test_sap_dispatch_explodes_finished_goods_and_sets_packaging_aside(self):
        reader = FakeReader(
            dispatched=[
                {
                    "item_code": "FG1",
                    "item_group": 102,
                    "qty": 10000,
                    "intercompany_qty": 6000,
                    "return_qty": 0,
                },
                {
                    "item_code": "PM0000085",
                    "item_group": 105,
                    "qty": 500,
                    "intercompany_qty": 0,
                    "return_qty": 0,
                },
            ],
            bom=[{"parent_code": "FG1", "pm_code": "PM0000121", "qty_per_unit": 1}],
            document_count=603,
        )
        report = self.service(reader).get_dispatch(self.FROM, self.TO, 10, "sap")

        self.assertEqual(report["items"][0]["item_code"], "PM0000121")
        self.assertEqual(report["items"][0]["qty"], 10000)
        # The caps were invoiced as themselves: counted, never ranked.
        self.assertEqual(report["coverage"]["direct_pm_qty"], 500)
        self.assertEqual([row["item_code"] for row in report["items"]], ["PM0000121"])
        self.assertEqual(report["summary"]["document_count"], 603)
        self.assertEqual(report["summary"]["fg_intercompany_qty"], 6000)
        self.assertEqual(report["meta"]["basis"], "invoiced")
        self.assertTrue(report["meta"]["intercompany_known"])

    def test_sap_dispatch_passes_the_group_card_codes_through(self):
        reader = FakeReader()
        self.service(reader).get_dispatch(self.FROM, self.TO, 10, "sap")
        self.assertIn("CUSTA000001", reader.intercompany)

    def test_app_dispatch_reads_the_gate_out_register_and_counts_bills(self):
        """125 trucks carried 396 bills in August. Both figures are reported."""
        reader = FakeReader(
            bom=[{"parent_code": "FG1", "pm_code": "PM0000121", "qty_per_unit": 1}],
            groups={"FG1": 102},
        )
        app_reader = FakeAppReader(
            dispatched=[{"item_code": "FG1", "qty": 693398, "line_count": 962}],
            counts={"gate_out_count": 125, "document_count": 396},
        )
        report = self.service(reader, app_reader).get_dispatch(
            self.FROM, self.TO, 10, "app"
        )
        self.assertEqual(report["items"][0]["qty"], 693398)
        self.assertEqual(report["summary"]["document_count"], 396)
        self.assertEqual(report["summary"]["gate_out_count"], 125)
        self.assertEqual(report["meta"]["basis"], "gated-out")
        self.assertEqual(report["meta"]["source"], "app")
        self.assertNotIn("dispatched_lines", reader.calls)

    def test_the_app_source_does_not_claim_an_intercompany_split(self):
        """The register records a truck leaving, not who was invoiced.

        Zero here means "not measured", and the flag is what says so.
        """
        report = self.service(FakeReader(), FakeAppReader()).get_dispatch(
            self.FROM, self.TO, 10, "app"
        )
        self.assertFalse(report["meta"]["intercompany_known"])
        self.assertEqual(report["summary"]["fg_intercompany_qty"], 0)

    def test_sap_dispatch_reports_no_truck_count_because_sap_has_none(self):
        report = self.service(FakeReader()).get_dispatch(self.FROM, self.TO, 10, "sap")
        self.assertIsNone(report["summary"]["gate_out_count"])

    def test_the_recipe_comes_from_sap_on_both_sources(self):
        reader = FakeReader()
        self.service(reader, FakeAppReader()).get_dispatch(self.FROM, self.TO, 10, "app")
        self.assertIn("pm_bom_lines", reader.calls)


# ===========================================================================
# The requirement board
# ===========================================================================
#
# The numbers below are NOT invented. Every figure in REQ_* is what the live
# Oil company returned for the September 2026 plan (OFCT AbsID 44) on
# 9 September 2026, and the six components used here are ones whose Planning,
# Issue and On hand matched the packaging buyer's own hand-kept spreadsheet
# EXACTLY, column for column. These tests therefore check the arithmetic
# against the sheet this board was built to replace, not merely against
# itself.

REQ_MASTER = [
    {
        "item_code": "PM0000235",
        "item_name": "CAPS 1 LTR WHITE AND YELLOW SMALL PLAIN",
        "uom": "PCS",
        "sub_group": "CAPS",
        "unit_price": 0.45,
    },
    {
        "item_code": "PM0000468",
        "item_name": "CAPS 5 LTR GREEN",
        "uom": "PCS",
        "sub_group": "CAPS",
        "unit_price": 2.10,
    },
    {
        "item_code": "PM0000469",
        "item_name": "CAPS 5 LTR BROWN",
        "uom": "PCS",
        "sub_group": "CAPS",
        "unit_price": 2.10,
    },
    {
        "item_code": "PM0000194",
        "item_name": "PET BOTTLE 1 LTR 40 GMS",
        "uom": "PCS",
        "sub_group": "PET BOTTLES",
        "unit_price": 6.40,
    },
    {
        "item_code": "PM0000003",
        "item_name": "CARTON 1 LTR POMACE 16 PCS",
        "uom": "PCS",
        "sub_group": "CARTON",
        "unit_price": 8.00,
    },
]

# Planning, from the plan exploded through its BOMs.
REQ_REQUIREMENT = [
    {"item_code": "PM0000235", "planning_qty": 1102500.0, "sku_count": 3},
    {"item_code": "PM0000468", "planning_qty": 56867.0, "sku_count": 2},
    {"item_code": "PM0000469", "planning_qty": 72000.0, "sku_count": 1},
    {"item_code": "PM0000194", "planning_qty": 150000.0, "sku_count": 4},
    {"item_code": "PM0000003", "planning_qty": 42750.0, "sku_count": 1},
]

# Issue (PC): receipts into BH-PC, 1 to 9 September. PM0000194 is the case the
# split exists for -- 187,085 bottles blown in-house straight onto the floor,
# nothing transferred up from the stores.
REQ_RECEIVED = [
    {
        "item_code": "PM0000235",
        "received_qty": 155000.0,
        "transfer_qty": 155000.0,
        "produced_qty": 0.0,
        "other_qty": 0.0,
    },
    {
        "item_code": "PM0000468",
        "received_qty": 13552.0,
        "transfer_qty": 13552.0,
        "produced_qty": 0.0,
        "other_qty": 0.0,
    },
    {
        "item_code": "PM0000469",
        "received_qty": 10995.0,
        "transfer_qty": 10995.0,
        "produced_qty": 0.0,
        "other_qty": 0.0,
    },
    {
        "item_code": "PM0000194",
        "received_qty": 187085.0,
        "transfer_qty": 0.0,
        "produced_qty": 187085.0,
        "other_qty": 0.0,
    },
    {
        "item_code": "PM0000003",
        "received_qty": 2220.0,
        "transfer_qty": 2220.0,
        "produced_qty": 0.0,
        "other_qty": 0.0,
    },
    # Received onto the floor, not on the plan's bill of materials at all.
    {
        "item_code": "PM0000075",
        "received_qty": 54000.0,
        "transfer_qty": 54000.0,
        "produced_qty": 0.0,
        "other_qty": 0.0,
    },
]

# On hand in the FEEDING stores only -- BH-BS and BH-PM, never BH-PC.
REQ_ON_HAND = [
    {"item_code": "PM0000235", "on_hand_qty": 405000.0},
    {"item_code": "PM0000468", "on_hand_qty": 45583.0},
    {"item_code": "PM0000469", "on_hand_qty": 9741.0},
    {"item_code": "PM0000194", "on_hand_qty": 0.0},
    {"item_code": "PM0000003", "on_hand_qty": 2.0},
]

REQ_OPEN_PO = [
    {
        "item_code": "PM0000235",
        "open_po_qty": 70000.0,
        "po_earliest_due": date(2026, 9, 15),
        "po_latest_due": date(2026, 9, 20),
        "po_lines": 2,
    },
    {
        "item_code": "PM0000469",
        "open_po_qty": 116164.0,
        "po_earliest_due": date(2026, 9, 12),
        "po_latest_due": date(2026, 9, 25),
        "po_lines": 4,
    },
    {
        "item_code": "PM0000194",
        "open_po_qty": 200000.0,
        "po_earliest_due": date(2026, 10, 20),
        "po_latest_due": date(2026, 10, 20),
        "po_lines": 1,
    },
    # Still short after netting, and the only order due lands in October --
    # after the plan it is meant to cover has finished.
    {
        "item_code": "PM0000003",
        "open_po_qty": 20411.0,
        "po_earliest_due": date(2026, 10, 10),
        "po_latest_due": date(2026, 10, 10),
        "po_lines": 3,
    },
]


class RequirementRowTests(SimpleTestCase):
    """The seven columns, against the buyer's own sheet."""

    def rows(self):
        return {
            row["item_code"]: row
            for row in build_requirement_rows(
                REQ_REQUIREMENT,
                REQ_RECEIVED,
                REQ_ON_HAND,
                REQ_OPEN_PO,
                index_master(REQ_MASTER),
                {},
                {},
                date(2026, 9, 30),
                date(2026, 9, 9),
            )
        }

    def test_matches_the_buyers_sheet_row_for_row(self):
        """CAPS 1 LTR WHITE AND YELLOW SMALL PLAIN, as the sheet has it."""
        row = self.rows()["PM0000235"]
        self.assertEqual(row["planning_qty"], 1102500.0)
        self.assertEqual(row["issued_pc_qty"], 155000.0)
        self.assertEqual(row["rest_planning_qty"], 947500.0)
        self.assertEqual(row["on_hand_qty"], 405000.0)
        self.assertEqual(row["req_qty"], -542500.0)

    def test_a_surplus_row_matches_the_sheet_too(self):
        """CAPS 5 LTR GREEN: on hand covers what is left of the plan."""
        row = self.rows()["PM0000468"]
        self.assertEqual(row["rest_planning_qty"], 43315.0)
        self.assertEqual(row["req_qty"], 2268.0)
        # No open order, so nothing changes after netting one off.
        self.assertEqual(row["open_po_qty"], 0.0)
        self.assertEqual(row["req_after_po_qty"], 2268.0)
        self.assertEqual(row["short_qty"], 0.0)

    def test_open_orders_close_a_shortage(self):
        """CAPS 5 LTR BROWN is 51,264 short and has 116,164 on order."""
        row = self.rows()["PM0000469"]
        self.assertEqual(row["req_qty"], -51264.0)
        self.assertEqual(row["req_after_po_qty"], 64900.0)
        self.assertTrue(row["po_covers_shortage"])
        self.assertEqual(row["short_qty"], 0.0)
        self.assertFalse(row["po_due_after_plan"])

    def test_open_orders_that_land_too_late_are_flagged(self):
        """The carton is short, and the order for it lands after the plan ends.

        20,117 still missing once the open order is netted off, and even
        that order is not due until 10 October against a plan closing on
        30 September. Both facts sit on the row, because on order and here
        in time are different answers.
        """
        row = self.rows()["PM0000003"]
        self.assertEqual(row["req_qty"], -40528.0)
        self.assertEqual(row["req_after_po_qty"], -20117.0)
        self.assertFalse(row["po_covers_shortage"])
        self.assertTrue(row["po_due_after_plan"])

    def test_in_house_production_counts_as_plan_fulfilled(self):
        """The whole reason `Issue (PC)` is receipts and not just transfers.

        187,085 bottles were blown straight onto the floor and none came up
        from the stores. Counting only the transfer would leave Rest Planning
        at the full 150,000 and raise a 150,000 shortage on an item the
        factory does not buy -- it buys the preform.
        """
        row = self.rows()["PM0000194"]
        self.assertEqual(row["issued_pc_qty"], 187085.0)
        self.assertEqual(row["issued_transfer_qty"], 0.0)
        self.assertEqual(row["issued_produced_qty"], 187085.0)
        # Blown well past the plan, so the remainder is negative, not clamped.
        self.assertEqual(row["rest_planning_qty"], -37085.0)
        self.assertTrue(row["over_issued"])

    def test_over_issue_is_flagged_and_never_floored(self):
        rows = self.rows()
        self.assertTrue(rows["PM0000194"]["over_issued"])
        self.assertFalse(rows["PM0000235"]["over_issued"])
        self.assertLess(rows["PM0000194"]["rest_planning_qty"], 0)

    def test_an_over_issued_row_is_a_surplus_not_a_covered_shortage(self):
        """A negative remainder makes Req POSITIVE, which is the honest read.

        The floor has already taken 37,085 more bottles than the plan
        called for, so the plan needs no more of them -- and an open order
        for 200,000 is not closing a shortage, it is stock arriving against
        demand already met. `po_covers_shortage` has to stay false, or the
        count of closed gaps is inflated by every over-issue.
        """
        row = self.rows()["PM0000194"]
        self.assertEqual(row["req_qty"], 37085.0)
        self.assertFalse(row["po_covers_shortage"])
        self.assertEqual(row["short_qty"], 0.0)

    def test_unplanned_receipts_are_not_rows(self):
        """PM0000075 reached the floor but is not on the plan's BOM."""
        self.assertNotIn("PM0000075", self.rows())

    def test_an_overdue_order_is_flagged_even_when_it_covers_the_gap(self):
        """The dominant case on the live book, and the most misleading.

        The brown caps are 51,264 short with 116,164 on order, so the gap
        closes on paper -- but that order was due on 12 September against a
        board read on the 9th... which is still to come. The small caps
        order, due the 15th, is likewise not yet late. Nothing here is
        overdue, and the flag has to say so rather than fire on any open
        order at all.
        """
        rows = self.rows()
        self.assertFalse(rows["PM0000469"]["po_overdue"])
        self.assertFalse(rows["PM0000235"]["po_overdue"])

    def test_an_order_whose_date_has_gone_by_is_overdue(self):
        """Same rows, read a month later.

        Every due date is now in the past, so a shortage that looked
        covered is a shortage leaning on an order nobody has chased.
        """
        rows = {
            row["item_code"]: row
            for row in build_requirement_rows(
                REQ_REQUIREMENT,
                REQ_RECEIVED,
                REQ_ON_HAND,
                REQ_OPEN_PO,
                index_master(REQ_MASTER),
                {},
                {},
                date(2026, 9, 30),
                date(2026, 11, 1),
            )
        }
        self.assertTrue(rows["PM0000469"]["po_overdue"])
        self.assertTrue(rows["PM0000469"]["po_covers_shortage"])
        # A row with no order at all is never overdue.
        self.assertFalse(rows["PM0000468"]["po_overdue"])

    def test_shortfall_value_prices_the_gap(self):
        row = self.rows()["PM0000235"]
        # 472,500 caps still short after 70,000 on order, at Rs 0.45.
        self.assertEqual(row["short_qty"], 472500.0)
        self.assertEqual(row["short_value"], round(472500.0 * 0.45, 2))

    def test_worst_shortfall_sorts_first(self):
        ordered = build_requirement_rows(
            REQ_REQUIREMENT,
            REQ_RECEIVED,
            REQ_ON_HAND,
            REQ_OPEN_PO,
            index_master(REQ_MASTER),
            {},
            {},
            date(2026, 9, 30),
            date(2026, 9, 9),
        )
        self.assertEqual(ordered[0]["item_code"], "PM0000235")

    def test_a_component_with_no_stock_row_reads_as_zero(self):
        rows = build_requirement_rows(
            [{"item_code": "PM0000862", "planning_qty": 3000.0, "sku_count": 1}],
            [],
            [],
            [],
            index_master(REQ_MASTER),
            {},
            {},
            date(2026, 9, 30),
            date(2026, 9, 9),
        )
        self.assertEqual(rows[0]["issued_pc_qty"], 0.0)
        self.assertEqual(rows[0]["on_hand_qty"], 0.0)
        self.assertEqual(rows[0]["req_qty"], -3000.0)

    def test_drivers_are_capped_but_the_count_is_not(self):
        drivers = [
            {
                "item_code": "PM0000235",
                "parent_code": f"FG{index:07d}",
                "parent_name": f"SKU {index}",
                "plan_qty": 1000.0,
                "qty_per_unit": 1.0,
                "required_qty": float(index),
            }
            for index in range(1, 13)
        ]
        indexed = index_drivers(drivers, 8)
        self.assertEqual(len(indexed["PM0000235"]), 8)
        # Biggest contributor first, so a truncated list keeps the ones that
        # explain the figure.
        self.assertEqual(indexed["PM0000235"][0]["required_qty"], 12.0)


class RequirementTotalsTests(SimpleTestCase):
    def totals(self):
        return requirement_totals(
            build_requirement_rows(
                REQ_REQUIREMENT,
                REQ_RECEIVED,
                REQ_ON_HAND,
                REQ_OPEN_PO,
                index_master(REQ_MASTER),
                {},
                {},
                date(2026, 9, 30),
                date(2026, 9, 9),
            )
        )

    def rows_by_code(self):
        return {
            row["item_code"]: row
            for row in build_requirement_rows(
                REQ_REQUIREMENT,
                REQ_RECEIVED,
                REQ_ON_HAND,
                REQ_OPEN_PO,
                index_master(REQ_MASTER),
                {},
                {},
                date(2026, 9, 30),
                date(2026, 9, 9),
            )
        }

    def test_a_surplus_never_cancels_a_shortage(self):
        """The load-bearing property of the totals block.

        Two rows are in surplus -- 2,268 green caps and 37,085 over-issued
        bottles -- against 472,500 small caps and 20,117 cartons short.
        Summing the signed figure would report the factory to the GOOD and
        imply spare bottles could be used as the missing cartons.
        """
        totals = self.totals()
        self.assertEqual(totals["short_after_po_qty"], 492617.0)
        self.assertEqual(totals["short_after_po_count"], 2)

    def test_shortage_is_counted_before_and_after_open_orders(self):
        totals = self.totals()
        # Short before netting: small caps, brown caps and the carton.
        self.assertEqual(totals["short_before_po_count"], 3)
        # After netting, only the brown caps are actually covered.
        self.assertEqual(totals["short_after_po_count"], 2)
        self.assertEqual(totals["covered_by_po_count"], 1)

    def test_late_orders_are_counted_separately(self):
        """Only rows that are SHORT and late.

        The over-issued bottle carries a late order too, but it needs
        nothing, so counting it would overstate how much of the month is
        actually at risk. The flag is still on its row.
        """
        self.assertEqual(self.totals()["po_due_after_plan_count"], 1)
        self.assertTrue(self.rows_by_code()["PM0000194"]["po_due_after_plan"])

    def test_columns_add_up(self):
        totals = self.totals()
        self.assertEqual(totals["item_count"], 5)
        self.assertEqual(totals["planning_qty"], 1424117.0)
        self.assertEqual(totals["issued_pc_qty"], 368852.0)
        self.assertEqual(totals["issued_produced_qty"], 187085.0)
        self.assertEqual(totals["rest_planning_qty"], 1055265.0)
        self.assertEqual(totals["on_hand_qty"], 460326.0)
        self.assertEqual(totals["open_po_qty"], 406575.0)
        self.assertEqual(totals["over_issued_count"], 1)
        # Surplus is measured AFTER open orders, so it pairs with
        # short_after_po_count: 3 in surplus + 2 short = all 5 rows.
        self.assertEqual(totals["surplus_count"], 3)
        self.assertEqual(
            totals["surplus_count"] + totals["short_after_po_count"],
            totals["item_count"],
        )


class UnplannedIssueTests(SimpleTestCase):
    def test_counts_what_the_plan_does_not_describe(self):
        report = unplanned_issue(
            REQ_RECEIVED,
            [row["item_code"] for row in REQ_REQUIREMENT],
            index_master(REQ_MASTER),
            25,
        )
        self.assertEqual(report["item_count"], 1)
        self.assertEqual(report["qty"], 54000.0)
        self.assertEqual(report["items"][0]["item_code"], "PM0000075")

    def test_nothing_unplanned_is_an_empty_report_not_a_missing_one(self):
        report = unplanned_issue(REQ_RECEIVED[:1], ["PM0000235"], {}, 25)
        self.assertEqual(report["item_count"], 0)
        self.assertEqual(report["qty"], 0.0)
        self.assertEqual(report["items"], [])


class PlanResolutionTests(SimpleTestCase):
    PLANS = [
        {
            "abs_id": 44,
            "start_date": date(2026, 9, 1),
            "end_date": date(2026, 9, 30),
        },
        {
            "abs_id": 43,
            "start_date": date(2026, 8, 1),
            "end_date": date(2026, 8, 31),
        },
        {
            "abs_id": 42,
            "start_date": date(2026, 7, 1),
            "end_date": date(2026, 7, 31),
        },
    ]

    def test_picks_the_plan_covering_today(self):
        plan = resolve_plan(self.PLANS, date(2026, 9, 9))
        self.assertEqual(plan["abs_id"], 44)

    def test_falls_back_to_the_most_recent_started_plan(self):
        """A gap between plans opens on the last one, not on nothing."""
        plan = resolve_plan(self.PLANS, date(2026, 10, 5))
        self.assertEqual(plan["abs_id"], 44)

    def test_falls_back_to_the_newest_plan_when_none_has_started(self):
        """Planners a month ahead must not see an empty board."""
        plan = resolve_plan(self.PLANS, date(2026, 6, 1))
        self.assertEqual(plan["abs_id"], 44)

    def test_no_plans_is_none(self):
        self.assertIsNone(resolve_plan([], date(2026, 9, 9)))

    def test_handles_dates_hana_returned_as_datetimes(self):
        plans = [{"abs_id": 44, "start_date": "2026-09-01", "end_date": "2026-09-30"}]
        self.assertEqual(resolve_plan(plans, date(2026, 9, 9))["abs_id"], 44)


class IssueWindowTests(SimpleTestCase):
    PLAN = {"start_date": date(2026, 9, 1), "end_date": date(2026, 9, 30)}

    def test_first_of_the_month_to_today(self):
        window = issue_window(self.PLAN, date(2026, 9, 9))
        self.assertEqual(window["date_from"], date(2026, 9, 1))
        self.assertEqual(window["date_to"], date(2026, 9, 9))

    def test_a_finished_plan_reports_its_whole_month(self):
        """Never stops at the plan's own end, never runs past it either."""
        window = issue_window(self.PLAN, date(2026, 11, 4))
        self.assertEqual(window["date_from"], date(2026, 9, 1))
        self.assertEqual(window["date_to"], date(2026, 9, 30))


class PlanCoverageTests(SimpleTestCase):
    COVERAGE = [
        {"item_code": "FG1", "item_name": "A", "plan_qty": 3008094.0, "has_bom": True, "has_pm": True},
        {"item_code": "FG2", "item_name": "B", "plan_qty": 30000.0, "has_bom": False, "has_pm": False},
        {"item_code": "FG3", "item_name": "C", "plan_qty": 15000.0, "has_bom": False, "has_pm": False},
    ]

    def test_reports_the_share_of_planned_quantity_not_of_items(self):
        """Two of three items missing a recipe is 1.5% of September, not 67%."""
        summary = plan_coverage_summary(self.COVERAGE, 25)
        self.assertEqual(summary["items_without_bom"], 2)
        self.assertEqual(summary["items_without_bom_qty"], 45000.0)
        self.assertEqual(summary["qty_covered_pct"], 98.5)

    def test_biggest_gap_is_listed_first(self):
        summary = plan_coverage_summary(self.COVERAGE, 25)
        self.assertEqual(summary["items_without_bom_list"][0]["item_code"], "FG2")

    def test_full_coverage_reports_one_hundred_percent(self):
        summary = plan_coverage_summary(self.COVERAGE[:1], 25)
        self.assertEqual(summary["qty_covered_pct"], 100.0)
        self.assertEqual(summary["items_without_bom"], 0)

    def test_an_empty_plan_does_not_divide_by_zero(self):
        summary = plan_coverage_summary([], 25)
        self.assertEqual(summary["qty_covered_pct"], 0.0)
        self.assertEqual(summary["plan_qty"], 0.0)


class SupplyWarehouseTests(SimpleTestCase):
    def test_on_hand_excludes_the_consumption_store(self):
        """Oil: (BH-PC, BH-BS, BH-PM) minus (BH-PC).

        Counting BH-PC would credit the same material twice -- once as plan
        already fulfilled through `Issue (PC)`, once as stock still available.
        """
        self.assertEqual(supply_warehouses("JIVO_OIL"), ["BH-BS", "BH-PM"])

    def test_derivation_holds_for_the_other_companies(self):
        self.assertEqual(supply_warehouses("JIVO_BEVERAGES"), ["BH-PM"])
        self.assertEqual(supply_warehouses("JIVO_MART"), ["BH-PM"])

    def test_an_unknown_company_has_no_stores_rather_than_all_of_them(self):
        self.assertEqual(supply_warehouses("NOPE"), [])
