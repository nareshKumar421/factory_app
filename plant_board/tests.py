"""
plant_board/tests.py

Tests for the arithmetic that would be wrong silently.

Every band is built from injected readers, so none of these needs a HANA
connection or a company's SAP config. What they cover is chosen deliberately:
each one is a number that, if it broke, would keep rendering on the wall
looking exactly as confident as a correct one.
"""

from datetime import date, datetime, timedelta
from decimal import Decimal

from unittest import mock

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from company.models import Company
from sap_client.exceptions import SAPConnectionError
from production_execution.models import (
    ProductionLine,
    ProductionMaterialUsage,
    ProductionRun,
    ProductionSegment,
    WasteLog,
)

from warehouse.models_bst import BSTBoxScan, BSTTransfer
from warehouse.models_pf_movement import PFStockMovement, PFStockMovementLine

from stock_dashboard.models import PlantBoardSettings, PlantBoardWorkforce

from .constants import STORE_WAREHOUSES, WORKFORCE_DEPARTMENTS
from .workforce import departments, slugify_key, unique_key
from .non_moving import non_moving_snapshot
from .views_workforce import _clean, _payload
from .services import PlantBoardService, _tons

TODAY = date(2026, 9, 10)


class FakeReader:
    """Stands in for the SAP reads."""

    def __init__(self, last_out=None, litres=None, yields=None, kinds=None):
        self._last_out = last_out or {}
        #: Litres in one piece, per item code. The Shifting band's only SAP
        #: read, and the one thing standing between its box scans and tonnes.
        self.litres = litres or {}
        self._yields = yields or {
            "issued_litres": 0.0,
            "packed_litres": 0.0,
            "issued_lines": 0,
            "packed_lines": 0,
            "assumed_litre_uom_lines": 0,
        }
        self._kinds = kinds or {}

    def last_out_dates(self, warehouse):
        return self._last_out

    def litres_per_piece(self, codes):
        return {code: self.litres.get(code, 0.0) for code in codes}

    def oil_yield(self, date_from, date_to):
        return self._yields

    def classify_items(self, codes):
        return self._kinds


class FakeStock:
    def __init__(self, occupancy=None, levels=None):
        self._occupancy = occupancy or {"data": [], "meta": {}}
        self._levels = levels or {"meta": {}}
        #: The filter the board last sent, which is what the benchmark tile's
        #: agreement with the Stock Benchmark page actually rests on.
        self.last_filters = None

    def get_warehouse_occupancy(self, warehouse):
        return self._occupancy

    def get_stock_levels(self, filters):
        self.last_filters = filters
        return self._levels


def service(**kwargs):
    """A board wired entirely from fakes, so nothing reaches SAP."""
    defaults = dict(
        company_code="JIVO_OIL",
        reader=FakeReader(),
        stock=FakeStock(),
        plans=object(),
        packing=object(),
        plan_reader=object(),
        today=TODAY,
    )
    defaults.update(kwargs)
    return PlantBoardService(**defaults)


class TonnageTests(SimpleTestCase):
    """1000 L = 1 t, and nothing else pretends to be a weight."""

    def test_litres_convert_on_the_agreed_rule(self):
        self.assertEqual(_tons(1000), 1.0)
        self.assertEqual(_tons(2500), 2.5)

    def test_no_litres_is_no_tonnage_not_an_error(self):
        # Packing material carries no litre volume in SAP. It must read zero
        # tons rather than blowing up or inventing a weight.
        self.assertEqual(_tons(None), 0.0)
        self.assertEqual(_tons(0), 0.0)


class FloorAgeTests(SimpleTestCase):
    """The 4-7 / >7 split, aged from when stock last LEFT."""

    def board(self, last_out):
        occupancy = {
            "data": [
                # Left yesterday: fresh, and must not appear in either bucket.
                {
                    "item_code": "FG-FRESH",
                    "on_hand": 100,
                    "stock_value": 1000,
                    "litres_per_piece": 1,
                },
                # Left 5 days ago: the 4-7 bucket.
                {
                    "item_code": "FG-MID",
                    "on_hand": 200,
                    "stock_value": 2000,
                    "litres_per_piece": 1,
                },
                # Left 30 days ago: over 7.
                {
                    "item_code": "FG-OLD",
                    "on_hand": 300,
                    "stock_value": 3000,
                    "litres_per_piece": 1,
                },
                # Never left at all.
                {
                    "item_code": "FG-NEVER",
                    "on_hand": 400,
                    "stock_value": 4000,
                    "litres_per_piece": 1,
                },
                # Zero on hand: a ledger row, not stock. Excluded entirely --
                # BH-PF carries 68 of these and they hold negative value.
                {
                    "item_code": "FG-EMPTY",
                    "on_hand": 0,
                    "stock_value": -50000,
                    "litres_per_piece": 1,
                },
            ],
            "meta": {"total_value": 10000, "total_on_hand": 1000, "item_count": 5},
        }
        return service(
            reader=FakeReader(last_out=last_out), stock=FakeStock(occupancy=occupancy)
        )._floor_stock()

    def test_buckets_split_on_days_since_last_out(self):
        floor = self.board(
            {
                "FG-FRESH": date(2026, 9, 9),
                "FG-MID": date(2026, 9, 5),
                "FG-OLD": date(2026, 8, 11),
            }
        )
        age = floor["age"]
        self.assertEqual(age["fresh"]["items"], 1)
        self.assertEqual(age["d4_7"]["items"], 1)
        self.assertEqual(age["d4_7"]["value"], 2000)
        self.assertEqual(age["d7_plus"]["items"], 1)
        self.assertEqual(age["d7_plus"]["value"], 3000)

    def test_never_shipped_is_its_own_bucket(self):
        # "Has never left" is a worse problem than "left a long time ago", so
        # it must not be folded into the oldest bucket.
        floor = self.board({"FG-FRESH": date(2026, 9, 9)})
        self.assertEqual(floor["age"]["never_shipped"]["items"], 3)
        self.assertEqual(floor["age"]["never_shipped"]["value"], 9000)

    def test_zero_quantity_rows_never_reach_a_bucket(self):
        floor = self.board({})
        bucketed = sum(bucket["items"] for bucket in floor["age"].values())
        self.assertEqual(bucketed, 4)

    def test_boundary_days_land_in_the_bucket_they_are_named_for(self):
        # Exactly 4 days is the first day worth showing; exactly 7 is still the
        # lower bucket. Off-by-one here silently moves stock between tiles.
        four = self.board({"FG-MID": date(2026, 9, 6)})
        self.assertEqual(four["age"]["d4_7"]["items"], 1)
        seven = self.board({"FG-MID": date(2026, 9, 3)})
        self.assertEqual(seven["age"]["d4_7"]["items"], 1)
        eight = self.board({"FG-MID": date(2026, 9, 2)})
        self.assertEqual(eight["age"]["d7_plus"]["items"], 1)


class BenchmarkTests(SimpleTestCase):
    """The tile must report what the Stock Benchmark dashboard reports."""

    LEVELS = {
        "meta": {
            # Deliberately larger than the three statuses add to: the extra
            # rows are slow movers, which SAP excludes from every status.
            "total_items": 260,
            "healthy_count": 60,
            "low_stock_count": 15,
            "critical_stock_count": 5,
            "below_benchmark_tonnes": 12.5,
            "unweighed_below_benchmark": 3,
        }
    }

    def test_sends_the_stock_benchmark_dashboard_filters(self):
        """The whole promise of the tile: same filter, therefore same number."""
        stock = FakeStock(levels=self.LEVELS)
        service(stock=stock)._benchmark()

        sent = stock.last_filters
        # Pinned to the constant, not to a copy of it: the business adds and
        # removes stores, and a hardcoded list here would only ever say that
        # somebody edited the constant, which is not a defect.
        self.assertEqual(sent["warehouse"], list(STORE_WAREHOUSES))
        # What IS worth pinning is that the non-moving store is in scope. It
        # holds over a million pieces of packaging, and leaving it out did not
        # make that stock disappear -- it made the tile understate the holding.
        self.assertIn("BH-NM", sent["warehouse"])
        self.assertEqual(sent["status"], ["healthy", "low", "critical"])
        self.assertEqual(sent["movement_status"], ["recent"])
        # SAP's own group name, letter for letter. The frontend's fallback
        # string is "Packing Material", which matches nothing: the backend
        # compares the group name with an exact equality, so a near-miss reads
        # as an empty store rather than as a broken filter.
        self.assertEqual(sent["item_group"], "PACKAGING MATERIAL")

    def test_counts_the_three_statuses_not_the_unfiltered_total(self):
        # 260 rows come back, but the page's own "Total Items" card shows
        # healthy + low + critical. Reading total_items instead is what made
        # this tile report an order of magnitude too many SKUs.
        benchmark = service(stock=FakeStock(levels=self.LEVELS))._benchmark()
        self.assertEqual(benchmark["sku_count"], 80)
        self.assertEqual(benchmark["below_benchmark_count"], 20)

    def test_carries_the_tonnage_and_what_it_could_not_weigh(self):
        benchmark = service(stock=FakeStock(levels=self.LEVELS))._benchmark()
        self.assertEqual(benchmark["below_benchmark_tonnes"], 12.5)
        # Disclosed, never rounded away: a confident tonnage over a
        # half-weighed set looks identical to a correct one.
        self.assertEqual(benchmark["unweighed_below_benchmark"], 3)

    def test_tonnage_is_unknown_rather_than_zero_when_sap_records_no_weight(self):
        levels = {"meta": {"healthy_count": 1, "low_stock_count": 1, "critical_stock_count": 0}}
        benchmark = service(stock=FakeStock(levels=levels))._benchmark()
        self.assertIsNone(benchmark["below_benchmark_tonnes"])


