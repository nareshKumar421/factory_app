"""
pm_demand/tests.py

The arithmetic that produces the numbers on screen, tested without SAP.

The reader is injected, so every figure the board shows is asserted against
hand-written movement and BOM rows. The two tests that do look at SQL check
the two things a wrong query would get silently wrong: that consumption reads
TransType 60 ``OutQty`` and not the store transfer, and that an empty
warehouse list never becomes ``IN ()``.
"""

from datetime import date

from django.test import SimpleTestCase, TestCase

from . import constants
from .hana_reader import PmDemandReader
from .permissions import PM_DEMAND_VIEW_PERMISSIONS, CanViewPmDemand
from .serializers import PmDemandFilterSerializer
from .services import (
    PmDemandService,
    build_item_rows,
    cover_days,
    cover_status,
    explode,
    index_bom,
    is_in_house,
    rank,
    roll_up_families,
    working_days,
)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class StubReader:
    """Stands in for ``PmDemandReader`` with rows a test can reason about."""

    def __init__(
        self,
        *,
        produced=None,
        dispatched=None,
        bom_lines=None,
        movements=None,
        master=None,
        stock=None,
        open_po=None,
        group_name="PACKAGING MATERIAL",
    ):
        self._produced = produced or []
        self._dispatched = dispatched or []
        self._bom_lines = bom_lines or []
        self._movements = movements or []
        self._master = master or []
        self._stock = stock or []
        self._open_po = open_po or []
        self._group_name = group_name

    def pm_group_name(self):
        return self._group_name

    def fg_produced(self, warehouses, date_from, date_to):
        return self._produced

    def fg_dispatched(self, date_from, date_to, intercompany_card_codes):
        return self._dispatched

    def pm_bom_lines(self):
        return self._bom_lines

    def pm_movements(self, date_from, date_to, consumption, wastage, upstream):
        return self._movements

    def pm_master(self):
        return self._master

    def pm_stock(self, warehouses):
        return self._stock

    def pm_open_po(self, as_of):
        return self._open_po


class CapturingReader(PmDemandReader):
    """A real reader whose SQL is captured instead of executed."""

    def __init__(self, schema="TEST_SCHEMA"):  # noqa: D107 - deliberately no super()
        self.schema_override = schema
        self.company_code = "JIVO_OIL"
        self._columns_cache = {"OITM": {"U_Sub_Group", "AvgPrice", "LastPurPrc"}}
        self.calls = []
        self.next_rows = []

    def _execute(self, query, params):
        self.calls.append((query, list(params)))
        return self.next_rows


class StubAppReader:
    """Stands in for ``PmDemandAppReader``."""

    def __init__(self, produced=None, dispatched=None, movements=None):
        self._produced = produced or []
        self._dispatched = dispatched or []
        self._movements = movements or []
        self.pm_codes_seen = None

    def fg_produced(self, date_from, date_to):
        return self._produced

    def fg_dispatched(self, date_from, date_to):
        return self._dispatched

    def pm_movements(self, date_from, date_to, pm_codes):
        self.pm_codes_seen = list(pm_codes)
        return self._movements


def _service(**reader_kwargs):
    """A service wired to a stub reader, with no CompanyContext resolution."""
    service = PmDemandService.__new__(PmDemandService)
    service.company_code = "JIVO_OIL"
    service.context = None
    service.reader = StubReader(**reader_kwargs)
    service.app_reader = StubAppReader()
    return service


# ---------------------------------------------------------------------------
# BOM indexing and explosion
# ---------------------------------------------------------------------------


class IndexBomTests(SimpleTestCase):
    def test_groups_lines_by_parent(self):
        index = index_bom(
            [
                {"parent_code": "FG1", "pm_code": "PM1", "qty_per_unit": 1.0},
                {"parent_code": "FG1", "pm_code": "PM2", "qty_per_unit": 2.0},
                {"parent_code": "FG2", "pm_code": "PM1", "qty_per_unit": 0.5},
            ]
        )
        self.assertEqual(sorted(index), ["FG1", "FG2"])
        self.assertEqual(sorted(index["FG1"]), [("PM1", 1.0), ("PM2", 2.0)])

    def test_drops_unusable_lines(self):
        """A zero or negative per-unit is not a recipe, and would poison a sum."""
        index = index_bom(
            [
                {"parent_code": "FG1", "pm_code": "PM1", "qty_per_unit": 0},
                {"parent_code": "FG1", "pm_code": "PM2", "qty_per_unit": -1},
                {"parent_code": "", "pm_code": "PM3", "qty_per_unit": 1},
                {"parent_code": "FG1", "pm_code": "", "qty_per_unit": 1},
            ]
        )
        self.assertEqual(index, {})


