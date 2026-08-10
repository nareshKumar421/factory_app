"""Tests for the Live Trail.

The SQL is exercised against SAP; what is worth testing is the arithmetic and
the judgements sitting on top of it — how demand is pooled across two order
books, what counts as cover, which purchase orders count as supply, and when an
alarm is allowed to say CRITICAL. So the reader is faked and the assembly is
driven with rows shaped exactly as HANA returns them.

The fixture is deliberately small and hand-checkable: one SKU that can ship, one
that cannot, one component covered by a live PO and one covered only by a dead
one.
"""
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from .models import AlarmDispatch, AlarmSubscription, MachineCapacity, MaterialMachineMap
from .services.live_trail import (
    CREDIBLE_PO_SLIP_DAYS,
    PRODUCTION_COMPANY,
    SCOPE_ALL,
    SCOPE_EXTERNAL,
    build_live_trail,
)

TODAY = timezone.localdate()


def day(offset):
    return (TODAY + timedelta(days=offset)).isoformat()


class FakeReader:
    """A HANA reader with the rows written down instead of fetched.

    Mirrors :class:`LiveTrailReader`'s return shapes exactly; if those change,
    these tests should break.
    """

    production_company = PRODUCTION_COMPANY
    demand_companies = ["JIVO_OIL", "JIVO_MART"]

    def __init__(self, **overrides):
        self.data = {
            "orders": [
                # Covered from stock, external.
                {"company": "JIVO_OIL", "doc": 1, "entry": 1, "line": 0,
                 "card": "CUSTA000496", "party": "BIG BASKET", "ordered": day(-40),
                 "due": day(-40), "item": "FG_COVERED", "name": "COVERED 1 LTR",
                 "qty": 100, "open": 100, "delivered": 0, "price": 100.0},
                # Short, external, from Mart's book.
                {"company": "JIVO_MART", "doc": 2, "entry": 2, "line": 0,
                 "card": "CUSTA000048", "party": "R K WORLDINFOCOM", "ordered": day(-20),
                 "due": day(-10), "item": "FG_SHORT", "name": "SHORT 1 LTR",
                 "qty": 1000, "open": 1000, "delivered": 0, "price": 200.0},
                # Intercompany: Oil selling to Mart. The same litres are already
                # in Mart's own book above.
                {"company": "JIVO_OIL", "doc": 3, "entry": 3, "line": 0,
                 "card": "CUSTA000827", "party": "JIVO MART PVT LTD - HR",
                 "ordered": day(-5), "due": day(-5), "item": "FG_SHORT",
                 "name": "SHORT 1 LTR", "qty": 500, "open": 500, "delivered": 0,
                 "price": 200.0},
            ],
            "stock": {
                "JIVO_OIL": {"FG_COVERED": {"onhand": 60, "committed": 0},
                             "PM_LIVE": {"onhand": 100, "committed": 0},
                             "PM_DEAD": {"onhand": 150, "committed": 0},
                             "PM_PREFORM": {"onhand": 5000, "committed": 0}},
                "JIVO_MART": {"FG_COVERED": {"onhand": 40, "committed": 0}},
            },
            "work_orders": {"FG_SHORT": {"wip": 200, "wo_count": 1}},
            "boms": {
                "FG_SHORT": {"base_qty": 10, "lines": [
                    {"child": "PM_LIVE", "bom_qty": 10, "bom_base": 10, "per_unit": 1.0,
                     "is_resource": False, "bom_price": 2.0},
                    {"child": "PM_DEAD", "bom_qty": 10, "bom_base": 10, "per_unit": 1.0,
                     "is_resource": False, "bom_price": 3.0},
                    {"child": "RES_FILL", "bom_qty": 10, "bom_base": 10, "per_unit": 1.0,
                     "is_resource": True, "bom_price": 1.5},
                ]},
                "PM_LIVE": {"base_qty": 1, "lines": [
                    {"child": "PM_PREFORM", "bom_qty": 1, "bom_base": 1, "per_unit": 1.0,
                     "is_resource": False, "bom_price": 1.0},
                ]},
            },
            "items": {
                "FG_COVERED": {"name": "COVERED 1 LTR", "group": "FINISHED", "uom": "PCS",
                               "min_level": 0, "price": 90.0, "type": "COMMODITY",
                               "variety": "MUSTARD", "purchased": False},
                "FG_SHORT": {"name": "SHORT 1 LTR", "group": "FINISHED", "uom": "PCS",
                             "min_level": 0, "price": 180.0, "type": "PREMIUM",
                             "variety": "OLIVE", "purchased": False},
                "PM_LIVE": {"name": "BOTTLE 1 LTR", "group": "PACKAGING MATERIAL",
                            "uom": "PCS", "min_level": 0, "price": 4.0,
                            "type": "", "variety": "PET BOTTLES", "purchased": True},
                "PM_DEAD": {"name": "CAP 1 LTR", "group": "PACKAGING MATERIAL",
                            "uom": "PCS", "min_level": 0, "price": 1.0,
                            "type": "", "variety": "CAPS", "purchased": True},
                "PM_PREFORM": {"name": "PREFORM 23 GMS", "group": "PACKAGING MATERIAL",
                               "uom": "PCS", "min_level": 0, "price": 3.0,
                               "type": "", "variety": "PREFORM", "purchased": True},
            },
            "resources": {"RES_FILL": {"name": "Filling Cost", "rate": 1.5, "uom": "LTR"}},
            "purchase_lines": {
                # Believable: due tomorrow.
                "PM_LIVE": [{"entry": 10, "doc": 10, "vendor": "BOTTLE CO",
                             "eta": day(1), "ordered": day(-20), "qty": 400, "price": 4.0}],
                # Dead: slipped well past the credible window.
                "PM_DEAD": [{"entry": 11, "doc": 11, "vendor": "CAP CO",
                             "eta": day(-(CREDIBLE_PO_SLIP_DAYS + 60)), "ordered": day(-200),
                             "qty": 900, "price": 1.0}],
            },
            "lead_times": {
                "PM_LIVE": {"lead_lines": 12, "lead_avg": 20.0, "lead_max": 40.0},
                "PM_DEAD": {"lead_lines": 8, "lead_avg": 15.0, "lead_max": 30.0},
            },
            "vendors": {"PM_LIVE": "BOTTLE CO", "PM_DEAD": "CAP CO"},
            # Mart's own item master. The code space diverged from Oil's, so a
            # code that exists in both is not necessarily the same product.
            "demand_names": {"JIVO_MART": {"FG_SHORT": "SHORT 1 LTR"}},
            "overdue": {"overdue_po_lines": 794, "overdue_po_docs": 322,
                        "overdue_po_value": 338163866.45, "overdue_po_over180": 445,
                        "overdue_po_oldest": "2024-10-10"},
        }
        self.data.update(overrides)

    def open_order_lines(self):
        return [dict(row) for row in self.data["orders"]]

    def demand_item_names(self, company, codes):
        names = self.data["demand_names"].get(company, {})
        return {k: v for k, v in names.items() if k in set(codes)}

    def production_codes_for_names(self, names):
        wanted = {n.upper().strip() for n in names}
        out = {}
        for code, item in self.data["items"].items():
            key = item["name"].upper().strip()
            if key in wanted:
                out.setdefault(key, []).append(code)
        return out

    def stock_on_hand(self, company, codes):
        return {k: v for k, v in self.data["stock"].get(company, {}).items() if k in set(codes)}

    def open_work_orders(self, codes):
        return {k: v for k, v in self.data["work_orders"].items() if k in set(codes)}

    def bills_of_material(self, codes):
        return {k: v for k, v in self.data["boms"].items() if k in set(codes)}

    def sub_bills_of_material(self, codes):
        return self.bills_of_material(codes)

    def item_master(self, codes):
        return {k: v for k, v in self.data["items"].items() if k in set(codes)}

    def resources(self):
        return self.data["resources"]

    def open_purchase_lines(self, codes):
        return {k: v for k, v in self.data["purchase_lines"].items() if k in set(codes)}

    def measured_lead_times(self, codes):
        return {k: v for k, v in self.data["lead_times"].items() if k in set(codes)}

    def last_vendors(self, codes):
        return {k: v for k, v in self.data["vendors"].items() if k in set(codes)}

    def overdue_purchase_summary(self):
        return dict(self.data["overdue"])


