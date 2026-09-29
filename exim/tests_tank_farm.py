"""
The tank farm and oil lots: what each move does to a lot, what it records, who
may make it, and what the tanks say they hold.

The promises worth pinning down:
  - a lot's litres, rate per litre and total follow its kilograms and rate;
  - dispatch splits a lot, arrive collects arrivals, a move hands a difference
    back only when asked (RETAIN) and never loses it silently;
  - weighing a lot into the tanks below its loaded weight records the shortage
    by EXIM's rule, and the first arrival is always written to the tank log;
  - which lots the tanks still hold is settled from the tanks' own levels,
    oldest lots first, as EXIM did - and only when a dip changes;
  - every endpoint needs EXIM's own right, and nothing crosses companies;
  - the Admin board reads these tanks once the farm has moved here.
"""

from datetime import date, timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase
from rest_framework.test import APIClient, APITestCase

from admin_board import exim_reader
from company.models import Company, UserCompany, UserRole

from . import services_lot as lots
from . import services_tank as tanks
from .models_lot import LotChange, LotShortage, LotStatus, OilLot, TemporaryVendor
from .models_tank import Tank, TankItem, TankKind, TankLog
from .services_licence import EximError

D = Decimal
BASE = "/api/v1/exim/"

TANK_RIGHTS = [
    "view_tankitem", "add_tankitem", "change_tankitem", "delete_tankitem",
    "view_tankdata", "add_tankdata", "change_tankdata", "delete_tankdata",
    "view_itemwise_average", "view_tanklog", "add_opening_rate",
]
LOT_RIGHTS = [
    "view_stockstatus", "add_stockstatus", "change_stockstatus", "delete_stockstatus",
    "view_vehicle_report", "view_debitentry", "view_stockstatusupdatelog",
    "view_contractualhistory", "change_dashboardorder", "add_party", "view_director_report",
]


class FarmMixin:
    def setUp(self):
        super().setUp()
        self.company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        self.mart = Company.objects.create(name="Jivo Mart", code="JIVO_MART")
        self.role = UserRole.objects.create(name="Import / Export")
        self.user = self.make_user("farm@example.com", "FM1", TANK_RIGHTS + LOT_RIGHTS)
        self.canola = TankItem.objects.create(company=self.company, code="RM00CN", name="CANOLA", color="#d95c26")
        self.mustard = TankItem.objects.create(company=self.company, code="RM0MKG", name="MUSTARD KG")

    def make_user(self, email, code, rights=(), company=None):
        user = get_user_model().objects.create_user(
            email=email, password="x", full_name=email, employee_code=code
        )
        UserCompany.objects.create(user=user, company=company or self.company, role=self.role, is_default=True)
        user.user_permissions.add(
            *Permission.objects.filter(content_type__app_label="exim", codename__in=list(rights))
        )
        return user

    def lot(self, status=LotStatus.IN_CONTRACT, qty="10000", rate="120", item=None, **extra):
        return lots.create_lot(
            company=self.company, user=self.user, item=item or self.canola, status=status,
            vendor_code="VENDA000224", vendor_name="AWL AGRI BUSINESS LIMITED",
            rate=D(rate), quantity=D(qty), **extra,
        )

    def tank(self, level="0", item=None, capacity="50000", kind=TankKind.TANK):
        return tanks.create_tank(
            company=self.company, user=self.user, kind=kind, capacity_l=D(capacity),
            item=item, level_l=D(level),
        )

    def fresh(self, lot):
        return OilLot.objects.get(pk=lot.pk)


class LotArithmeticTests(FarmMixin, TestCase):
    def test_litres_rate_per_litre_and_total_follow_the_kilograms(self):
        lot = self.lot(qty="10000", rate="120")
        self.assertEqual(lot.quantity_litres, D("10989.00"))
        self.assertEqual(lot.rate_per_litre, D("109.200"))  # 120 / 1.0989
        self.assertEqual(lot.total, D("1200000.00"))

    def test_creating_a_lot_writes_its_first_values_to_its_history(self):
        lot = self.lot()
        change = LotChange.objects.get(lot=lot)
        self.assertEqual(change.action, "CREATE")
        self.assertEqual(change.changed_by, self.user)
        first = change.field_changes.get()
        self.assertEqual(first.field_name, "__create__")
        self.assertEqual(first.new_value["quantity"], "10000.00")

    def test_a_temporary_vendors_name_is_its_own(self):
        TemporaryVendor.objects.create(company=self.company, code="TEMP0001", name="GARG AGRO")
        lot = lots.create_lot(
            company=self.company, user=self.user, item=self.canola, status=LotStatus.IN_CONTRACT,
            vendor_code="TEMP0001", vendor_name="whatever the browser said", rate=D("1"), quantity=D("1"),
        )
        self.assertEqual(lot.vendor_name, "GARG AGRO")