class ExplodeTests(SimpleTestCase):
    def test_multiplies_and_accumulates_across_parents(self):
        required, coverage = explode(
            {"FG1": 100.0, "FG2": 50.0},
            {
                "FG1": [("CAP", 1.0), ("LABEL", 2.0)],
                "FG2": [("CAP", 1.0)],
            },
        )
        self.assertEqual(required["CAP"], 150.0)
        self.assertEqual(required["LABEL"], 200.0)
        self.assertEqual(coverage["qty_covered_pct"], 100.0)

    def test_reports_finished_goods_with_no_bom(self):
        """Silent under-reporting is the failure this coverage block prevents."""
        required, coverage = explode(
            {"FG1": 100.0, "NOBOM": 300.0},
            {"FG1": [("CAP", 1.0)]},
        )
        self.assertEqual(required, {"CAP": 100.0})
        self.assertEqual(coverage["fg_items"], 2)
        self.assertEqual(coverage["fg_items_with_bom"], 1)
        self.assertEqual(coverage["fg_items_without_bom"], ["NOBOM"])
        self.assertEqual(coverage["qty_covered_pct"], 25.0)

    def test_negative_finished_goods_quantity_explodes_negative(self):
        """A SKU whose returns beat its invoices really did send less out."""
        required, _ = explode({"FG1": -10.0}, {"FG1": [("CAP", 2.0)]})
        self.assertEqual(required["CAP"], -20.0)

    def test_empty_input_does_not_divide_by_zero(self):
        required, coverage = explode({}, {})
        self.assertEqual(required, {})
        self.assertEqual(coverage["qty_covered_pct"], 0.0)


# ---------------------------------------------------------------------------
# Item rows
# ---------------------------------------------------------------------------


class BuildItemRowsTests(SimpleTestCase):
    def _rows(self):
        return {
            row["item_code"]: row
            for row in build_item_rows(
                consumed={
                    "CAP": {"issued_qty": 570.0, "wastage_qty": 7.0, "in_house_qty": 0},
                    "BOTTLE": {"issued_qty": 600.0, "wastage_qty": 0, "in_house_qty": 660.0},
                    "STRAY": {"issued_qty": 5.0, "wastage_qty": 0, "in_house_qty": 0},
                },
                bom_from_production={"CAP": 516.0, "BOTTLE": 600.0, "GHOST": 10.0},
                bom_from_dispatch={"CAP": 300.0, "BOTTLE": 700.0},
                master={
                    "CAP": {
                        "item_name": "CAPS 1 LTR",
                        "uom": "PCS",
                        "sub_group": "CAPS",
                        "unit_price": 1.29,
                    },
                    "BOTTLE": {
                        "item_name": "PET BOTTLE 1 LTR",
                        "uom": "PCS",
                        "sub_group": "PET BOTTLES",
                        "unit_price": 8.8,
                    },
                },
                fg_produced_qty=1000.0,
            )
        }

    def test_row_appears_for_every_column_not_just_the_intersection(self):
        """An issue with no recipe, and a recipe with no issue, are both findings."""
        rows = self._rows()
        self.assertIn("STRAY", rows)  # issued, no recipe
        self.assertIn("GHOST", rows)  # recipe, never issued
        self.assertEqual(rows["GHOST"]["consumed_qty"], 0.0)
        self.assertEqual(rows["STRAY"]["bom_qty"], 0.0)

    def test_variance_is_actual_minus_standard(self):
        cap = self._rows()["CAP"]
        self.assertEqual(cap["variance_qty"], 54.0)
        self.assertAlmostEqual(cap["variance_pct"], 10.47, places=2)
        self.assertAlmostEqual(cap["variance_value"], 54.0 * 1.29, places=2)

    def test_variance_pct_is_null_rather_than_infinite_without_a_recipe(self):
        self.assertIsNone(self._rows()["STRAY"]["variance_pct"])

    def test_retained_goes_negative_when_dispatch_outruns_production(self):
        """Not an error: the period shipped finished-goods stock it did not make."""
        bottle = self._rows()["BOTTLE"]
        self.assertEqual(bottle["retained_qty"], -100.0)
        self.assertAlmostEqual(bottle["retained_value"], -880.0, places=2)

    def test_in_house_flag_marks_material_the_factory_made_itself(self):
        rows = self._rows()
        self.assertTrue(rows["BOTTLE"]["in_house"])  # 660 made vs 600 used
        self.assertFalse(rows["CAP"]["in_house"])
        self.assertEqual(rows["BOTTLE"]["in_house_qty"], 660.0)

    def test_per_1000_fg_is_intensity_not_a_total(self):
        self.assertEqual(self._rows()["CAP"]["per_1000_fg"], 570.0)

    def test_unpriced_item_keeps_its_quantity_and_reports_zero_value(self):
        rows = self._rows()
        self.assertEqual(rows["STRAY"]["consumed_qty"], 5.0)
        self.assertEqual(rows["STRAY"]["consumed_value"], 0.0)


class IsInHouseTests(SimpleTestCase):
    """The threshold exists because "any receipt at all" flagged every row.

    August 2026 Oil booked a small TransType 59 receipt into BH-PC for most
    packaging -- 2,578 caps against 570,331 consumed -- which is rework, not
    manufacture. Those receipts must not read as in-house manufacture.
    """

    def test_a_rework_receipt_is_not_manufacture(self):
        self.assertFalse(is_in_house(2578, 570331))  # CAPS, real August figures
        self.assertFalse(is_in_house(1283, 390503))  # LABEL

    def test_a_blown_bottle_is_manufacture(self):
        self.assertTrue(is_in_house(662769, 603505))  # PET BOTTLE POMACE
        self.assertTrue(is_in_house(195891, 260576))  # PET BOTTLE 40 GMS

    def test_no_receipt_is_never_in_house(self):
        self.assertFalse(is_in_house(0, 100))

    def test_made_but_not_issued_still_counts(self):
        """Nothing to take a share of, but something was plainly made."""
        self.assertTrue(is_in_house(500, 0))

    def test_exactly_at_the_threshold_counts(self):
        self.assertTrue(is_in_house(50, 100))
        self.assertFalse(is_in_house(49, 100))