def trail(scope=SCOPE_EXTERNAL, **overrides):
    return build_live_trail(scope=scope, reader=FakeReader(**overrides))


class DemandTests(TestCase):
    """Two order books, one factory."""

    def test_mart_orders_are_demand_on_the_oil_factory(self):
        """Mart sells what Oil makes, so Mart's book is part of the same trail."""
        payload = trail()
        books = {b["company"]: b for b in payload["summary"]["books"]}
        self.assertIn("JIVO_MART", books)
        self.assertEqual(books["JIVO_MART"]["lines"], 1)
        self.assertEqual(books["JIVO_MART"]["units"], 1000)

    def test_intercompany_is_dropped_by_default_so_litres_are_not_planned_twice(self):
        """Oil's order to Mart and Mart's own order are the same goods.

        Counting both would put 1,500 pieces of demand on a factory that owes
        1,000 — the single most expensive mistake available in a consolidated
        view, so EXTERNAL is the default.
        """
        external = trail(SCOPE_EXTERNAL)["summary"]
        every = trail(SCOPE_ALL)["summary"]
        self.assertEqual(external["demand_units"], 1100)
        self.assertEqual(every["demand_units"], 1600)
        # Both readings know about the intercompany lines; only one counts them.
        self.assertEqual(external["interco_lines"], 1)
        self.assertEqual(every["interco_lines"], 1)

    def test_an_unknown_scope_falls_back_to_external_rather_than_guessing(self):
        self.assertEqual(trail("SOMETHING ELSE")["scope"], SCOPE_EXTERNAL)