class DispatchTests(FarmMixin, TestCase):
    def test_part_of_a_contract_leaves_as_a_new_lot_under_it(self):
        contract = self.lot(qty="30000")
        truck = lots.dispatch_lot(
            contract, user=self.user, status=LotStatus.UNDER_LOADING, quantity=D("10000"),
            action=lots.Action.RETAIN, vehicle_number="GJ05AB1234", payment_status="PAID",
        )
        contract = self.fresh(contract)
        self.assertEqual(contract.quantity, D("20000.00"))
        self.assertFalse(contract.deleted)
        self.assertEqual(truck.parent, contract)
        self.assertEqual(truck.status, LotStatus.UNDER_LOADING)
        # EXIM's screen asked for the payment status and the dispatch dropped it.
        self.assertEqual(truck.payment_status, "PAID")
        self.assertEqual(truck.vehicle_number, "GJ05AB1234")

    def test_tolerate_closes_the_source_with_the_dispatch(self):
        contract = self.lot(qty="30000")
        lots.dispatch_lot(contract, user=self.user, status=LotStatus.UNDER_LOADING,
                          quantity=D("10000"), action=lots.Action.TOLERATE)
        self.assertTrue(self.fresh(contract).deleted)

    def test_a_changed_quantity_needs_an_action(self):
        contract = self.lot(qty="30000")
        with self.assertRaises(EximError) as caught:
            lots.dispatch_lot(contract, user=self.user, status=LotStatus.UNDER_LOADING, quantity=D("10000"))
        self.assertEqual(caught.exception.detail["code"], "action_required")

    def test_cannot_dispatch_more_than_the_lot_holds(self):
        contract = self.lot(qty="1000")
        with self.assertRaises(EximError) as caught:
            lots.dispatch_lot(contract, user=self.user, status=LotStatus.UNDER_LOADING,
                              quantity=D("1001"), action=lots.Action.RETAIN)
        self.assertEqual(caught.exception.detail["code"], "dispatch_too_large")


class MoveTests(FarmMixin, TestCase):
    def test_retain_hands_the_difference_back_to_the_storage_lot(self):
        contract = self.lot(qty="30000")
        truck = lots.dispatch_lot(contract, user=self.user, status=LotStatus.UNDER_LOADING,
                                  quantity=D("10000"), action=lots.Action.RETAIN)
        lots.move_lot(truck, user=self.user, status=LotStatus.ON_THE_WAY, quantity=D("9800"),
                      action=lots.Action.RETAIN)
        self.assertEqual(self.fresh(truck).quantity, D("9800.00"))
        self.assertEqual(self.fresh(contract).quantity, D("20200.00"))

    def test_retain_without_a_storage_lot_says_so(self):
        loose = self.lot(status=LotStatus.ON_THE_WAY)
        with self.assertRaises(EximError) as caught:
            lots.move_lot(loose, user=self.user, status=LotStatus.OUT_SIDE_FACTORY, quantity=D("9000"),
                          action=lots.Action.RETAIN)
        self.assertEqual(caught.exception.detail["code"], "retain_without_parent")

    def test_reaching_the_gate_dates_the_arrival_from_the_eta(self):
        eta = date.today() - timedelta(days=1)
        lot = self.lot(status=LotStatus.ON_THE_WAY, eta=eta)
        lots.move_lot(lot, user=self.user, status=LotStatus.OUT_SIDE_FACTORY, quantity=lot.quantity)
        self.assertEqual(self.fresh(lot).arrival_date, eta)

    def test_a_move_does_not_blank_what_it_was_not_given(self):
        lot = self.lot(status=LotStatus.OUT_SIDE_FACTORY, location="SONIPAT, FACTORY",
                       arrival_date=date(2026, 9, 1))
        lots.move_lot(lot, user=self.user, status=LotStatus.IN_WAREHOUSE, quantity=lot.quantity)
        lot = self.fresh(lot)
        self.assertEqual(lot.location, "SONIPAT, FACTORY")
        self.assertEqual(lot.arrival_date, date(2026, 9, 1))

    def test_the_history_records_what_changed(self):
        lot = self.lot(status=LotStatus.ON_THE_WAY)
        lots.move_lot(lot, user=self.user, status=LotStatus.OUT_SIDE_FACTORY, quantity=D("9900"),
                      action=lots.Action.TOLERATE)
        change = LotChange.objects.filter(lot=lot, action="UPDATE").get()
        changed = {f.field_name: (f.old_value, f.new_value) for f in change.field_changes.all()}
        self.assertEqual(changed["status"], ("ON_THE_WAY", "OUT_SIDE_FACTORY"))
        self.assertEqual(changed["quantity"], ("10000.00", "9900.00"))

    def test_the_same_quantity_written_differently_is_not_a_change(self):
        lot = self.fresh(self.lot(status=LotStatus.ON_THE_WAY))
        lots.move_lot(lot, user=self.user, status=LotStatus.OUT_SIDE_FACTORY, quantity=D("10000"))
        change = LotChange.objects.filter(lot=lot, action="UPDATE").get()
        self.assertEqual([f.field_name for f in change.field_changes.all()], ["status"])