class WorkingDaysTests(SimpleTestCase):
    """The rate's denominator. Getting it wrong moves every cover figure."""

    def test_august_2026_is_26_working_days_not_31(self):
        self.assertEqual(
            working_days(date(2026, 8, 1), date(2026, 8, 31), (6,)), 26
        )

    def test_a_six_day_week_excludes_only_sunday(self):
        # Mon 3 Aug to Sun 9 Aug 2026
        self.assertEqual(working_days(date(2026, 8, 3), date(2026, 8, 9), (6,)), 6)

    def test_a_five_day_week_can_be_configured(self):
        self.assertEqual(working_days(date(2026, 8, 3), date(2026, 8, 9), (5, 6)), 5)

    def test_range_is_inclusive_of_both_ends(self):
        self.assertEqual(working_days(date(2026, 8, 3), date(2026, 8, 3), (6,)), 1)

    def test_a_range_of_only_sundays_never_returns_zero(self):
        """Zero would make the burn rate infinite and invent a shortage."""
        self.assertEqual(working_days(date(2026, 8, 2), date(2026, 8, 2), (6,)), 1)

    def test_a_reversed_range_is_tolerated(self):
        self.assertEqual(
            working_days(date(2026, 8, 31), date(2026, 8, 1), (6,)), 26
        )


class CoverDaysTests(SimpleTestCase):
    def test_cover_is_stock_over_the_working_day_burn_rate(self):
        # 520,000 on hand, 260,000 used across 26 working days = 10,000/day
        self.assertEqual(cover_days(520000, 260000, 26), 52.0)

    def test_using_calendar_days_would_have_flattered_the_figure(self):
        """The reason the denominator matters: 19% on the same stock."""
        working = cover_days(520000, 260000, 26)
        calendar = cover_days(520000, 260000, 31)
        self.assertLess(working, calendar)
        self.assertEqual(round(calendar / working, 2), 1.19)

    def test_nothing_consumed_has_no_cover_rather_than_infinite(self):
        """A large number would rank a dead item as the safest in the store."""
        self.assertIsNone(cover_days(500000, 0, 26))

    def test_no_stock_against_real_consumption_is_zero_cover(self):
        self.assertEqual(cover_days(0, 260000, 26), 0.0)

    def test_negative_stock_reads_as_empty_not_as_negative_days(self):
        self.assertEqual(cover_days(-500, 260000, 26), 0.0)


class CoverStatusTests(SimpleTestCase):
    def test_bands(self):
        self.assertEqual(cover_status(3), "critical")
        self.assertEqual(cover_status(10), "low")
        self.assertEqual(cover_status(40), "ok")
        self.assertEqual(cover_status(None), "unknown")

    def test_band_edges_fall_on_the_safer_side(self):
        self.assertEqual(cover_status(constants.COVER_CRITICAL_DAYS), "low")
        self.assertEqual(cover_status(constants.COVER_LOW_DAYS), "ok")


# ---------------------------------------------------------------------------
# Ranking and roll-up
# ---------------------------------------------------------------------------


class RankTests(SimpleTestCase):
    def test_ranks_by_value_because_units_are_not_comparable(self):
        """Metres of tape must not out-rank rupees of tin."""
        rows = [
            {"item_code": "TAPE", "consumed_qty": 100000.0, "consumed_value": 5000.0},
            {"item_code": "TIN", "consumed_qty": 500.0, "consumed_value": 90000.0},
        ]
        ranked, axis = rank(rows, "consumed_value", "consumed_qty")
        self.assertEqual(axis, "value")
        self.assertEqual([r["item_code"] for r in ranked], ["TIN", "TAPE"])

    def test_falls_back_to_quantity_when_nothing_is_priced(self):
        rows = [
            {"item_code": "A", "consumed_qty": 1.0, "consumed_value": 0.0},
            {"item_code": "B", "consumed_qty": 9.0, "consumed_value": 0.0},
        ]
        ranked, axis = rank(rows, "consumed_value", "consumed_qty")
        self.assertEqual(axis, "quantity")
        self.assertEqual([r["item_code"] for r in ranked], ["B", "A"])


