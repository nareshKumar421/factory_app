"""
admin_board/tests.py

The alert rules, and the two arithmetic decisions that are easy to get wrong.

These are pure-function tests over a board dict. Nothing here touches HANA or
the database, because the part worth testing is the JUDGEMENT — which figures
raise an alarm and which do not — and that is exactly the part a live-data test
cannot pin down, since live data changes underneath it.
"""

from datetime import date
from decimal import Decimal
from unittest import mock

from django.test import SimpleTestCase, TestCase

from accounts.models import Department
from company.models import Company

from .services import AdminBoardService

from . import exim_reader, tonnage

from .alerts import (
    AUDIT_STALE_DAYS,
    PLAN_CRITICAL_GAP_PCT,
    PLAN_WARNING_GAP_PCT,
    WAREHOUSE_CRITICAL_PCT,
    build_alerts,
)


def board(**overrides):
    """A board with everything healthy. Tests break one thing at a time."""
    base = {
        "output": {
            "production": {
                "mtd_tons": 2200.0,
                "plan_tons": 4320.0,
                "plan_pct": 50.9,
                "required_tons_per_day": 141.3,
                "avg_tons_per_producing_day": 157.1,
            },
            "dispatch": {"mtd_tons": 940.0},
        },
        "storage": {
            "fg": {
                "total_tons": 846.0,
                "rows": [
                    {
                        "warehouse": "BH-BT",
                        "label": "BH-BT",
                        "tons": 300.0,
                        "capacity_tons": 502.0,
                        "used_pct": 59.8,
                        "free_tons": 202.0,
                        "last_audit_date": "2026-09-10",
                    },
                ],
                "unrated": [],
            },
            "pm": {"used_pct": 12.0, "no_capacity_reason": None},
            "oil": {"used_pct": 40.0, "no_capacity_reason": None},
        },
        "cost": {
            "total": 2_658_138.0,
            "slices": [
                {"key": "labour", "label": "Labour", "amount": 671_400.0, "has_source": True, "warning": None},
                {"key": "electricity", "label": "Electricity", "amount": 1_986_738.0, "has_source": True, "warning": None},
            ],
            "warnings": [],
        },
        "meta": {"period": {"elapsed_pct": 50.0}},
    }
    base.update(overrides)
    return base


def keys(alerts):
    return {alert["key"] for alert in alerts}


def find(alerts, key):
    return next((alert for alert in alerts if alert["key"] == key), None)


class HealthyBoardTests(SimpleTestCase):
    def test_a_plant_doing_well_raises_nothing(self):
        self.assertEqual(build_alerts(board(), today=date(2026, 9, 15)), [])


class DegradationTests(SimpleTestCase):
    """The most important behaviour in this module."""

    def test_a_tile_that_could_not_be_read_raises_no_alert_at_all(self):
        # NOT a warning, NOT an "unknown" — nothing. A green all-clear derived
        # from having learned nothing is the one failure that would make this
        # board actively dangerous, so every rule guards on its section.
        blind = board(
            output={"production": None, "dispatch": None},
            storage={"fg": None, "pm": None, "oil": None},
            cost=None,
        )
        self.assertEqual(build_alerts(blind, today=date(2026, 9, 15)), [])

    def test_one_dead_tile_does_not_silence_the_others(self):
        partial = board(output={"production": None, "dispatch": None})
        partial["storage"]["fg"]["rows"][0]["used_pct"] = 95.0
        alerts = build_alerts(partial, today=date(2026, 9, 15))
        self.assertIn("storage.full.BH-BT", keys(alerts))
        self.assertNotIn("production.behind_plan", keys(alerts))


class ProductionPlanTests(SimpleTestCase):
    def test_fractionally_behind_is_not_an_alert(self):
        # An alert that fires every month is an alert nobody reads. Output
        # arrives in lumpy batches against an evenly-spread plan, so a small
        # gap is the normal state for most days of most months.
        data = board()
        data["output"]["production"]["plan_pct"] = 49.0  # 1 point behind
        alerts = build_alerts(data, today=date(2026, 9, 15))
        self.assertNotIn("production.behind_plan", keys(alerts))

    def test_the_warning_floor_is_where_the_constant_says(self):
        for gap, expected in ((PLAN_WARNING_GAP_PCT - 0.1, None), (PLAN_WARNING_GAP_PCT, "warning")):
            data = board()
            data["output"]["production"]["plan_pct"] = 50.0 - gap
            alert = find(build_alerts(data, today=date(2026, 9, 15)), "production.behind_plan")
            if expected is None:
                self.assertIsNone(alert, f"at a {gap} point gap")
            else:
                self.assertEqual(alert["severity"], expected, f"at a {gap} point gap")

    def test_behind_by_less_than_the_critical_gap_is_a_warning(self):
        data = board()
        data["output"]["production"]["plan_pct"] = 50.0 - (PLAN_CRITICAL_GAP_PCT - 1)
        alert = find(build_alerts(data, today=date(2026, 9, 15)), "production.behind_plan")
        self.assertEqual(alert["severity"], "warning")

    def test_half_a_month_behind_is_critical_and_states_the_rate_needed(self):
        data = board()
        data["output"]["production"].update(
            {"mtd_tons": 1190.7, "plan_pct": 27.6, "required_tons_per_day": 208.6,
             "avg_tons_per_producing_day": 85.0}
        )
        alert = find(build_alerts(data, today=date(2026, 9, 15)), "production.behind_plan")
        self.assertEqual(alert["severity"], "critical")
        # The gap alone is not actionable; the rate needed to close it is.
        self.assertIn("208.6 T/day", alert["detail"])
        self.assertIn("85.0 T actual", alert["detail"])

    def test_ahead_of_plan_raises_nothing(self):
        data = board()
        data["output"]["production"]["plan_pct"] = 80.0
        self.assertNotIn("production.behind_plan", keys(build_alerts(data, today=date(2026, 9, 15))))

    def test_no_plan_filed_is_the_planner_s_problem_not_the_floor_s(self):
        data = board()
        data["output"]["production"]["plan_pct"] = None
        alerts = build_alerts(data, today=date(2026, 9, 15))
        self.assertIn("production.no_plan", keys(alerts))
        self.assertNotIn("production.behind_plan", keys(alerts))