class ArriveTests(FarmMixin, TestCase):
    def test_arrivals_from_one_contract_collect_in_one_lot_at_the_refinery(self):
        contract = self.lot(qty="30000")
        first = lots.dispatch_lot(contract, user=self.user, status=LotStatus.OTW_TO_REFINERY,
                                  quantity=D("10000"), action=lots.Action.RETAIN)
        second = lots.dispatch_lot(contract, user=self.user, status=LotStatus.OTW_TO_REFINERY,
                                   quantity=D("10000"), action=lots.Action.RETAIN)
        a = lots.arrive_lot(first, user=self.user, weighed_qty=D("10000"), job_work="VAISHNODEVI REFOILS")
        b = lots.arrive_lot(second, user=self.user, weighed_qty=D("9950"), action=lots.Action.TOLERATE)
        self.assertEqual(a.pk, b.pk)
        pooled = self.fresh(a)
        self.assertTrue(pooled.is_accumulator)
        self.assertEqual(pooled.quantity, D("19950.00"))
        # EXIM's screen sent job_work_vendor; the model's field is job_work.
        self.assertEqual(pooled.job_work, "VAISHNODEVI REFOILS")
        self.assertTrue(self.fresh(first).deleted)
        self.assertTrue(self.fresh(second).deleted)


class IntoTankTests(FarmMixin, TestCase):
    def test_a_short_weight_records_the_shortage_by_eximss_rule(self):
        lot = self.lot(status=LotStatus.OUT_SIDE_FACTORY, qty="20000", rate="120", vehicle_number="HR55")
        lots.into_tank(lot, user=self.user, weighed_qty=D("19900"), bilty_number="BIL7")
        shortage = LotShortage.objects.get(lot=lot)
        self.assertEqual(shortage.load_qty_mt, D("20.000"))
        self.assertEqual(shortage.unload_qty_mt, D("19.900"))
        self.assertEqual(shortage.shortage_mt, D("0.100"))
        self.assertEqual(shortage.allowed_mt, D("0.050"))  # 0.25% of 20 MT
        self.assertEqual(shortage.deducted_mt, D("0.050"))
        self.assertEqual(shortage.rate, D("120000.000"))  # per MT
        self.assertEqual(shortage.deduction_amount, D("6000.000"))
        self.assertEqual(shortage.bilty_number, "BIL7")

    def test_a_loss_within_the_allowance_debits_nothing(self):
        lot = self.lot(status=LotStatus.OUT_SIDE_FACTORY, qty="20000")
        lots.into_tank(lot, user=self.user, weighed_qty=D("19960"))
        self.assertEqual(LotShortage.objects.get(lot=lot).deduction_amount, D("0.000"))

    def test_an_exact_weight_records_no_shortage_but_is_still_logged(self):
        """EXIM only logged an arrival whose weight had changed."""
        lot = self.lot(status=LotStatus.OUT_SIDE_FACTORY, qty="20000")
        lots.into_tank(lot, user=self.user, weighed_qty=D("20000"))
        self.assertFalse(LotShortage.objects.filter(lot=lot).exists())
        log = TankLog.objects.get(lot=lot)
        self.assertEqual(log.quantity_kg, D("20000.00"))
        self.assertEqual(log.item_code, "RM00CN")

    def test_going_back_into_the_tanks_is_not_logged_twice(self):
        lot = self.lot(status=LotStatus.OUT_SIDE_FACTORY, qty="20000")
        lots.into_tank(lot, user=self.user, weighed_qty=D("20000"))
        OilLot.objects.filter(pk=lot.pk).update(status=LotStatus.COMPLETED)
        lots.bulk([self.fresh(lot)], user=self.user, action="mark_in_tank")
        self.assertEqual(TankLog.objects.filter(lot=lot).count(), 1)


