"""The meter tree: who pays for each unit, worked out down the tree.

The campus's meters are sub-meters of sub-meters. A parent's reading contains
its sub-meters', so each meter pays only for its *own* units — its reading less
theirs — and its setup says who that is: fixed shares, the run hours of the
lines it serves, or the readings of the meters it follows. Every unit on the
incomer lands in exactly one account.

The engine is tested first with no database at all, then through the API the
screens use: placing meters, versioning who pays, the chain of readings, the
day sheet, and the split itself.
"""

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from blowing.models import BlowingMachine, BlowingRun, BlowingSegment, PreformSpec
from company.models import Company
from maintenance.electricity import engine
from maintenance.electricity.engine import (
    UNASSIGNED,
    Basis,
    Driver,
    IssueKind,
    Meter,
    Reading,
    Setup,
    Share,
    fixed_split,
)
from maintenance.electricity.run_hours import MAX_SEGMENT_HOURS, capped, hours_by_day, merge
from maintenance.models import (
    DailyElectricityReading,
    ElectricityMeter,
    ElectricityMeterSetup,
)
from maintenance.models_manager import UserElectricityMeter
from maintenance.tests_meter_scope import client_for, make_user
from production_execution.models import ProductionLine, ProductionRun, ProductionSegment

METERS_URL = "/api/v1/maintenance/electricity-tree-meters/"
SETUPS_URL = "/api/v1/maintenance/electricity-meter-setups/"
READINGS_URL = "/api/v1/maintenance/electricity-tree-readings/"
SHEET_URL = "/api/v1/maintenance/electricity-day-sheet/"
SPLIT_URL = "/api/v1/maintenance/electricity-allocation/"
SOURCES_URL = "/api/v1/maintenance/electricity-run-sources/"

OIL, BEV = "company:JIVO_OIL", "company:JIVO_BEVERAGES"
D1 = date(2026, 9, 1)


def day(n: int) -> date:
    return D1 + timedelta(days=n - 1)


def reading(meter_id, on, opening, closing, *, mf=1, rate=9, reset=False, rid=None):
    opening, closing, mf = Decimal(str(opening)), Decimal(str(closing)), Decimal(str(mf))
    return Reading(
        meter_id, on, opening, closing, (closing - opening) * mf, Decimal(str(rate)), mf, reset, rid
    )


def shares(*pairs):
    return tuple(Share(party, Decimal(str(percent))) for party, percent in pairs)


# ---------------------------------------------------------------------------
# The engine, with no database
# ---------------------------------------------------------------------------