class WarehouseTests(SimpleTestCase):
    def test_a_full_store_names_where_the_room_is(self):
        # "Shift stock" with nowhere named is not an instruction.
        data = board()
        data["storage"]["fg"]["rows"] = [
            {"warehouse": "BH-BT", "label": "BH-BT", "tons": 450.0, "capacity_tons": 502.0,
             "used_pct": 90.4, "free_tons": 52.0, "last_audit_date": None},
            {"warehouse": "GP-FGM", "label": "Gupta", "tons": 402.0, "capacity_tons": 1120.0,
             "used_pct": 35.9, "free_tons": 718.0, "last_audit_date": None},
        ]
        alert = find(build_alerts(data, today=date(2026, 9, 15)), "storage.full.BH-BT")
        self.assertEqual(alert["severity"], "critical")
        self.assertIn("Gupta", alert["detail"])
        self.assertIn("718.0 T", alert["detail"])

    def test_the_emptier_store_is_not_itself_reported_as_full(self):
        data = board()
        data["storage"]["fg"]["rows"] = [
            {"warehouse": "BH-BT", "label": "BH-BT", "tons": 450.0, "capacity_tons": 502.0,
             "used_pct": 90.4, "free_tons": 52.0, "last_audit_date": None},
            {"warehouse": "GP-FGM", "label": "Gupta", "tons": 402.0, "capacity_tons": 1120.0,
             "used_pct": 35.9, "free_tons": 718.0, "last_audit_date": None},
        ]
        self.assertNotIn("storage.full.GP-FGM", keys(build_alerts(data, today=date(2026, 9, 15))))

    def test_the_critical_threshold_is_where_the_constant_says(self):
        for used, expected in ((WAREHOUSE_CRITICAL_PCT - 0.1, "warning"), (WAREHOUSE_CRITICAL_PCT, "critical")):
            data = board()
            data["storage"]["fg"]["rows"][0]["used_pct"] = used
            alert = find(build_alerts(data, today=date(2026, 9, 15)), "storage.full.BH-BT")
            self.assertEqual(alert["severity"], expected, f"at {used}%")

    def test_an_unrated_store_is_never_reported_as_full(self):
        # No denominator means no condition. This is the rule that stops an
        # unrated warehouse reading as an empty one.
        data = board()
        data["storage"]["fg"]["rows"][0].update({"used_pct": None, "capacity_tons": None})
        self.assertNotIn("storage.full.BH-BT", keys(build_alerts(data, today=date(2026, 9, 15))))

    def test_stock_outside_the_rating_is_named_rather_than_absorbed(self):
        data = board()
        data["storage"]["fg"]["unrated"] = [
            {"warehouse": "GP-FG", "label": "GP-FG (Oil, Gupta basement)", "tons": 148.3}
        ]
        alert = find(build_alerts(data, today=date(2026, 9, 15)), "storage.unrated_stock.GP-FG")
        self.assertIsNotNone(alert)
        self.assertIn("148.3 T", alert["title"])

    def test_a_stale_stock_check_is_chased_and_a_fresh_one_is_not(self):
        fresh = board()
        fresh["storage"]["fg"]["rows"][0]["last_audit_date"] = "2026-09-10"
        self.assertNotIn("storage.audit.BH-BT", keys(build_alerts(fresh, today=date(2026, 9, 15))))

        stale = board()
        stale["storage"]["fg"]["rows"][0]["last_audit_date"] = "2026-08-02"
        alert = find(build_alerts(stale, today=date(2026, 9, 15)), "storage.audit.BH-BT")
        self.assertIn("44 days ago", alert["title"])

    def test_the_audit_rule_survives_a_date_it_cannot_parse(self):
        data = board()
        data["storage"]["fg"]["rows"][0]["last_audit_date"] = "not-a-date"
        build_alerts(data, today=date(2026, 9, 15))  # must not raise

    def test_unrated_capacity_is_one_alert_naming_both(self):
        data = board()
        data["storage"]["pm"] = {"used_pct": None, "no_capacity_reason": "Rated in sq ft only."}
        data["storage"]["oil"] = {"used_pct": None, "no_capacity_reason": "No rating exists."}
        alerts = [a for a in build_alerts(data, today=date(2026, 9, 15)) if a["key"] == "storage.unrated"]
        self.assertEqual(len(alerts), 1)
        # Sentence-cased by hand: `.capitalize()` would make this "Pm stores".
        self.assertTrue(alerts[0]["title"].startswith("PM stores"), alerts[0]["title"])
        self.assertIn("oil tanks", alerts[0]["title"])