class ItemResolutionTests(TestCase):
    """Two item masters, one factory. The codes do not line up.

    Oil and Mart number their items independently past the point their databases
    diverged, so the same code can be two different products. Getting this wrong
    is not a rounding error — it plans the wrong goods.
    """

    def test_a_shared_code_is_accepted_when_both_masters_agree_on_the_name(self):
        payload = trail()
        mart = next(o for o in payload["orders"] if o["company"] == "JIVO_MART")
        self.assertEqual(mart["match"], "code")
        self.assertEqual(mart["item"], "FG_SHORT")

    def test_a_renumbered_item_is_followed_by_name_to_the_right_product(self):
        """Mart calls it FG_MART_9; the factory calls the same bottle FG_SHORT."""
        orders = [dict(o) for o in FakeReader().data["orders"]]
        orders[1]["item"] = "FG_MART_9"
        payload = trail(orders=orders,
                        demand_names={"JIVO_MART": {"FG_MART_9": "SHORT 1 LTR"}})
        mart = next(o for o in payload["orders"] if o["company"] == "JIVO_MART")
        self.assertEqual(mart["match"], "name")
        self.assertEqual(mart["source_item"], "FG_MART_9")
        self.assertEqual(mart["item"], "FG_SHORT")
        # And it lands on the factory's SKU as demand, not as a second SKU.
        self.assertEqual(len(payload["skus"]), 2)

    def test_a_code_that_means_something_else_here_is_not_planned_against_it(self):
        """The expensive one: Mart's FG_SHORT is a 1 LTR bottle, the factory's is
        a 200 LTR drum. Trusting the code explodes 1,000 bottles into 200,000
        litres of the wrong oil."""
        payload = trail(demand_names={"JIVO_MART": {"FG_SHORT": "SOMETHING ELSE ENTIRELY"}})
        mart = next(o for o in payload["orders"] if o["company"] == "JIVO_MART")
        self.assertEqual(mart["item"], "")
        self.assertEqual(mart["match"], "unknown")
        # Nothing was produced or bought for it...
        self.assertEqual(payload["summary"]["units_to_produce"], 0)
        # ...and it is named rather than dropped.
        self.assertEqual(payload["summary"]["unplannable_lines"], 1)
        self.assertEqual(payload["unresolved_demand"][0]["item"], "FG_SHORT")
        self.assertIn("could not be matched", " ".join(payload["caveats"]))

    def test_an_ambiguous_name_is_refused_rather_than_guessed(self):
        """Two factory items share the name, so which one was ordered is not
        knowable from the data. A guess here is a wrong purchase order.

        Only reached when the code itself is not a hit — an exact code-and-name
        match is a direct answer and settles the question on its own.
        """
        items = dict(FakeReader().data["items"])
        items["FG_TWIN"] = dict(items["FG_SHORT"], name="SHORT 1 LTR")
        orders = [dict(o) for o in FakeReader().data["orders"]]
        orders[1]["item"] = "FG_MART_9"
        payload = trail(orders=orders, items=items,
                        demand_names={"JIVO_MART": {"FG_MART_9": "SHORT 1 LTR"}})
        self.assertEqual(payload["unresolved_demand"][0]["lines"], 1)
        self.assertIn("more than one item", payload["unresolved_demand"][0]["reason"])

    def test_unmatched_demand_keeps_its_place_in_the_order_book(self):
        """It is still a real order for real money — it just cannot be planned."""
        payload = trail(demand_names={"JIVO_MART": {"FG_SHORT": "SOMETHING ELSE"}})
        self.assertEqual(payload["summary"]["open_lines"], 2)
        self.assertEqual(payload["summary"]["unplannable_value"], 200000.0)
        self.assertEqual(len(payload["orders"]), 2)