class RollUpFamiliesTests(SimpleTestCase):
    def test_family_totals_reconcile_to_the_item_totals(self):
        rows = [
            {
                "sub_group": "CAPS",
                "consumed_value": 100.0,
                "bom_value": 90.0,
                "dispatched_value": 50.0,
                "wastage_value": 5.0,
            },
            {
                "sub_group": "CAPS",
                "consumed_value": 100.0,
                "bom_value": 110.0,
                "dispatched_value": 50.0,
                "wastage_value": 0.0,
            },
            {
                "sub_group": "LABEL",
                "consumed_value": 300.0,
                "bom_value": 300.0,
                "dispatched_value": 200.0,
                "wastage_value": 1.0,
            },
        ]
        families = {f["sub_group"]: f for f in roll_up_families(rows)}
        self.assertEqual(families["CAPS"]["item_count"], 2)
        self.assertEqual(families["CAPS"]["consumed_value"], 200.0)
        self.assertEqual(families["CAPS"]["variance_value"], 0.0)
        self.assertEqual(
            sum(f["consumed_value"] for f in families.values()),
            sum(r["consumed_value"] for r in rows),
        )

    def test_ungrouped_items_are_bucketed_not_dropped(self):
        families = roll_up_families([{"sub_group": "", "consumed_value": 10.0}])
        self.assertEqual(families[0]["sub_group"], "UNGROUPED")
        self.assertEqual(families[0]["consumed_share_pct"], 100.0)

    def test_families_are_ordered_by_spend(self):
        families = roll_up_families(
            [
                {"sub_group": "SMALL", "consumed_value": 1.0},
                {"sub_group": "BIG", "consumed_value": 100.0},
            ]
        )
        self.assertEqual([f["sub_group"] for f in families], ["BIG", "SMALL"])


# ---------------------------------------------------------------------------
# The board end to end, over a stub reader
# ---------------------------------------------------------------------------