class CostTests(SimpleTestCase):
    def test_a_line_that_is_genuinely_nil_is_not_an_alert(self):
        # Zero with a reason is a gap; zero without one is a real nil.
        data = board()
        data["cost"]["slices"].append(
            {"key": "maintenance", "label": "Maintenance", "amount": 0.0,
             "has_source": True, "warning": None}
        )
        self.assertNotIn("cost.unsourced", keys(build_alerts(data, today=date(2026, 9, 15))))

    def test_one_unsourced_line_warns_and_two_are_critical(self):
        one = board()
        one["cost"]["slices"].append(
            {"key": "salary", "label": "Salary", "amount": 0.0, "has_source": False,
             "warning": "No 'factory-salary' rate in force."}
        )
        self.assertEqual(find(build_alerts(one, today=date(2026, 9, 15)), "cost.unsourced")["severity"], "warning")

        two = board()
        two["cost"]["slices"] += [
            {"key": "salary", "label": "Salary", "amount": 0.0, "has_source": False, "warning": "No rate."},
            {"key": "maintenance", "label": "Maintenance", "amount": 0.0, "has_source": False, "warning": "No entry."},
        ]
        alert = find(build_alerts(two, today=date(2026, 9, 15)), "cost.unsourced")
        self.assertEqual(alert["severity"], "critical")
        self.assertIn("salary", alert["detail"])
        self.assertIn("maintenance", alert["detail"])

    def test_the_wall_board_s_own_warnings_come_through_once(self):
        data = board()
        data["cost"]["warnings"] = ["408 labourers have no rate in this period."]
        alerts = build_alerts(data, today=date(2026, 9, 15))
        carried = [a for a in alerts if a["detail"] == "408 labourers have no rate in this period."]
        self.assertEqual(len(carried), 1)

    def test_a_warning_already_shown_on_an_unsourced_slice_is_not_repeated(self):
        data = board()
        warning = "No 'factory-salary' rate in force."
        data["cost"]["slices"].append(
            {"key": "salary", "label": "Salary", "amount": 0.0, "has_source": False, "warning": warning}
        )
        data["cost"]["warnings"] = [warning]
        alerts = build_alerts(data, today=date(2026, 9, 15))
        self.assertEqual(len([a for a in alerts if a["detail"] == warning]), 0)
        self.assertIn("cost.unsourced", keys(alerts))


class OrderingTests(SimpleTestCase):
    def test_critical_alerts_come_first(self):
        data = board()
        data["output"]["production"]["plan_pct"] = 20.0  # critical
        data["storage"]["fg"]["rows"][0]["last_audit_date"] = "2026-07-01"  # warning
        severities = [alert["severity"] for alert in build_alerts(data, today=date(2026, 9, 15))]
        self.assertEqual(severities, sorted(severities, key=lambda s: {"critical": 0, "warning": 1, "info": 2}[s]))


class ConstantsTests(SimpleTestCase):
    def test_the_thresholds_the_front_end_mirrors(self):
        # `utils/format.ts` hard-codes 90 and 80 in `fillCondition`, so a tile
        # wearing the bad tint is a tile this module is also shouting about.
        # If these move, that function moves with them.
        self.assertEqual(WAREHOUSE_CRITICAL_PCT, 90.0)
        self.assertEqual(PLAN_CRITICAL_GAP_PCT, 10.0)
        self.assertLess(PLAN_WARNING_GAP_PCT, PLAN_CRITICAL_GAP_PCT)
        self.assertEqual(AUDIT_STALE_DAYS, 30)