class CoverTests(TestCase):
    """What actually has to be produced."""

    def test_stock_is_pooled_across_both_warehouses(self):
        """60 in Oil plus 40 in Mart covers an order for 100.

        Reading Oil's shelf alone would raise a production alarm for a SKU that
        is sitting in a Mart warehouse ready to ship.
        """
        covered = next(s for s in trail()["skus"] if s["item"] == "FG_COVERED")
        self.assertEqual(covered["onhand"], 100)
        self.assertEqual(covered["onhand_by_company"], {"JIVO_OIL": 60, "JIVO_MART": 40})
        self.assertEqual(covered["to_produce"], 0)

    def test_work_orders_are_netted_off_before_anything_is_called_short(self):
        """1,000 demanded, nothing on the shelf, 200 already on a work order."""
        short = next(s for s in trail()["skus"] if s["item"] == "FG_SHORT")
        self.assertEqual(short["wip"], 200)
        self.assertEqual(short["to_produce"], 800)
        self.assertEqual(trail()["summary"]["units_to_produce"], 800)

    def test_the_gap_is_exploded_through_the_bom_at_the_per_unit_rate(self):
        """A BOM stated for a batch of 10 must not be read as a batch of one."""
        components = {c["item"]: c for c in trail()["components"]}
        self.assertEqual(components["PM_LIVE"]["reqd"], 800)
        self.assertEqual(components["PM_LIVE"]["parents"][0]["per_unit"], 1.0)

    def test_a_covered_sku_is_never_exploded(self):
        """Nothing is bought for an order that ships from the shelf."""
        components = {c["item"] for c in trail()["components"]}
        self.assertNotIn("FG_COVERED", components)


class SupplyTests(TestCase):
    """Which purchase orders are allowed to cancel a shortage."""

    def test_a_believable_po_counts_as_supply(self):
        components = {c["item"]: c for c in trail()["components"]}
        live = components["PM_LIVE"]
        self.assertEqual(live["po_live"], 400)
        self.assertEqual(live["po_stale"], 0)
        # 800 needed - 100 on hand - 400 inbound.
        self.assertEqual(live["short_strict"], 300)

    def test_a_po_that_slipped_past_the_window_funds_nothing(self):
        """The open PO book is full of dead orders; treating them as supply
        silently cancels real shortages."""
        components = {c["item"]: c for c in trail()["components"]}
        dead = components["PM_DEAD"]
        self.assertEqual(dead["po_live"], 0)
        self.assertEqual(dead["po_stale"], 900)
        self.assertEqual(dead["stale_pos"], 1)
        # The strict reading ignores the dead PO and still calls it short...
        self.assertEqual(dead["short_strict"], 650)  # 800 needed - 150 on hand
        # ...while the lenient one, shown for comparison, does not.
        self.assertEqual(dead["short"], 0)

    def test_a_conversion_resource_is_never_a_shortage(self):
        """Filling capacity is a cost line and a constraint. No one can raise a
        purchase order for it, so it must not appear on the buy list."""
        components = {c["item"]: c for c in trail()["components"]}
        self.assertTrue(components["RES_FILL"]["is_resource"])
        self.assertEqual(components["RES_FILL"]["short_strict"], 0)
        self.assertNotIn("RES_FILL", {a["item"] for a in trail()["actions"]})

    def test_the_resource_line_still_reports_its_cost(self):
        resource = trail()["resources"][0]
        self.assertEqual(resource["litres_reqd"], 800)
        self.assertEqual(resource["cost"], 1200)
        self.assertEqual(trail()["summary"]["filling_cost"], 1200)


class ActionTests(TestCase):
    """When the dashboard is allowed to shout."""

    def test_a_shortage_that_had_to_be_ordered_already_is_critical(self):
        """Needed 10 days ago against a 20-day lead time: expediting, not
        scheduling."""
        actions = {a["item"]: a for a in trail()["actions"]}
        self.assertEqual(actions["PM_LIVE"]["urgency"], "CRITICAL")
        self.assertEqual(actions["PM_LIVE"]["value"], 1200)  # 300 short x Rs 4

    def test_a_shortage_with_room_left_is_planned_not_shouted(self):
        """An alarm that fires on everything is an alarm nobody reads."""
        orders = FakeReader().data["orders"]
        relaxed = [dict(o, due=day(365)) for o in orders]
        actions = {a["item"]: a for a in trail(orders=relaxed)["actions"]}
        self.assertEqual(actions["PM_LIVE"]["urgency"], "PLAN")

    def test_no_measured_lead_time_means_no_order_by_date_to_have_missed(self):
        """With no receipt history there is no honest deadline, so the row is
        raised as PLAN with the missing history visible rather than assumed."""
        payload = trail(lead_times={})
        action = next(a for a in payload["actions"] if a["item"] == "PM_LIVE")
        self.assertIsNone(action["lead_avg"])
        self.assertEqual(action["urgency"], "PLAN")

    def test_the_buy_list_is_priced_at_the_last_purchase_price(self):
        self.assertEqual(trail()["summary"]["buy_value"], 1200 + 650)