class ServiceReportTests(TestCase):
    """The "made 400, dispatched 200" shape, with a 2-per-unit recipe."""

    def _board(self, include_intercompany=True, top_n=10):
        service = _service(
            produced=[{"item_code": "FG1", "item_name": "OIL 1L", "qty": 400.0}],
            dispatched=[
                {
                    "item_code": "FG1",
                    "item_name": "OIL 1L",
                    "qty": 200.0,
                    "intercompany_qty": 120.0,
                    "return_qty": 10.0,
                }
            ],
            bom_lines=[
                {"parent_code": "FG1", "pm_code": "CAP", "qty_per_unit": 1.0},
                {"parent_code": "FG1", "pm_code": "LABEL", "qty_per_unit": 2.0},
            ],
            movements=[
                {
                    "item_code": "CAP",
                    "issued_qty": 410.0,
                    "wastage_qty": 5.0,
                    "in_house_qty": 0.0,
                    "upstream_qty": 0.0,
                },
                {
                    "item_code": "LABEL",
                    "issued_qty": 800.0,
                    "wastage_qty": 0.0,
                    "in_house_qty": 0.0,
                    "upstream_qty": 0.0,
                },
                {
                    "item_code": "PREFORM",
                    "issued_qty": 0.0,
                    "wastage_qty": 0.0,
                    "in_house_qty": 0.0,
                    "upstream_qty": 900.0,
                },
            ],
            stock=[
                # 410 caps a period, 205 on the shelf -> half a period left
                {"item_code": "CAP", "stock_qty": 205.0, "stock_value": 410.0},
                {"item_code": "LABEL", "stock_qty": 8000.0, "stock_value": 4000.0},
            ],
            open_po=[
                # The cap is thin on the shelf but heavily on order: this is
                # the shape of the real PM0000085, and it must NOT alarm.
                {
                    "item_code": "CAP",
                    "open_po_qty": 4000.0,
                    "earliest_due": date(2026, 6, 11),
                    "po_lines": 5,
                },
            ],
            master=[
                {
                    "item_code": "CAP",
                    "item_name": "CAPS 1 LTR",
                    "uom": "PCS",
                    "sub_group": "CAPS",
                    "unit_price": 2.0,
                },
                {
                    "item_code": "LABEL",
                    "item_name": "LABEL 1 LTR",
                    "uom": "PCS",
                    "sub_group": "LABEL",
                    "unit_price": 0.5,
                },
                {
                    "item_code": "PREFORM",
                    "item_name": "PREFORM 49.5 GMS",
                    "uom": "PCS",
                    "sub_group": "PREFORM",
                    "unit_price": 4.0,
                },
            ],
        )
        return service.get_report(
            date_from=date(2026, 8, 1),
            date_to=date(2026, 8, 31),
            top_n=top_n,
            include_intercompany=include_intercompany,
        )

    def _board_with_top_n(self, top_n):
        return self._board(top_n=top_n)

    def test_production_column_is_the_actual_issue(self):
        rows = {r["item_code"]: r for r in self._board()["production_top"]}
        self.assertEqual(rows["CAP"]["consumed_qty"], 410.0)
        self.assertEqual(rows["CAP"]["bom_qty"], 400.0)  # 400 FG x 1
        self.assertEqual(rows["CAP"]["variance_qty"], 10.0)
        self.assertEqual(rows["LABEL"]["bom_qty"], 800.0)  # 400 FG x 2

    def test_dispatch_column_explodes_only_what_was_invoiced(self):
        rows = {r["item_code"]: r for r in self._board()["dispatch_top"]}
        self.assertEqual(rows["CAP"]["dispatched_qty"], 200.0)
        self.assertEqual(rows["LABEL"]["dispatched_qty"], 400.0)

    def test_retained_is_what_is_still_sitting_in_finished_goods(self):
        rows = {r["item_code"]: r for r in self._board()["production_top"]}
        self.assertEqual(rows["CAP"]["retained_qty"], 210.0)  # 410 used, 200 shipped

    def test_intercompany_toggle_moves_dispatch_and_leaves_consumption_alone(self):
        with_ic = self._board(include_intercompany=True)
        without_ic = self._board(include_intercompany=False)

        self.assertEqual(with_ic["summary"]["fg_dispatched_qty"], 200.0)
        self.assertEqual(without_ic["summary"]["fg_dispatched_qty"], 80.0)
        self.assertEqual(
            with_ic["summary"]["pm_consumed_value"],
            without_ic["summary"]["pm_consumed_value"],
        )

    def test_both_dispatch_readings_are_always_reported(self):
        """Neither figure can be quoted as the other if the screen carries both."""
        summary = self._board(include_intercompany=False)["summary"]
        self.assertEqual(summary["fg_dispatched_all_qty"], 200.0)
        self.assertEqual(summary["fg_dispatched_intercompany_qty"], 120.0)
        self.assertEqual(summary["fg_dispatched_third_party_qty"], 80.0)
        self.assertEqual(summary["fg_returns_qty"], 10.0)

    def test_dispatch_ratio_answers_how_much_of_what_was_made_went_out(self):
        self.assertEqual(self._board()["summary"]["dispatch_ratio_pct"], 50.0)

    def test_blowing_line_is_reported_apart_from_every_total(self):
        board = self._board()
        upstream = {r["item_code"]: r for r in board["upstream"]}
        self.assertEqual(upstream["PREFORM"]["consumed_qty"], 900.0)
        # 410 caps x 2 + 800 labels x 0.5 = 1220; the 900 preforms are not in it
        self.assertEqual(board["summary"]["pm_consumed_value"], 1220.0)
        self.assertNotIn(
            "PREFORM", {r["item_code"] for r in board["production_top"]}
        )

    def test_cover_is_stock_over_the_working_day_burn_rate(self):
        rows = {r["item_code"]: r for r in self._board()["production_top"]}
        # August 2026 = 26 working days; 410 caps / 26 = 15.769 a day
        self.assertEqual(rows["CAP"]["avg_daily_qty"], 15.769)
        self.assertEqual(rows["CAP"]["stock_qty"], 205.0)
        self.assertEqual(rows["CAP"]["days_cover"], 13.0)

    def test_status_reads_the_open_orders_not_just_the_shelf(self):
        """The false-alarm fix, in the shape the live data taught it.

        PM0000085 really had 5,660 on hand against 21,936 a working day and
        1,148,000 units on open purchase orders. Judged on the shelf alone it
        screams; judged on what is bought it is fine.
        """
        rows = {r["item_code"]: r for r in self._board()["production_top"]}
        cap = rows["CAP"]
        self.assertEqual(cap["days_cover"], 13.0)  # shelf alone: would be "low"
        self.assertEqual(cap["open_po_qty"], 4000.0)
        # 4,205 / (410/26). Divided by the unrounded rate, so the figure does
        # not compound the rounding already shown in avg_daily_qty.
        self.assertEqual(cap["days_cover_incl_po"], 266.7)
        self.assertEqual(cap["cover_status"], "ok")

    def test_an_overdue_purchase_order_is_flagged_not_discounted(self):
        """A late supplier and a stale PO look identical from here."""
        rows = {r["item_code"]: r for r in self._board()["production_top"]}
        self.assertTrue(rows["CAP"]["open_po_overdue"])
        self.assertEqual(rows["CAP"]["open_po_earliest_due"], "2026-06-11")
        self.assertEqual(rows["CAP"]["open_po_lines"], 5)
        # still counted in cover despite being overdue
        self.assertGreater(rows["CAP"]["days_cover_incl_po"], rows["CAP"]["days_cover"])
        self.assertEqual(self._board()["summary"]["pm_items_overdue_po"], 1)

    def test_an_item_with_nothing_on_order_keeps_its_shelf_cover(self):
        rows = {r["item_code"]: r for r in self._board()["production_top"]}
        label = rows["LABEL"]
        self.assertEqual(label["open_po_qty"], 0.0)
        self.assertEqual(label["days_cover"], label["days_cover_incl_po"])
        self.assertFalse(label["open_po_overdue"])

    def test_an_item_with_no_stock_row_reads_as_empty(self):
        """A missing OITW row means none on the shelf, not unknown."""
        rows = {r["item_code"]: r for r in self._board()["production_top"]}
        self.assertEqual(rows["LABEL"]["stock_qty"], 8000.0)
        # PREFORM is consumed only at the blowing line and stocked nowhere in
        # the cover scope, so it must read as empty rather than absent.
        watch = {r["item_code"]: r for r in self._board()["cover_watch"]}
        self.assertNotIn("PREFORM", watch)

    def test_cover_watch_is_ordered_by_time_left_not_by_value(self):
        """A 50-paise label that stops the line outranks a costly bottle.

        LABEL leads despite being the cheaper item, because the cap's open
        orders cover it and the label's do not.
        """
        watch = self._board()["cover_watch"]
        self.assertEqual([r["item_code"] for r in watch], ["LABEL", "CAP"])
        self.assertLess(watch[0]["days_cover_incl_po"], watch[1]["days_cover_incl_po"])
        # and the cheaper item really is the one that leads
        self.assertLess(watch[0]["consumed_value"], watch[1]["consumed_value"])

    def test_summary_counts_items_by_cover_band(self):
        summary = self._board()["summary"]
        # Both items sit above the low band once open orders are counted:
        # LABEL has 8,000 against 30.8 a day, CAP is covered by its PO.
        self.assertEqual(summary["pm_items_critical_cover"], 0)
        self.assertEqual(summary["pm_items_low_cover"], 0)
        self.assertEqual(summary["pm_stock_value"], 4410.0)

    def test_meta_states_the_cover_scope_and_working_days(self):
        meta = self._board()["meta"]
        self.assertEqual(meta["stock_warehouses"], ["BH-PM", "BH-BS", "BH-PC", "GP-PM", "BH-PS"])
        self.assertEqual(meta["period_working_days"], 26)
        self.assertEqual(meta["cover_critical_days"], constants.COVER_CRITICAL_DAYS)

    def test_meta_states_the_scope_it_counted(self):
        meta = self._board()["meta"]
        self.assertEqual(meta["consumption_warehouses"], ["BH-PC"])
        self.assertEqual(meta["upstream_warehouses"], ["BH-SDL"])
        self.assertEqual(meta["fg_warehouses"], ["BH-PF"])
        self.assertEqual(meta["pm_item_group"], constants.PM_ITEM_GROUP)
        self.assertEqual(meta["ranked_by"], "value")
        self.assertEqual(meta["production_bom_coverage"]["qty_covered_pct"], 100.0)

    def test_top_n_limits_each_list_and_keeps_the_dearest(self):
        board = self._board_with_top_n(1)
        self.assertEqual(len(board["production_top"]), 1)
        self.assertEqual(len(board["dispatch_top"]), 1)
        # CAP is 410 x 2.0 = 820 against LABEL's 800 x 0.5 = 400
        self.assertEqual(board["production_top"][0]["item_code"], "CAP")

    def test_share_pct_is_of_the_whole_period_not_of_the_top_list(self):
        """Truncating to a top 10 must not inflate the shares that survive."""
        board = self._board_with_top_n(1)
        self.assertEqual(board["production_top"][0]["share_pct"], 67.21)