class SettleTests(FarmMixin, TestCase):
    """EXIM's rule: the tanks hold the OLDEST in-tank lots; the rest are completed."""

    def in_tank(self, qty_kg, age_days):
        lot = self.lot(status=LotStatus.IN_TANK, qty=qty_kg)
        OilLot.objects.filter(pk=lot.pk).update(created_at=lot.created_at - timedelta(days=age_days))
        return lot

    def test_a_dip_below_the_lots_completes_the_newest(self):
        old = self.in_tank("10000", 10)   # 10,989 L
        new = self.in_tank("10000", 1)    # 10,989 L
        tank = self.tank(level="15000", item=self.canola)
        self.assertEqual(self.fresh(old).status, LotStatus.IN_TANK)
        self.assertEqual(self.fresh(new).status, LotStatus.IN_TANK)  # 4,011 L of it still counted
        tanks.update_tank(tank, user=self.user, level_l=D("9000"))
        self.assertEqual(self.fresh(old).status, LotStatus.IN_TANK)
        self.assertEqual(self.fresh(new).status, LotStatus.COMPLETED)

    def test_a_lot_going_in_is_not_settled_before_the_tank_is_dipped(self):
        self.in_tank("10000", 10)
        self.tank(level="10989", item=self.canola)
        arriving = self.lot(status=LotStatus.OUT_SIDE_FACTORY)
        lots.into_tank(arriving, user=self.user, weighed_qty=D("10000"))
        self.assertEqual(self.fresh(arriving).status, LotStatus.IN_TANK)

    def test_an_oil_no_tank_holds_is_left_alone(self):
        lot = self.in_tank("10000", 1)
        tank = self.tank(level="10989", item=self.canola)
        tanks.empty_tank(tank, user=self.user)
        self.assertEqual(self.fresh(lot).status, LotStatus.IN_TANK)

    def test_opening_the_average_writes_nothing(self):
        self.in_tank("10000", 10)
        new = self.in_tank("10000", 1)
        tank = self.tank(level="15000", item=self.canola)
        Tank.objects.filter(pk=tank.pk).update(level_l=D("9000"))  # a level nobody settled
        tanks.average_cost(self.company, self.canola)
        self.assertEqual(self.fresh(new).status, LotStatus.IN_TANK)


class AverageCostTests(FarmMixin, TestCase):
    def test_the_average_is_over_the_litres_the_lots_account_for(self):
        a = self.lot(status=LotStatus.IN_TANK, qty="10000", rate="110")
        OilLot.objects.filter(pk=a.pk).update(created_at=a.created_at - timedelta(days=5))
        self.lot(status=LotStatus.IN_TANK, qty="10000", rate="120")
        self.tank(level="21978", item=self.canola)  # exactly both lots
        result = tanks.average_cost(self.company, self.canola)
        self.assertEqual(result["matched_l"], D("21978.00"))
        self.assertEqual(result["matched_average_per_kg"], D("115.00"))
        self.assertEqual(len(result["lots"]), 2)
        self.assertIsNone(result["warning"])

    def test_kilograms_are_litres_over_the_density(self):
        """EXIM multiplied here, which made its tonnes a fifth too high."""
        self.tank(level="10989", item=self.canola)
        result = tanks.average_cost(self.company, self.canola)
        self.assertEqual(result["tank_kg"], D("10000.00"))

    def test_litres_no_lot_accounts_for_are_named(self):
        self.lot(status=LotStatus.IN_TANK, qty="1000")
        self.tank(level="5000", item=self.canola)
        self.assertIn("not accounted for", tanks.average_cost(self.company, self.canola)["warning"])