class ShiftingTests(TestCase):
    """The Shifting band reads BST, and reads it at two different stages.

    A ``TestCase`` because every figure here is Postgres: the register is
    FactoryFlow's own. Only the tonnage is SAP's, and the last test in this
    class is that losing SAP costs the band its unit and not its figures.
    """

    #: Mid-morning on the board's own "today". Both the scan and the dispatch
    #: are stamped with it, because the band is dated on those stamps and the
    #: board's today is injected rather than read off the clock.
    AT = timezone.make_aware(datetime(TODAY.year, TODAY.month, TODAY.day, 10, 0))

    def setUp(self):
        self.company = Company.objects.create(code="JIVO_OIL", name="Jivo Oil")

    def transfer(self, to_warehouse="BH-BT", source_type="STOCK_TRANSFER",
                 dispatched=True, status="RECEIVED", from_warehouse="BH-PF",
                 company=None):
        return BSTTransfer.objects.create(
            company=company or self.company,
            entry_no=f"BST-{BSTTransfer.objects.count() + 1:04d}",
            sap_from_warehouse=from_warehouse,
            sap_to_warehouse=to_warehouse,
            source_type=source_type,
            status=status,
            dispatched_at=self.AT if dispatched else None,
        )

    def scan(self, transfer, item_code="FG1", pieces=100, receive="PENDING"):
        scan = BSTBoxScan.objects.create(
            transfer=transfer,
            box_barcode=f"BX{BSTBoxScan.objects.count() + 1:06d}",
            item_code=item_code,
            quantity=pieces,
            receive_status=receive,
        )
        # `scanned_at` is auto_now_add, so the only way to date a scan is to
        # stamp it after the row exists.
        BSTBoxScan.objects.filter(pk=scan.pk).update(scanned_at=self.AT)
        return scan

    def band(self, litres=None):
        """The band, with SAP's litres per piece stubbed."""
        reader = FakeReader()
        reader.litres = litres if litres is not None else {"FG1": 1.0}
        return service(reader=reader)._shifting()

    # ------------------------------------------------------------------

    def declare(
        self,
        to_warehouse="BH-BT",
        kind="GODOWN",
        active=True,
        lines=None,
        to_company=None,
    ):
        movement = PFStockMovement.objects.create(
            company=self.company,
            entry_no=f"PF-{PFStockMovement.objects.count() + 1:04d}",
            movement_date=TODAY,
            from_warehouse="BH-PF",
            destination_kind=kind,
            to_warehouse="" if kind == "DISPATCH" else to_warehouse,
            # The register requires a destination COMPANY on a godown move and
            # forbids one on a dispatch: a dispatch has left the company, so
            # there is no receiving set of books. Enforced in the database, so
            # a helper that skipped it would not build a row at all.
            to_company=None if kind == "DISPATCH" else (to_company or self.company),
            is_active=active,
        )
        for line in lines or [{"item_code": "FG1", "pieces": 100, "litres_per_piece": 1}]:
            PFStockMovementLine.objects.create(
                movement=movement,
                item_code=line["item_code"],
                pieces=line["pieces"],
                pieces_per_box=line.get("pieces_per_box", 20),
                litres_per_piece=line.get("litres_per_piece"),
            )
        return movement

    # --- the declaration half -----------------------------------------------

    def test_declared_reads_the_godown_register_not_the_transfers(self):
        """Two registers, and the tiles must not read each other's rows."""
        self.declare(to_warehouse="BH-BT", lines=[
            {"item_code": "FG1", "pieces": 400, "litres_per_piece": 1},
        ])
        # A BST exists too, and must not appear on the declaration side.
        self.scan(self.transfer(to_warehouse="BH-EC"), pieces=999)

        declared = self.band()["allocated"]
        by_route = {row["route"]: row for row in declared["routes"]}
        self.assertEqual(by_route["BH-BT"]["pieces"], 400)
        # The BST's 999 pieces are the other tile's, and must not leak in here.
        self.assertNotIn("ELSEWHERE", by_route)
        self.assertEqual(declared["transfers"], 1)

    def test_the_declaration_tile_has_no_standing_dispatch_row(self):
        """One godown, and no second row reading 0.0 t every day of the year.

        A sale off this floor is raised as an invoice and reaches the board
        through BST, which does carry a dispatch row and a real figure.
        """
        self.declare(lines=[{"item_code": "FG1", "pieces": 100, "litres_per_piece": 1}])

        self.assertEqual(
            [row["route"] for row in self.band()["allocated"]["routes"]],
            ["BH-BT"],
        )
        # The shipped half keeps the dispatch row, because that is where a sale
        # lands.
        self.assertEqual(
            [row["route"] for row in self.band()["shipped"]["routes"]],
            ["BH-BT", "DISPATCH"],
        )

    def test_a_declared_dispatch_is_disclosed_rather_than_dropped(self):
        """No standing row is not the same as throwing the figure away."""
        self.declare(kind="DISPATCH", lines=[
            {"item_code": "FG1", "pieces": 250, "litres_per_piece": 1},
        ])

        declared = self.band()["allocated"]
        self.assertEqual(declared["total_pieces"], 250)

        rows = {row["route"]: row for row in declared["routes"]}
        self.assertNotIn("DISPATCH", rows)
        # It folds into the Elsewhere row, which exists only when it carries
        # something, and which names what it folded.
        self.assertEqual(rows["ELSEWHERE"]["pieces"], 250)
        self.assertEqual(rows["ELSEWHERE"]["codes"], ["DISPATCH"])

    def test_the_declared_tonnage_never_asks_sap(self):
        """The register snapshots litres per piece as each line is typed.

        That is what makes this the one figure on the board whose UNIT survives
        a HANA outage, not just its quantity.
        """
        class NoSap(FakeReader):
            def litres_per_piece(self, codes):
                raise AssertionError("the declaration half must not call SAP")

        self.declare(lines=[
            {"item_code": "FG5", "pieces": 4, "litres_per_piece": 5},
        ])
        declared = service(reader=NoSap())._shifting()["allocated"]

        self.assertEqual(declared["total_tons"], 0.02)
        self.assertTrue(declared["tonnage_available"])

    def test_a_retracted_declaration_is_excluded_and_counted(self):
        """The register deactivates rather than deletes, and so does the board.

        A withdrawn declaration is the difference between a keeper who planned
        nothing and one who changed his mind.
        """
        self.declare(lines=[{"item_code": "FG1", "pieces": 300, "litres_per_piece": 1}])
        self.declare(active=False, lines=[
            {"item_code": "FG1", "pieces": 700, "litres_per_piece": 1},
        ])

        declared = self.band()["allocated"]
        self.assertEqual(declared["total_pieces"], 300)
        self.assertEqual(declared["transfers"], 1)
        self.assertEqual(declared["retracted_movements"], 1)
        self.assertEqual(declared["retracted_pieces"], 700)

    def test_an_item_with_no_litre_volume_is_disclosed_not_zeroed(self):
        """A carton is not zero litres; it is not measured in litres."""
        self.declare(lines=[
            {"item_code": "FG1", "pieces": 100, "litres_per_piece": 1},
            {"item_code": "CARTON", "pieces": 500, "litres_per_piece": None},
        ])

        declared = self.band()["allocated"]
        self.assertEqual(declared["total_pieces"], 600)
        self.assertEqual(declared["total_tons"], 0.1)
        self.assertEqual(declared["unweighed_items"], 1)

    def test_yesterday_and_another_floor_are_not_today_declaration(self):
        stale = self.declare(lines=[
            {"item_code": "FG1", "pieces": 900, "litres_per_piece": 1},
        ])
        PFStockMovement.objects.filter(pk=stale.pk).update(
            movement_date=TODAY - timedelta(days=1)
        )
        elsewhere = self.declare(lines=[
            {"item_code": "FG1", "pieces": 800, "litres_per_piece": 1},
        ])
        PFStockMovement.objects.filter(pk=elsewhere.pk).update(from_warehouse="BH-BT")

        self.assertEqual(self.band()["allocated"]["total_pieces"], 0)

    def test_nothing_declared_is_a_real_answer(self):
        """The register has been empty since it was built. That is not an error."""
        declared = self.band()["allocated"]
        self.assertEqual(declared["transfers"], 0)
        self.assertEqual(declared["total_pieces"], 0)
        # The row still stands, so the wall does not reflow on a quiet day.
        self.assertEqual(
            [row["route"] for row in declared["routes"]],
            ["BH-BT"],
        )

    # --- the shipped half ---------------------------------------------------

    def test_the_named_routes_are_always_present_in_a_fixed_order(self):
        """A route that shows nothing today still holds its row.

        The two tiles are read down the same rows, so a row that disappears on
        a quiet day shuffles the ones below it and the comparison the band
        exists for stops working.
        """
        self.scan(self.transfer(to_warehouse="BH-BT"), pieces=1000)

        routes = self.band()["shipped"]["routes"]
        self.assertEqual([row["route"] for row in routes], ["BH-BT", "DISPATCH"])
        self.assertEqual(routes[0]["pieces"], 1000)
        self.assertEqual(routes[1]["pieces"], 0)

    def test_gupta_is_not_a_godown_row_and_folds_into_elsewhere_named(self):
        """The Gupta godown holds MART's stock.

        A load into it is the same event as the sale to Mart, but SAP booked it
        as an Oil internal transfer, so the board carried it as a third godown
        beside a Dispatch row that meant the same thing. It has no row now --
        but 236,582 pieces moved on that code in sixty days, so it must not
        vanish either: it folds into Elsewhere, and Elsewhere says which code
        it folded.
        """
        self.scan(self.transfer(to_warehouse="BH-BT"), pieces=1000)
        self.scan(self.transfer(to_warehouse="GP-FG"), pieces=250)

        routes = self.band()["shipped"]["routes"]
        self.assertEqual(
            [row["route"] for row in routes], ["BH-BT", "DISPATCH", "ELSEWHERE"]
        )
        elsewhere = routes[-1]
        self.assertEqual(elsewhere["pieces"], 250)
        self.assertEqual(elsewhere["codes"], ["GP-FG"])
        # And it is NOT quietly added to the sale it resembles.
        self.assertEqual(routes[1]["pieces"], 0)

    def test_elsewhere_stays_away_when_nothing_took_a_retired_route(self):
        """No standing Elsewhere row: it appears only when it carries something."""
        self.scan(self.transfer(to_warehouse="BH-BT"), pieces=1000)
        self.assertNotIn(
            "ELSEWHERE", [row["route"] for row in self.band()["shipped"]["routes"]]
        )

    def test_an_invoice_transfer_is_dispatch_not_a_warehouse(self):
        """Cross-company sale: the stock left, so it has no destination godown."""
        self.scan(
            self.transfer(to_warehouse="", source_type="INVOICE"), pieces=500
        )

        routes = {row["route"]: row for row in self.band()["shipped"]["routes"]}
        self.assertEqual(routes["DISPATCH"]["pieces"], 500)
        self.assertTrue(routes["DISPATCH"]["is_dispatch"])
        self.assertEqual(routes["BH-BT"]["pieces"], 0)

    def test_the_two_halves_are_never_netted_against_each_other(self):
        """A declaration and a posting answer different questions.

        The keeper says he will send 1,000 and BST records 600 gone so far.
        Neither figure is wrong, and the gap is usually just the hours between
        them, so the band reports both and subtracts nothing.
        """
        self.declare(lines=[{"item_code": "FG1", "pieces": 1000, "litres_per_piece": 1}])
        self.scan(self.transfer(dispatched=True), pieces=600)

        band = self.band()
        self.assertEqual(band["allocated"]["total_pieces"], 1000)
        self.assertEqual(band["shipped"]["total_pieces"], 600)
        # No variance field exists, and none should.
        self.assertNotIn("awaiting_dispatch_pieces", band["allocated"])
        self.assertNotIn("variance", band["allocated"])

    def test_a_cancelled_transfer_is_not_a_movement(self):
        self.scan(self.transfer(status="CANCELLED"), pieces=900)
        band = self.band()
        self.assertEqual(band["allocated"]["total_pieces"], 0)
        self.assertEqual(band["shipped"]["total_pieces"], 0)

    def test_another_floor_and_another_company_are_not_this_band(self):
        mart = Company.objects.create(code="JIVO_MART", name="Jivo Mart")
        self.scan(self.transfer(from_warehouse="BH-BT"), pieces=700)
        self.scan(self.transfer(company=mart), pieces=800)

        self.assertEqual(self.band()["shipped"]["total_pieces"], 0)

    def test_tonnage_comes_from_sap_and_not_from_the_box(self):
        """A box scan stores PIECES; only SAP knows what a piece holds."""
        # Four pieces of a five-litre pack: 20 L, which is 0.02 t.
        self.scan(self.transfer(), item_code="FG5", pieces=4)

        band = self.band(litres={"FG5": 5.0})
        self.assertEqual(band["shipped"]["total_tons"], 0.02)
        self.assertTrue(band["shipped"]["tonnage_available"])
        self.assertEqual(band["shipped"]["unweighed_items"], 0)

    def test_a_sku_with_no_litre_volume_is_counted_out_loud(self):
        self.scan(self.transfer(), item_code="PCONLY", pieces=100)

        shipped = self.band(litres={})["shipped"]
        self.assertEqual(shipped["total_pieces"], 100)
        self.assertEqual(shipped["total_tons"], 0.0)
        self.assertEqual(shipped["unweighed_items"], 1)

    def test_losing_sap_costs_the_unit_and_not_the_band(self):
        """The register is Postgres. A HANA outage must not blank the tiles."""
        class NoSap(FakeReader):
            def litres_per_piece(self, codes):
                raise RuntimeError("HANA is down")

        self.scan(self.transfer(), pieces=1000)
        band = service(reader=NoSap())._shifting()

        shipped = band["shipped"]
        self.assertEqual(shipped["total_pieces"], 1000)
        # Withheld, never reported as zero — a zero tonnage reads as a quiet day.
        self.assertIsNone(shipped["total_tons"])
        self.assertFalse(shipped["tonnage_available"])
        # And not every SKU blamed on a missing litre volume it may well have.
        self.assertEqual(shipped["unweighed_items"], 0)

    def test_rejected_boxes_still_left_this_floor(self):
        transfer = self.transfer()
        self.scan(transfer, pieces=800)
        self.scan(transfer, pieces=200, receive="REJECTED")

        shipped = self.band()["shipped"]
        self.assertEqual(shipped["total_pieces"], 1000)
        self.assertEqual(shipped["rejected_pieces"], 200)


