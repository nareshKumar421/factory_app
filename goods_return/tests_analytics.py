"""What the Customer Returns dashboard counts, and how.

The three things worth pinning are the three a reader of the board would get
wrong on their own: which day a return lands on, that a cancelled return is not
a return, and that "leaked" is read out of free text rather than stored.

Uses the DEBIT_NOTE basis throughout -- an invoice-basis return would call SAP.
"""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from company.models import Company, UserCompany, UserRole
from driver_management.models import Driver
from vehicle_management.models import Vehicle

from .analytics import DEFAULT_WINDOW_DAYS, build_dashboard, classify_reason, resolve_window
from .models import GoodsReturn, GoodsReturnItem, GoodsReturnStatus
from .services import GoodsReturnService


class ReasonClassificationTests(TestCase):
    def test_leakage_is_found_in_the_text_the_clerk_actually_types(self):
        for text in ("Oil leaked in transit", "LEAKAGE from cap", "bottles leaking", "spillage"):
            self.assertEqual(classify_reason(text), "LEAKAGE", text)

    def test_a_leak_that_also_mentions_damage_is_still_a_leak(self):
        # The common sentence is "leaked and carton damaged". Ranking DAMAGE
        # first would bury every leak the board exists to surface.
        self.assertEqual(classify_reason("Pouch leaked, carton damaged"), "LEAKAGE")

    def test_short_shelf_life_is_an_expiry_problem_not_a_short_supply(self):
        self.assertEqual(classify_reason("short shelf life remaining"), "EXPIRY")
        self.assertEqual(classify_reason("short quantity supplied"), "WRONG_SHORT")

    def test_nothing_written_and_nothing_matched_are_different_answers(self):
        self.assertEqual(classify_reason(""), "UNSPECIFIED")
        self.assertEqual(classify_reason("   "), "UNSPECIFIED")
        self.assertEqual(classify_reason("customer changed his mind"), "OTHER")


class WindowTests(TestCase):
    def test_no_dates_means_the_last_ninety_days_ending_today(self):
        start, end = resolve_window(None, None)
        self.assertEqual(end, timezone.localdate())
        self.assertEqual((end - start).days, DEFAULT_WINDOW_DAYS - 1)

    def test_a_backwards_window_is_swapped_rather_than_returning_nothing(self):
        today = timezone.localdate()
        start, end = resolve_window(today, today - timedelta(days=7))
        self.assertEqual(start, today - timedelta(days=7))
        self.assertEqual(end, today)


class DashboardTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Jivo Oil", code="OIL")
        self.other = Company.objects.create(name="Jivo Mart", code="MART")
        self.user = get_user_model().objects.create(
            email="clerk@example.com", full_name="Return Clerk"
        )
        self.vehicle = Vehicle.objects.create(vehicle_number="PB01AB1234")
        self.driver = Driver.objects.create(
            name="Ranjit", mobile_no="9990001111", license_no="DL-1"
        )
        self.service = GoodsReturnService(self.company)

    def make_return(self, *, customer="Sharma Traders", code="CUST001", company=None):
        service = GoodsReturnService(company or self.company)
        return service.create_return(
            {
                "basis": "DEBIT_NOTE",
                "customer_name": customer,
                "customer_code": code,
                "vehicle_id": self.vehicle.id,
                "driver_id": self.driver.id,
            },
            self.user,
        )

    def add_line(self, gr, *, item_code="OIL-1L", qty="10", condition="DAMAGED", reason="", price="0"):
        return GoodsReturnItem.objects.create(
            goods_return=gr,
            item_code=item_code,
            item_name=f"{item_code} name",
            uom="CAS",
            return_quantity=qty,
            unit_price=price,
            condition=condition,
            reason=reason,
        )

    def build(self, **kwargs):
        return build_dashboard([self.company.id], **kwargs)

    # -- scope ---------------------------------------------------------------

    def test_a_cancelled_return_counts_only_as_cancelled(self):
        gr = self.make_return()
        self.add_line(gr, qty="25")
        gr.status = GoodsReturnStatus.CANCELLED
        gr.save(update_fields=["status"])

        data = self.build()
        self.assertEqual(data["totals"]["cancelled"], 1)
        self.assertEqual(data["totals"]["returns"], 0)
        self.assertEqual(data["totals"]["quantity"], 0)
        self.assertEqual(data["top_skus"], [])

    def test_another_company_s_returns_are_not_in_this_company_s_totals(self):
        self.add_line(self.make_return(), qty="5")
        self.add_line(self.make_return(company=self.other), qty="500")

        self.assertEqual(self.build()["totals"]["quantity"], 5)
        self.assertEqual(
            build_dashboard([self.company.id, self.other.id])["totals"]["quantity"], 505
        )

    # -- which day a return lands on ------------------------------------------

    def test_a_gated_in_return_counts_on_the_day_it_arrived_not_the_day_it_was_booked(self):
        gr = self.make_return()
        self.add_line(gr, qty="7")
        booked = timezone.now() - timedelta(days=40)
        GoodsReturn.objects.filter(pk=gr.pk).update(
            created_at=booked, gated_in_at=timezone.now(), status=GoodsReturnStatus.ARRIVED
        )

        today = timezone.localdate()
        recent = self.build(from_date=today - timedelta(days=2), to_date=today)
        self.assertEqual(recent["totals"]["returns"], 1)
        self.assertEqual(recent["totals"]["arrived"], 1)

        # ...and it is NOT in the window it was booked in.
        booked_day = timezone.localtime(booked).date()
        self.assertEqual(
            self.build(from_date=booked_day, to_date=booked_day)["totals"]["returns"], 0
        )

    def test_a_return_still_on_the_road_counts_on_the_day_it_was_booked(self):
        self.add_line(self.make_return(), qty="3")
        data = self.build()
        self.assertEqual(data["totals"]["returns"], 1)
        self.assertEqual(data["totals"]["arrived"], 0)
        self.assertEqual(data["totals"]["awaiting_arrival"], 1)
        self.assertFalse(data["recent_returns"][0]["has_arrived"])

    # -- the cuts -------------------------------------------------------------

    def test_condition_and_reason_are_separate_readings_of_the_same_lines(self):
        gr = self.make_return()
        self.add_line(gr, item_code="OIL-1L", qty="10", condition="DAMAGED", reason="Leaked in transit")
        self.add_line(gr, item_code="OIL-5L", qty="4", condition="DAMAGED", reason="Carton dented")
        self.add_line(gr, item_code="OIL-1L", qty="6", condition="GOOD", reason="Not sold")

        data = self.build()
        conditions = {row["condition"]: row["quantity"] for row in data["by_condition"]}
        self.assertEqual(conditions["DAMAGED"], 14)
        self.assertEqual(conditions["GOOD"], 6)

        reasons = {row["reason"]: row["quantity"] for row in data["by_reason"]}
        # Leakage is only visible through the text -- the condition said DAMAGED.
        self.assertEqual(reasons["LEAKAGE"], 10)
        self.assertEqual(reasons["DAMAGE"], 4)
        self.assertEqual(reasons["UNSOLD"], 6)

        # Every bucket is reported even at zero, so the board's colours hold still.
        self.assertEqual(len(data["by_reason"]), 9)

    # -- leakage, across the day LEAKED was added ------------------------------

    def test_leaked_is_its_own_condition_not_a_kind_of_damaged(self):
        gr = self.make_return()
        self.add_line(gr, qty="12", condition="LEAKED", reason="cap seal failed")
        self.add_line(gr, qty="4", condition="DAMAGED", reason="carton dented")

        conditions = {row["condition"]: row["quantity"] for row in self.build()["by_condition"]}
        self.assertEqual(conditions["LEAKED"], 12)
        self.assertEqual(conditions["DAMAGED"], 4)

    def test_a_leak_counts_once_whether_it_was_keyed_or_only_written(self):
        gr = self.make_return()
        # Keyed on the new choice.
        self.add_line(gr, qty="10", condition="LEAKED", reason="")
        # The old way: DAMAGED, with the word only in the reason.
        self.add_line(gr, qty="6", condition="DAMAGED", reason="Oil leaked in transit")
        # Keyed LEAKED *and* saying so -- must not be counted in both halves.
        self.add_line(gr, qty="5", condition="LEAKED", reason="leakage from pouch")

        totals = self.build()["totals"]
        self.assertEqual(totals["leaked_recorded"], 15)
        self.assertEqual(totals["leaked_inferred"], 6)
        self.assertEqual(totals["leaked_quantity"], 21)

    def test_a_leak_is_unsellable_whichever_way_it_was_recorded(self):
        gr = self.make_return()
        self.add_line(gr, qty="8", condition="LEAKED")
        self.add_line(gr, qty="2", condition="GOOD")

        totals = self.build()["totals"]
        self.assertEqual(totals["damaged_quantity"], 8)
        self.assertEqual(self.build()["trend"][-1]["damaged_quantity"], 8)

    def test_nothing_leaked_reads_as_zero_rather_than_going_missing(self):
        self.add_line(self.make_return(), qty="9", condition="GOOD", reason="not sold")
        totals = self.build()["totals"]
        self.assertEqual(totals["leaked_quantity"], 0)
        self.assertEqual(totals["leaked_share"], 0)

    def test_the_sku_table_ranks_by_quantity_and_splits_each_sku_by_condition(self):
        gr = self.make_return()
        self.add_line(gr, item_code="OIL-1L", qty="30", condition="DAMAGED", reason="leaked")
        self.add_line(gr, item_code="OIL-1L", qty="10", condition="GOOD")
        self.add_line(gr, item_code="OIL-5L", qty="5", condition="EXPIRED")

        top = self.build()["top_skus"]
        self.assertEqual([row["item_code"] for row in top], ["OIL-1L", "OIL-5L"])
        self.assertEqual(top[0]["quantity"], 40)
        self.assertEqual(top[0]["conditions"]["DAMAGED"], 30)
        self.assertEqual(top[0]["conditions"]["GOOD"], 10)
        self.assertEqual(top[0]["reasons"]["LEAKAGE"], 30)
        self.assertEqual(top[0]["share"], 88.9)

    def test_a_customer_whose_paperwork_is_not_keyed_in_is_still_a_returning_customer(self):
        # A truck at the gate with no lines yet is the gap the board should show.
        self.make_return(customer="Bansal Stores", code="CUST009")
        customers = self.build()["top_customers"]
        self.assertEqual(len(customers), 1)
        self.assertEqual(customers[0]["returns"], 1)
        self.assertEqual(customers[0]["lines"], 0)

    def test_value_only_counts_the_lines_that_carry_a_price(self):
        gr = self.make_return()
        self.add_line(gr, qty="10", price="150.00")
        self.add_line(gr, qty="10", price="0")  # hand-keyed DN line, no price
        self.assertEqual(self.build()["totals"]["value"], 1500)

    def test_a_long_window_is_trended_by_month_and_a_short_one_by_day(self):
        self.add_line(self.make_return(), qty="2")
        today = timezone.localdate()

        short = self.build(from_date=today - timedelta(days=6), to_date=today)
        self.assertEqual(short["window"]["granularity"], "day")
        self.assertEqual(short["trend"][0]["bucket"], today.isoformat())

        long = self.build(from_date=today - timedelta(days=300), to_date=today)
        self.assertEqual(long["window"]["granularity"], "month")
        self.assertEqual(long["trend"][-1]["bucket"], f"{today.year:04d}-{today.month:02d}")

    def test_the_trend_separates_the_part_that_came_back_unsellable(self):
        gr = self.make_return()
        self.add_line(gr, qty="8", condition="DAMAGED", reason="leaked")
        self.add_line(gr, qty="2", condition="GOOD")

        point = self.build()["trend"][-1]
        self.assertEqual(point["returns"], 1)
        self.assertEqual(point["quantity"], 10)
        self.assertEqual(point["damaged_quantity"], 8)
        self.assertEqual(self.build()["totals"]["damaged_share"], 80.0)