class TankRuleTests(FarmMixin, TestCase):
    def test_tanks_and_totes_are_numbered_in_their_own_series(self):
        self.assertEqual(self.tank().code, "TNK0001")
        self.assertEqual(self.tank().code, "TNK0002")
        self.assertEqual(self.tank(kind=TankKind.TOTE, capacity="1000").code, "TOT001")

    def test_the_lowest_free_number_is_reused(self):
        first, second = self.tank(), self.tank()
        tanks.delete_tank(first)
        self.assertEqual(self.tank().code, "TNK0001")

    def test_a_level_needs_an_oil_and_an_oil_needs_a_level(self):
        with self.assertRaises(EximError) as caught:
            self.tank(level="100")
        self.assertEqual(caught.exception.detail["code"], "oil_required")
        with self.assertRaises(EximError) as caught:
            self.tank(item=self.canola)
        self.assertEqual(caught.exception.detail["code"], "level_required")

    def test_a_level_cannot_pass_the_capacity(self):
        with self.assertRaises(EximError) as caught:
            self.tank(level="50001", item=self.canola)
        self.assertEqual(caught.exception.detail["code"], "level_over_capacity")

    def test_an_oil_in_use_cannot_be_deleted(self):
        self.tank(level="10", item=self.canola)
        with self.assertRaises(EximError) as caught:
            tanks.delete_oil(self.canola)
        self.assertEqual(caught.exception.detail["code"], "oil_in_use")

    def test_the_farm_headline_is_the_tanks_with_the_totes_beside_it(self):
        self.tank(level="25000", item=self.canola)  # 50,000 L tank, half full
        self.tank(kind=TankKind.TOTE, capacity="1000", level="1000", item=self.mustard)
        farm = tanks.tank_summary(self.company)
        self.assertEqual((farm["capacity_l"], farm["level_l"]), (D("50000"), D("25000")))
        self.assertEqual(farm["used_pct"], D("50.00"))
        self.assertEqual(farm["tank_count"], 1)
        self.assertEqual(farm["totes"], {"capacity_l": D("1000"), "level_l": D("1000"), "count": 1})
        self.assertEqual(farm["oil_count"], 2)


class StockDashboardTests(FarmMixin, TestCase):
    def test_kilograms_by_status_and_vendor_in_the_shared_order(self):
        self.lot(status=LotStatus.IN_CONTRACT, qty="1000")
        self.lot(status=LotStatus.OUT_SIDE_FACTORY, qty="500", item=self.mustard)
        self.lot(status=LotStatus.IN_TANK, qty="99999")  # from the tanks, not a column
        lots.reorder_dashboard(self.company, [self.mustard.pk, self.canola.pk])
        board = lots.stock_dashboard(self.company)
        self.assertEqual([r["code"] for r in board["rows"]], ["RM0MKG", "RM00CN"])
        self.assertEqual([c["status"] for c in board["columns"]], ["IN_CONTRACT"])
        self.assertEqual(board["rows"][0]["outside_factory"], D("500.00"))
        self.assertEqual(board["rows"][1]["values"]["IN_CONTRACT__AWL AGRI BUSINESS LIMITED"], D("1000.00"))
        self.assertEqual(board["totals"]["grand_total"], D("1500.00"))

    def test_an_oil_only_in_the_tanks_keeps_its_place_and_vendors_carry_codes(self):
        olive = TankItem.objects.create(company=self.company, code="RMSOLIVE", name="OLIVE")
        self.tank(level="100", item=olive)
        self.lot(status=LotStatus.IN_CONTRACT, qty="1000")
        lots.reorder_dashboard(self.company, [olive.pk, self.canola.pk])
        board = lots.stock_dashboard(self.company)
        self.assertEqual([(r["code"], r["position"]) for r in board["rows"]], [("RMSOLIVE", 1), ("RM00CN", 2)])
        self.assertEqual(board["vendors"], [{"code": "VENDA000224", "name": "AWL AGRI BUSINESS LIMITED"}])
        # A vendor or a stage picked: the tanks know neither, so no tank-only row.
        narrowed = lots.stock_dashboard(self.company, vendor_code="VENDA000224")
        self.assertEqual([r["code"] for r in narrowed["rows"]], ["RM00CN"])


class DirectorInventoryTests(FarmMixin, TestCase):
    def test_sap_down_leaves_the_rest_standing(self):
        from sap_client.exceptions import SAPConnectionError

        self.tank(level="10989", item=self.canola)
        self.lot(status=LotStatus.OUT_SIDE_FACTORY, qty="2000")
        with mock.patch("exim.hana_reader.finished_litres", side_effect=SAPConnectionError("down")):
            result = lots.director_inventory(self.company)
        self.assertIsNone(result["finished"])
        self.assertIn("down", result["finished_reason"])
        self.assertEqual(result["at_factory"]["in_tank"]["mt"], D("10.000"))
        self.assertEqual(result["at_factory"]["outside_factory"]["mt"], D("2.000"))


class AdminBoardSourceTests(FarmMixin, TestCase):
    def test_the_tile_reads_these_tanks_once_they_exist(self):
        self.tank(level="10000", item=self.canola, capacity="50000")
        self.tank(level="0", capacity="20000", kind=TankKind.TOTE)
        reading = exim_reader.read_tanks()
        self.assertTrue(reading.ok)
        self.assertEqual(reading.capacity_tons, 50.0)  # the tote is not the farm
        self.assertEqual(reading.stock_tons, 10.0)
        self.assertEqual(reading.excluded["vessels"], 1)

    def test_before_the_farm_moves_it_still_reads_exim(self):
        with self.settings(DATABASES={"default": {}}):  # no exim alias
            with self.assertLogs("admin_board.exim_reader", level="WARNING"):
                self.assertIn("not connected", exim_reader.read_tanks().reason)