class EngineTreeTests(SimpleTestCase):
    """KWH feeds the two floors; the lab is a sub-meter of the ground floor."""

    KWH, GROUND, LAB, FIRST, KVAH = 1, 2, 3, 4, 5

    def setUp(self):
        self.meters = [
            Meter(self.KWH, "KWH"),
            Meter(self.GROUND, "Ground Floor"),
            Meter(self.LAB, "Lab"),
            Meter(self.FIRST, "First Floor"),
            Meter(self.KVAH, "KVAH", register_of=self.KWH),
        ]
        self.setups = [
            Setup(self.KWH, D1, basis=Basis.UNASSIGNED),
            Setup(self.GROUND, D1, parent_id=self.KWH, basis=Basis.FIXED, shares=shares((BEV, 100))),
            Setup(self.LAB, D1, parent_id=self.GROUND, basis=Basis.FIXED, shares=shares((OIL, 50), (BEV, 50))),
            Setup(self.FIRST, D1, parent_id=self.KWH, basis=Basis.FIXED, shares=shares((OIL, 100))),
        ]

    def allocate(self, readings, date_from=D1, date_to=D1, setups=None, run_hours=None):
        return engine.Allocator(
            self.meters, setups if setups is not None else self.setups, readings, run_hours
        ).allocate(date_from, date_to)

    def full_day(self, on=D1, kwh=1000, ground=400, lab=40, first=500):
        return [
            reading(self.KWH, on, 0, kwh),
            reading(self.GROUND, on, 0, ground),
            reading(self.LAB, on, 0, lab),
            reading(self.FIRST, on, 0, first),
        ]

    def nodes(self, result, on=D1):
        return {node.meter_id: node for node in result.nodes if node.day == on}

    def kinds(self, result):
        return [(issue.kind, issue.meter_id) for issue in result.issues]

    def test_a_parent_pays_only_for_what_its_sub_meters_did_not_read(self):
        result = self.allocate(self.full_day())
        nodes = self.nodes(result)
        self.assertEqual(nodes[self.KWH].own, Decimal("100"))
        self.assertEqual(nodes[self.GROUND].own, Decimal("360"))
        self.assertEqual(nodes[self.LAB].own, Decimal("40"))
        totals = result.party_totals()
        # The ground floor's rest (360) and half the lab; the first floor and
        # the other half of the lab; the incomer's own direct load, unassigned.
        self.assertEqual(totals[BEV]["units"], Decimal("380"))
        self.assertEqual(totals[OIL]["units"], Decimal("520"))
        self.assertEqual(totals[UNASSIGNED]["units"], Decimal("100"))

    def test_every_unit_on_the_incomer_lands_in_exactly_one_account(self):
        result = self.allocate(self.full_day())
        totals = result.party_totals()
        self.assertEqual(sum(v["units"] for v in totals.values()), result.supply()["units"])
        self.assertEqual(result.supply()["units"], Decimal("1000"))
        self.assertEqual(sum(v["cost"] for v in totals.values()), Decimal("9000"))

    def test_sub_meters_that_overshoot_leave_the_parent_nothing_rather_than_a_refund(self):
        result = self.allocate(self.full_day(ground=400, lab=450))
        nodes = self.nodes(result)
        self.assertEqual(nodes[self.GROUND].own, Decimal("0"))
        self.assertEqual(nodes[self.GROUND].over_read, Decimal("50"))
        self.assertIn((IssueKind.OVER_READ, self.GROUND), self.kinds(result))
        # The lab is still split in full: the fault is the parent's reading.
        self.assertEqual(nodes[self.LAB].split, {OIL: Decimal("225"), BEV: Decimal("225")})

    def test_an_unread_sub_meter_leaves_its_load_in_the_parents_rest(self):
        readings = [r for r in self.full_day() if r.meter_id != self.LAB]
        result = self.allocate(readings)
        nodes = self.nodes(result)
        self.assertEqual(nodes[self.GROUND].own, Decimal("400"))
        self.assertEqual(nodes[self.GROUND].unread_children, (self.LAB,))
        self.assertIsNone(nodes[self.LAB].own)
        self.assertIn((IssueKind.NOT_READ, self.LAB), self.kinds(result))

    def test_an_unread_parent_splits_nothing_of_its_own(self):
        readings = [r for r in self.full_day() if r.meter_id != self.KWH]
        result = self.allocate(readings)
        self.assertIsNone(self.nodes(result)[self.KWH].own)
        self.assertNotIn(UNASSIGNED, result.party_totals())
        self.assertEqual(result.supply()["units"], Decimal("0"))
        self.assertIn((IssueKind.NOT_READ, self.KWH), self.kinds(result))

    def test_a_reading_after_skipped_days_is_spread_over_them(self):
        readings = [
            reading(self.FIRST, day(1), 0, 100),
            reading(self.FIRST, day(3), 100, 400, rid=7),
        ]
        result = self.allocate(readings, day(1), day(3))
        first = {node.day: node for node in result.nodes if node.meter_id == self.FIRST}
        self.assertEqual(first[day(2)].measured, Decimal("150"))
        self.assertEqual(first[day(3)].measured, Decimal("150"))
        self.assertEqual(first[day(3)].spread_over, 2)
        spread = [i for i in result.issues if i.kind == IssueKind.SPREAD]
        self.assertEqual(len(spread), 1, "one note per reading, not one per day")

    def test_an_opening_that_does_not_follow_on_is_a_break(self):
        readings = [
            reading(self.FIRST, day(1), 0, 100),
            reading(self.FIRST, day(2), 150, 200),
        ]
        result = self.allocate(readings, day(1), day(2))
        breaks = [i for i in result.issues if i.kind == IssueKind.BREAK]
        self.assertEqual(len(breaks), 1)
        self.assertEqual(breaks[0].units, Decimal("50"))
        first = {node.day: node for node in result.nodes if node.meter_id == self.FIRST}
        self.assertEqual(first[day(2)].measured, Decimal("50"))

    def test_a_meter_reset_is_not_a_break(self):
        readings = [
            reading(self.FIRST, day(1), 0, 100),
            reading(self.FIRST, day(2), 5, 60, reset=True),
        ]
        result = self.allocate(readings, day(1), day(2))
        self.assertNotIn(IssueKind.BREAK, [i.kind for i in result.issues])

    def test_a_day_nobody_entered_is_one_note_not_a_flood(self):
        result = self.allocate(self.full_day(), day(1), day(2))
        second = [i for i in result.issues if i.day == day(2)]
        self.assertEqual([i.kind for i in second], [IssueKind.DAY_NOT_ENTERED])

    def test_a_change_applies_from_its_own_date_only(self):
        setups = self.setups + [
            Setup(self.FIRST, day(2), parent_id=self.KWH, basis=Basis.FIXED, shares=shares((OIL, 50), (BEV, 50)))
        ]
        result = self.allocate(self.full_day(day(1)) + self.full_day(day(2)), day(1), day(2), setups)
        self.assertEqual(self.nodes(result, day(1))[self.FIRST].split, {OIL: Decimal("500")})
        self.assertEqual(
            self.nodes(result, day(2))[self.FIRST].split, {OIL: Decimal("250"), BEV: Decimal("250")}
        )

    def test_rewiring_moves_a_meter_to_another_parent_from_that_day(self):
        setups = self.setups + [
            Setup(self.LAB, day(2), parent_id=self.FIRST, basis=Basis.FIXED, shares=shares((OIL, 50), (BEV, 50)))
        ]
        result = self.allocate(self.full_day(day(1)) + self.full_day(day(2)), day(1), day(2), setups)
        self.assertEqual(self.nodes(result, day(1))[self.GROUND].own, Decimal("360"))
        self.assertEqual(self.nodes(result, day(2))[self.GROUND].own, Decimal("400"))
        self.assertEqual(self.nodes(result, day(2))[self.FIRST].own, Decimal("460"))

    def test_a_second_register_is_shown_never_counted(self):
        result = self.allocate(self.full_day() + [reading(self.KVAH, D1, 0, 1050)])
        self.assertEqual(result.supply()["units"], Decimal("1000"))
        self.assertNotIn(self.KVAH, [node.meter_id for node in result.nodes])
        register = result.registers[0]
        self.assertEqual((register.measured, register.principal_measured), (Decimal("1050"), Decimal("1000")))

    def test_a_meter_out_of_service_leaves_the_tree_from_its_date(self):
        setups = self.setups + [Setup(self.LAB, day(2), in_service=False)]
        result = self.allocate(self.full_day(day(1)) + self.full_day(day(2)), day(1), day(2), setups)
        self.assertNotIn(self.LAB, self.nodes(result, day(2)))
        self.assertEqual(self.nodes(result, day(2))[self.GROUND].own, Decimal("400"))
        self.assertIn(
            (IssueKind.READ_OUT_OF_SERVICE, self.LAB),
            [(i.kind, i.meter_id) for i in result.issues if i.day == day(2)],
        )

    def test_a_sub_meter_whose_parent_is_out_of_service_stands_as_a_main(self):
        setups = [s for s in self.setups if s.meter_id != self.GROUND] + [
            Setup(self.GROUND, D1, in_service=False)
        ]
        result = self.allocate(self.full_day(), setups=setups)
        self.assertIsNone(self.nodes(result)[self.LAB].parent_id)
        self.assertIn((IssueKind.PARENT_OUT_OF_SERVICE, self.LAB), self.kinds(result))

    def test_a_meter_with_no_rule_is_unassigned_and_said_to_be(self):
        result = self.allocate(self.full_day())
        self.assertIn((IssueKind.UNASSIGNED, self.KWH), self.kinds(result))

    def test_shares_that_do_not_add_to_100_still_hand_out_every_unit(self):
        split = fixed_split(shares((OIL, 30), (BEV, 30)), Decimal("90"))
        self.assertEqual(split, {OIL: Decimal("45"), BEV: Decimal("45")})