class MakeVsBuyTests(TestCase):
    """The second answer to a shortage."""

    def test_a_purchased_item_with_its_own_bom_is_a_make_candidate(self):
        """A bottle we buy for Rs 4 and can blow from a Rs 3 preform."""
        row = next(r for r in trail()["makevsbuy"] if r["item"] == "PM_LIVE")
        self.assertEqual(row["buy_price"], 4.0)
        self.assertEqual(row["make_cost"], 3.0)
        self.assertEqual(row["verdict"], "MAKE")
        self.assertEqual(row["sub"], "PM_PREFORM")
        self.assertEqual(row["sub_onhand"], 5000)

    def test_the_make_route_is_offered_on_the_action_that_needs_it(self):
        action = next(a for a in trail()["actions"] if a["item"] == "PM_LIVE")
        self.assertTrue(action["can_make"])
        self.assertEqual(action["make"]["verdict"], "MAKE")


class CapacityTests(TestCase):
    """Can the lines run the gap?"""

    def test_without_the_reference_template_it_says_so_instead_of_passing(self):
        """A green feasibility light nobody has earned is worse than no light."""
        capacity = trail()["capacity"]
        self.assertFalse(capacity["available"])
        self.assertIsNone(capacity["totals"]["feasible"])
        self.assertIn("Reference Template", capacity["reason"])

    def test_with_the_template_the_gap_becomes_hours_on_a_named_machine(self):
        MachineCapacity.objects.create(
            company_code=PRODUCTION_COMPANY, machine_id="M-01", name="PET Line 1",
            location="Plant A", output_per_hour=100, shift_hours=8, shifts_per_day=2,
            working_days_per_month=26, changeover_minutes=30,
        )
        MaterialMachineMap.objects.create(
            company_code=PRODUCTION_COMPANY, sku_code="FG_SHORT", sku_name="SHORT 1 LTR",
            primary_machine_id="M-01", output_on_primary=100,
        )
        capacity = trail()["capacity"]
        machine = capacity["machines"][0]
        self.assertEqual(machine["required_hours"], 8.0)      # 800 pieces at 100/hr
        self.assertEqual(machine["available_hours"], 416.0)   # 8 x 2 x 26
        self.assertEqual(machine["changeover_hours"], 0.5)    # one SKU, 30 minutes
        self.assertTrue(machine["feasible"])

    def test_a_gap_sku_with_no_machine_mapped_is_named_not_silently_dropped(self):
        MachineCapacity.objects.create(
            company_code=PRODUCTION_COMPANY, machine_id="M-01", output_per_hour=100,
            shift_hours=8, shifts_per_day=2, working_days_per_month=26,
        )
        MaterialMachineMap.objects.create(
            company_code=PRODUCTION_COMPANY, sku_code="SOMETHING_ELSE",
            primary_machine_id="M-01", output_on_primary=100,
        )
        capacity = trail()["capacity"]
        self.assertEqual(capacity["totals"]["unmapped_skus"], 1)
        self.assertEqual(capacity["unmapped"][0]["sku"], "FG_SHORT")
        self.assertFalse(capacity["totals"]["feasible"])