class TonnageTests(SimpleTestCase):
    """The formula the Logistics board uses, and the two traps in it."""

    def row(self, **kw):
        base = {
            "item_code": "FG1", "on_hand": 1000.0, "uom": "PCS",
            "pieces_per_box": 20.0, "gross_weight_per_case": 10.0,
            "litres_per_piece": 1.0,
        }
        base.update(kw)
        return base

    def test_weight_is_per_case_over_pieces_per_case(self):
        # 1000 pieces / 20 per case = 50 cases x 10 kg = 500 kg.
        # SAP's own procedure multiplies case weight by PIECE count and answers
        # 10,000 kg -- twenty times too much.
        self.assertEqual(tonnage.row_kilograms(self.row()), 500.0)

    def test_a_pack_factor_of_one_is_not_an_error(self):
        # It means the SKU is sold by the piece, so one piece IS one case.
        self.assertEqual(tonnage.row_kilograms(self.row(pieces_per_box=1.0)), 10_000.0)

    def test_a_mass_or_volume_row_is_never_weighed_by_case(self):
        # On a KG- or LTR-stocked row the on-hand figure is already a mass or a
        # volume; dividing it by a pack factor means nothing.
        for uom in ("KG", "LTR", "MT", "ML"):
            self.assertIsNone(tonnage.row_kilograms(self.row(uom=uom)), uom)

    def test_a_blank_unit_is_not_assumed_to_be_pieces(self):
        # SAP leaves InvntryUom empty often enough that guessing would fold
        # unweighable rows into a total silently.
        self.assertIsNone(tonnage.row_kilograms(self.row(uom="")))
        self.assertFalse(tonnage.is_piece_uom(None))

    def test_an_unweighable_row_returns_none_not_zero(self):
        # So a caller cannot add "unknown" into a total as though it were
        # "nothing" -- the failure that makes a half-weighed warehouse look
        # exactly like a correctly weighed one.
        self.assertIsNone(tonnage.row_kilograms(self.row(gross_weight_per_case=None)))
        self.assertIsNone(tonnage.row_kilograms(self.row(gross_weight_per_case=0)))
        self.assertIsNone(tonnage.row_kilograms(self.row(pieces_per_box=0)))

    def test_the_roll_up_discloses_what_it_could_not_weigh(self):
        result = tonnage.roll_up([
            self.row(),                                  # 500 kg
            self.row(gross_weight_per_case=None),        # unweighed
            self.row(uom="LTR"),                         # non-piece
        ])
        self.assertEqual(result["tonnes"], 0.5)
        self.assertEqual(result["weighed_items"], 1)
        self.assertEqual(result["unweighed_items"], 1)
        self.assertEqual(result["non_piece_items"], 1)
        self.assertAlmostEqual(result["coverage"], 1 / 3, places=3)

    def test_negative_stock_is_carried_not_clamped(self):
        # SAP does hold negatives, and hiding them would make this board
        # disagree with the stock screen for a reason nobody could see.
        self.assertEqual(tonnage.roll_up([self.row(on_hand=-1000.0)])["tonnes"], -0.5)

    def test_litres_skips_a_mass_but_keeps_a_volume(self):
        # Kilograms of flavouring stored beside the oil are not litres of oil.
        rows = [
            {"on_hand": 1000.0, "uom": "LTR", "litres_per_piece": 1.0},
            {"on_hand": 50.0, "uom": "KGS", "litres_per_piece": 1.0},
            {"on_hand": 10.0, "uom": "PCS", "litres_per_piece": 15.0},
        ]
        self.assertEqual(tonnage.litres(rows), 1000.0 + 150.0)

    def test_litres_ignores_a_piece_row_with_no_volume_recorded(self):
        rows = [{"on_hand": 100.0, "uom": "PCS", "litres_per_piece": 0}]
        self.assertEqual(tonnage.litres(rows), 0.0)