class ServiceDegradationTests(TestCase):
    def test_group_name_read_failure_does_not_fail_the_board(self):
        """The group name is context. Losing it must not cost the numbers."""

        class Exploding(StubReader):
            def pm_group_name(self):
                raise RuntimeError("HANA said no")

        service = PmDemandService.__new__(PmDemandService)
        service.company_code = "JIVO_OIL"
        service.context = None
        service.reader = Exploding()

        board = service.get_report(
            date_from=date(2026, 8, 1), date_to=date(2026, 8, 31)
        )
        self.assertEqual(board["meta"]["pm_item_group_name"], "")
        self.assertEqual(board["summary"]["pm_consumed_value"], 0.0)

    def test_empty_period_reports_zeroes_rather_than_dividing_by_zero(self):
        board = _service().get_report(
            date_from=date(2026, 8, 1), date_to=date(2026, 8, 31)
        )
        self.assertEqual(board["production_top"], [])
        self.assertIsNone(board["summary"]["dispatch_ratio_pct"])
        self.assertIsNone(board["summary"]["pm_variance_pct"])


# ---------------------------------------------------------------------------
# The two SQL facts a wrong query would get silently wrong
# ---------------------------------------------------------------------------


class ReaderSqlTests(SimpleTestCase):
    def test_consumption_reads_the_goods_issue_not_the_store_transfer(self):
        """TransType 67 into BH-PC reported 2 bottles against 603,505 used."""
        reader = CapturingReader()
        reader.pm_movements(
            "2026-08-01", "2026-08-31", ["BH-PC"], ["BH-WST"], ["BH-SDL"]
        )
        query, params = reader.calls[0]

        self.assertIn('O."TransType" = 60', query)
        self.assertIn('COALESCE(O."OutQty", 0)', query)
        self.assertIn(f'I."ItmsGrpCod" = {constants.PM_ITEM_GROUP}', query)
        # consumption, wastage, in-house (consumption again), upstream, dates,
        # then the warehouse filter -- HANA binds by position, so the order is
        # the contract.
        self.assertEqual(
            params,
            [
                "BH-PC",
                "BH-WST",
                "BH-PC",
                "BH-SDL",
                "2026-08-01",
                "2026-08-31",
                "BH-PC",
                "BH-WST",
                "BH-SDL",
            ],
        )

    def test_no_blowing_line_yields_a_literal_zero_not_an_empty_in_clause(self):
        reader = CapturingReader()
        reader.pm_movements("2026-08-01", "2026-08-31", ["BH-PP"], ["BH-WST"], [])
        query, params = reader.calls[0]

        self.assertNotIn("IN ()", query)
        self.assertEqual(params.count("BH-PP"), 3)  # 2 cases + the filter
        self.assertNotIn("BH-SDL", params)

    def test_bom_divides_by_the_batch_the_recipe_is_written_for(self):
        """Skipping OITT.Qauntity overstates every component by the case factor."""
        reader = CapturingReader()
        reader.pm_bom_lines()
        query, _ = reader.calls[0]

        self.assertIn('C."Quantity" / NULLIF(T."Qauntity", 0)', query)
        self.assertIn(f'C."Type" = {constants.BOM_LINE_TYPE_ITEM}', query)
        self.assertIn("""T."TreeType" = 'P'""", query)

    def test_dispatch_nets_credit_notes_and_binds_in_statement_order(self):
        reader = CapturingReader()
        reader.fg_dispatched("2026-08-01", "2026-08-31", ["CUSTA000606"])
        query, params = reader.calls[0]

        self.assertIn('"ORIN"', query)
        self.assertIn('"OINV"', query)
        self.assertIn('H."CANCELED" = \'N\'', query)
        self.assertEqual(
            params,
            [
                "CUSTA000606",
                "2026-08-01",
                "2026-08-31",
                "CUSTA000606",
                "2026-08-01",
                "2026-08-31",
            ],
        )

    def test_no_group_customers_configured_is_not_a_syntax_error(self):
        reader = CapturingReader()
        reader.fg_dispatched("2026-08-01", "2026-08-31", [])
        query, params = reader.calls[0]

        self.assertNotIn("IN ()", query)
        self.assertIn("1 = 0", query)
        self.assertEqual(params, ["2026-08-01", "2026-08-31"] * 2)

    def test_finished_goods_receipt_reads_trans_type_59_inwards(self):
        reader = CapturingReader()
        reader.fg_produced(["BH-PF"], "2026-08-01", "2026-08-31")
        query, params = reader.calls[0]

        self.assertIn('O."TransType" = 59', query)
        self.assertIn('SUM(O."InQty")', query)
        self.assertIn(f'I."ItmsGrpCod" = {constants.FG_ITEM_GROUP}', query)
        self.assertEqual(params, ["2026-08-01", "2026-08-31", "BH-PF"])

    def test_no_finished_goods_warehouse_configured_reads_nothing(self):
        reader = CapturingReader()
        self.assertEqual(reader.fg_produced([], "2026-08-01", "2026-08-31"), [])
        self.assertEqual(reader.calls, [])