# ---------------------------------------------------------------------------
# The API: rights, companies, shapes
# ---------------------------------------------------------------------------

class FarmAPITests(FarmMixin, APITestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.headers = {"HTTP_COMPANY_CODE": "JIVO_OIL"}

    def api(self, method, path, payload=None, user=None, **params):
        client = self.client
        if user is not None:
            client = APIClient()
            client.force_authenticate(user)
        call = getattr(client, method)
        if method == "get":
            return call(f"{BASE}{path}", params, **self.headers)
        return call(f"{BASE}{path}", payload or {}, format="json", **self.headers)

    def test_a_lot_goes_from_contract_to_tank_through_the_api(self):
        created = self.api("post", "lots/", {
            "item": self.canola.pk, "status": "IN_CONTRACT", "vendor_code": "VENDA000224",
            "vendor_name": "AWL", "rate": "120", "quantity": "20000",
            "contract_start": "2026-09-01", "contract_end": "2026-10-01",
        })
        self.assertEqual(created.status_code, 201, created.data)
        lot = created.data["id"]
        truck = self.api("post", f"lots/{lot}/dispatch/", {
            "status": "UNDER_LOADING", "quantity": "20000", "vehicle_number": "HR55X", "payment_status": "PAID",
        })
        self.assertEqual(truck.status_code, 200, truck.data)
        truck_id = truck.data["id"]
        self.assertEqual(truck.data["parent"], lot)
        moved = self.api("post", f"lots/{truck_id}/move/", {"status": "OUT_SIDE_FACTORY", "quantity": "20000"})
        self.assertEqual(moved.data["status"], "OUT_SIDE_FACTORY")
        weighed = self.api("post", f"lots/{truck_id}/into-tank/", {"weighed_qty": "19900", "bilty_number": "B1"})
        self.assertEqual(weighed.data["status"], "IN_TANK")
        self.assertEqual(len(weighed.data["history"]), 3)  # dispatched, moved, weighed in
        shortages = self.api("get", "shortages/")
        self.assertEqual(shortages.data["totals"]["count"], 1)
        self.assertEqual(len(self.api("get", "tank-log/").data), 1)

    def test_the_list_skips_completed_and_removed_lots_unless_asked(self):
        self.lot()
        done = self.lot(status=LotStatus.COMPLETED)
        gone = self.lot()
        lots.delete_lot(gone, user=self.user)
        ids = [r["id"] for r in self.api("get", "lots/").data]
        self.assertNotIn(done.pk, ids)
        self.assertNotIn(gone.pk, ids)
        completed = [r["id"] for r in self.api("get", "lots/", status="COMPLETED").data]
        self.assertEqual(completed, [done.pk])

    def test_insights_cover_the_lots_the_list_shows(self):
        self.lot(qty="1000", rate="100")
        self.lot(status=LotStatus.COMPLETED, qty="5000", rate="100")
        insights = self.api("get", "lots/insights/").data
        self.assertEqual(insights["count"], 1)
        self.assertEqual(insights["total_value"], D("100000.00"))

    def test_every_lot_endpoint_needs_eximss_own_right(self):
        nobody = self.make_user("nobody@example.com", "NB1", [])
        lot = self.lot()
        for method, path in [
            ("get", "lots/"), ("post", "lots/"), ("get", f"lots/{lot.pk}/"), ("post", f"lots/{lot.pk}/move/"),
            ("get", "tanks/"), ("get", "oils/"), ("get", "tank-log/"), ("get", "shortages/"),
            ("get", "lots/dashboard/"), ("get", "director-inventory/"), ("get", "vendors/"),
        ]:
            self.assertEqual(self.api(method, path, user=nobody).status_code, 403, f"{method} {path}")

    def test_a_viewer_cannot_move_a_lot(self):
        viewer = self.make_user("viewer@example.com", "VW1", ["view_stockstatus"])
        lot = self.lot()
        self.assertEqual(self.api("get", f"lots/{lot.pk}/", user=viewer).status_code, 200)
        response = self.api("post", f"lots/{lot.pk}/move/", {"status": "UNDER_LOADING", "quantity": "10000"},
                            user=viewer)
        self.assertEqual(response.status_code, 403)

    def test_another_companys_lots_and_tanks_do_not_exist_from_here(self):
        mart_oil = TankItem.objects.create(company=self.mart, code="X", name="X")
        theirs = OilLot.objects.create(company=self.mart, item=mart_oil, status="IN_CONTRACT",
                                       vendor_code="V", rate=D("1"), quantity=D("1"))
        their_tank = Tank.objects.create(company=self.mart, code="TNK0001", capacity_l=D("1"))
        self.assertEqual(self.api("get", f"lots/{theirs.pk}/").status_code, 404)
        self.assertEqual(self.api("patch", f"tanks/{their_tank.pk}/", {"level_l": "0"}).status_code, 404)
        created = self.api("post", "lots/", {"item": mart_oil.pk, "status": "IN_CONTRACT", "vendor_code": "V",
                                             "rate": "1", "quantity": "1"})
        self.assertEqual(created.status_code, 400)

    def test_a_dip_through_the_api(self):
        tank = self.tank(level="100", item=self.canola)
        response = self.api("patch", f"tanks/{tank.pk}/", {"level_l": "60000"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "level_over_capacity")
        response = self.api("post", f"tanks/{tank.pk}/empty/")
        self.assertEqual(response.data["level_l"], "0.00")
        self.assertIsNone(response.data["item"])

    def test_opening_stock_is_a_lot_in_the_tanks_from_the_branch(self):
        response = self.api("post", "tanks/opening-stock/", {
            "item": self.canola.pk, "rate_per_litre": "100", "quantity_litres": "10989",
        })
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["status"], "IN_TANK")
        self.assertEqual(response.data["vendor_code"], "VENDA000004")
        self.assertEqual(response.data["quantity"], "10000.00")
        self.assertEqual(response.data["rate"], "109.890")

    def test_the_vendor_list_survives_sap_being_down(self):
        from sap_client.exceptions import SAPConnectionError

        TemporaryVendor.objects.create(company=self.company, code="TEMP0010", name="UKRAINE")
        with mock.patch("exim.views_lot.SAPClient") as client:
            client.return_value.get_active_vendors.side_effect = SAPConnectionError("down")
            response = self.api("get", "vendors/")
        self.assertTrue(response.data["sap_unavailable"])
        self.assertEqual(response.data["vendors"], [{"code": "TEMP0010", "name": "UKRAINE", "temporary": True}])
        added = self.api("post", "vendors/", {"name": "NEW MILL"})
        self.assertEqual(added.data["code"], "TEMP0011")

    def test_bulk_is_all_or_nothing(self):
        a = self.lot(status=LotStatus.COMPLETED)
        b = self.lot(status=LotStatus.IN_CONTRACT)
        response = self.api("post", "lots/bulk/", {"action": "mark_in_tank", "lots": [a.pk, b.pk]})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.fresh(a).status, LotStatus.COMPLETED)

    def test_the_change_log_pages(self):
        for _ in range(3):
            self.lot()
        page = self.api("get", "lots/changes/", page_size="2").data
        self.assertEqual(page["count"], 3)
        self.assertEqual(len(page["results"]), 2)

    def test_the_change_log_finds_a_person_by_name_and_a_day(self):
        self.lot()
        self.assertEqual(self.api("get", "lots/changes/", changed_by="FARM@EX").data["count"], 1)
        other = self.make_user("other@example.com", "OT1", LOT_RIGHTS)
        other.full_name = "Neetu Sharma"
        other.save()
        lots.create_lot(
            company=self.company, user=other, item=self.canola, status=LotStatus.IN_CONTRACT,
            vendor_code="V1", vendor_name="V", rate=D("1"), quantity=D("1"),
        )
        self.assertEqual(self.api("get", "lots/changes/", changed_by="neetu").data["count"], 1)
        today = date.today().isoformat()
        self.assertEqual(self.api("get", "lots/changes/", since=today, until=today).data["count"], 2)
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        self.assertEqual(self.api("get", "lots/changes/", since=tomorrow).data["count"], 0)
        self.assertEqual(self.api("get", "lots/changes/", since="2026-02-30").status_code, 400)

    def test_the_vehicle_report_keeps_contracts_apart_and_names_the_lots(self):
        first = self.lot(contract_end=date(2026, 10, 1))
        second = self.lot(contract_end=date(2026, 12, 1))  # same oil, same vendor, no vehicle
        a = self.lot(status=LotStatus.ON_THE_WAY, vehicle_number="HR55A1", qty="1000")
        b = self.lot(status=LotStatus.ON_THE_WAY, vehicle_number="HR55A1", qty="2000")
        contracts = self.api("get", "lots/vehicle-report/", status="IN_CONTRACT").data
        lines = [line for truck in contracts for line in truck["items"]]
        self.assertEqual(sorted(line["lots"][0] for line in lines), [first.pk, second.pk])
        self.assertEqual(
            {line["lots"][0]: str(line["contract_end"]) for line in lines},
            {first.pk: "2026-10-01", second.pk: "2026-12-01"},
        )
        [truck] = self.api("get", "lots/vehicle-report/", status="ON_THE_WAY").data
        [line] = truck["items"]  # one oil from one vendor on one truck: one line
        self.assertEqual(line["lots"], [a.pk, b.pk])
        self.assertEqual(Decimal(str(line["kg"])), D("3000"))


