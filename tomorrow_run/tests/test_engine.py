"""The engine against the plan that was signed off.

``fixtures/inputs_2026-09-24.json`` is the 7 pm read behind the published
plan for Thursday 24 Sept 2026 (jivo-mark7.vercel.app), rebuilt from that
plan's own ``plan.json``. The numbers asserted below are that plan's: the
engine was rebuilt from the board's rules and has to land where the board
did, job for job and machine for machine.
"""

import json
from collections import Counter
from pathlib import Path

from django.test import SimpleTestCase

from tomorrow_run.engine import build_plan
from tomorrow_run.engine import items as I
from tomorrow_run.engine.planner import clock, sheet_machines, work_days

FIXTURE = Path(__file__).parent / "fixtures" / "inputs_2026-09-24.json"


def _inputs():
    return json.loads(FIXTURE.read_text())


class SignedOffPlanTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.plan = build_plan(_inputs())

    def test_the_day_totals_what_the_board_made(self):
        self.assertEqual(self.plan["total_l"], 135564.0)
        self.assertEqual(self.plan["final_list"], {"skus": 12, "pcs": 153751, "litres": 283438.0})
        self.assertEqual(self.plan["pile"]["skus"], 54)
        self.assertAlmostEqual(self.plan["pile"]["litres"], 1250070.87, places=2)

    def test_jobs_in_order_with_what_step_5_let_through(self):
        self.assertEqual(
            [(j["code"], j["make_pcs"], j["placed_pcs"]) for j in self.plan["jobs"]],
            [("FG0000030", 56589, 56589), ("FG0000194", 55000, 30525), ("FG0000091", 10000, 9975),
             ("FG0000008", 7784, 5700), ("FG0000226", 7193, 0), ("FG0000004", 6000, 0),
             ("FG0000143", 5104, 0), ("FG0000011", 3597, 0), ("FG0000306", 1476, 0),
             ("FG0000090", 975, 0), ("FG0000227", 32, 0), ("FG0000187", 1, 0)],
        )

    def test_mustard_is_split_so_both_machines_finish_together(self):
        job = self.plan["jobs"][0]
        self.assertEqual(job["chosen"], "split:Clear Pack+JP")
        self.assertEqual(self.plan["machines"]["Clear Pack"]["jobs"][0]["pcs"], 40294)
        self.assertEqual(self.plan["machines"]["JP"]["jobs"][0]["pcs"], 16295)
        self.assertEqual(self.plan["machines"]["JP"]["finish"], "18:46")
        self.assertEqual(self.plan["machines"]["Clear Pack"]["finish"], "18:46")

    def test_each_machine(self):
        got = {m: (v["litres"], v["finish"], [(j["code"], j["pcs"], j["change_h"]) for j in v["jobs"]])
               for m, v in self.plan["machines"].items()}
        self.assertEqual(got, {
            "JP": (16295.0, "18:46", [("FG0000030", 16295, 6.0)]),
            "Clear Pack": (40294.0, "18:46", [("FG0000030", 40294, 1.0)]),
            "10 Head": (19950.0, "19:30", [("FG0000091", 9975, 0.5)]),
            "6 Head": (28500.0, "19:30", [("FG0000008", 5700, 0.5)]),
            "Tin": (0, None, []),
            "Hitech pouch": (11100.0, "19:30", [("FG0000194", 11100, 0.75)]),
            "Samarpan pouch": (19425.0, "19:30", [("FG0000194", 19425, 0.75)]),
        })

    def test_top_three_per_machine(self):
        self.assertEqual({m: v["top3"] for m, v in self.plan["machine_menu"].items()}, {
            "JP": ["FG0000030-order", "FG0000090-order", "FG0000091-order"],
            "Clear Pack": ["FG0000030-order", "FG0000226-order", "FG0000004-order"],
            "10 Head": ["FG0000091-order", "FG0000090-order", "FG0000306-order"],
            "6 Head": ["FG0000008-order", "FG0000004-order", "FG0000226-order"],
            "Tin": [],
            "Hitech pouch": ["FG0000194-order"],
            "Samarpan pouch": ["FG0000194-order"],
        })

    def test_everything_else_waits_with_its_reason(self):
        self.assertEqual(Counter(p["reason_word"] for p in self.plan["pending"]), Counter({
            "no oil": 28, "no machine": 12, "no time": 11, "no room": 6, "no bottles": 2, "no cartons": 2,
            "no caps": 1, "no pouch film": 1, "no labels": 1,
        }))

    def test_the_sheet_tiles(self):
        s = self.plan["sheet"]
        self.assertAlmostEqual(s["need_l"], 1529852.87, places=2)
        self.assertEqual(s["made_l"], 352128.0)
        self.assertAlmostEqual(s["left_l"], 1250070.87, places=2)
        self.assertEqual(s["work_days_left"], 6)
        self.assertEqual([x["code"] for x in s["sheet_vs_board"]],
                         ["FG0000306", "FG0000149", "FG0000192", "FG0000038", "FG0000178"])

    def test_room_free_tomorrow(self):
        rooms = self.plan["have"]["rooms"]
        self.assertEqual(round(rooms["BH-BT"]["free_l"]), 88016)
        self.assertEqual(round(rooms["BH-PF"]["free_l"]), 195423)

    def test_a_mart_code_is_planned_as_the_oil_item(self):
        self.assertEqual([m["planned_as"] for m in self.plan["sheet"]["mart_codes"]], ["FG0000441", "FG0000349"])

    def test_the_line_with_no_code_is_warned_not_planned(self):
        self.assertTrue(any("PREMIUM OLIVE OIL 500ML" in w for w in self.plan["warnings"]))