class DashboardEndpointTests(TestCase):
    """The route the board calls: the plain view right, and the active company only.

    The permission check is the point of the first test — the board reports the
    same returns its reader can already open one at a time, and gating it on
    anything the returns clerk does not already hold would have meant a new
    permission row on the live database before anyone could use it.
    """

    def setUp(self):
        self.company = Company.objects.create(name="Jivo Oil", code="OIL")
        self.other = Company.objects.create(name="Jivo Mart", code="MART")
        self.user = get_user_model().objects.create(
            email="board@example.com", full_name="Returns Desk"
        )
        role = UserRole.objects.create(name="Returns")
        UserCompany.objects.create(
            user=self.user, company=self.company, role=role, is_active=True
        )
        UserCompany.objects.create(
            user=self.user, company=self.other, role=role, is_active=True
        )
        self.vehicle = Vehicle.objects.create(vehicle_number="PB07XY9999")
        self.driver = Driver.objects.create(
            name="Sukhdev", mobile_no="9990002222", license_no="DL-2"
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def grant(self, codename):
        self.user.user_permissions.add(
            Permission.objects.get(
                codename=codename,
                content_type=ContentType.objects.get_for_model(GoodsReturn),
            )
        )
        self.user = get_user_model().objects.get(pk=self.user.pk)  # drop the perm cache
        self.client.force_authenticate(self.user)

    def get(self, **params):
        return self.client.get(
            "/api/v1/goods-return/dashboard/", params, HTTP_COMPANY_CODE="OIL"
        )

    def seed(self, company, qty, *, reason="Leaked in transit"):
        gr = GoodsReturnService(company).create_return(
            {
                "basis": "DEBIT_NOTE",
                "customer_name": "Sharma Traders",
                "customer_code": "CUST001",
                "vehicle_id": self.vehicle.id,
                "driver_id": self.driver.id,
            },
            self.user,
        )
        GoodsReturnItem.objects.create(
            goods_return=gr,
            item_code="OIL-1L",
            item_name="Jivo Canola 1L",
            uom="CAS",
            return_quantity=qty,
            condition="DAMAGED",
            reason=reason,
        )
        return gr

    def test_the_plain_view_right_is_enough_to_open_the_board(self):
        self.assertEqual(self.get().status_code, 403)

        self.grant("can_view_goods_return")
        self.assertEqual(self.get().status_code, 200)

    def test_it_reports_the_active_company_until_all_companies_is_asked_for(self):
        self.grant("can_view_goods_return")
        self.seed(self.company, "10")
        self.seed(self.other, "90")

        self.assertEqual(self.get().data["totals"]["quantity"], 10)
        self.assertEqual(self.get(all_companies=1).data["totals"]["quantity"], 100)

    def test_it_carries_the_leakage_reading_the_stored_condition_cannot(self):
        self.grant("can_view_goods_return")
        self.seed(self.company, "10")

        data = self.get().data
        conditions = {row["condition"]: row["quantity"] for row in data["by_condition"]}
        reasons = {row["reason"]: row["quantity"] for row in data["by_reason"]}
        self.assertEqual(conditions["DAMAGED"], 10)
        self.assertEqual(reasons["LEAKAGE"], 10)

    def test_an_unparseable_date_falls_back_to_the_default_window(self):
        self.grant("can_view_goods_return")
        response = self.get(from_date="not-a-date")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["window"]["days"], DEFAULT_WINDOW_DAYS)