class WorkforceTests(TestCase):
    """The one figure on this board nobody else holds, and its null discipline.

    The business's own table, typed in:

        Oil Production Company    85    15,65,504
        Oil Production Outside    31     8,67,695
        Fg Shifting                5     1,17,172
        Bottle Blowing Company     4       81,561
        Bottle Blowing Outside    10     1,03,000
        Pm Warehouse               9     1,47,945
    """

    TABLE = {
        "oil_production_company": (85, 1565504),
        "oil_production_outside": (31, 867695),
        "fg_shifting": (5, 117172),
        "bottle_blowing_company": (4, 81561),
        "bottle_blowing_outside": (10, 103000),
        "pm_warehouse": (9, 147945),
    }

    def configure(self, table=None):
        for key, (people, salary) in (table or self.TABLE).items():
            PlantBoardWorkforce.objects.create(
                company_code="JIVO_OIL",
                department=key,
                employees=people,
                salary_monthly=salary,
            )

    def test_the_company_outside_split_fills_the_two_halves_of_a_strip(self):
        """"Company" is on the payroll, "Outside" is hired in."""
        self.configure()
        production = service()._workforce()["bands"]["production"]

        self.assertEqual(production["employees"], 85)
        self.assertEqual(production["employee_salary_monthly"], 1565504.0)
        self.assertEqual(production["labour"], 31)
        self.assertEqual(production["labour_salary_monthly"], 867695.0)

    def test_two_departments_can_share_one_half_of_a_strip(self):
        """Store's staff are the blowing line's and the PM warehouse's together."""
        self.configure()
        store = service()._workforce()["bands"]["store"]

        self.assertEqual(store["employees"], 13)  # 4 blowing + 9 warehouse
        self.assertEqual(store["employee_salary_monthly"], 229506.0)  # 81,561 + 1,47,945
        self.assertEqual(store["labour"], 10)

    def test_purchase_has_nobody_and_says_so_with_a_rule(self):
        """No department maps to Purchase. Null, never zero."""
        self.configure()
        purchase = service()._workforce()["bands"]["purchase"]

        self.assertIsNone(purchase["employees"])
        self.assertIsNone(purchase["labour"])
        self.assertIsNone(purchase["employee_cost_per_day"])

    def test_the_monthly_bill_is_divided_by_calendar_days(self):
        """A wage is paid for the Sunday too, so it is not working days."""
        self.configure()
        board = service()._workforce()

        # TODAY is 10 September 2026, a 30-day month.
        self.assertEqual(board["days_in_month"], 30)
        self.assertEqual(
            board["bands"]["production"]["employee_cost_per_day"],
            round(1565504 / 30, 2),
        )

    def test_the_factory_total_is_every_department_once(self):
        self.configure()
        board = service()._workforce()

        self.assertEqual(board["total_people"], 144)
        self.assertEqual(board["total_salary_monthly"], 2882877.0)
        self.assertEqual(board["total_cost_per_day"], round(2882877 / 30, 2))
        self.assertEqual(board["unconfigured"], [])

    def test_an_unconfigured_department_is_named_not_silently_dropped(self):
        """A total that quietly leaves people out is worse than one that says so."""
        table = dict(self.TABLE)
        table.pop("pm_warehouse")
        self.configure(table)

        board = service()._workforce()
        self.assertEqual(board["unconfigured"], ["Pm Warehouse"])
        # The rest still add up, without the missing department.
        self.assertEqual(board["total_people"], 135)
        # And its half of the Store strip is the blowing line alone, not a zero.
        self.assertEqual(board["bands"]["store"]["employees"], 4)

    def test_nothing_configured_reads_as_a_rule_and_not_as_an_empty_factory(self):
        board = service()._workforce()

        self.assertIsNone(board["total_people"])
        self.assertIsNone(board["total_salary_monthly"])
        self.assertIsNone(board["bands"]["production"]["employees"])
        self.assertEqual(len(board["unconfigured"]), 6)

    def test_a_counted_department_with_no_salary_still_counts_its_people(self):
        """The two figures are independent: one missing must not hide the other."""
        PlantBoardWorkforce.objects.create(
            company_code="JIVO_OIL",
            department="fg_shifting",
            employees=5,
            salary_monthly=None,
        )
        shifting = service()._workforce()["bands"]["shifting"]

        self.assertEqual(shifting["employees"], 5)
        self.assertIsNone(shifting["employee_salary_monthly"])
        self.assertIsNone(shifting["employee_cost_per_day"])

    def test_another_company_figures_are_not_this_board(self):
        PlantBoardWorkforce.objects.create(
            company_code="JIVO_MART", department="fg_shifting", employees=99
        )
        self.assertIsNone(service()._workforce()["bands"]["shifting"]["employees"])

    def test_the_strips_survive_a_sap_outage(self):
        """Typed figures have nothing to do with SAP and must not degrade with it."""
        class Unreachable:
            def list_plans(self, limit=24):
                raise SAPConnectionError("Unable to connect to SAP HANA.")

        self.configure()
        board = service(plans=Unreachable()).build()

        self.assertNotIn("workforce", board["meta"]["degraded"])
        self.assertEqual(board["workforce"]["total_people"], 144)