class NamedDepartmentLabourCostTests(TestCase):
    """The labour line prices five named departments, and nothing else.

    The register keeps two kinds of row under one shape: a row with NO
    department is what walked through the barrier, and a row WITH one is an HOD
    splitting those same people across departments afterwards. The Factory
    Expense wall board sums both — on the live register for 1-16 Sep that is
    1,888 man-days against 1,149 real ones. This tile prices the SPLIT rows for
    production(oil), Warehouse Basement, Dock, Scrap and Boiling Floor 1, so it
    is a subset of the gate by design and has to say so on the line.
    """

    def setUp(self):
        from cost_master.models import CostRate, CostType
        from labour_gate.models import LabourGateEntry
        from person_gatein.models import Contractor

        self.company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        self.boiling = Department.objects.create(name="Boiling Floor 1")
        self.scrap = Department.objects.create(name="Scrap")
        # Named on the board but never staffed this month; it must not break the
        # count and must not be claimed as staffed either.
        self.dock = Department.objects.create(name="Dock")
        # NOT one of the five. The mess is real labour and is somebody else's
        # cost line.
        self.mess = Department.objects.create(name="Mess")
        self.contractor = Contractor.objects.create(contractor_name="Balbir")

        cost_type = CostType.objects.create(
            code="factory-labour", name="Factory labour", default_basis="PER_PERSON_DAY"
        )
        CostRate.objects.create(
            cost_type=cost_type,
            scope="FACTORY",
            basis="PER_PERSON_DAY",
            rate=Decimal("600"),
            effective_from=date(2026, 9, 1),
        )

        def entry(day, department, count):
            return LabourGateEntry.objects.create(
                company=self.company,
                department=department,
                contractor=self.contractor,
                work_date=day,
                count_in=count,
            )

        # 40 walked through the barrier on the 2nd; the HOD later books 25 of
        # them to the boiling floor and 5 to the mess.
        entry(date(2026, 9, 2), None, 40)
        entry(date(2026, 9, 2), self.boiling, 25)
        entry(date(2026, 9, 2), self.mess, 5)
        entry(date(2026, 9, 3), None, 10)
        entry(date(2026, 9, 3), self.scrap, 4)

    def _cost(self):
        service = AdminBoardService("JIVO_OIL", today=date(2026, 9, 15))
        return service._cost()

    def _labour(self):
        return next(
            entry for entry in self._cost()["slices"] if entry["key"] == "labour"
        )

    def test_only_the_five_departments_are_priced(self):
        # 25 + 4 at Rs 600. NOT 50 (the barrier rows) and NOT 34 (the mess too).
        self.assertEqual(self._labour()["detail_value"], 29)
        self.assertEqual(self._labour()["amount"], 17_400.0)

    def test_a_department_outside_the_five_is_not_priced(self):
        from labour_gate.models import LabourGateEntry

        LabourGateEntry.objects.filter(department=self.mess).update(count_in=500)
        # The mess could triple the bill and this line would not move.
        self.assertEqual(self._labour()["amount"], 17_400.0)

    def test_the_barrier_rows_are_not_added_to_the_departmental_ones(self):
        # The failure this whole rule exists to prevent: 50 + 29 = 79 people who
        # were never here.
        self.assertNotEqual(self._labour()["detail_value"], 79)

    def test_the_detail_names_the_departments_staffed_and_the_days(self):
        self.assertEqual(
            self._labour()["detail"], "29 across 2 of 5 departments over 2 days"
        )

    def test_the_line_says_what_share_of_the_gate_it_covers(self):
        # A labour figure that silently omitted 21 of the 50 people who came in
        # would be worse than no labour figure.
        basis = self._labour()["basis"]
        self.assertIn("29 of the 50 people the gate counted", basis)
        self.assertIn("Boiling Floor 1", basis)
        self.assertIn("Factory Expense", basis)

    def test_a_month_with_nobody_booked_to_a_floor_is_not_read_as_quiet(self):
        from labour_gate.models import LabourGateEntry

        LabourGateEntry.objects.filter(department__isnull=False).delete()
        labour = self._labour()
        self.assertEqual(labour["amount"], 0.0)
        self.assertEqual(labour["detail"], "nobody booked to the 5 departments this month")
        # 50 people DID come in. Zero without that said beside it would read as
        # a factory nobody worked in.
        self.assertIn("50 labourers came through the gate", labour["warning"])

    def test_unpriced_people_are_counted_and_named_not_silently_free(self):
        from cost_master.models import CostRate

        CostRate.objects.all().delete()
        labour = self._labour()
        self.assertEqual(labour["detail_value"], 29)
        self.assertEqual(labour["amount"], 0.0)
        self.assertIn("29 unpriced", labour["detail"])
        self.assertIn("29 of the 29 labourers on these departments", labour["warning"])

    def test_a_department_the_board_names_but_the_site_lacks_is_flagged(self):
        self.dock.delete()
        service = AdminBoardService("JIVO_OIL", today=date(2026, 9, 15))
        service._cost()
        self.assertTrue(
            any("Dock" in entry for entry in service._warnings),
            service._warnings,
        )

    def test_the_electricity_line_is_named_electricity_not_others(self):
        keys_seen = {entry["key"]: entry["label"] for entry in self._cost()["slices"]}
        self.assertEqual(keys_seen["electricity"], "Electricity")
        self.assertNotIn("others", keys_seen)

    def test_the_electricity_note_says_which_meters_the_line_is(self):
        # The wall board reads several times this line, and the only thing that
        # explains the gap is WHICH meters each of them prices.
        note = self._cost()["electricity_note"]
        self.assertIn("sub-meters", note)
        self.assertIn("mains are left out", note)
        self.assertIn("counts half", note)