class PickTests(SimpleTestCase):
    def test_a_held_pick_gets_its_oil_first_and_runs_first(self):
        # the 23 Sept pick: cold press sunflower 1 L on JP, held for oil
        plan = build_plan(_inputs(), picks=[{"machine": "JP", "job": "FG0000081-order", "why": "Urgent order",
                                             "by": "Gurvinder veerji", "rank": 5}])
        jp = plan["machines"]["JP"]["jobs"]
        self.assertEqual((jp[0]["code"], jp[0]["pcs"], jp[0]["start"]), ("FG0000081", 11277, "07:30"))
        picked = plan["machine_menu"]["JP"]["picked"]
        self.assertTrue(picked["on_plan"])
        self.assertEqual(picked["rank"], 5)
        # the sunflower it took is gone from the combo that had it
        combo = next(j for j in plan["jobs"] if j["code"] == "FG0000091")
        self.assertLess(combo["make_pcs"], 10000)
        self.assertTrue(any(w["code"] == "FG0000081" and w["picked"] for w in combo["material"]["went_to"]))

    def test_a_pick_goes_first_and_the_rest_is_re_timed(self):
        plan = build_plan(_inputs(), picks=[{"machine": "10 Head", "job": "FG0000090-order", "why": "x", "by": "t"}])
        jobs = plan["machines"]["10 Head"]["jobs"]
        self.assertEqual([(j["code"], j["start"]) for j in jobs], [("FG0000090", "07:30"), ("FG0000091", "09:12")])

    def test_other_changes_nothing(self):
        base = build_plan(_inputs())
        plan = build_plan(_inputs(), picks=[{"machine": "Tin", "job": "other", "other": "Mustard 15 L",
                                             "why": "Urgent order", "by": "t"}])
        self.assertEqual(plan["total_l"], base["total_l"])
        self.assertEqual(plan["machine_menu"]["Tin"]["picked"]["other"], "Mustard 15 L")

    def test_learning_counts_picks_that_were_already_ours(self):
        history = [
            {"job": "A-order", "rank": 1, "top3": [{"job": "A-order"}]},
            {"job": "B-order", "rank": 2, "top3": [{"job": "A-order"}, {"job": "B-order"}]},
            {"job": "C-order", "rank": 7, "top3": [{"job": "A-order"}]},
        ]
        L = build_plan(_inputs(), history=history)["learning"]
        self.assertEqual((L["n"], L["was_first"], L["in_top3"]), (3, 1, 2))