class EngineProportionalTests(SimpleTestCase):
    """Splits that follow what actually ran, or what other meters read."""

    LP, OIL_SIDEL, BEV_SIDEL, CHILLER = 10, 11, 12, 13

    def allocate(self, setups, readings, run_hours=None):
        meters = [
            Meter(self.LP, "LP-212"),
            Meter(self.OIL_SIDEL, "1/4 Sidel"),
            Meter(self.BEV_SIDEL, "Sidel"),
            Meter(self.CHILLER, "TR 125"),
        ]
        return engine.Allocator(meters, setups, readings, run_hours).allocate(D1, D1)

    def run_hours_setup(self, fallback=(), drivers=None):
        return Setup(
            self.LP,
            D1,
            basis=Basis.RUN_HOURS,
            drivers=drivers or (Driver("line:1", OIL), Driver("line:2", BEV)),
            shares=fallback,
        )

    def test_oil_ran_24_hours_and_beverages_12_so_oil_pays_two_thirds(self):
        result = self.allocate(
            [self.run_hours_setup()],
            [reading(self.LP, D1, 0, 900)],
            {("line:1", D1): Decimal("24"), ("line:2", D1): Decimal("12")},
        )
        node = result.nodes[0]
        self.assertEqual(node.split, {OIL: Decimal("600"), BEV: Decimal("300")})
        self.assertEqual(node.drivers, {OIL: Decimal("24"), BEV: Decimal("12")})

    def test_a_heavier_line_can_be_weighted(self):
        setup = self.run_hours_setup(
            drivers=(Driver("line:1", OIL, Decimal("2")), Driver("line:2", BEV))
        )
        result = self.allocate(
            [setup],
            [reading(self.LP, D1, 0, 900)],
            {("line:1", D1): Decimal("12"), ("line:2", D1): Decimal("12")},
        )
        self.assertEqual(result.nodes[0].split, {OIL: Decimal("600"), BEV: Decimal("300")})

    def test_each_line_of_a_company_counts_its_own_hours(self):
        setup = self.run_hours_setup(
            drivers=(Driver("line:1", OIL), Driver("line:3", OIL), Driver("line:2", BEV))
        )
        result = self.allocate(
            [setup],
            [reading(self.LP, D1, 0, 900)],
            {("line:1", D1): Decimal("10"), ("line:3", D1): Decimal("10"), ("line:2", D1): Decimal("10")},
        )
        self.assertEqual(result.nodes[0].split, {OIL: Decimal("600"), BEV: Decimal("300")})

    def test_nothing_ran_so_the_fixed_split_is_used_and_said_to_be(self):
        result = self.allocate(
            [self.run_hours_setup(fallback=shares((OIL, 50), (BEV, 50)))],
            [reading(self.LP, D1, 0, 900)],
        )
        node = result.nodes[0]
        self.assertTrue(node.fallback_used)
        self.assertEqual(node.split, {OIL: Decimal("450"), BEV: Decimal("450")})
        self.assertIn(IssueKind.FALLBACK, [i.kind for i in result.issues])

    def test_nothing_ran_and_no_fallback_leaves_the_units_unassigned(self):
        result = self.allocate([self.run_hours_setup()], [reading(self.LP, D1, 0, 900)])
        self.assertEqual(result.nodes[0].split, {UNASSIGNED: Decimal("900")})
        self.assertIn(IssueKind.NO_BASIS, [i.kind for i in result.issues])

    def test_a_chiller_can_follow_the_machines_it_cools(self):
        setups = [
            Setup(self.OIL_SIDEL, D1, basis=Basis.FIXED, shares=shares((OIL, 100))),
            Setup(self.BEV_SIDEL, D1, basis=Basis.FIXED, shares=shares((BEV, 100))),
            Setup(
                self.CHILLER,
                D1,
                basis=Basis.METER_RATIO,
                drivers=(Driver(f"meter:{self.OIL_SIDEL}", OIL), Driver(f"meter:{self.BEV_SIDEL}", BEV)),
            ),
        ]
        result = self.allocate(
            setups,
            [
                reading(self.OIL_SIDEL, D1, 0, 300),
                reading(self.BEV_SIDEL, D1, 0, 100),
                reading(self.CHILLER, D1, 0, 800),
            ],
        )
        chiller = next(node for node in result.nodes if node.meter_id == self.CHILLER)
        self.assertEqual(chiller.split, {OIL: Decimal("600"), BEV: Decimal("200")})

    def test_an_unread_followed_meter_counts_as_nothing_and_is_said_to_be(self):
        setups = [
            Setup(self.OIL_SIDEL, D1, basis=Basis.FIXED, shares=shares((OIL, 100))),
            Setup(self.BEV_SIDEL, D1, basis=Basis.FIXED, shares=shares((BEV, 100))),
            Setup(
                self.CHILLER,
                D1,
                basis=Basis.METER_RATIO,
                drivers=(Driver(f"meter:{self.OIL_SIDEL}", OIL), Driver(f"meter:{self.BEV_SIDEL}", BEV)),
            ),
        ]
        result = self.allocate(
            setups, [reading(self.OIL_SIDEL, D1, 0, 300), reading(self.CHILLER, D1, 0, 800)]
        )
        chiller = next(node for node in result.nodes if node.meter_id == self.CHILLER)
        self.assertEqual(chiller.split, {OIL: Decimal("800")})
        self.assertIn(IssueKind.DRIVER_NOT_READ, [i.kind for i in result.issues])


class RunHoursArithmeticTests(SimpleTestCase):
    tz = ZoneInfo("Asia/Kolkata")

    def at(self, on, hour, minute=0):
        return datetime.combine(on, time(hour, minute), tzinfo=self.tz)

    def test_a_night_shift_is_split_at_midnight(self):
        hours = hours_by_day([(self.at(day(1), 20), self.at(day(2), 8))], [day(1), day(2)], self.tz)
        self.assertEqual(hours, {day(1): Decimal("4"), day(2): Decimal("8")})

    def test_overlapping_runs_on_one_line_count_once(self):
        merged = merge([(self.at(D1, 8), self.at(D1, 12)), (self.at(D1, 10), self.at(D1, 14))])
        self.assertEqual(merged, [(self.at(D1, 8), self.at(D1, 14))])
        self.assertEqual(hours_by_day(merged, [D1], self.tz), {D1: Decimal("6")})

    def test_a_segment_left_open_for_weeks_counts_one_day_at_most(self):
        start = self.at(D1, 11)
        interval = capped(start, None, start + timedelta(days=30))
        self.assertEqual(interval[1] - interval[0], timedelta(hours=MAX_SEGMENT_HOURS))

    def test_an_open_segment_runs_until_now(self):
        start = self.at(D1, 9)
        self.assertEqual(capped(start, None, start + timedelta(hours=3)), (start, start + timedelta(hours=3)))

    def test_a_closed_segment_claiming_weeks_is_capped_too(self):
        start = self.at(D1, 9)
        interval = capped(start, start + timedelta(days=40), start + timedelta(days=90))
        self.assertEqual(interval[1] - interval[0], timedelta(hours=MAX_SEGMENT_HOURS))