class OilOnlyElectricityTests(TestCase):
    """The electricity line is Jivo Oil's sub-meters, at Oil's share of them.

    Three rules meet on this line and no two of them alone get the figure
    right. The register is campus-wide — Beverages' boiler, ETP, RO and terrace
    meters are entered on the same page as Oil's, and the Factory Expense wall
    prices all of them on purpose — so a Beverages-only meter must not reach
    this board. The mains are the supply the sub-meters slice up, so a total
    holding both counts the same electricity twice. And several of Oil's
    sub-meters feed Beverages too, with one reading a day and nothing behind it
    to divide by, so this board splits them equally and says so on the line.
    """

    def setUp(self):
        from maintenance.models import ElectricityMeter

        self.oil = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        self.bev = Company.objects.create(name="Jivo Beverages", code="JIVO_BEVERAGES")

        def meter(name, companies, units, main=False, counts=True, day=None):
            row = ElectricityMeter.objects.create(
                name=name,
                rate_per_unit=Decimal("7"),
                multiplying_factor=Decimal("1"),
                is_main=main,
                counts_as_supply=counts,
            )
            row.companies.set(companies)
            self.reading(row, units, day=day)
            return row

        self.meter = meter
        meter("Production Floor OIL", [self.oil], "1000")
        meter("TR 125", [self.oil, self.bev], "400")
        meter("KWH", [self.oil, self.bev], "2000", main=True)
        meter("Boiler", [self.bev], "5000")

    def reading(self, row, units, day=None, companies=None):
        """One more day on a meter. ``companies`` overrides the meter's tags."""
        from maintenance.models import DailyElectricityReading

        entry = DailyElectricityReading.objects.create(
            meter=row,
            date=day or date(2026, 9, 2),
            opening_reading=Decimal("0"),
            closing_reading=Decimal(units),
            multiplying_factor=Decimal("1"),
            rate_per_unit=Decimal("7"),
        )
        if companies is not None:
            entry.companies.set(companies)
        return entry

    def _electricity(self):
        service = AdminBoardService("JIVO_OIL", today=date(2026, 9, 15))
        return next(
            entry for entry in service._cost()["slices"] if entry["key"] == "electricity"
        )

    def test_the_line_is_oils_sub_meters_added_up(self):
        # 1,000 on the production floor plus Oil's half of TR 125's 400 =
        # 1,200 units at Rs 7. NOT 3,200 with the main added on top, which
        # measures the supply these two draw from, and not 6,400 with the
        # boiler, which is Beverages' alone.
        self.assertEqual(self._electricity()["amount"], 8_400.0)

    def test_a_meter_shared_with_beverages_counts_half(self):
        by_meter = {row["label"]: row["amount"] for row in self._electricity()["rows"]}
        # 400 units at Rs 7 is Rs 2,800 on the dial; Oil carries half of it.
        self.assertEqual(by_meter["TR 125"], 1_400.0)
        self.assertEqual(by_meter["Production Floor OIL"], 7_000.0)

    def test_a_shared_row_says_it_is_showing_a_share_not_the_dial(self):
        # Without this a reader who checks the row against the register finds
        # double and concludes the board is wrong.
        by_meter = {row["label"]: row["detail"] for row in self._electricity()["rows"]}
        self.assertEqual(
            by_meter["TR 125"],
            "200 units at ₹7.00/unit · Jivo Oil's half, shared with Jivo Beverages",
        )
        # And a meter Oil has to itself carries no note — one on every row
        # would stop being read by the time it mattered.
        self.assertEqual(
            by_meter["Production Floor OIL"], "1,000 units at ₹7.00/unit"
        )

    def test_the_mains_are_left_out_although_they_feed_oil(self):
        labels = [row["label"] for row in self._electricity()["rows"]]
        self.assertNotIn("KWH", labels)

    def test_a_main_that_re_reads_another_main_is_left_out_too(self):
        # KVAH is the grid's KWH as apparent energy — the same electricity, not
        # a second feed. It is a main either way, so it never reaches the line.
        self.meter("KVAH", [self.oil, self.bev], "9000", main=True, counts=False)
        self.assertEqual(self._electricity()["amount"], 8_400.0)

    def test_a_beverages_only_meter_never_reaches_this_board(self):
        labels = [row["label"] for row in self._electricity()["rows"]]
        self.assertNotIn("Boiler", labels)

    def test_the_rows_add_up_to_the_line(self):
        power = self._electricity()
        self.assertTrue(power["rows_sum_to_line"])
        self.assertEqual(sum(row["amount"] for row in power["rows"]), power["amount"])

    def test_the_detail_counts_the_meters_behind_the_money(self):
        detail = self._electricity()
        self.assertEqual(detail["detail"], "2 sub-meters · 1,200 units")
        self.assertEqual(detail["detail_value"], 1_200.0)

    def test_a_day_moved_onto_oil_alone_stops_being_halved(self):
        # Attribution is the READING's, not the meter's: a line run for Oil
        # alone one day carries that day whole, even on a shared meter.
        self.reading(
            self.meter("TR 40", [self.oil, self.bev], "100"),
            "100",
            day=date(2026, 9, 3),
            companies=[self.oil],
        )
        by_meter = {row["label"]: row["amount"] for row in self._electricity()["rows"]}
        # Rs 350 for the shared day's half, Rs 700 for the day that named Oil.
        self.assertEqual(by_meter["TR 40"], 1_050.0)

    def test_the_basis_says_the_mains_are_out_and_a_shared_meter_is_halved(self):
        basis = self._electricity()["basis"]
        self.assertIn("mains are left out", basis)
        self.assertIn("counts half", basis)

    def test_a_month_with_no_oil_reading_says_so_rather_than_reading_nil(self):
        from maintenance.models import DailyElectricityReading

        DailyElectricityReading.objects.filter(
            meter__companies=self.oil
        ).delete()
        power = self._electricity()
        self.assertEqual(power["amount"], 0.0)
        self.assertEqual(power["detail"], "no Jivo Oil meter read this month")
        # The wall board stays quiet here — Beverages' boiler WAS read — so the
        # silence it would pass on cannot be trusted.
        self.assertIn("No reading on a Jivo Oil meter", power["warning"])

    def test_a_month_with_only_the_mains_read_is_a_gap_not_a_figure(self):
        # The register was kept, on the one kind of meter this line cannot use.
        # Pricing the main instead would be the double count the line exists to
        # avoid, and reporting a bare nil would say the plant drew nothing.
        from maintenance.models import DailyElectricityReading

        DailyElectricityReading.objects.filter(meter__is_main=False).delete()
        power = self._electricity()
        self.assertEqual(power["amount"], 0.0)
        self.assertFalse(power["has_source"])
        self.assertEqual(power["detail"], "no sub-meter read this month")
        self.assertIn("Only main meters were read", power["warning"])