class PermissionTests(SimpleTestCase):
    """Either right opens the board, and the sidebar's right must be one.

    The sidebar gates on ``production_execution.can_view_reports``. If this
    class demanded only the dedicated right, the menu entry would show and
    every request behind it would 403 -- a visible broken board.
    """

    class _User:
        def __init__(self, *held):
            self.held = set(held)

        def has_perm(self, perm):
            return perm in self.held

    def _allows(self, *held):
        return CanViewPmDemand().has_permission(
            type("Req", (), {"user": self._User(*held)})(), None
        )

    def test_the_dedicated_right_opens_it(self):
        self.assertTrue(self._allows("pm_demand.can_view_pm_demand"))

    def test_the_production_reports_right_opens_it(self):
        """The right the sidebar actually gates on."""
        self.assertTrue(self._allows("production_execution.can_view_reports"))

    def test_the_sidebar_right_is_one_of_the_accepted_ones(self):
        self.assertIn("production_execution.can_view_reports", PM_DEMAND_VIEW_PERMISSIONS)

    def test_neither_right_is_refused(self):
        self.assertFalse(self._allows())
        self.assertFalse(self._allows("some.other_permission"))


class SourceToggleTests(TestCase):
    """Only the quantities follow the toggle. The recipe and stock do not."""

    def _service(self):
        service = PmDemandService.__new__(PmDemandService)
        service.company_code = "JIVO_OIL"
        service.context = None
        service.reader = StubReader(
            produced=[{"item_code": "FG1", "item_name": "OIL 1L", "qty": 400.0}],
            dispatched=[
                {
                    "item_code": "FG1",
                    "item_name": "OIL 1L",
                    "qty": 200.0,
                    "intercompany_qty": 120.0,
                    "return_qty": 10.0,
                }
            ],
            bom_lines=[{"parent_code": "FG1", "pm_code": "CAP", "qty_per_unit": 1.0}],
            movements=[
                {
                    "item_code": "CAP",
                    "issued_qty": 410.0,
                    "wastage_qty": 5.0,
                    "in_house_qty": 0.0,
                    "upstream_qty": 0.0,
                }
            ],
            master=[
                {
                    "item_code": "CAP",
                    "item_name": "CAPS 1 LTR",
                    "uom": "PCS",
                    "sub_group": "CAPS",
                    "unit_price": 2.0,
                }
            ],
            stock=[{"item_code": "CAP", "stock_qty": 205.0, "stock_value": 410.0}],
        )
        service.app_reader = StubAppReader(
            produced=[{"item_code": "FG1", "item_name": "OIL 1L", "qty": 300.0}],
            dispatched=[
                {
                    "item_code": "FG1",
                    "item_name": "OIL 1L",
                    "qty": 150.0,
                    "intercompany_qty": 0.0,
                    "return_qty": 0.0,
                }
            ],
            movements=[
                {
                    "item_code": "CAP",
                    # approved, not issued -- the app records no issue at all
                    "issued_qty": 330.0,
                    "required_qty": 300.0,
                    "wastage_qty": 2.0,
                    "in_house_qty": 0.0,
                    "upstream_qty": 0.0,
                }
            ],
        )
        return service

    def _board(self, source):
        return self._service().get_report(
            date_from=date(2026, 8, 1), date_to=date(2026, 8, 31), source=source
        )

    def test_sap_is_the_default(self):
        board = self._service().get_report(
            date_from=date(2026, 8, 1), date_to=date(2026, 8, 31)
        )
        self.assertEqual(board["meta"]["source"], "sap")
        self.assertEqual(board["meta"]["consumption_basis"], "issued")

    def test_app_source_uses_the_app_quantities(self):
        board = self._board("app")
        self.assertEqual(board["summary"]["fg_produced_qty"], 300.0)
        self.assertEqual(board["summary"]["fg_dispatched_qty"], 150.0)
        rows = {r["item_code"]: r for r in board["production_top"]}
        self.assertEqual(rows["CAP"]["consumed_qty"], 330.0)

    def test_app_consumption_is_named_approved_not_issued(self):
        """The single most misleading thing this board could do."""
        self.assertEqual(self._board("app")["meta"]["consumption_basis"], "approved")
        self.assertEqual(self._board("sap")["meta"]["consumption_basis"], "issued")

    def test_the_recipe_comes_from_sap_in_both_modes(self):
        """400 x 1 on SAP quantities, 300 x 1 on app ones -- one same recipe."""
        self.assertEqual(
            {r["item_code"]: r["bom_qty"] for r in self._board("sap")["production_top"]},
            {"CAP": 400.0},
        )
        self.assertEqual(
            {r["item_code"]: r["bom_qty"] for r in self._board("app")["production_top"]},
            {"CAP": 300.0},
        )

    def test_stock_and_cover_come_from_sap_in_both_modes(self):
        for source in ("sap", "app"):
            rows = {r["item_code"]: r for r in self._board(source)["production_top"]}
            self.assertEqual(rows["CAP"]["stock_qty"], 205.0, source)

    def test_app_mode_filters_movements_to_the_sap_packaging_codes(self):
        """approved_qty covers raw material too -- 22.3M of it, mostly oil."""
        service = self._service()
        service.get_report(
            date_from=date(2026, 8, 1), date_to=date(2026, 8, 31), source="app"
        )
        self.assertEqual(service.app_reader.pm_codes_seen, ["CAP"])

    def test_app_mode_reports_no_intercompany_split(self):
        summary = self._board("app")["summary"]
        self.assertEqual(summary["fg_dispatched_intercompany_qty"], 0.0)
        self.assertEqual(summary["fg_returns_qty"], 0.0)

    def test_source_notes_state_the_app_limits_on_screen(self):
        notes = " ".join(self._board("app")["meta"]["source_notes"]).lower()
        self.assertIn("approved", notes)
        self.assertIn("issued_qty", notes)
        self.assertIn("sap", notes)
        self.assertEqual(len(self._board("sap")["meta"]["source_notes"]), 1)

    def test_an_unknown_source_falls_back_to_sap_rather_than_erroring(self):
        board = self._board("nonsense")
        self.assertEqual(board["meta"]["source"], "sap")
        self.assertEqual(board["summary"]["fg_produced_qty"], 400.0)