# ---------------------------------------------------------------------------
# Through the API
# ---------------------------------------------------------------------------

EVERY_RIGHT = (
    "can_view_daily_electricity",
    "can_view_electricity_meter",
    "can_manage_electricity_meter",
    "can_manage_electricity_allocation",
    "can_add_daily_electricity",
    "can_edit_daily_electricity",
    "can_delete_daily_electricity",
)


class TreeFixture(TestCase):
    """KWH → ground floor (→ lab) and first floor, placed through the API."""

    def setUp(self):
        self.oil = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        self.bev = Company.objects.create(name="Jivo Beverages", code="JIVO_BEVERAGES")
        self.admin = make_user("allocator@example.com", *EVERY_RIGHT)
        self.client = client_for(self.admin)
        self.kwh = self.make_meter("KWH")
        self.ground = self.make_meter("Ground Floor", parent=self.kwh, split=[("JIVO_BEVERAGES", 100)])
        self.lab = self.make_meter(
            "Lab", parent=self.ground, split=[("JIVO_OIL", 50), ("JIVO_BEVERAGES", 50)]
        )
        self.first = self.make_meter("First Floor", parent=self.kwh, split=[("JIVO_OIL", 100)])

    def make_meter(self, name, *, parent=None, split=None, since=D1, **extra):
        placement = {"effective_from": str(since), "parent": parent.id if parent else None}
        if split:
            placement["basis"] = "FIXED"
            placement["shares"] = [{"company": code, "percent": str(pct)} for code, pct in split]
        response = self.client.post(
            METERS_URL, {"name": name, "placement": placement, **extra}, format="json"
        )
        self.assertEqual(response.status_code, 201, response.data)
        return ElectricityMeter.objects.get(pk=response.data["id"])

    def read(self, meter, on, opening, closing, **extra):
        payload = {
            "meter": meter.id,
            "date": str(on),
            "opening_reading": str(opening),
            "closing_reading": str(closing),
        }
        payload.update(extra)
        return self.client.post(READINGS_URL, payload, format="json")

    def read_day(self, on=D1, kwh=(0, 1000), ground=(0, 400), lab=(0, 40), first=(0, 500)):
        for meter, (opening, closing) in (
            (self.kwh, kwh),
            (self.ground, ground),
            (self.lab, lab),
            (self.first, first),
        ):
            response = self.read(meter, on, opening, closing)
            self.assertEqual(response.status_code, 201, response.data)

    def new_version(self, meter, since, **fields):
        payload = {"meter": meter.id, "effective_from": str(since), **fields}
        return self.client.post(SETUPS_URL, payload, format="json")


