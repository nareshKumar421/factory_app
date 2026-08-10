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

from .models import MachineCapacity, MaterialMachineMap
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
                             "PM_DEAD": {"onhand": 0, "committed": 0},
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
        self.assertEqual(dead["short_strict"], 800)
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
        self.assertEqual(trail()["summary"]["buy_value"], 1200 + 800)


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