class EximTankReadingTests(SimpleTestCase):
    """Unit conversion, the vessel split, and the shape of an unhappy read."""

    # tank_code, tank_type, item_code_id, tank_item_name, category, capacity, stock
    TANK = ("TNK017", "TANK", "RM0MKG", "MUSTARD KACHI GHANI", "MUSTARD", 100_000, 98_000)
    TOTE = ("TOT002", "TOTES", None, None, None, 20_000, 0)

    def _reading(self, rows, unit="LITRES"):
        with self.settings(
            DATABASES={"default": {}, "exim": {}}, EXIM_TANK_UNIT=unit
        ):
            with mock.patch.object(exim_reader, "connections") as conns:
                cursor = conns.__getitem__.return_value.cursor.return_value
                cursor.__enter__.return_value.fetchall.return_value = rows
                return exim_reader.read_tanks()

    def test_litres_become_tonnes_at_the_business_rule(self):
        reading = self._reading([self.TANK])
        self.assertEqual(reading.capacity_tons, 100.0)
        self.assertEqual(reading.stock_tons, 98.0)
        self.assertEqual(reading.tanks[0]["used_pct"], 98.0)

    def test_a_tonne_source_is_not_divided_again(self):
        # The 1000x trap: the same figures read as tonnes must stay as they are.
        row = ("TNK017", "TANK", "RM0MKG", "MUSTARD", "MUSTARD", 100, 98)
        reading = self._reading([row], unit="TONNES")
        self.assertEqual(reading.capacity_tons, 100.0)
        self.assertEqual(reading.stock_tons, 98.0)

    def test_current_capacity_is_the_stock_and_never_the_rating(self):
        # The table's worst name. If the stock column were read as a capacity
        # the farm would be exactly 100% full, which is plausible and wrong.
        reading = self._reading([self.TANK])
        self.assertNotEqual(reading.capacity_tons, reading.stock_tons)
        self.assertLess(reading.stock_tons, reading.capacity_tons)

    def test_the_headline_is_tanks_only_and_the_totes_are_not_lost(self):
        # The question is "how full is the tank farm", so four IBC totes do not
        # dilute the percentage — but their oil is still reported.
        reading = self._reading([self.TANK, self.TOTE])
        self.assertEqual(reading.capacity_tons, 100.0)
        self.assertEqual(reading.stock_tons, 98.0)
        self.assertEqual(reading.excluded["vessels"], 1)
        self.assertEqual(reading.excluded["capacity_tons"], 20.0)
        self.assertEqual(reading.excluded["types"], ["TOTES"])
        # Both kinds stay in the per-type split and in the vessel list.
        self.assertEqual(reading.by_type["TOTES"]["capacity_tons"], 20.0)
        self.assertEqual(len(reading.tanks), 2)

    def test_a_farm_of_tanks_alone_excludes_nothing(self):
        reading = self._reading([self.TANK])
        self.assertEqual(reading.excluded, {})

    def test_an_empty_vessel_says_empty_rather_than_showing_a_blank_item(self):
        reading = self._reading([self.TOTE])
        self.assertEqual(reading.tanks[0]["item"], "empty")

    def test_an_empty_table_is_a_reason_not_a_zero(self):
        reading = self._reading([])
        self.assertFalse(reading.ok)
        self.assertIn("no active vessels", reading.reason)
        self.assertIsNone(reading.capacity_tons)

    def test_an_unconfigured_alias_says_so_without_naming_server_settings(self):
        """The reason reaches a factory wall; the fix reaches the log.

        It is printed in the board's action centre, where the audience is
        whoever is standing in front of the screen. Environment variable names
        are noise to them and actionable by none of them, so the sentence says
        what is wrong and the log carries what to set.
        """
        with self.settings(DATABASES={"default": {}}):
            with self.assertLogs("admin_board.exim_reader", level="WARNING") as logged:
                reading = exim_reader.read_tanks()

        self.assertFalse(reading.ok)
        self.assertIn("not connected", reading.reason)
        for secret in ("EXIM_DB_NAME", "EXIM_DB_HOST", "EXIM_DB_USER", "EXIM_DB_PASSWORD"):
            self.assertNotIn(secret, reading.reason)
            # ...but every one of them is in the log, for the person who can act.
            self.assertIn(secret, "".join(logged.output))

    def test_a_vessel_with_no_rating_reports_no_percentage_rather_than_zero(self):
        row = ("TNK009", "TANK", None, None, None, 0, 5_000)
        reading = self._reading([row])
        self.assertIsNone(reading.tanks[0]["used_pct"])

    def test_a_server_that_does_not_answer_is_reported_not_raised(self):
        from django.db import DatabaseError

        with self.settings(DATABASES={"default": {}, "exim": {}}):
            with mock.patch.object(exim_reader, "connections") as conns:
                conns.__getitem__.return_value.cursor.side_effect = DatabaseError(
                    "connection timed out"
                )
                with self.assertLogs("admin_board.exim_reader", level="WARNING") as logged:
                    reading = exim_reader.read_tanks()
        self.assertFalse(reading.ok)
        # Reported, but in the reader's terms. The driver's own complaint can
        # name hosts and credentials and means nothing to somebody walking past
        # a wall, so it goes to the log and "could not be read" goes on screen.
        self.assertIn("could not be read", reading.reason)
        self.assertNotIn("timed out", reading.reason)
        self.assertIn("timed out", "".join(logged.output))