class MeterPlacementTests(TreeFixture):
    def test_a_new_meter_is_born_with_the_old_pages_flags_and_nothing_more(self):
        """A meter added here shows on the Daily Electricity page too, so it gets
        that page's main flag — and no companies: the tree's split is its own,
        and never rewrites what the old page shows."""
        self.kwh.refresh_from_db()
        self.ground.refresh_from_db()
        self.assertTrue(self.kwh.is_main)
        self.assertFalse(self.ground.is_main)
        self.assertEqual(list(self.ground.companies.all()), [])
        self.assertEqual(list(self.lab.companies.all()), [])

    def test_the_list_is_in_tree_order_with_depths(self):
        response = self.client.get(METERS_URL)
        self.assertEqual(response.status_code, 200)
        order = [(row["name"], row["tree"]["depth"]) for row in response.data]
        self.assertEqual(
            order, [("KWH", 0), ("First Floor", 1), ("Ground Floor", 1), ("Lab", 2)]
        )
        lab = response.data[-1]
        self.assertEqual(lab["tree"]["parent_name"], "Ground Floor")
        self.assertEqual(lab["tree"]["setup"]["summary"], "Jivo Oil 50% · Jivo Beverages 50%")

    def test_a_second_register_hangs_off_its_meter(self):
        response = self.client.post(
            METERS_URL, {"name": "KVAH", "register_of": self.kwh.id}, format="json"
        )
        self.assertEqual(response.status_code, 201, response.data)
        kvah = ElectricityMeter.objects.get(pk=response.data["id"])
        self.assertFalse(kvah.counts_as_supply)
        self.assertTrue(kvah.is_main)
        self.assertFalse(kvah.setups.exists())
        listed = self.client.get(METERS_URL).data
        names = [row["name"] for row in listed]
        self.assertEqual(names.index("KVAH"), names.index("KWH") + 1)
        # One level under its meter, so it never reads as the parent of the
        # meter's sub-meters.
        tree = listed[names.index("KVAH")]["tree"]
        self.assertEqual((tree["depth"], tree["parent"]), (1, self.kwh.id))

    def test_a_second_register_cannot_be_placed_in_the_tree(self):
        kvah = self.client.post(METERS_URL, {"name": "KVAH", "register_of": self.kwh.id}, format="json")
        response = self.new_version(ElectricityMeter.objects.get(pk=kvah.data["id"]), day(5))
        self.assertEqual(response.status_code, 400)

    def test_the_old_pages_flags_are_not_written_from_here(self):
        response = self.client.patch(
            f"{METERS_URL}{self.ground.id}/",
            {"is_main": True, "company_codes": ["JIVO_OIL"]},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.ground.refresh_from_db()
        self.assertFalse(self.ground.is_main)
        self.assertEqual(list(self.ground.companies.all()), [])

    def test_the_old_pages_flags_stay_as_that_page_set_them(self):
        """Moving or re-splitting a meter in the tree leaves the Daily
        Electricity page — and every board that reads it — exactly as it was."""
        self.ground.companies.set([self.oil])
        ElectricityMeter.objects.filter(pk=self.ground.pk).update(is_main=True)
        version = self.ground.setups.get()
        response = self.client.patch(
            f"{SETUPS_URL}{version.id}/",
            {"basis": "FIXED", "shares": [{"company": "JIVO_BEVERAGES", "percent": "100"}]},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.ground.refresh_from_db()
        self.assertTrue(self.ground.is_main)
        self.assertEqual([c.code for c in self.ground.companies.all()], ["JIVO_OIL"])


class SetupVersionTests(TreeFixture):
    def test_a_change_is_a_new_version_from_its_date(self):
        response = self.new_version(
            self.lab,
            day(11),
            parent=self.ground.id,
            basis="FIXED",
            shares=[{"company": "JIVO_OIL", "percent": "100"}],
            note="Lab moved to Oil's QC",
        )
        self.assertEqual(response.status_code, 201, response.data)
        versions = self.client.get(SETUPS_URL, {"meter": self.lab.id}).data
        self.assertEqual([v["effective_from"] for v in versions], [str(day(11)), str(D1)])

    def test_correcting_a_version_rewrites_it_in_place(self):
        version = self.lab.setups.get()
        response = self.client.patch(
            f"{SETUPS_URL}{version.id}/",
            {"shares": [{"company": "JIVO_OIL", "percent": "60"}, {"company": "JIVO_BEVERAGES", "percent": "40"}]},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["id"], version.id)
        self.assertEqual(response.data["summary"], "Jivo Oil 60% · Jivo Beverages 40%")
        self.assertEqual(self.lab.setups.count(), 1)

    def test_a_loop_is_refused(self):
        version = self.ground.setups.get()
        response = self.client.patch(
            f"{SETUPS_URL}{version.id}/", {"parent": self.lab.id}, format="json"
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("its own sub-meters", str(response.data))

    def test_a_loop_that_only_closes_later_is_refused_too(self):
        # From the 20th the first floor will sit under the lab. Putting the lab
        # under the first floor from the 10th is fine for ten days and a loop
        # from then on.
        self.assertEqual(
            self.new_version(self.first, day(20), parent=self.lab.id, basis="FIXED",
                             shares=[{"company": "JIVO_OIL", "percent": "100"}]).status_code,
            201,
        )
        response = self.new_version(
            self.lab, day(10), parent=self.first.id, basis="FIXED",
            shares=[{"company": "JIVO_OIL", "percent": "100"}],
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("20 Sep 2026", str(response.data))

    def test_a_parent_not_yet_in_the_tree_is_refused(self):
        later = self.make_meter("Terrace", parent=self.kwh, since=day(15))
        response = self.new_version(self.lab, day(5), parent=later.id)
        self.assertEqual(response.status_code, 400)
        self.assertIn("not in the meter tree", str(response.data))

    def test_shares_must_add_up_to_100(self):
        response = self.new_version(
            self.lab, day(5), parent=self.ground.id, basis="FIXED",
            shares=[{"company": "JIVO_OIL", "percent": "50"}, {"company": "JIVO_BEVERAGES", "percent": "40"}],
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("add up to 100", str(response.data))

    def test_a_fixed_split_needs_shares(self):
        response = self.new_version(self.lab, day(5), parent=self.ground.id, basis="FIXED")
        self.assertEqual(response.status_code, 400)

    def test_a_run_hours_split_follows_lines_and_a_meter_ratio_follows_meters(self):
        line = ProductionLine.objects.create(company=self.oil, name="10 Head")
        bad_hours = self.new_version(
            self.lab, day(5), parent=self.ground.id, basis="RUN_HOURS",
            drivers=[{"meter": self.first.id, "company": "JIVO_OIL"}],
        )
        self.assertEqual(bad_hours.status_code, 400)
        bad_ratio = self.new_version(
            self.lab, day(5), parent=self.ground.id, basis="METER_RATIO",
            drivers=[{"production_line": line.id}],
        )
        self.assertEqual(bad_ratio.status_code, 400)
        no_company = self.new_version(
            self.lab, day(5), parent=self.ground.id, basis="METER_RATIO",
            drivers=[{"meter": self.first.id}],
        )
        self.assertEqual(no_company.status_code, 400)
        good = self.new_version(
            self.lab, day(5), parent=self.ground.id, basis="RUN_HOURS",
            drivers=[{"production_line": line.id}],
            shares=[{"company": "JIVO_OIL", "percent": "50"}, {"company": "JIVO_BEVERAGES", "percent": "50"}],
        )
        self.assertEqual(good.status_code, 201, good.data)
        self.assertEqual(good.data["drivers"][0]["company"], "JIVO_OIL")
        self.assertEqual(good.data["summary"], "By run hours of 10 Head (Jivo Oil); otherwise Jivo Oil 50% · Jivo Beverages 50%")

    def test_a_meter_cannot_leave_the_tree_while_sub_meters_hang_off_it(self):
        response = self.new_version(self.ground, day(10), in_service=False)
        self.assertEqual(response.status_code, 400)
        self.assertIn("Lab", str(response.data))
        # Move the lab first, and then it may go.
        self.assertEqual(
            self.new_version(self.lab, day(10), parent=self.kwh.id, basis="FIXED",
                             shares=[{"company": "JIVO_OIL", "percent": "100"}]).status_code,
            201,
        )
        self.assertEqual(self.new_version(self.ground, day(10), in_service=False).status_code, 201)

    def test_the_only_version_cannot_be_deleted_and_a_later_one_can(self):
        only = self.lab.setups.get()
        self.assertEqual(self.client.delete(f"{SETUPS_URL}{only.id}/").status_code, 400)
        later = self.new_version(self.lab, day(10), parent=self.ground.id, basis="FIXED",
                                 shares=[{"company": "JIVO_OIL", "percent": "100"}])
        self.assertEqual(self.client.delete(f"{SETUPS_URL}{later.data['id']}/").status_code, 204)
        self.assertEqual(self.lab.setups.count(), 1)

    def test_two_versions_cannot_start_on_one_day(self):
        response = self.new_version(self.lab, D1, parent=self.ground.id)
        self.assertEqual(response.status_code, 400)
        self.assertIn("already has a version", str(response.data))


class AllocationRightsTests(TreeFixture):
    def test_a_meter_keeper_cannot_change_who_pays(self):
        keeper = make_user(
            "keeper@example.com",
            "can_view_daily_electricity",
            "can_view_electricity_meter",
            "can_manage_electricity_meter",
            "can_add_daily_electricity",
        )
        UserElectricityMeter.objects.create(user=keeper, meter=self.lab)
        response = client_for(keeper).patch(
            f"{SETUPS_URL}{self.lab.setups.get().id}/",
            {"shares": [{"company": "JIVO_OIL", "percent": "100"}]},
            format="json",
        )
        self.assertEqual(response.status_code, 403)

    def test_a_keeper_may_add_a_meter_but_not_decide_its_split(self):
        keeper = make_user("keeper2@example.com", "can_view_electricity_meter", "can_manage_electricity_meter")
        client = client_for(keeper)
        refused = client.post(
            METERS_URL,
            {"name": "Labeler", "placement": {"parent": self.ground.id, "effective_from": str(D1),
                                              "basis": "FIXED", "shares": [{"company": "JIVO_BEVERAGES", "percent": "100"}]}},
            format="json",
        )
        self.assertEqual(refused.status_code, 403)
        placed = client.post(
            METERS_URL,
            {"name": "Labeler", "placement": {"parent": self.ground.id, "effective_from": str(D1)}},
            format="json",
        )
        self.assertEqual(placed.status_code, 201, placed.data)
        self.assertEqual(placed.data["tree"]["setup"]["basis"], "UNASSIGNED")
        self.assertEqual(placed.data["tree"]["parent_name"], "Ground Floor")

    def test_anyone_on_the_register_can_read_the_split(self):
        viewer = make_user("viewer@example.com", "can_view_daily_electricity")
        self.assertEqual(client_for(viewer).get(SPLIT_URL).status_code, 200)
        stranger = make_user("stranger@example.com")
        self.assertEqual(client_for(stranger).get(SPLIT_URL).status_code, 403)


class ReadingChainTests(TreeFixture):
    def test_the_opening_is_carried_from_the_previous_closing(self):
        self.assertEqual(self.read(self.first, day(1), 0, 100).status_code, 201)
        response = self.client.post(
            READINGS_URL, {"meter": self.first.id, "date": str(day(2)), "closing_reading": "180"}, format="json"
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(Decimal(response.data["opening_reading"]), Decimal("100"))

    def test_an_opening_that_does_not_follow_on_is_refused(self):
        self.read(self.first, day(1), 0, 100)
        response = self.read(self.first, day(2), 150, 200)
        self.assertEqual(response.status_code, 400)
        self.assertIn("previous closing", str(response.data["opening_reading"]))

    def test_a_reset_meter_may_start_from_a_new_number(self):
        self.read(self.first, day(1), 0, 100)
        response = self.read(self.first, day(2), 5, 60, meter_reset=True)
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(Decimal(response.data["units_consumed"]), Decimal("55"))

    def test_correcting_a_closing_moves_the_next_opening(self):
        first_day = self.read(self.first, day(1), 0, 100)
        self.read(self.first, day(2), 100, 180)
        response = self.client.patch(
            f"{READINGS_URL}{first_day.data['id']}/", {"closing_reading": "120"}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.data)
        second = DailyElectricityReading.objects.get(meter=self.first, date=day(2))
        self.assertEqual((second.opening_reading, second.units_consumed), (Decimal("120"), Decimal("60")))

    def test_a_closing_cannot_overtake_the_next_reading(self):
        first_day = self.read(self.first, day(1), 0, 100)
        self.read(self.first, day(2), 100, 180)
        response = self.client.patch(
            f"{READINGS_URL}{first_day.data['id']}/", {"closing_reading": "200"}, format="json"
        )
        self.assertEqual(response.status_code, 400)

    def test_filling_in_a_skipped_day_takes_it_out_of_the_next_reading(self):
        self.read(self.first, day(1), 0, 100)
        self.read(self.first, day(3), 100, 300)
        response = self.client.post(
            READINGS_URL, {"meter": self.first.id, "date": str(day(2)), "closing_reading": "180"}, format="json"
        )
        self.assertEqual(response.status_code, 201, response.data)
        third = DailyElectricityReading.objects.get(meter=self.first, date=day(3))
        self.assertEqual((third.opening_reading, third.units_consumed), (Decimal("180"), Decimal("120")))

    def test_deleting_a_reading_hands_its_days_to_the_next(self):
        self.read(self.first, day(1), 0, 100)
        middle = self.read(self.first, day(2), 100, 180)
        self.read(self.first, day(3), 180, 250)
        self.assertEqual(self.client.delete(f"{READINGS_URL}{middle.data['id']}/").status_code, 204)
        third = DailyElectricityReading.objects.get(meter=self.first, date=day(3))
        self.assertEqual((third.opening_reading, third.units_consumed), (Decimal("100"), Decimal("150")))

    def test_a_reading_cannot_be_moved(self):
        created = self.read(self.first, day(1), 0, 100)
        response = self.client.patch(
            f"{READINGS_URL}{created.data['id']}/", {"date": str(day(2))}, format="json"
        )
        self.assertEqual(response.status_code, 400)

    def test_a_meter_out_of_service_takes_no_reading(self):
        self.assertEqual(self.new_version(self.lab, day(5), in_service=False).status_code, 201)
        response = self.read(self.lab, day(6), 0, 10)
        self.assertEqual(response.status_code, 400)
        self.assertIn("out of service", str(response.data))

    def test_history_with_a_broken_opening_can_still_be_corrected(self):
        self.read(self.first, day(1), 0, 100)
        broken = DailyElectricityReading.objects.create(
            meter=self.first, date=day(2), opening_reading=Decimal("150"), closing_reading=Decimal("200"),
        )
        response = self.client.patch(f"{READINGS_URL}{broken.id}/", {"remarks": "checked"}, format="json")
        self.assertEqual(response.status_code, 200, response.data)


class DaySheetTests(TreeFixture):
    def test_the_sheet_lists_every_meter_in_tree_order_with_its_previous_closing(self):
        self.read_day(day(1))
        sheet = self.client.get(SHEET_URL, {"date": str(day(2))})
        self.assertEqual(sheet.status_code, 200)
        rows = sheet.data["rows"]
        self.assertEqual([row["name"] for row in rows], ["KWH", "First Floor", "Ground Floor", "Lab"])
        self.assertEqual([row["depth"] for row in rows], [0, 1, 1, 2])
        self.assertEqual(rows[0]["previous"]["closing_reading"], "1000.00")
        self.assertIsNone(rows[0]["reading"])
        self.assertEqual(rows[3]["split"], "Jivo Oil 50% · Jivo Beverages 50%")

    def test_the_sheet_saves_all_or_nothing(self):
        self.read_day(day(1))
        response = self.client.post(
            SHEET_URL,
            {
                "date": str(day(2)),
                "entries": [
                    {"meter": self.kwh.id, "closing_reading": "1900"},
                    {"meter": self.first.id, "closing_reading": "400"},  # below its 500 opening
                ],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn(str(self.first.id), {str(key) for key in response.data["errors"]})
        self.assertFalse(DailyElectricityReading.objects.filter(date=day(2)).exists())

    def test_saving_the_sheet_creates_and_corrects_in_one_go(self):
        self.read_day(day(1))
        self.read(self.kwh, day(2), 1000, 1800)
        response = self.client.post(
            SHEET_URL,
            {
                "date": str(day(2)),
                "entries": [
                    {"meter": self.kwh.id, "closing_reading": "1900"},
                    {"meter": self.first.id, "closing_reading": "900"},
                ],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual((response.data["created"], response.data["updated"]), (1, 1))
        kwh = DailyElectricityReading.objects.get(meter=self.kwh, date=day(2))
        self.assertEqual(kwh.units_consumed, Decimal("900"))
        first = DailyElectricityReading.objects.get(meter=self.first, date=day(2))
        self.assertEqual(first.opening_reading, Decimal("500"))

    def test_correcting_needs_the_edit_right(self):
        self.read_day(day(1))
        operator = make_user("operator@example.com", "can_view_daily_electricity", "can_add_daily_electricity")
        UserElectricityMeter.objects.create(user=operator, meter=self.kwh)
        response = client_for(operator).post(
            SHEET_URL,
            {"date": str(day(1)), "entries": [{"meter": self.kwh.id, "closing_reading": "1100"}]},
            format="json",
        )
        self.assertEqual(response.status_code, 403)

    def test_a_keeper_may_save_only_the_meters_he_keeps(self):
        self.read_day(day(1))
        keeper = make_user("keeper3@example.com", "can_view_daily_electricity", "can_add_daily_electricity")
        UserElectricityMeter.objects.create(user=keeper, meter=self.kwh)
        response = client_for(keeper).post(
            SHEET_URL,
            {
                "date": str(day(2)),
                "entries": [
                    {"meter": self.kwh.id, "closing_reading": "1900"},
                    {"meter": self.first.id, "closing_reading": "900"},
                ],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(DailyElectricityReading.objects.filter(date=day(2)).exists())


class SplitReportTests(TreeFixture):
    def split(self, date_from=D1, date_to=D1):
        response = self.client.get(SPLIT_URL, {"date_from": str(date_from), "date_to": str(date_to)})
        self.assertEqual(response.status_code, 200, response.data)
        return response.data

    def by_party(self, data):
        return {row["party"]: row for row in data["totals"]["by_party"]}

    def test_the_split_adds_up_to_the_supply(self):
        self.read_day(D1)
        data = self.split()
        parties = self.by_party(data)
        self.assertEqual(parties[BEV]["units"], "380.00")
        self.assertEqual(parties[OIL]["units"], "520.00")
        self.assertEqual(parties[UNASSIGNED]["units"], "100.00")
        self.assertEqual(data["totals"]["supply_units"], "1000.00")
        self.assertEqual(data["totals"]["allocated_units"], "1000.00")
        ground = next(row for row in data["meters"] if row["name"] == "Ground Floor")
        self.assertEqual((ground["units"], ground["sub_metered_units"], ground["own_units"]),
                         ("400.00", "40.00", "360.00"))
        unassigned = [issue for issue in data["issues"] if issue["kind"] == "UNASSIGNED"]
        self.assertEqual(unassigned[0]["meter_name"], "KWH")

    def test_an_unread_meter_is_named_with_where_its_load_went(self):
        for meter, closing in ((self.kwh, 1000), (self.ground, 400), (self.first, 500)):
            self.read(meter, D1, 0, closing)
        issue = next(i for i in self.split()["issues"] if i["kind"] == "NOT_READ")
        self.assertEqual(issue["meter_name"], "Lab")
        self.assertIn("rest of Ground Floor", issue["message"])

    def test_run_hours_come_from_the_production_segments(self):
        oil_line = ProductionLine.objects.create(company=self.oil, name="10 Head")
        bev_line = ProductionLine.objects.create(company=self.bev, name="Sidel")
        tz = timezone.get_current_timezone()

        def ran(line, start, end, number):
            run = ProductionRun.objects.create(company=line.company, run_number=number, date=start.date(), line=line)
            ProductionSegment.objects.create(production_run=run, start_time=start, end_time=end, is_active=False)

        # Oil ran the whole day (a night shift into it and a day shift);
        # Beverages ran twelve hours.
        ran(oil_line, datetime.combine(D1 - timedelta(days=1), time(20), tzinfo=tz),
            datetime.combine(D1, time(8), tzinfo=tz), 1)
        ran(oil_line, datetime.combine(D1, time(8), tzinfo=tz), datetime.combine(D1 + timedelta(days=1), time(0), tzinfo=tz), 2)
        ran(bev_line, datetime.combine(D1, time(8), tzinfo=tz), datetime.combine(D1, time(20), tzinfo=tz), 3)

        lp = self.make_meter("LP-212", parent=self.kwh)
        version = lp.setups.get()
        response = self.client.patch(
            f"{SETUPS_URL}{version.id}/",
            {"basis": "RUN_HOURS", "drivers": [{"production_line": oil_line.id}, {"production_line": bev_line.id}]},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.read(lp, D1, 0, 900)

        row = next(row for row in self.split()["meters"] if row["name"] == "LP-212")
        split = {item["party"]: item["units"] for item in row["split"]}
        self.assertEqual(split, {OIL: "600.00", BEV: "300.00"})
        self.assertEqual({d["party"]: d["amount"] for d in row["drivers"]}, {OIL: "24.0", BEV: "12.0"})

    def test_blowing_machines_can_drive_a_split(self):
        machine = BlowingMachine.objects.create(company=self.oil, name="Synergy 1/4")
        spec = PreformSpec.objects.create(company=self.oil, make="Frystal", gram=Decimal("18"), preforms_per_box=1000)
        run = BlowingRun.objects.create(company=self.oil, run_number=1, date=D1, machine=machine, preform_spec=spec)
        tz = timezone.get_current_timezone()
        BlowingSegment.objects.create(
            blowing_run=run,
            start_time=datetime.combine(D1, time(9), tzinfo=tz),
            end_time=datetime.combine(D1, time(15), tzinfo=tz),
        )
        chiller = self.make_meter("TR 40", parent=self.kwh)
        response = self.client.patch(
            f"{SETUPS_URL}{chiller.setups.get().id}/",
            {"basis": "RUN_HOURS", "drivers": [{"blowing_machine": machine.id}]},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.read(chiller, D1, 0, 300)
        row = next(row for row in self.split()["meters"] if row["name"] == "TR 40")
        self.assertEqual([(s["party"], s["units"]) for s in row["split"]], [(OIL, "300.00")])

    def test_an_unplaced_meter_is_still_counted_and_named(self):
        stray = ElectricityMeter.objects.create(name="Admin Block")
        DailyElectricityReading.objects.create(
            meter=stray, date=D1, opening_reading=Decimal("0"), closing_reading=Decimal("25"),
            rate_per_unit=Decimal("9"),
        )
        data = self.split()
        self.assertEqual([m["name"] for m in data["unplaced_meters"]], ["Admin Block"])
        row = next(row for row in data["meters"] if row["name"] == "Admin Block")
        self.assertTrue(row["unplaced"])
        self.assertEqual(row["split"][0]["party"], UNASSIGNED)

    def test_each_meter_is_charted_day_by_day_per_party(self):
        self.read_day(D1)
        data = self.split()
        ground = next(m for m in data["meters"] if m["name"] == "Ground Floor")
        series = [row for row in data["meter_daily"] if row["meter_id"] == ground["id"]]
        self.assertEqual(series, [{"meter_id": ground["id"], "party": BEV, "units": ["360.00"]}])

    def test_a_day_nobody_entered_does_not_count_against_a_meter(self):
        self.read_day(D1)
        data = self.split(D1, day(2))
        ground = next(m for m in data["meters"] if m["name"] == "Ground Floor")
        self.assertEqual((ground["days_read"], ground["days_in_service"]), (1, 1))
        self.assertEqual(data["entered_days"], 1)
        self.assertIn("DAY_NOT_ENTERED", [issue["kind"] for issue in data["issues"]])

    def test_a_second_register_sits_under_its_meter_with_the_ratio(self):
        created = self.client.post(METERS_URL, {"name": "KVAH", "register_of": self.kwh.id}, format="json")
        self.read(ElectricityMeter.objects.get(pk=created.data["id"]), D1, 0, 1040)
        self.read_day(D1)
        rows = self.split()["meters"]
        names = [row["name"] for row in rows]
        register = rows[names.index("KVAH")]
        self.assertEqual(names.index("KVAH"), names.index("KWH") + 1)
        self.assertEqual((register["depth"], register["parent_id"]), (1, self.kwh.id))
        self.assertEqual(register["ratio"], "0.962")

    def test_a_break_says_what_fell_between_and_what_was_counted_twice(self):
        self.read(self.first, day(1), 0, 100)
        # The next opening skips 50 units; the one after starts 20 back.
        for on, opening, closing in ((day(2), "150", "200"), (day(3), "180", "250")):
            DailyElectricityReading.objects.create(
                meter=self.first, date=on, opening_reading=Decimal(opening),
                closing_reading=Decimal(closing), rate_per_unit=Decimal("9"),
            )
        issue = next(i for i in self.split(day(1), day(3))["issues"] if i["kind"] == "BREAK")
        self.assertIn("50.00 units fell into no reading at all", issue["message"])
        self.assertIn("20.00 units were counted twice", issue["message"])

    def test_a_year_at_most_in_one_request(self):
        response = self.client.get(SPLIT_URL, {"date_from": "2025-01-01", "date_to": "2026-09-01"})
        self.assertEqual(response.status_code, 400)


class RunSourcesTests(TreeFixture):
    def test_lines_and_blowing_machines_are_offered_with_their_company(self):
        ProductionLine.objects.create(company=self.bev, name="Sidel")
        BlowingMachine.objects.create(company=self.oil, name="Synergy 1/4")
        rows = self.client.get(SOURCES_URL).data
        self.assertCountEqual(
            [(r["kind"], r["name"], r["company"]) for r in rows],
            [("LINE", "Sidel", "JIVO_BEVERAGES"), ("BLOWING_MACHINE", "Synergy 1/4", "JIVO_OIL")],
        )


class SeedCommandTests(TestCase):
    """seed_electricity_tree places the campus's meters the way the factory said."""

    def setUp(self):
        self.oil = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        self.bev = Company.objects.create(name="Jivo Beverages", code="JIVO_BEVERAGES")
        # KWH and KVAH are both mains on the Daily Electricity page, as on live.
        for name, first in (("KWH", date(2026, 8, 22)), ("KVAH", date(2026, 8, 22)),
                            ("LP-196", D1), ("HP-512", D1)):
            meter = ElectricityMeter.objects.create(
                name=name, rate_per_unit=Decimal("9"), is_main=name in ("KWH", "KVAH")
            )
            DailyElectricityReading.objects.create(
                meter=meter, date=first, opening_reading=Decimal("0"),
                closing_reading=Decimal("100"), rate_per_unit=Decimal("9"),
            )

    def seed(self, **options):
        from io import StringIO

        from django.core.management import call_command

        out = StringIO()
        call_command(
            "seed_electricity_tree", start="2026-09-25", hp512_moved_on="2026-09-23",
            stdout=out, **options,
        )
        return out.getvalue()

    def test_a_dry_run_saves_nothing(self):
        plan = self.seed()
        self.assertIn("dry run", plan)
        self.assertFalse(ElectricityMeterSetup.objects.exists())
        self.assertFalse(ElectricityMeter.objects.filter(name="Gov Main").exists())

    def test_the_tree_is_placed_as_described(self):
        self.seed(apply=True)
        kwh = ElectricityMeter.objects.get(name="KWH")
        # The register's "LP-196" is really LP-212: half each, off the main
        # meter. It keeps the name the Daily Electricity page knows it by.
        lp = ElectricityMeter.objects.get(name="LP-196")
        self.assertFalse(ElectricityMeter.objects.filter(name="LP-212").exists())
        version = lp.setups.get()
        self.assertEqual((version.parent_id, version.effective_from), (kwh.id, D1))
        self.assertEqual(sorted(s.percent for s in version.shares.all()), [Decimal("50"), Decimal("50")])
        # KVAH is the main meter's second register, never in the tree.
        kvah = ElectricityMeter.objects.get(name="KVAH")
        self.assertEqual(kvah.register_of_id, kwh.id)
        self.assertFalse(kvah.setups.exists())
        self.assertFalse(kvah.counts_as_supply)

    def test_hp512_is_under_kwh_until_it_moved_onto_gov_main(self):
        self.seed(apply=True)
        gov = ElectricityMeter.objects.get(name="Gov Main")
        self.assertEqual(gov.setups.get().effective_from, date(2026, 9, 23))
        versions = list(
            ElectricityMeter.objects.get(name="HP-512").setups.order_by("effective_from")
            .values_list("effective_from", "parent__name")
        )
        self.assertEqual(versions, [(D1, "KWH"), (date(2026, 9, 23), "Gov Main")])

    def test_a_second_run_changes_nothing(self):
        self.seed(apply=True)
        before = ElectricityMeterSetup.objects.count()
        plan = self.seed(apply=True)
        self.assertEqual(ElectricityMeterSetup.objects.count(), before)
        self.assertIn("already placed", plan)