# ---------------------------------------------------------------------------
# Query parameters
# ---------------------------------------------------------------------------


class FilterSerializerTests(SimpleTestCase):
    def test_defaults_include_intercompany_and_a_top_ten(self):
        serializer = PmDemandFilterSerializer(
            data={"date_from": "2026-08-01", "date_to": "2026-08-31"}
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data["top"], constants.DEFAULT_TOP_N)
        self.assertTrue(serializer.validated_data["include_intercompany"])

    def test_source_defaults_to_sap_and_rejects_nonsense(self):
        base = {"date_from": "2026-08-01", "date_to": "2026-08-31"}
        ok = PmDemandFilterSerializer(data=base)
        self.assertTrue(ok.is_valid(), ok.errors)
        self.assertEqual(ok.validated_data["source"], "sap")

        app = PmDemandFilterSerializer(data={**base, "source": "app"})
        self.assertTrue(app.is_valid(), app.errors)
        self.assertEqual(app.validated_data["source"], "app")

        bad = PmDemandFilterSerializer(data={**base, "source": "hana"})
        self.assertFalse(bad.is_valid())
        self.assertIn("source", bad.errors)

    def test_reversed_range_is_rejected(self):
        serializer = PmDemandFilterSerializer(
            data={"date_from": "2026-08-31", "date_to": "2026-08-01"}
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("date_from", serializer.errors)

    def test_range_wider_than_the_cap_is_rejected(self):
        serializer = PmDemandFilterSerializer(
            data={"date_from": "2020-01-01", "date_to": "2026-08-31"}
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("date_to", serializer.errors)

    def test_a_single_day_is_a_valid_range(self):
        serializer = PmDemandFilterSerializer(
            data={"date_from": "2026-08-01", "date_to": "2026-08-01"}
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_top_is_capped(self):
        serializer = PmDemandFilterSerializer(
            data={
                "date_from": "2026-08-01",
                "date_to": "2026-08-31",
                "top": constants.MAX_TOP_N + 1,
            }
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("top", serializer.errors)