class WorkforceSettingsAPITests(TestCase):
    """The settings page's payload, and what it does with an emptied field."""

    def test_every_department_comes_back_configured_or_not(self):
        """A department nobody has filled in must be visibly empty, not absent.

        Returning only saved rows would make a new department look like a bug
        on the page that exists to fix it.
        """
        PlantBoardWorkforce.objects.create(
            company_code="JIVO_OIL",
            department="fg_shifting",
            employees=5,
            salary_monthly=117172,
        )

        rows = _payload("JIVO_OIL")
        self.assertEqual(len(rows), len(WORKFORCE_DEPARTMENTS))

        by_key = {row["key"]: row for row in rows}
        self.assertEqual(by_key["fg_shifting"]["employees"], 5)
        self.assertEqual(by_key["fg_shifting"]["salary_monthly"], 117172.0)
        # Untouched departments are present and null, not missing.
        self.assertIsNone(by_key["pm_warehouse"]["employees"])
        self.assertIsNone(by_key["pm_warehouse"]["updated_at"])

    def test_the_payload_carries_the_band_and_kind_so_the_page_can_say_where(self):
        by_key = {row["key"]: row for row in _payload("JIVO_OIL")}
        self.assertEqual(by_key["pm_warehouse"]["band"], "store")
        self.assertEqual(by_key["oil_production_outside"]["kind"], "labour")

    def test_an_emptied_field_is_a_real_edit(self):
        """Clearing means "nobody has counted this any more", not "leave alone"."""
        self.assertIsNone(_clean("", "Employees"))
        self.assertIsNone(_clean(None, "Employees"))
        self.assertEqual(_clean("85", "Employees"), 85.0)
        self.assertEqual(_clean(1565504, "Salary"), 1565504.0)

    def test_a_negative_or_unparseable_figure_is_refused(self):
        with self.assertRaises(ValueError):
            _clean("-1", "Employees")
        with self.assertRaises(ValueError):
            _clean("eighty five", "Employees")


class AddedDepartmentTests(TestCase):
    """A department the plant grew, added without a deploy.

    The rule these pin is the one the built-in catalogue exists to protect: an
    operator can add a department and give it a band, and can never re-band or
    remove one the board was designed around. A mistyped head count is a
    mistyped head count; a department silently moved to another band changes
    what every figure above it means.
    """

    def add(self, label="Night Loading", band="shifting", kind="labour", **extra):
        row = PlantBoardWorkforce.objects.create(
            company_code="JIVO_OIL",
            department=unique_key(label, {e["key"] for e in departments("JIVO_OIL")}),
            label=label,
            band=band,
            kind=kind,
            **extra,
        )
        return row

    def test_an_added_department_joins_the_built_in_list(self):
        self.add()
        rows = departments("JIVO_OIL")
        self.assertEqual(len(rows), len(WORKFORCE_DEPARTMENTS) + 1)
        # Built-ins keep their order and come first, because both the strip and
        # the settings table are read down the same rows every day.
        self.assertEqual(
            [row["key"] for row in rows[: len(WORKFORCE_DEPARTMENTS)]],
            [entry["key"] for entry in WORKFORCE_DEPARTMENTS],
        )
        added = rows[-1]
        self.assertEqual(added["label"], "Night Loading")
        self.assertEqual(added["band"], "shifting")
        self.assertEqual(added["kind"], "labour")

    def test_only_an_added_department_says_it_can_be_removed(self):
        self.add()
        by_key = {row["key"]: row for row in departments("JIVO_OIL")}
        self.assertTrue(by_key["night_loading"]["is_custom"])
        # The six the board was designed around are code, and the settings page
        # must not offer to delete one.
        self.assertFalse(by_key["fg_shifting"]["is_custom"])

    def test_a_built_in_never_takes_its_band_from_the_table(self):
        """The whole point of the split: the database cannot re-band a built-in.

        A row carrying a band for a key that IS a built-in is ignored on that
        field — the catalogue in code wins, so a deploy that re-bands a
        department moves it everywhere at once with no stale copy surviving.
        """
        PlantBoardWorkforce.objects.create(
            company_code="JIVO_OIL",
            department="fg_shifting",
            label="Somewhere Else",
            band="purchase",
            kind="labour",
            employees=5,
        )
        by_key = {row["key"]: row for row in departments("JIVO_OIL")}
        self.assertEqual(by_key["fg_shifting"]["band"], "shifting")
        self.assertEqual(by_key["fg_shifting"]["label"], "Fg Shifting")
        self.assertEqual(by_key["fg_shifting"]["kind"], "employee")
        # Its figures are still read — only its identity is fixed.
        self.assertEqual(by_key["fg_shifting"]["employees"], 5)

    def test_an_added_department_reaches_the_wall_not_just_the_form(self):
        """The settings page and the board read one resolver, so this follows.

        Worth pinning anyway: the failure mode of two lists is a department that
        saves happily and never appears, which reads as a missing row rather
        than as two functions disagreeing.
        """
        self.add(label="Night Loading", band="shifting", kind="labour",
                 employees=7, salary_monthly=90000)
        strip = service()._workforce()
        shifting = strip["bands"]["shifting"]
        self.assertEqual(shifting["labour"], 7)
        self.assertIn(
            "Night Loading", [d["label"] for d in shifting["departments"]]
        )

    def test_a_row_with_no_band_of_its_own_is_skipped_not_guessed_at(self):
        """A built-in retired from the catalogue has nowhere on the board to go.

        Its figures are left in the table rather than destroyed by a deploy, but
        it is not shown: there is no band to show it under and inventing one
        would put people on a strip they do not belong to.
        """
        PlantBoardWorkforce.objects.create(
            company_code="JIVO_OIL", department="retired_dept", employees=3
        )
        keys = {row["key"] for row in departments("JIVO_OIL")}
        self.assertNotIn("retired_dept", keys)
        self.assertTrue(
            PlantBoardWorkforce.objects.filter(department="retired_dept").exists()
        )

    def test_another_company_does_not_see_it(self):
        self.add()
        self.assertNotIn(
            "night_loading", {row["key"] for row in departments("JIVO_MART")}
        )

    def test_a_key_is_slugified_and_never_collides(self):
        self.assertEqual(slugify_key("Night Loading"), "night_loading")
        self.assertEqual(slugify_key("  Oil / Packing  "), "oil_packing")
        # A label that slugifies onto a built-in gets a tail rather than
        # silently overwriting six months of figures.
        taken = {"pm_warehouse"}
        self.assertEqual(unique_key("Pm Warehouse", taken), "pm_warehouse_2")