class DepartmentRoutingTests(TestCase):
    """Every issue lands on exactly one desk.

    The brief's first complaint is that five departments each keep their own view
    and coordination between them is manual. A shared number fixes half of that;
    the other half is an issue having one owner instead of none.
    """

    def department(self, code, payload=None):
        payload = payload or trail()
        return next(d for d in payload["departments"] if d["code"] == code)

    def test_the_five_departments_the_brief_names_are_always_present(self):
        """A department with nothing to do still appears, saying so. A card that
        vanishes when it is clear looks the same as a card nobody built."""
        codes = [d["code"] for d in trail()["departments"]]
        self.assertEqual(codes, [
            "PRODUCTION", "PACKAGING_PROCUREMENT", "RAW_PROCUREMENT",
            "INFRASTRUCTURE", "FINANCE",
        ])

    def test_a_packaging_shortage_goes_to_the_packaging_buyer(self):
        packaging = self.department("PACKAGING_PROCUREMENT")
        titles = " ".join(a["title"] for a in packaging["actions"])
        self.assertIn("BOTTLE 1 LTR", titles)
        self.assertIn("CAP 1 LTR", titles)

    def test_a_bulk_oil_shortage_never_reaches_the_packaging_buyer(self):
        """"A packaging buyer should not be paged about bulk oil" — the same rule
        the alarm digest already applies, applied here."""
        items = [dict(FakeReader().data["items"]["PM_LIVE"], group="RAW MATERIAL",
                      name="LOOSE OIL")]
        catalogue = dict(FakeReader().data["items"])
        catalogue["PM_LIVE"] = items[0]
        payload = trail(items=catalogue)
        packaging = self.department("PACKAGING_PROCUREMENT", payload)
        raw = self.department("RAW_PROCUREMENT", payload)
        self.assertNotIn("LOOSE OIL", " ".join(a["title"] for a in packaging["actions"]))
        self.assertIn("LOOSE OIL", " ".join(a["title"] for a in raw["actions"]))

    def test_an_issue_belongs_to_one_department_only(self):
        payload = trail()
        seen = {}
        for department in payload["departments"]:
            for action in department["actions"]:
                self.assertNotIn(action["id"], seen,
                                 f"{action['id']} owned by two departments")
                seen[action["id"]] = department["code"]
        self.assertTrue(seen)

    def test_severity_is_earned_from_the_date_not_assigned(self):
        """CRITICAL means the order-by date has passed. An action list where
        everything is red is a list nobody reads."""
        packaging = self.department("PACKAGING_PROCUREMENT")
        by_id = {a["id"]: a for a in packaging["actions"]}
        self.assertEqual(by_id["buy:PM_LIVE"]["severity"], "CRITICAL")
        # A dead PO is a decision to make, not a deadline that was missed.
        self.assertEqual(by_id["chase:PM_DEAD"]["severity"], "WATCH")

    def test_a_shortage_with_no_measured_lead_time_is_a_data_gap_not_a_deadline(self):
        payload = trail(lead_times={})
        packaging = self.department("PACKAGING_PROCUREMENT", payload)
        action = next(a for a in packaging["actions"] if a["id"] == "buy:PM_LIVE")
        self.assertEqual(action["severity"], "WATCH")
        self.assertIn("no order-by date can be measured", action["detail"])

    def test_a_missing_bom_is_productions_problem_and_says_what_it_hides(self):
        payload = trail(boms={})
        production = self.department("PRODUCTION", payload)
        action = next(a for a in production["actions"] if a["kind"] == "MISSING_BOM")
        self.assertIn("invisible to procurement", action["detail"])

    def test_an_unmatched_item_still_gets_an_owner(self):
        """It has no tidy home — an item master disagreeing with itself is nobody's
        obvious job — so it is given one rather than dropped."""
        payload = trail(demand_names={"JIVO_MART": {"FG_SHORT": "SOMETHING ELSE"}})
        production = self.department("PRODUCTION", payload)
        kinds = [a["kind"] for a in production["actions"]]
        self.assertIn("UNMATCHED_ITEM", kinds)

    def test_finance_is_asked_to_release_exactly_the_buy_list(self):
        finance = self.department("FINANCE")
        release = next(a for a in finance["actions"] if a["kind"] == "RELEASE_FUNDS")
        self.assertEqual(release["value"], trail()["summary"]["buy_value"])

    def test_finance_owns_the_decision_on_the_stale_po_book(self):
        finance = self.department("FINANCE")
        stale = next(a for a in finance["actions"] if a["kind"] == "STALE_PO_DECISION")
        self.assertIn("overstating cover", stale["detail"])
        self.assertEqual(stale["severity"], "WATCH")

    def test_infrastructure_is_told_the_capacity_check_cannot_run(self):
        infra = self.department("INFRASTRUCTURE")
        action = next(a for a in infra["actions"] if a["kind"] == "MISSING_REFERENCE")
        self.assertIn("reference template", action["detail"])

    def test_an_over_capacity_line_is_critical_and_names_the_shortfall(self):
        MachineCapacity.objects.create(
            company_code=PRODUCTION_COMPANY, machine_id="M-01", name="PET Line 1",
            output_per_hour=1, shift_hours=1, shifts_per_day=1,
            working_days_per_month=1, changeover_minutes=0,
        )
        MaterialMachineMap.objects.create(
            company_code=PRODUCTION_COMPANY, sku_code="FG_SHORT",
            primary_machine_id="M-01", output_on_primary=1,
        )
        infra = self.department("INFRASTRUCTURE")
        action = next(a for a in infra["actions"] if a["kind"] == "OVER_CAPACITY")
        self.assertEqual(action["severity"], "CRITICAL")
        self.assertIn("over capacity by", action["title"])

    def test_each_action_carries_the_evidence_to_check_it(self):
        """A receiving HOD should be able to verify the row, not just believe it."""
        for department in trail()["departments"]:
            for action in department["actions"]:
                self.assertTrue(action["title"])
                self.assertTrue(action["detail"])
                self.assertIn("subject", action)
                self.assertIn(action["severity"], {"CRITICAL", "PLAN", "WATCH"})

    def test_a_buy_action_names_the_skus_it_blocks(self):
        packaging = self.department("PACKAGING_PROCUREMENT")
        action = next(a for a in packaging["actions"] if a["id"] == "buy:PM_LIVE")
        self.assertIn("SHORT 1 LTR", action["blocks"])

    def test_a_department_with_nothing_to_do_says_so(self):
        empty = [d for d in trail()["departments"] if d["total"] == 0]
        for department in empty:
            self.assertEqual(department["headline"], "Nothing outstanding.")