class RuleTests(SimpleTestCase):
    def item(self, name, lp, code="X"):
        return I.describe(code, name, lp)

    def test_packs(self):
        self.assertEqual(self.item("MUSTARD KACCHI GHANI 1 LTR + 1 LTR COMBO 10 SET PLAIN", 2).pack, "1 L")
        self.assertEqual(self.item("MUSTARD KACCHI GHANI 1 LTR + 1 LTR COMBO 10 SET PLAIN", 2).bottles, 2)
        self.assertEqual(self.item("POMACE OLIVE 5 LTR TIN 4 PCS", 5).type, "tin")
        self.assertEqual(self.item("REFINED OIL 15 LTR", 15).type, "tin")
        self.assertEqual(self.item("SOYABEAN OIL 1 LTR POUCH 12 PCS", 1).pack, "pouch")
        self.assertEqual(self.item("EXTRA VIRGIN OLIVE 250 MLS 4 PCS", 0.25).pack, "250 ml")
        self.assertIsNone(self.item("NO VOLUME", None))

    def test_filters(self):
        self.assertEqual(I.machines_for(self.item("MUSTARD KACHI GHANI 1 LTR 20 PCS", 1)), ["JP", "Clear Pack", "10 Head"])
        self.assertEqual(I.machines_for(self.item("YELLOW MUSTARD OIL 1 LTR 20 PCS", 1)), ["10 Head"])
        self.assertEqual(I.machines_for(self.item("COLD PRESS SUNFLOWER 1 LTR 20 PCS", 1)), ["JP", "Clear Pack", "10 Head"])
        self.assertEqual(I.machines_for(self.item("COLD PRESS 5 LTR 4 PCS", 5)), ["Clear Pack", "6 Head"])
        self.assertEqual(I.machines_for(self.item("POMACE OLIVE 5 LTR TIN 4 PCS", 5)), ["6 Head"])
        self.assertEqual(I.machines_for(self.item("REFINED OIL 15 LTR", 15)), ["Tin"])
        self.assertEqual(I.machines_for(self.item("MUSTARD KACHI GHANI 15 KGS", 16.48)), [])
        self.assertEqual(I.machines_for(self.item("GROUNDNUT 200 MLS", 0.2)), [])

    def test_changes(self):
        mustard = self.item("MUSTARD KACHI GHANI 1 LTR 20 PCS", 1, "M")
        rice = self.item("RICE BRAN OIL 1 LTR 16 PCS", 1, "R")
        cold = self.item("COLD PRESS 1 LTR + 1 LTR COMBO 10 SET", 2, "C")
        self.assertEqual(I.change("JP", rice, mustard)[0], 6.0)
        self.assertEqual(I.change("JP", mustard, mustard)[0], 0.0)
        self.assertEqual(I.change("Clear Pack", mustard, rice)[0], 2.0)
        self.assertEqual(I.change("Clear Pack", rice, mustard)[0], 1.0)
        self.assertEqual(I.change("Clear Pack", rice, cold)[0], 1.25)
        self.assertEqual(I.change("10 Head", mustard, rice)[0], 1.5)
        self.assertEqual(I.change("10 Head", rice, mustard)[0], 0.5)
        self.assertEqual(I.change("Tin", None, mustard)[0], 0.75)
        # two carton codes of one bottle are the same SKU
        a = self.item("COLD PRESS SUNFLOWER 1 LTR 20 PCS", 1, "FG0000081")
        b = self.item("COLD PRESS SUNFLOWER 1 LTR 20 PCS 26", 1, "FG0000456")
        self.assertEqual(I.change("JP", a, b)[0], 0.0)

    def test_clock(self):
        self.assertEqual(clock(0), "07:30")
        self.assertEqual(clock(10), "19:30")
        self.assertEqual(clock(9.394791666666666), "18:46")
        self.assertEqual(clock(17.789375), "04:50 +1 day")

    def test_sheet_machine_words(self):
        self.assertEqual(sheet_machines("Clearpack 1"), ["Clear Pack"])
        self.assertEqual(sheet_machines("Pouch"), ["Hitech pouch", "Samarpan pouch"])
        self.assertEqual(sheet_machines("Manual"), [])
        self.assertEqual(sheet_machines("10 Head"), ["10 Head"])

    def test_sundays_off(self):
        from datetime import date
        self.assertEqual(work_days(date(2026, 9, 24), date(2026, 9, 30)), 6)