class ProductionBasisTests(SimpleTestCase):
    """What "Total production" counts.

    THE FLOOR'S OUTPUT, not the plan's share of it. The plan lists what the
    month intends to make and nothing else, so measuring output by it drops
    every item nobody planned -- on Oil in September that was 230 t, a fifth of
    the month, in cold-pressed groundnut, rice bran and the Kachi Ghani cold
    presses. The plan stays as the TARGET beside the figure; it is no longer
    the filter on it.
    """

    def _production(self, planned_litres, produced_litres, by_day, attainment=None):
        service = AdminBoardService.__new__(AdminBoardService)
        service.company_code = "JIVO_OIL"
        service.today = date(2026, 9, 17)
        service.month_first = date(2026, 9, 1)
        service.days_in_month = 30
        service._warnings = []
        service._resolve_plan = lambda: {"abs_id": 24, "name": "SEP 2026"}
        service._plan_service = lambda: _FakePlanService(
            planned_litres, produced_litres, attainment
        )
        service._daily_production = lambda: by_day
        return service._production()

    DAYS = {
        date(2026, 9, 16): {"tons": 700.0, "pieces": 500_000},
        date(2026, 9, 17): {"tons": 800.0, "pieces": 600_000},
    }

    def test_the_headline_is_every_receipt_not_the_planned_share(self):
        out = self._production(4_000_000, 1_200_000, self.DAYS)
        # 1,500 t made; the plan can only speak for 1,200 t of it.
        self.assertEqual(out["mtd_tons"], 1500.0)
        self.assertEqual(out["planned_items_tons"], 1200.0)
        self.assertEqual(out["unplanned_tons"], 300.0)

    def test_the_bar_follows_the_headline(self):
        """A percentage that measures a different tonnage from the figure above
        it is a tile arguing with itself."""
        out = self._production(4_000_000, 1_200_000, self.DAYS, attainment=30.0)
        self.assertEqual(out["plan_pct"], round(1500.0 / 4000.0 * 100, 1))
        # SAP's own attainment on its own lines is kept beside it, not thrown
        # away: it is the right answer to a different question.
        self.assertEqual(out["planned_items_pct"], 30.0)

    def test_output_beyond_the_plan_never_reads_as_negative(self):
        """A plan whose actuals exceed the floor's receipts -- a receipt posted
        to another warehouse, say -- must not print a negative surplus."""
        out = self._production(4_000_000, 1_900_000, self.DAYS)
        self.assertEqual(out["unplanned_tons"], 0)

    def test_the_basis_says_it_counts_the_unplanned(self):
        """Two boards quoting different tonnages for "production" have to be
        tellable apart from the tile itself."""
        out = self._production(4_000_000, 1_200_000, self.DAYS)
        self.assertIn("Every production receipt", out["basis"])

    def test_with_no_plan_the_floor_still_reports_its_own_output(self):
        service = AdminBoardService.__new__(AdminBoardService)
        service.company_code = "JIVO_OIL"
        service.today = date(2026, 9, 17)
        service.month_first = date(2026, 9, 1)
        service.days_in_month = 30
        service._warnings = []
        service._resolve_plan = lambda: None
        service._daily_production = lambda: self.DAYS
        out = service._production()
        self.assertEqual(out["mtd_tons"], 1500.0)
        self.assertIsNone(out["plan_tons"])
        self.assertIsNone(out["unplanned_tons"])


class _FakePlanService:
    """The plan service's shape, with the two litre totals under control."""

    def __init__(self, planned_litres, produced_litres, attainment):
        self._planned = planned_litres
        self._produced = produced_litres
        self._attainment = attainment

    def get_plan(self, abs_id, include_actuals=True):
        return {
            "plan": {
                "planned_litres": self._planned,
                "produced_litres": self._produced,
                "produced_qty": 0,
                "attainment_pct": self._attainment,
            },
            "lines": [],
        }


class OilTileSourceTests(SimpleTestCase):
    """Which system the tile reports, and what it does when EXIM is silent."""

    def _oil(self, reading):
        service = AdminBoardService.__new__(AdminBoardService)
        service.company_code = "JIVO_OIL"
        service._tank_reader = lambda: reading
        service._occupancy = lambda *a, **k: []
        service._capacity = lambda *a, **k: None
        return service._oil_storage()

    def test_exim_is_the_source_when_it_answers(self):
        reading = exim_reader.TankReading(
            tanks=[{"code": "T1", "stock_tons": 98.0}],
            capacity_tons=1351.5,
            stock_tons=988.2,
        )
        oil = self._oil(reading)
        self.assertEqual(oil["source"], "EXIM")
        self.assertEqual(oil["total_tons"], 988.2)
        self.assertEqual(oil["capacity_tons"], 1351.5)
        self.assertIsNone(oil["no_capacity_reason"])

    def test_sap_is_kept_beside_it_so_a_discrepancy_is_visible(self):
        reading = exim_reader.TankReading(
            tanks=[{"code": "T1", "stock_tons": 98.0}],
            capacity_tons=1351.5,
            stock_tons=988.2,
        )
        oil = self._oil(reading)
        # SAP read nothing here, but the field is always present and is what a
        # reader compares the EXIM figure against.
        self.assertIn("sap_tons", oil)
        self.assertEqual(oil["sap_tons"], 0.0)

    def test_a_silent_farm_falls_back_to_sap_and_says_why(self):
        reading = exim_reader.TankReading(reason="The farm did not answer.")
        oil = self._oil(reading)
        self.assertEqual(oil["source"], "SAP")
        self.assertIsNone(oil["capacity_tons"])
        self.assertIsNone(oil["used_pct"])
        self.assertEqual(oil["no_capacity_reason"], "The farm did not answer.")