class AddedDepartmentAPITests(TestCase):
    """The endpoint behind the Add department button.

    Exercised through the view rather than the resolver, because the guarantee
    worth testing is what a REQUEST can and cannot do: the settings page is the
    only way into this table, so "a built-in cannot be re-banded" has to hold at
    the door, not just in the function behind it.
    """

    def setUp(self):
        from django.contrib.auth import get_user_model
        from rest_framework.test import APIRequestFactory, force_authenticate

        from .views_workforce import PlantBoardWorkforceAPI

        # Permissions are what put the company on the request in production and
        # are not what these tests are about, so the view is subclassed with
        # them off and the context supplied by hand.
        class OpenAPI(PlantBoardWorkforceAPI):
            permission_classes = []

        self.view = OpenAPI.as_view()
        self.factory = APIRequestFactory()
        self.authenticate = force_authenticate
        self.company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        self.user = get_user_model().objects.create_user(
            email="wf@example.com", password="x"
        )

    def call(self, method, data=None):
        """One request, with the company context supplied by hand."""
        request = getattr(self.factory, method)("/workforce/", data, format="json")
        self.authenticate(request, user=self.user)
        company = self.company

        class _Ctx:
            pass

        ctx = _Ctx()
        ctx.company = company
        request.company = ctx

        response = self.view(request)
        response.render()
        return response

    def test_a_department_can_be_added_with_its_band_and_kind(self):
        response = self.call(
            "post",
            {
                "label": "Night Loading",
                "band": "shifting",
                "kind": "labour",
                "employees": 7,
                "salary_monthly": 90000,
            },
        )
        self.assertEqual(response.status_code, 201)
        by_key = {row["key"]: row for row in response.data["data"]}
        self.assertIn("night_loading", by_key)
        self.assertEqual(by_key["night_loading"]["band"], "shifting")
        self.assertEqual(by_key["night_loading"]["kind"], "labour")
        self.assertEqual(by_key["night_loading"]["employees"], 7)
        self.assertTrue(by_key["night_loading"]["is_custom"])

    def test_a_band_that_is_not_a_band_is_refused(self):
        # Not filed somewhere nobody looks: refused at the door.
        response = self.call(
            "post", {"label": "Somewhere", "band": "canteen", "kind": "labour"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Band must be one of", response.data["detail"])

    def test_a_department_with_no_name_is_refused(self):
        response = self.call("post", {"label": "  ", "band": "store", "kind": "labour"})
        self.assertEqual(response.status_code, 400)

    def test_two_departments_cannot_share_a_name(self):
        """On a wall the same caption twice with different figures is worse
        than a refusal."""
        self.call("post", {"label": "Night Loading", "band": "store", "kind": "labour"})
        again = self.call(
            "post", {"label": "night loading", "band": "store", "kind": "labour"}
        )
        self.assertEqual(again.status_code, 400)
        self.assertIn("already exists", again.data["detail"])

    def test_an_added_department_can_be_removed(self):
        self.call("post", {"label": "Night Loading", "band": "store", "kind": "labour"})
        response = self.call("delete", {"key": "night_loading"})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(
            "night_loading", {row["key"] for row in response.data["data"]}
        )

    def test_a_built_in_department_cannot_be_removed(self):
        response = self.call("delete", {"key": "fg_shifting"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("cannot be removed", response.data["detail"])
        self.assertIn("fg_shifting", {row["key"] for row in _payload("JIVO_OIL")})

    def test_saving_figures_does_not_wipe_an_added_department_identity(self):
        """Its label and band are its identity, not one of the numbers."""
        self.call(
            "post", {"label": "Night Loading", "band": "shifting", "kind": "labour"}
        )
        self.call(
            "put",
            {"departments": [{"key": "night_loading", "employees": 9, "salary_monthly": 1}]},
        )
        by_key = {row["key"]: row for row in _payload("JIVO_OIL")}
        self.assertEqual(by_key["night_loading"]["label"], "Night Loading")
        self.assertEqual(by_key["night_loading"]["band"], "shifting")
        self.assertEqual(by_key["night_loading"]["employees"], 9)


class FloorAreaTests(TestCase):
    """How full the packaging stores are, in two measured steps.

    SAP can answer neither step: checked against all 878 packaging items in
    Oil, ``OITM`` holds no volume, no dimensions and a gross weight on 155. So
    the factory measures pieces per pallet per item, and the pallet's own
    footprint, and the board multiplies live stock by both.
    """

    class StockReader(FakeReader):
        """Stands in for the SAP stock read, one row per store and item."""

        rows = []

        def packaging_stock(self, warehouses):
            return [row for row in self.rows if row["warehouse"] in warehouses]

    def reader(self, rows):
        reader = self.StockReader()
        reader.rows = rows
        return reader

    def row(self, warehouse, code, pieces, value=0.0, unpriced=False):
        return {
            "warehouse": warehouse,
            "item_code": code,
            "pieces": float(pieces),
            "value": float(value),
            "unpriced": unpriced,
        }

    def area(self, rows, stacking=None):
        board = service(reader=self.reader(rows))
        with mock.patch(
            "plant_board.services.pieces_per_pallet",
            return_value=stacking if stacking is not None else {"BOTTLE5": 300.0},
        ):
            return board._stock_space()["area"]

    # ------------------------------------------------------------------

    def test_each_item_stands_on_its_own_pallet_figure(self):
        """300 five-litre bottles fill a pallet; 60,000 caps fill one.

        A blended rate would be dominated by whichever item is numerous, which
        in these stores is caps -- and would report the floor as nearly empty.
        """
        area = self.area(
            [
                self.row("BH-PM", "BOTTLE5", 75_000),
                self.row("BH-PM", "CAPS", 60_000),
            ],
            stacking={"BOTTLE5": 300.0, "CAPS": 60_000.0},
        )

        # 250 pallets of bottles, 1 of caps.
        self.assertEqual(area["pallets"], 251.0)
        self.assertEqual(area["occupied_sqft"], 251.0 * 15)

    def test_the_floor_is_the_four_stores_measured_blocks(self):
        area = self.area([])
        self.assertEqual(area["sqft"], 38_900)
        self.assertEqual([block["sqft"] for block in area["blocks"]], [16_900, 15_000, 7_000])

    def test_occupancy_is_the_share_of_that_floor(self):
        area = self.area([self.row("BH-PM", "BOTTLE5", 300_000)])  # 1,000 pallets

        self.assertEqual(area["occupied_sqft"], 15_000.0)
        self.assertEqual(area["free_sqft"], 23_900.0)
        self.assertEqual(area["occupied_pct"], round(15_000 / 38_900 * 100, 1))

    def test_an_overfull_store_shows_it_rather_than_going_negative(self):
        """Packed past the measured floor is a real thing, and readable."""
        area = self.area([self.row("BH-PM", "BOTTLE5", 900_000)])  # 3,000 pallets

        self.assertEqual(area["occupied_sqft"], 45_000.0)
        # Floored, because negative free space is arithmetic nobody can act on.
        self.assertEqual(area["free_sqft"], 0.0)
        # But the percentage is uncapped, so the overfill is still visible.
        self.assertGreater(area["occupied_pct"], 100)

    def test_stock_with_no_pallet_figure_is_counted_and_named(self):
        """Every unmeasured piece makes the stores read emptier than they are.

        That is the one direction this tile must not fail in silently, so the
        pieces are still reported and the gap is named.
        """
        area = self.area(
            [
                self.row("BH-PM", "BOTTLE5", 3_000),
                self.row("BH-PM", "MYSTERY", 500_000),
            ],
            stacking={"BOTTLE5": 300.0},
        )

        self.assertEqual(area["held_pieces"], 503_000)
        self.assertEqual(area["pallets"], 10.0)  # the mystery item adds none
        self.assertEqual(area["unmeasured_items"], 1)
        self.assertEqual(area["unmeasured_pieces"], 500_000)

    def test_the_measured_default_is_used_before_anybody_configures_one(self):
        """An unconfigured board should be right, not silent."""
        area = self.area([self.row("BH-PM", "BOTTLE5", 300)])

        self.assertEqual(area["sqft_per_pallet"], 15.0)
        self.assertEqual(area["occupied_sqft"], 15.0)
        self.assertEqual(area["occupancy_blocked_on"], "")

    def test_clearing_the_footprint_withholds_the_percentage(self):
        """An explicit null is somebody saying the figure is not known here."""
        PlantBoardSettings.objects.create(
            company_code="JIVO_OIL", sqft_per_pallet=None
        )
        area = self.area([self.row("BH-PM", "BOTTLE5", 300)])

        self.assertIsNone(area["occupied_sqft"])
        self.assertIsNone(area["occupied_pct"])
        # The stock is still reported -- only the unit it cannot reach is gone.
        self.assertEqual(area["held_pieces"], 300)
        self.assertIn("one pallet stands on", area["occupancy_blocked_on"])

    def test_a_company_can_measure_its_own_pallet(self):
        PlantBoardSettings.objects.create(company_code="JIVO_OIL", sqft_per_pallet=12)
        area = self.area([self.row("BH-PM", "BOTTLE5", 300)])
        self.assertEqual(area["occupied_sqft"], 12.0)

    def test_the_store_split_adds_up_to_the_total(self):
        area = self.area(
            [
                self.row("BH-PM", "BOTTLE5", 30_000),
                self.row("BH-BS", "BOTTLE5", 15_000),
            ]
        )
        by_store = {row["warehouse"]: row for row in area["by_store"]}

        self.assertEqual(by_store["BH-PM"]["pallets"], 100.0)
        self.assertEqual(by_store["BH-BS"]["pallets"], 50.0)
        self.assertEqual(by_store["BH-NM"]["pallets"], 0.0)
        self.assertEqual(
            round(sum(row["occupied_sqft"] for row in area["by_store"]), 1),
            area["occupied_sqft"],
        )


class StackingSheetTests(SimpleTestCase):
    """The factory's own measurements, as the board reads them."""

    def test_every_item_on_the_sheet_has_a_positive_pallet_figure(self):
        from .stacking import pieces_per_pallet

        factors = pieces_per_pallet()
        self.assertGreater(len(factors), 300)
        self.assertTrue(all(value > 0 for value in factors.values()))

    def test_the_sheet_is_read_for_factors_and_not_for_quantities(self):
        """Its quantity column is a snapshot of one morning and is ignored.

        Several of its rows differ from live stock by orders of magnitude, so
        the board applies the factors to live SAP stock instead.
        """
        from . import stacking

        self.assertNotIn("qty", str(stacking._loaded().keys()).lower())
        self.assertEqual(stacking._loaded().get("unit"), "pieces per pallet")

    def test_known_items_carry_the_figures_the_factory_measured(self):
        from .stacking import pieces_per_pallet

        factors = pieces_per_pallet()
        # A five-litre HDPE bottle and a cap are three orders of magnitude
        # apart, which is exactly why the factor is per item.
        self.assertEqual(factors["PM0000053"], 300.0)
        self.assertEqual(factors["PM0000060"], 60_000.0)


class DegradationTests(TestCase):
    """A band that cannot be read must not take the wall down with it.

    A ``TestCase`` rather than a ``SimpleTestCase`` on purpose: the Shifting
    band reads Postgres, and the whole claim being tested is that a HANA outage
    leaves the Postgres half of the screen standing. Blocking database access
    here would make that band degrade for a reason the test invented.
    """

    def test_a_failing_band_is_named_and_the_rest_still_build(self):
        class Exploding:
            def list_plans(self, limit=24):
                raise RuntimeError("HANA is down")

        board = service(plans=Exploding()).build()

        # The plan is the window for two bands, so losing it loses them too --
        # but it is reported, not raised.
        self.assertIsNone(board["meta"]["plan"])
        self.assertIn("plan", board["meta"]["degraded"])
        self.assertIn("production", board["meta"]["degraded"])

        # Shifting comes out of Postgres, so a HANA outage must leave it alone.
        # It loses its UNIT, not its figures: see
        # ShiftingTests.test_losing_sap_costs_the_unit_and_not_the_band.
        self.assertNotIn("shifting", board["meta"]["degraded"])
        self.assertIsNotNone(board["shifting"])
        self.assertEqual(board["shifting"]["allocated"]["total_pieces"], 0)

    def test_one_sap_outage_costs_one_timeout_not_one_per_band(self):
        """The bug this guards: a blank screen, not a degraded one.

        A HANA connect attempt blocks for fifteen seconds. Six of them in a row
        is ninety seconds, past the client's own thirty-second limit, so the
        whole request failed and the board rendered its "could not be read"
        state -- every band lost, including the one that never needed SAP.
        """
        calls = []

        class Unreachable:
            def list_plans(self, limit=24):
                calls.append("plan")
                raise SAPConnectionError("Unable to connect to SAP HANA.")

        class CountingPacking:
            def get_requirement(self, abs_id):
                calls.append("purchase")
                return {}

        board = service(plans=Unreachable(), packing=CountingPacking()).build()

        # Tried once. Every later SAP band is skipped, not retried.
        self.assertEqual(calls, ["plan"])
        self.assertEqual(
            board["meta"]["degraded"], ["plan", "purchase", "store", "production"]
        )
        # Named once, not once per band.
        self.assertEqual(len(board["meta"]["warnings"]), 1)
        self.assertIn("SAP did not answer", board["meta"]["warnings"][0])

        # And the band that does not need SAP is untouched by the latch.
        self.assertNotIn("shifting", board["meta"]["degraded"])
        self.assertIsNotNone(board["shifting"])

    def test_only_a_connection_failure_latches_the_board(self):
        """A renamed column is one band's problem; a dead socket is everyone's.

        Tested on _section directly rather than through build, because
        in this harness the Store band really does reach for SAP and would
        latch the flag for a reason the test did not set up.
        """
        def raises(exc):
            def fail():
                raise exc
            return fail

        board = service()
        board._section("a", raises(KeyError("someone renamed a column")))
        self.assertFalse(board._sap_down)

        ran = []
        board._section("b", lambda: ran.append("b"))
        self.assertEqual(ran, ["b"], "an ordinary failure must not skip the next band")

        board._section("c", raises(SAPConnectionError("Unable to connect to SAP HANA.")))
        self.assertTrue(board._sap_down)

        # Latched: a band that needs SAP is not asked again.
        board._section("d", lambda: ran.append("d"))
        self.assertEqual(ran, ["b"])
        self.assertIn("d", board._degraded)

        # But one that can stand without it still runs.
        board._section("e", lambda: ran.append("e"), needs_sap=False)
        self.assertEqual(ran, ["b", "e"])

    def test_pending_tiles_are_declared_not_zeroed(self):
        board = service(plans=object()).build()
        pending = board["meta"]["pending"]
        # A tile nobody has data for says what it waits on. A zero here would
        # be believed for a week and ignored forever.
        self.assertIn("salary module", pending["employee_salary"])
        # Capacity and the audit date are configured on the board's settings
        # page now, so neither is pending. What is still missing is the tonnage
        # HELD: no stock query in this app reads a weight for these stores.
        self.assertNotIn("last_audited", pending)
        self.assertNotIn("stock_space", pending)
        # The floor area is known now. What Stock space still cannot do is
        # turn a piece count into floor used, and the pending entry says which
        # factor would.
        self.assertIn("square feet per pallet", pending["space_percent"])


class NonMovingTests(SimpleTestCase):
    """The Non-Moving dashboard's own rules, or the figures will not agree."""

    class FakeNonMoving:
        def __init__(self, rows):
            self._rows = rows
            self.age = None

        def get_report(self, age, item_group):
            self.age = age
            return {"data": self._rows}

    def row(self, code, warehouse, days, value=100):
        return {
            "item_code": code,
            "item_name": code,
            "warehouse": warehouse,
            "days_since_last_movement": days,
            "value": value,
            "quantity": 1,
        }

    def snapshot(self, rows, warehouses=("BH-PM", "BH-BS", "BH-PC")):
        fake = self.FakeNonMoving(rows)
        result = non_moving_snapshot("JIVO_OIL", list(warehouses), service=fake)
        # No minimum idle age on the fetch, so the status split is computed
        # over every row rather than over a pre-trimmed set.
        self.assertEqual(fake.age, 0)
        return result

    def test_recently_moved_skus_are_not_counted(self):
        # Under 30 idle days is "recently moved", which that page hides by
        # default. Counting them is how this tile reported several times the
        # number the page shows.
        snapshot = self.snapshot(
            [
                self.row("FRESH", "BH-PM", 5),
                self.row("SLOW", "BH-PM", 31),
                self.row("DEAD", "BH-PM", 200),
            ]
        )
        self.assertEqual(snapshot["item_count"], 2)
        self.assertEqual(snapshot["recent_count"], 1)
        self.assertEqual(snapshot["slow_moving_count"], 1)
        self.assertEqual(snapshot["non_moving_count"], 1)

    def test_a_sku_in_two_stores_keeps_its_FRESHEST_movement(self):
        # Consumed in one store, sitting in another. The page treats that as
        # alive, deliberately — so this must too, or the tile ages stock the
        # page calls fresh. Taking the oldest here would report 1 idle SKU.
        snapshot = self.snapshot(
            [self.row("PM1", "BH-PM", 200), self.row("PM1", "BH-BS", 3)]
        )
        self.assertEqual(snapshot["item_count"], 0)
        self.assertEqual(snapshot["recent_count"], 1)

    def test_value_and_quantity_still_add_across_stores(self):
        # The age does not add up; the money does.
        snapshot = self.snapshot(
            [
                self.row("PM1", "BH-PM", 100, value=300),
                self.row("PM1", "BH-BS", 90, value=200),
            ]
        )
        self.assertEqual(snapshot["item_count"], 1)
        self.assertEqual(snapshot["total_value"], 500)
        self.assertEqual(snapshot["items"][0]["days"], 90)
        self.assertEqual(snapshot["items"][0]["warehouses"], ["BH-BS", "BH-PM"])

    def test_the_thresholds_are_the_dashboard_thresholds(self):
        snapshot = self.snapshot(
            [
                self.row("A", "BH-PM", 29),
                self.row("B", "BH-PM", 30),
                self.row("C", "BH-PM", 45),
                self.row("D", "BH-PM", 46),
            ]
        )
        self.assertEqual(snapshot["recent_count"], 1)
        self.assertEqual(snapshot["slow_moving_count"], 2)
        self.assertEqual(snapshot["non_moving_count"], 1)

    def test_stores_outside_the_scope_are_dropped(self):
        snapshot = self.snapshot(
            [self.row("PM1", "BH-PM", 100), self.row("PM2", "GP-PM", 900)]
        )
        self.assertEqual(snapshot["item_count"], 1)
        self.assertEqual(snapshot["total_value"], 100)


class ProductionSeriesTests(TestCase):
    """The Production band must build, not just its pieces.

    This exists because a rename got the daily series and the code reading it
    out of step, and the board looked like a SAP outage rather than a bug:
    `_section` catches everything a band raises, so a KeyError renders
    identically to HANA being down. A band failing for its OWN reasons is the
    one thing that design cannot show, which is why this asserts the whole band
    builds rather than testing the series in isolation.

    A `TestCase` because the waste half of the band reads Postgres.
    """

    class FakePlanReader:
        def __init__(self, rows=None):
            self._rows = rows or []

        def get_daily_produced_quantities(self, codes, date_from, date_to):
            return self._rows

    class FakePlans:
        def __init__(self, lines):
            self._lines = lines

        def get_plan(self, abs_id, include_actuals=True):
            return {"lines": self._lines}

    PLAN = {
        "abs_id": 24,
        "start_date": "2026-09-01",
        "end_date": "2026-09-30",
        "days_elapsed": 10,
    }

    def band(self, daily_rows=None, lines=None):
        if lines is None:
            lines = [
                {
                    "item_code": "FG1",
                    "planned_qty": 1000,
                    "produced_qty": 400,
                    "planned_cases": 50,
                    "produced_cases": 20,
                    "planned_litres": 1000,
                    "produced_litres": 400,
                    "pieces_per_case": 20,
                }
            ]
        board = service(
            plans=self.FakePlans(lines),
            plan_reader=self.FakePlanReader(daily_rows),
        )
        return board._production(self.PLAN)

    # A mix whose piece ratio and tonne ratio are DIFFERENT numbers, which is
    # the whole reason both are reported: a litre item at 1 L a piece and a
    # five-litre item cannot share one percentage.
    MIXED = [
        {
            "item_code": "FG1",
            "planned_qty": 1000,
            "produced_qty": 900,
            "planned_litres": 1000,
            "produced_litres": 900,
        },
        {
            "item_code": "FG5",
            "planned_qty": 1000,
            "produced_qty": 100,
            "planned_litres": 5000,
            "produced_litres": 500,
        },
    ]

    def test_the_band_builds(self):
        """The regression itself: this raised KeyError and degraded the band."""
        band = self.band(
            [{"ItemCode": "FG1", "DocDate": date(2026, 9, 2), "ProducedQty": 400}]
        )
        self.assertEqual(band["produced_qty"], 400)
        self.assertEqual(band["planned_qty"], 1000)

    def test_attainment_is_computed_on_pieces(self):
        # 400 of 1000 pieces is 40%. The case ratio is 20 of 50 -- the same here
        # only because there is one item; across a real mix they diverge.
        band = self.band()
        self.assertEqual(band["attainment_pct"], 40.0)

    def test_the_tonne_ratio_is_not_the_piece_ratio(self):
        """The tile reads in tonnes, so the percentage beside it must too."""
        band = self.band(lines=self.MIXED)

        # 1000 of 2000 pieces is 50%; 1.4 t of 6 t is 23.3%. A board printing
        # the piece ratio next to the tonne figures would be off by half.
        self.assertEqual(band["attainment_pct"], 50.0)
        self.assertEqual(band["planned_tons"], 6.0)
        self.assertEqual(band["produced_tons"], 1.4)
        self.assertEqual(band["attainment_tons_pct"], 23.3)

    def test_a_sku_with_no_litre_volume_is_counted_out_loud(self):
        """A piece-only SKU is in the plan, in the pieces, and in no tonnage."""
        band = self.band(
            lines=self.MIXED
            + [
                {
                    "item_code": "PCONLY",
                    "planned_qty": 500,
                    "produced_qty": 500,
                    # SAP holds no `U_IsLitre` for it, so no litres at all.
                    "planned_litres": 0,
                    "produced_litres": 0,
                }
            ]
        )

        self.assertEqual(band["unweighed_lines"], 1)
        # Its pieces count; its weight does not exist, so the tons are unmoved.
        self.assertEqual(band["planned_qty"], 2500)
        self.assertEqual(band["planned_tons"], 6.0)

    def test_nothing_is_disclosed_when_every_planned_sku_is_weighed(self):
        self.assertEqual(self.band(lines=self.MIXED)["unweighed_lines"], 0)

    def test_no_tonnage_plan_reports_nothing_rather_than_zero(self):
        band = self.band(
            lines=[
                {
                    "item_code": "PCONLY",
                    "planned_qty": 500,
                    "produced_qty": 500,
                    "planned_litres": 0,
                    "produced_litres": 0,
                }
            ]
        )
        self.assertIsNone(band["attainment_tons_pct"])
        self.assertEqual(band["unweighed_lines"], 1)

    def test_the_average_counts_only_days_that_produced(self):
        band = self.band(
            [
                {"ItemCode": "FG1", "DocDate": date(2026, 9, 2), "ProducedQty": 300},
                {"ItemCode": "FG1", "DocDate": date(2026, 9, 5), "ProducedQty": 100},
            ]
        )
        # Two days produced out of ten elapsed, so 400 / 2 rather than 400 / 10.
        self.assertEqual(band["active_days"], 2)
        self.assertEqual(band["avg_qty_per_active_day"], 200.0)

    def test_the_series_is_keyed_on_qty_and_fills_every_idle_day(self):
        band = self.band(
            [{"ItemCode": "FG1", "DocDate": date(2026, 9, 2), "ProducedQty": 500}]
        )
        series = band["daily"]
        self.assertTrue(all("qty" in row for row in series))
        self.assertFalse(any("cases" in row for row in series))
        # Day one produced nothing and is still present, as a zero.
        self.assertEqual(series[0], {"date": "2026-09-01", "qty": 0.0})


class TodayOnTheLinesTests(TestCase):
    """Today's plan against today's output, from the lines' own register.

    The one figure on this band that does not come from SAP, and not as a
    preference: SAP holds no plan for a single day, and it learns of output
    only when the goods receipt is posted after the shift. Both halves exist
    nowhere but Production Execution while the day is still running.

    What is worth pinning here is the unit and the summing. The module records
    both halves in CASES and the board speaks in pieces, so a missed conversion
    reads twenty times too small and looks entirely plausible on a wall; and a
    day is several runs on several lines, so a figure that silently reported
    only one of them would look plausible too.
    """

    def setUp(self):
        self.company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        self.other = Company.objects.create(name="Jivo Mart", code="JIVO_MART")
        self.line_a = ProductionLine.objects.create(company=self.company, name="Line A")
        self.line_b = ProductionLine.objects.create(company=self.company, name="Line B")

    def make_run(
        self,
        line=None,
        run_number=1,
        on=TODAY,
        required=100,
        produced=0,
        per_case=20,
        status="COMPLETED",
        company=None,
    ):
        return ProductionRun.objects.create(
            company=company or self.company,
            line=line or self.line_a,
            run_number=run_number,
            date=on,
            required_qty=required,
            total_production=produced,
            pieces_per_case=per_case,
            status=status,
        )

    def today(self):
        return service()._today_on_the_lines()

    def test_cases_are_converted_to_pieces_by_each_run_own_factor(self):
        # 100 cases planned and 60 packed, at 20 bottles a case.
        self.make_run(required=100, produced=60, per_case=20)
        band = self.today()
        self.assertEqual(band["planned_qty"], 2000.0)
        self.assertEqual(band["produced_qty"], 1200.0)
        # The cases the register actually holds ride alongside, unconverted.
        self.assertEqual(band["planned_cases"], 100.0)
        self.assertEqual(band["produced_cases"], 60.0)

    def test_every_run_on_every_line_is_summed(self):
        # A day is several runs across several lines, and both halves are the
        # whole day. Two pack factors, so no single shared one can be assumed.
        self.make_run(line=self.line_a, run_number=1, required=100, produced=100, per_case=20)
        self.make_run(line=self.line_a, run_number=2, required=50, produced=40, per_case=20)
        self.make_run(line=self.line_b, run_number=3, required=30, produced=30, per_case=12)
        band = self.today()
        self.assertEqual(band["planned_qty"], 100 * 20 + 50 * 20 + 30 * 12)
        self.assertEqual(band["produced_qty"], 100 * 20 + 40 * 20 + 30 * 12)
        self.assertEqual(band["runs"], 3)
        self.assertEqual(band["lines"], 2)

    def test_a_draft_counts_as_planned(self):
        # Runs are planned the evening before and sit in DRAFT until the line
        # starts them. A board that counted only started runs would read
        # nothing planned every morning -- when the number matters most.
        self.make_run(required=80, produced=0, status="DRAFT")
        band = self.today()
        self.assertEqual(band["planned_qty"], 1600.0)
        self.assertEqual(band["produced_qty"], 0.0)
        self.assertEqual(band["completed_runs"], 0)

    def test_an_open_run_is_counted_from_its_segments(self):
        # `total_production` is typed at completion and stays zero for the
        # whole shift until then. The floor updates segments as it goes, so a
        # run still running reports from those rather than from a zero.
        run = self.make_run(required=100, produced=0, status="IN_PROGRESS")
        ProductionSegment.objects.create(
            production_run=run, start_time=timezone.now(), produced_cases=25
        )
        ProductionSegment.objects.create(
            production_run=run, start_time=timezone.now(), produced_cases=15
        )
        self.assertEqual(self.today()["produced_qty"], 40 * 20)

    def test_a_completed_total_wins_over_its_segments(self):
        # Once the supervisor types the day's total it is the declared figure,
        # and the segments -- which disagree with it by up to 3.4x on live
        # records -- must not override it.
        run = self.make_run(required=100, produced=90, status="COMPLETED")
        ProductionSegment.objects.create(
            production_run=run, start_time=timezone.now(), produced_cases=200
        )
        self.assertEqual(self.today()["produced_qty"], 90 * 20)

    def test_a_run_with_no_case_factor_is_left_out_and_named(self):
        # Cases cannot be added to a piece total. A run whose SKU never
        # resolved is excluded from both piece halves and counted, because the
        # alternative understates by a factor of twenty in silence.
        self.make_run(run_number=1, required=100, produced=100, per_case=20)
        self.make_run(run_number=2, required=50, produced=50, per_case=None)
        band = self.today()
        self.assertEqual(band["planned_qty"], 2000.0)
        self.assertEqual(band["produced_qty"], 2000.0)
        self.assertEqual(band["unconverted_runs"], 1)
        # Cases are unaffected: that side needs no factor.
        self.assertEqual(band["planned_cases"], 150.0)

    def test_yesterday_and_another_company_are_not_today(self):
        self.make_run(run_number=1, required=100, produced=100)
        self.make_run(
            run_number=2, on=TODAY - timedelta(days=1), required=500, produced=500
        )
        other_line = ProductionLine.objects.create(company=self.other, name="Line M")
        self.make_run(
            line=other_line, run_number=3, required=900, produced=900, company=self.other
        )
        band = self.today()
        self.assertEqual(band["runs"], 1)
        self.assertEqual(band["produced_qty"], 2000.0)

    def test_a_discarded_run_is_not_a_plan(self):
        # A thrown-away plan must not keep inflating the target it was removed
        # from. The default manager hides it; this pins that the board reads
        # through that manager rather than through `all_objects`.
        self.make_run(run_number=1, required=100, produced=100)
        binned = self.make_run(run_number=2, required=400, produced=0)
        binned.is_deleted = True
        binned.save(update_fields=["is_deleted"])
        band = self.today()
        self.assertEqual(band["runs"], 1)
        self.assertEqual(band["planned_qty"], 2000.0)
        self.assertEqual(band["attainment_pct"], 100.0)

    def test_a_day_with_no_runs_reports_no_attainment_rather_than_zero(self):
        # Nothing planned and nothing made is not 0% of plan; the board has to
        # be able to say "nothing planned" instead of drawing an empty meter.
        band = self.today()
        self.assertEqual(band["runs"], 0)
        self.assertEqual(band["planned_qty"], 0.0)
        self.assertIsNone(band["attainment_pct"])


class WasteRegisterTests(TestCase):
    """The waste register, read per day and valued.

    Three things here would be wrong silently, which is why each has a test.
    The DATE, because a waste row is typed days after the shift and keying on
    the typing date piles two shifts onto one morning. The PRICE, because the
    register holds no money and the rate has to be fetched from the run the
    waste came out of. And the UNIT, because the register mixes pieces, kilos
    and metres, so any total that is not money is a total of nothing.
    """

    RATE = Decimal("7.40")

    def setUp(self):
        self.company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        self.line = ProductionLine.objects.create(company=self.company, name="Line A")
        self.run = self.make_run(1, date(2026, 9, 9))

    def make_run(self, number, on):
        return ProductionRun.objects.create(
            company=self.company, line=self.line, run_number=number, date=on,
            required_qty=100, pieces_per_case=20,
        )

    def price(self, run, code, rate, name="PET BOTTLE 1 LTR"):
        return ProductionMaterialUsage.objects.create(
            production_run=run, material_code=code, material_name=name,
            unit_price=rate, uom="PCS",
        )

    def log(self, code="PM0000194", qty=100, uom="PCS", run=None, typed=None):
        row = WasteLog.objects.create(
            company=self.company,
            production_run=self.run if run is None else run,
            material_code=code,
            material_name="PET BOTTLE 1 LTR",
            wastage_qty=qty,
            uom=uom,
        )
        if typed:
            # `created_at` is auto_now_add, so the typing date has to be forced
            # -- which is the whole point of these tests.
            WasteLog.objects.filter(pk=row.pk).update(
                created_at=timezone.make_aware(datetime(typed.year, typed.month, typed.day, 9, 0))
            )
        return row

    def band(self, kinds=None):
        reader = FakeReader(
            kinds=kinds if kinds is not None else {"PM0000194": "PACKAGING"}
        )
        return service(reader=reader)._wastage(date(2026, 9, 1), TODAY, 0)

    def day_of(self, band, iso):
        return next(row for row in band["logged_daily"] if row["date"] == iso)

    def test_waste_lands_on_the_run_date_not_the_day_it_was_typed(self):
        # The regression this tile was rebuilt for. The run was on the 9th and
        # the row was typed on the 10th; read on its typing date the 9th looks
        # like a clean shift and the 10th carries someone else's waste.
        self.price(self.run, "PM0000194", self.RATE)
        self.log(typed=date(2026, 9, 10))
        band = self.band()

        self.assertEqual(self.day_of(band, "2026-09-09")["pm_value"], 740.0)
        self.assertEqual(self.day_of(band, "2026-09-10")["pm_value"], 0.0)
        self.assertEqual(self.day_of(band, "2026-09-10")["logs"], 0)

    def test_each_row_is_valued_at_its_own_run_price(self):
        # 100 bottles at the rate that run was costed at.
        self.price(self.run, "PM0000194", self.RATE)
        self.log(qty=100)
        self.assertEqual(self.band()["logged_latest"]["pm_value"], 740.0)

    def test_the_run_own_price_wins_over_a_newer_one_elsewhere(self):
        # A later run repriced the same material. The waste belongs to the
        # earlier run and must be valued at what THAT run was costed at, or the
        # day's waste stops reconciling with the day's costing.
        self.price(self.run, "PM0000194", self.RATE)
        self.price(self.make_run(2, date(2026, 9, 10)), "PM0000194", Decimal("99.00"))
        self.log(qty=100)
        self.assertEqual(self.day_of(self.band(), "2026-09-09")["pm_value"], 740.0)

    def test_a_run_with_no_price_falls_back_to_the_latest_known_one(self):
        # The waste row's own run carries no priced line, so the most recent
        # price for that material stands in rather than the row being dropped.
        self.price(self.make_run(2, date(2026, 9, 8)), "PM0000194", Decimal("5.00"))
        self.log(qty=100)
        band = self.band()
        self.assertEqual(band["logged_latest"]["pm_value"], 500.0)
        self.assertEqual(band["logged_latest"]["unpriced"], 0)

    def test_a_row_with_no_price_anywhere_is_counted_not_zeroed_quietly(self):
        # Valued at nothing, but SAID to be valued at nothing: the money is
        # short by exactly this row and the tile has to be able to disclose it.
        self.log(qty=100)
        band = self.band()
        self.assertEqual(band["logged_latest"]["pm_value"], 0.0)
        self.assertEqual(band["logged_latest"]["unpriced"], 1)
        self.assertEqual(band["logged_unpriced_count"], 1)

    def test_units_are_kept_apart_and_only_money_is_added(self):
        # 100 pieces and 40 metres. 140 is not a quantity of anything, but
        # their two values are a sum that means something.
        self.price(self.run, "PM0000194", self.RATE)
        self.price(self.run, "PM0000075", Decimal("0.80"), name="TAPE")
        self.log(code="PM0000194", qty=100, uom="PCS")
        self.log(code="PM0000075", qty=40, uom="MTR")

        day = self.band(
            kinds={"PM0000194": "PACKAGING", "PM0000075": "PACKAGING"}
        )["logged_latest"]
        self.assertEqual(day["pm_pieces"], 100.0)
        self.assertEqual(day["pm_other"], [{"uom": "MTR", "qty": 40.0}])
        # 100 x 7.40 + 40 x 0.80 = 772.
        self.assertEqual(day["pm_value"], 772.0)

    def test_raw_material_is_split_out_and_kept_in_litres(self):
        self.price(self.run, "RM0000003", Decimal("135.00"), name="MUSTARD OIL")
        self.log(code="RM0000003", qty=10, uom="LTR")
        day = self.band(kinds={"RM0000003": "RAW"})["logged_latest"]
        self.assertEqual(day["rm_litres"], 10.0)
        self.assertEqual(day["rm_value"], 1350.0)
        self.assertEqual(day["pm_value"], 0.0)

    def test_the_latest_logged_day_is_named_with_its_age(self):
        # Today is the 10th and the only waste belongs to the 9th.
        self.price(self.run, "PM0000194", self.RATE)
        self.log()
        band = self.band()
        self.assertEqual(band["logged_latest_date"], "2026-09-09")
        self.assertEqual(band["logged_days_behind"], 1)

    def test_an_empty_register_reports_no_day_rather_than_a_zero(self):
        band = self.band()
        self.assertIsNone(band["logged_latest"])
        self.assertIsNone(band["logged_latest_date"])
        self.assertIsNone(band["logged_days_behind"])

    def test_material_usage_wastage_qty_is_never_read_as_waste(self):
        # `ProductionMaterialUsage.wastage_qty` is `opening + issued - closing`
        # and on live rows equals the run's whole BOM requirement -- reading it
        # as waste reports a day's entire consumption as spoilage. A fat value
        # on that field must not move this band by a rupee.
        usage = self.price(self.run, "PM0000194", self.RATE)
        usage.opening_qty = 20000
        usage.wastage_qty = 20000
        usage.save(update_fields=["opening_qty", "wastage_qty"])
        self.log(qty=100)

        band = self.band()
        self.assertEqual(band["logged_latest"]["pm_value"], 740.0)
        self.assertEqual(band["logged_latest"]["pm_pieces"], 100.0)

    def test_every_day_of_the_week_is_present_even_with_no_logs(self):
        # A wall reads the shape of a week; a week with days removed has the
        # wrong shape.
        self.price(self.run, "PM0000194", self.RATE)
        self.log()
        series = self.band()["logged_daily"]
        self.assertEqual(
            [row["date"] for row in series],
            [f"2026-09-{d:02d}" for d in range(4, 11)],
        )