class TomorrowPlanTests(TestCase):
    """What can actually be RUN tomorrow, which is not the same as what is owed."""

    def plan(self, payload=None):
        return (payload or trail())["tomorrow"]

    def test_the_plan_is_capped_by_material_on_hand_not_by_the_gap(self):
        """800 owed, but only 100 bottles on the shelf. The plan is 100 and a
        purchase order, not 800 and a disappointment."""
        row = next(r for r in self.plan()["rows"] if r["sku"] == "FG_SHORT")
        self.assertEqual(row["to_produce"], 800)
        self.assertEqual(row["planned"], 100)
        self.assertEqual(row["limited_by"], "MATERIAL")

    def test_a_material_capped_row_names_the_component_that_capped_it(self):
        """So the run plan and the buy list point at the same thing."""
        row = next(r for r in self.plan()["rows"] if r["sku"] == "FG_SHORT")
        self.assertIn(row["blocker"]["item"], {"PM_LIVE", "PM_DEAD"})
        self.assertGreater(row["blocker"]["short"], 0)

    def test_shared_stock_is_spent_not_promised_twice(self):
        """Two SKUs, one shared component, 100 units of it.

        Computing each SKU's buildable quantity independently promises the same
        100 caps to both and produces a plan the floor cannot run.
        """
        reader = FakeReader()
        orders = [dict(o) for o in reader.data["orders"]]
        orders.append(dict(orders[1], doc=4, entry=4, item="FG_SECOND",
                           name="SECOND 1 LTR", card="CUSTA000048"))
        items = dict(reader.data["items"])
        items["FG_SECOND"] = dict(items["FG_SHORT"], name="SECOND 1 LTR")
        boms = dict(reader.data["boms"])
        boms["FG_SECOND"] = boms["FG_SHORT"]
        payload = trail(orders=orders, items=items, boms=boms,
                        demand_names={"JIVO_MART": {"FG_SHORT": "SHORT 1 LTR",
                                                    "FG_SECOND": "SECOND 1 LTR"}})
        rows = {r["sku"]: r for r in payload["tomorrow"]["rows"]}
        self.assertIn("FG_SECOND", rows)
        # 100 units of PM_LIVE exist; the two runs together must not exceed it.
        self.assertLessEqual(rows["FG_SHORT"]["planned"] + rows["FG_SECOND"]["planned"], 100)

    def test_the_oldest_promise_is_planned_first(self):
        rows = self.plan()["rows"]
        self.assertEqual(rows[0]["priority"], 1)
        dues = [r["earliest_due"] for r in rows if r["earliest_due"]]
        self.assertEqual(dues, sorted(dues))

    def test_a_sku_with_no_bom_is_not_in_the_run_plan_at_all(self):
        """Nothing can be exploded for it, so a quantity would be invented."""
        payload = trail(boms={})
        self.assertEqual(payload["tomorrow"]["rows"], [])

    def test_the_line_caps_the_run_when_the_reference_template_is_on_file(self):
        MachineCapacity.objects.create(
            company_code=PRODUCTION_COMPANY, machine_id="M-01", name="PET Line 1",
            output_per_hour=10, shift_hours=1, shifts_per_day=1,
            working_days_per_month=26, changeover_minutes=0,
        )
        MaterialMachineMap.objects.create(
            company_code=PRODUCTION_COMPANY, sku_code="FG_SHORT",
            primary_machine_id="M-01", output_on_primary=10,
        )
        row = next(r for r in self.plan()["rows"] if r["sku"] == "FG_SHORT")
        # One hour at 10/hr = 10, which bites before the 100 the material allows.
        self.assertEqual(row["planned"], 10)
        self.assertEqual(row["limited_by"], "CAPACITY")

    def test_a_day_is_one_day_not_a_month(self):
        """working_days_per_month belongs to the monthly feasibility check; using
        it here would plan 26 days of output into tomorrow."""
        MachineCapacity.objects.create(
            company_code=PRODUCTION_COMPANY, machine_id="M-01",
            output_per_hour=1, shift_hours=8, shifts_per_day=2,
            working_days_per_month=26, changeover_minutes=0,
        )
        MaterialMachineMap.objects.create(
            company_code=PRODUCTION_COMPANY, sku_code="FG_SHORT",
            primary_machine_id="M-01", output_on_primary=1,
        )
        row = next(r for r in self.plan()["rows"] if r["sku"] == "FG_SHORT")
        self.assertEqual(row["planned"], 16)  # 8h x 2 shifts x 1/hr

    def test_the_plan_is_never_dated_on_a_sunday(self):
        from datetime import date

        from .services.live_trail_plan import next_working_day
        saturday = date(2026, 8, 8)
        self.assertEqual(next_working_day(saturday).isoformat(), "2026-08-10")

    def test_totals_separate_what_runs_from_what_is_blocked(self):
        totals = self.plan()["totals"]
        self.assertEqual(totals["skus"], 1)
        self.assertEqual(totals["pieces"], 100)


