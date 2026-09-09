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

from .services import (
    PackingMaterialService,
    build_stock_board,
    explode_dispatch,
    index_bom,
    index_master,
    rank_by_qty,
    split_dispatch_lines,
    summarise_dispatch,
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