SAP_OLIVE = {"code": "RMSOLIVE", "name": "OLIVE OIL POMACE (IMPORTED)", "sub_group": "OLIVE", "frozen": False}


class SapOilTests(FarmMixin, APITestCase):
    """An oil is one of SAP's raw-material oils: picked, never typed."""

    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.headers = {"HTTP_COMPANY_CODE": "JIVO_OIL"}

    def post(self, path, payload):
        return self.client.post(f"{BASE}{path}", payload, format="json", **self.headers)

    def patch(self, path, payload):
        return self.client.patch(f"{BASE}{path}", payload, format="json", **self.headers)

    def test_an_oil_takes_its_name_from_sap(self):
        with mock.patch("exim.hana_reader.raw_material_oil", return_value=SAP_OLIVE) as sap:
            response = self.post("oils/", {"code": "RMSOLIVE", "name": "typed", "category": "OLIVE"})
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual((response.data["code"], response.data["name"]), ("RMSOLIVE", SAP_OLIVE["name"]))
        sap.assert_called_once_with("JIVO_OIL", "RMSOLIVE")

    def test_a_code_sap_does_not_have_is_refused(self):
        with mock.patch("exim.hana_reader.raw_material_oil", return_value=None):
            response = self.post("oils/", {"code": "RM-001"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "not_a_sap_oil")
        self.assertFalse(TankItem.objects.filter(code="RM-001").exists())

    def test_sap_down_refuses_the_oil_and_says_why(self):
        from sap_client.exceptions import SAPConnectionError

        with mock.patch("exim.hana_reader.raw_material_oil", side_effect=SAPConnectionError("down")):
            response = self.post("oils/", {"code": "RMSOLIVE"})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.data["code"], "sap_unavailable")

    def test_an_oil_already_here_is_not_added_twice(self):
        with mock.patch("exim.hana_reader.raw_material_oil") as sap:
            response = self.post("oils/", {"code": "rm00cn"})
        self.assertEqual(response.data["code"], "oil_exists")
        sap.assert_not_called()

    def test_a_colour_changes_without_asking_sap_and_a_new_code_takes_sap_name(self):
        with mock.patch("exim.hana_reader.raw_material_oil") as sap:
            response = self.patch(f"oils/{self.canola.pk}/", {"color": "#112233", "code": "RM00CN", "name": "x"})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual((response.data["color"], response.data["name"]), ("#112233", "CANOLA"))
        sap.assert_not_called()
        with mock.patch("exim.hana_reader.raw_material_oil", return_value=SAP_OLIVE):
            response = self.patch(f"oils/{self.canola.pk}/", {"code": "RMSOLIVE"})
        self.assertEqual((response.data["code"], response.data["name"]), ("RMSOLIVE", SAP_OLIVE["name"]))

    def test_the_sap_list_marks_the_oils_already_here(self):
        listed = [dict(SAP_OLIVE), {"code": "RM00CN", "name": "CANOLA OIL", "sub_group": "CANOLA", "frozen": False}]
        with mock.patch("exim.hana_reader.raw_material_oils", return_value=listed):
            data = self.client.get(f"{BASE}oils/sap/", **self.headers).data
        self.assertFalse(data["sap_unavailable"])
        self.assertEqual({o["code"]: o["oil"] for o in data["oils"]}, {"RMSOLIVE": None, "RM00CN": self.canola.pk})

    def test_the_sap_list_says_when_sap_is_down(self):
        from sap_client.exceptions import SAPDataError

        with mock.patch("exim.hana_reader.raw_material_oils", side_effect=SAPDataError("bad")):
            data = self.client.get(f"{BASE}oils/sap/", **self.headers).data
        self.assertEqual(data, {"oils": [], "sap_unavailable": True})

    def test_the_sap_list_needs_the_right_to_add_or_change_an_oil(self):
        viewer = self.make_user("oilviewer@example.com", "OV1", ["view_tankitem"])
        client = APIClient()
        client.force_authenticate(viewer)
        self.assertEqual(client.get(f"{BASE}oils/sap/", **self.headers).status_code, 403)