class AutopilotTests(TestCase):
    """The loop closing without anybody opening the dashboard."""

    def setUp(self):
        self.trail = trail()

    def run_it(self, **kwargs):
        from .services.live_trail_autopilot import run_live_trail_autopilot
        return run_live_trail_autopilot(PRODUCTION_COMPANY, trail=self.trail, **kwargs)

    def test_a_dry_run_builds_every_digest_and_sends_nothing(self):
        results = self.run_it(dry_run=True)
        sent = [r for r in results if r.get("sent")]
        self.assertEqual(sent, [])
        self.assertEqual(AlarmDispatch.objects.count(), 0)
        self.assertTrue(any(r.get("title") for r in results))

    def test_a_department_with_nothing_to_do_is_not_paged(self):
        results = {r["department"]: r for r in self.run_it(dry_run=True)}
        quiet = [r for r in results.values() if r["reason"] == "nothing outstanding"]
        self.assertTrue(all(not r.get("sent") for r in quiet))

    def test_production_gets_the_run_plan_and_the_others_do_not(self):
        results = {r["department"]: r for r in self.run_it(dry_run=True)}
        self.assertIn("Run plan for", results["Production"]["body"])
        self.assertNotIn("Run plan for", results["Packaging Procurement"]["body"])

    def test_the_digest_names_what_capped_the_run(self):
        results = {r["department"]: r for r in self.run_it(dry_run=True)}
        self.assertIn("material-capped", results["Production"]["body"])

    def test_with_no_subscription_it_still_reaches_the_supply_chain_team(self):
        """A department nobody has configured must not be a department nobody
        tells. Zero config has to deliver something."""
        from .services.live_trail_autopilot import FALLBACK_PERMISSION
        results = self.run_it(dry_run=True)
        addressed = {r.get("permission") for r in results if r.get("permission")}
        self.assertEqual(addressed, {FALLBACK_PERMISSION})

    def test_a_bound_subscription_narrows_a_department_to_its_own_permission(self):
        AlarmSubscription.objects.create(
            company_code=PRODUCTION_COMPANY, label="Packaging buyers",
            permission_codename="supply_chain.packaging_only",
            live_trail_department="PACKAGING_PROCUREMENT",
        )
        results = {r["department"]: r for r in self.run_it(dry_run=True)}
        self.assertEqual(results["Packaging Procurement"]["permission"],
                         "supply_chain.packaging_only")

    def test_an_unchanged_digest_is_not_re_sent(self):
        """A shortage is a standing condition, not an event. Re-sending the same
        list every morning is how a channel gets muted."""
        from unittest import mock
        with mock.patch("notifications.services.NotificationService."
                        "send_notification_by_permission", return_value=3):
            first = self.run_it()
            second = self.run_it()
        self.assertTrue(any(r.get("sent") for r in first))
        self.assertTrue(all(not r.get("sent") for r in second))
        self.assertTrue(all(r["reason"] == "unchanged since last send"
                            for r in second if not r.get("sent")
                            and r["reason"] != "nothing outstanding"))

    def test_force_re_sends_an_unchanged_digest(self):
        from unittest import mock
        with mock.patch("notifications.services.NotificationService."
                        "send_notification_by_permission", return_value=3):
            self.run_it()
            again = self.run_it(force=True)
        self.assertTrue(any(r.get("sent") for r in again))

    def test_one_departments_delivery_failure_does_not_silence_the_others(self):
        from unittest import mock
        calls = {"n": 0}

        def flaky(**kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("FCM unavailable")
            return 2

        with mock.patch("notifications.services.NotificationService."
                        "send_notification_by_permission", side_effect=flaky):
            results = self.run_it()
        self.assertTrue(any("delivery failed" in str(r.get("reason", "")) for r in results))
        self.assertTrue(any(r.get("sent") for r in results))

    def test_the_scheduled_job_survives_a_bad_morning_in_sap(self):
        """A dead HANA box must not take down the scheduler that also runs work
        permit expiry."""
        from unittest import mock

        from .jobs import run_live_trail_digest
        with mock.patch("supply_chain.jobs.run_live_trail_autopilot",
                        side_effect=RuntimeError("HANA unreachable")):
            run_live_trail_digest()  # must not raise


class DisclosureTests(TestCase):
    """The dashboard has to say what it cannot tell you."""

    def test_the_weak_delivery_dates_are_declared(self):
        caveats = " ".join(trail()["caveats"])
        self.assertIn("ship date equals the order date", caveats)

    def test_a_gap_sku_without_a_bom_is_declared_rather_than_read_as_covered(self):
        """No BOM means nothing was exploded — the buy list is incomplete, and
        saying so is the difference between a gap and a lie."""
        payload = trail(boms={})
        self.assertEqual(payload["summary"]["skus_without_bom"], 1)
        self.assertIn("no bill of materials", " ".join(payload["caveats"]))
