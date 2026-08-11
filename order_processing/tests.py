"""Tests for the OMS mirror.

Written against the quirks the live database actually has, not against the
specification — the two differ, and the database is the one that will be read in
production. Nothing here touches OMS or SAP; the reader is mocked.
"""
from datetime import date, datetime, timedelta
from decimal import Decimal
from unittest import mock

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from .integrations.oms import mapper
from .integrations.oms.reader import OmsUnavailable
from .models import (
    LineIssue,
    OmsOrder,
    OmsOrderLine,
    OmsSyncRun,
    OrderState,
    ProcessingEvent,
    SyncStatus,
)
from .routers import OmsReadOnlyRouter
from .services import order_sync

WAREHOUSES = {"OIL": "GP-FG"}


def order_row(oid=1, **kwargs):
    """A raw OMS `orders` row, with the real column names and types."""
    row = {
        "id": oid, "order_number": f"ORD-20260811-{oid:04d}",
        "card_code": "CUSTA000606", "card_name": "JIVO MART PVT LTD",
        "company": "1",                    # varchar in OMS, not an int
        "dispatch_from_id": 2, "dispatch_from_name": "FACTORY",
        "po_number": "", "ship_to_address": "Somewhere",
        "total_amount": Decimal("1280000.00"), "is_foc": False, "remarks": "",
        "order_type": "PARTY", "employee_id": None,
        "delivery_date": "2026-08-13",     # TEXT in OMS, not a date
        "sap_created": True, "sap_doc_number": "12345",
        "quotation_cancelled": False,
        "created_at": datetime(2026, 8, 11, 5, 0, 0),
        "updated_at": datetime(2026, 8, 11, 5, 14, 47),
        "status_code": "COMPLETED",
    }
    row.update(kwargs)
    return row


def line_row(lid=1, order_id=1, **kwargs):
    row = {
        "id": lid, "order_id": order_id,
        "item_code": "FG0000379",
        "item_name": "MUSTARD KACHI GHANI 1 LTR 20 PCS ROUND BOTTLE",
        "category": "OIL", "brand": "JIVO", "sub_group": "MUSTARD",
        "qty": Decimal("8000.00"), "pcs": Decimal("20.00"),
        "boxes": Decimal("400.00"), "ltrs": Decimal("8000.0000"),
        "qty_scheme": Decimal("0.00"), "basic_price": Decimal("160.0000"),
        "total": Decimal("1280000.00"),
    }
    row.update(kwargs)
    return row


@override_settings(OMS_CATEGORY_WAREHOUSE=WAREHOUSES)
class MapperTests(TestCase):
    """The findings from the live data, pinned as behaviour."""

    def test_qty_is_the_quantity_not_pcs(self):
        """Checked against 1,081 real SAP payload lines: qty matched 1,021,
        boxes 96, pcs 23. The spec says pcs is the piece count; it is the pack
        size, and using it would understate every order by that factor."""
        line = mapper.map_line(line_row())
        self.assertEqual(line["quantity"], Decimal("8000.00"))
        self.assertEqual(line["pack_size"], Decimal("20.00"))
        self.assertEqual(line["cases"], Decimal("400.00"))
        self.assertEqual(line["issues"], [])

    def test_rounding_in_boxes_does_not_flag_an_honest_line(self):
        """boxes = round(qty/pcs, 2), so boxes x pcs rarely equals qty exactly.
        197 of 226 apparent mismatches in the live data are only this."""
        line = mapper.map_line(line_row(qty=Decimal("22"), pcs=Decimal("16"),
                                        boxes=Decimal("1.38")))   # 22/16 = 1.375
        self.assertNotIn(LineIssue.QTY_DISAGREES.value, line["issues"])

    def test_a_genuinely_inconsistent_quantity_is_flagged(self):
        """The other 29: qty=128 with boxes=8, pcs=4 -> 8x4=32, not 128. boxes
        and ltrs agree with each other and qty does not, so the line is doubted
        rather than corrected — we do not know which figure is right."""
        line = mapper.map_line(line_row(qty=Decimal("128"), pcs=Decimal("4"),
                                        boxes=Decimal("8"), ltrs=Decimal("160")))
        self.assertIn(LineIssue.QTY_DISAGREES.value, line["issues"])

    def test_the_inverted_convention_is_caught(self):
        """OMS holds two conventions in one column. In the minority, qty is CASES
        and boxes is PIECES -- qty=40, pcs=16, boxes=640 is 40 x 16, not 40 / 16.
        Read as pieces that understates the order 16-fold, and nothing in the row
        says which convention applies. Seen on 15 of 823 live lines."""
        line = mapper.map_line(line_row(qty=Decimal("40"), pcs=Decimal("16"),
                                        boxes=Decimal("640")))
        self.assertIn(LineIssue.QTY_DISAGREES.value, line["issues"])
        # Flagged, not corrected: we cannot know which figure the customer meant.
        self.assertEqual(line["quantity"], Decimal("40"))

    def test_oil_resolves_to_gp_fg(self):
        self.assertEqual(mapper.map_line(line_row())["warehouse_code"], "GP-FG")

    def test_beverages_gets_no_warehouse_and_says_so(self):
        """OMS sends no WarehouseCode for BEVERAGES on 1,598 real lines. Inventing
        one would check stock in the wrong place and look authoritative doing it."""
        line = mapper.map_line(line_row(category="BEVERAGES"))
        self.assertEqual(line["warehouse_code"], "")
        self.assertIn(LineIssue.NO_WAREHOUSE.value, line["issues"])

    def test_missing_item_code_and_zero_quantity_are_flagged(self):
        line = mapper.map_line(line_row(item_code="", qty=Decimal("0")))
        self.assertIn(LineIssue.NO_ITEM_CODE.value, line["issues"])
        self.assertIn(LineIssue.ZERO_QTY.value, line["issues"])

    def test_delivery_date_is_parsed_from_text_and_the_raw_kept(self):
        """orders.delivery_date is TEXT in OMS, so it can hold anything."""
        self.assertEqual(mapper.parse_delivery_date("2026-08-13"), (date(2026, 8, 13), "2026-08-13"))
        self.assertEqual(mapper.parse_delivery_date("13-08-2026")[0], date(2026, 8, 13))
        # Unparseable stays visible rather than silently becoming NULL.
        parsed, raw = mapper.parse_delivery_date("next tuesday")
        self.assertIsNone(parsed)
        self.assertEqual(raw, "next tuesday")
        self.assertEqual(mapper.parse_delivery_date(None), (None, ""))

    def test_company_stays_text(self):
        """OMS stores '1' / '2'; coercing to int would break the day it stops
        being numeric, and company 1 is Jivo Wellness, not 'Oil'."""
        self.assertEqual(mapper.map_order(order_row())["company_code"], "1")


@override_settings(OMS_CATEGORY_WAREHOUSE=WAREHOUSES)
class SyncTests(TestCase):
    def _sync(self, orders, lines, **kwargs):
        with mock.patch.object(order_sync.reader, "fetch_orders", return_value=orders), \
             mock.patch.object(order_sync.reader, "fetch_lines", return_value=lines):
            return order_sync.sync_orders(**kwargs)

    def test_a_first_sync_mirrors_orders_and_lines(self):
        run = self._sync([order_row(1)], [line_row(10, 1)])
        self.assertEqual(run.status, SyncStatus.SUCCESS)
        self.assertEqual((run.orders_created, run.orders_updated, run.lines_written), (1, 0, 1))
        order = OmsOrder.objects.get(oms_order_id=1)
        self.assertEqual(order.order_number, "ORD-20260811-0001")
        self.assertEqual(order.state, OrderState.RECEIVED)
        self.assertEqual(order.lines.get().item_code, "FG0000379")

    def test_syncing_the_same_order_twice_creates_nothing_new(self):
        """Rule 8. The upsert keys on OMS's own ids, which never change."""
        self._sync([order_row(1)], [line_row(10, 1)])
        run = self._sync([order_row(1)], [line_row(10, 1)])
        self.assertEqual(OmsOrder.objects.count(), 1)
        self.assertEqual(OmsOrderLine.objects.count(), 1)
        self.assertEqual((run.orders_created, run.orders_updated), (0, 1))

    def test_a_resync_refreshes_oms_fields_but_never_our_workflow_state(self):
        """OMS owns the order; we own what we decided about it. Resetting an
        allocated order back to RECEIVED would throw away real work."""
        self._sync([order_row(1)], [line_row(10, 1)])
        order = OmsOrder.objects.get(oms_order_id=1)
        order.state = OrderState.STOCK_ALLOCATED
        order.save(update_fields=["state"])

        self._sync([order_row(1, card_name="RENAMED LTD")], [line_row(10, 1)])
        order.refresh_from_db()
        self.assertEqual(order.customer_name, "RENAMED LTD")
        self.assertEqual(order.state, OrderState.STOCK_ALLOCATED)

    def test_a_line_removed_in_oms_disappears_here(self):
        """Otherwise we keep planning production for demand that no longer exists."""
        self._sync([order_row(1)], [line_row(10, 1), line_row(11, 1, item_code="FG0000042")])
        self.assertEqual(OmsOrderLine.objects.count(), 2)
        self._sync([order_row(1)], [line_row(10, 1)])
        self.assertEqual(OmsOrderLine.objects.count(), 1)

    def test_cancellation_in_oms_cancels_here(self):
        self._sync([order_row(1)], [line_row(10, 1)])
        self._sync([order_row(1, quotation_cancelled=True)], [line_row(10, 1)])
        self.assertEqual(OmsOrder.objects.get(oms_order_id=1).state, OrderState.CANCELLED)

    def test_the_watermark_advances_only_past_orders_actually_written(self):
        newer = datetime(2026, 8, 11, 6, 0, 0)          # naive, as OMS stores it
        run = self._sync([order_row(1), order_row(2, updated_at=newer)],
                         [line_row(10, 1), line_row(11, 2)])
        # Stored aware, in the OMS server's own zone — see mapper.to_aware.
        expected = mapper.to_aware(newer)
        self.assertEqual(run.watermark_to, expected)
        self.assertEqual(order_sync.current_watermark(), expected)

    def test_oms_naive_timestamps_become_aware_and_round_trip(self):
        """OMS columns are `timestamp WITHOUT time zone` while this project runs
        USE_TZ=True. Without an explicit zone the first incremental sync raises
        `can't compare offset-naive and offset-aware datetimes`."""
        naive = datetime(2026, 8, 11, 5, 14, 47)
        aware = mapper.to_aware(naive)
        self.assertIsNotNone(aware.tzinfo)
        # And back out again, unchanged, for the watermark we hand to OMS.
        self.assertEqual(mapper.to_naive_oms(aware), naive)

    def test_nothing_changed_is_a_success_not_a_failure(self):
        run = self._sync([], [])
        self.assertEqual(run.status, SyncStatus.SUCCESS)
        self.assertEqual(run.orders_seen, 0)

    def test_one_bad_order_does_not_abort_the_whole_sync(self):
        good, bad = order_row(1), order_row(2)
        bad["id"] = None                    # oms_order_id is NOT NULL -> this row fails
        with mock.patch.object(order_sync.reader, "fetch_orders", return_value=[bad, good]), \
             mock.patch.object(order_sync.reader, "fetch_lines",
                               return_value=[line_row(10, 1)]):
            run = order_sync.sync_orders()
        self.assertTrue(OmsOrder.objects.filter(oms_order_id=1).exists())
        self.assertEqual(run.status, SyncStatus.SUCCESS)
        self.assertTrue(ProcessingEvent.objects.filter(
            event="ORDER_SYNC_FAILED", result="FAILED").exists())

    def test_oms_being_down_fails_the_run_loudly(self):
        with mock.patch.object(order_sync.reader, "fetch_orders",
                               side_effect=OmsUnavailable("connection refused")):
            with self.assertRaises(OmsUnavailable):
                order_sync.sync_orders()
        run = OmsSyncRun.objects.latest("started_at")
        self.assertEqual(run.status, SyncStatus.FAILED)
        self.assertIn("connection refused", run.error)
        self.assertTrue(ProcessingEvent.objects.filter(event="SYNC_FAILED").exists())

    def test_line_issues_are_counted_on_the_run(self):
        run = self._sync([order_row(1)], [line_row(10, 1, category="BEVERAGES")])
        self.assertEqual(run.issues_found, 1)
        self.assertIn(LineIssue.NO_WAREHOUSE.value, OmsOrderLine.objects.get().issues)

    def test_every_sync_leaves_an_audit_trail(self):
        self._sync([order_row(1)], [line_row(10, 1)])
        events = set(ProcessingEvent.objects.values_list("event", flat=True))
        self.assertIn("SYNC_STARTED", events)
        self.assertIn("SYNC_FINISHED", events)
        # One correlation id ties the whole operation together.
        self.assertEqual(len(set(ProcessingEvent.objects.values_list(
            "correlation_id", flat=True))), 1)


@override_settings(OMS_CATEGORY_WAREHOUSE=WAREHOUSES)
class OrderSemanticsTests(TestCase):
    def _order(self, **kwargs):
        with mock.patch.object(order_sync.reader, "fetch_orders", return_value=[order_row(1, **kwargs)]), \
             mock.patch.object(order_sync.reader, "fetch_lines", return_value=[line_row(10, 1)]):
            order_sync.sync_orders()
        return OmsOrder.objects.get(oms_order_id=1)

    @override_settings(OMS_SHIPPING_STATUSES=["COMPLETED"], OMS_PIPELINE_STATUSES=["APPROVED"])
    def test_only_shipping_and_pipeline_statuses_count_as_demand(self):
        """Counting rejected orders makes the factory look permanently short."""
        self.assertTrue(self._order(status_code="COMPLETED").is_demand)
        self.assertTrue(self._order(status_code="APPROVED").is_demand)
        self.assertFalse(self._order(status_code="REJECTED").is_demand)

    @override_settings(OMS_SHIPPING_STATUSES=["COMPLETED"], OMS_PIPELINE_STATUSES=[])
    def test_a_cancelled_order_is_never_demand(self):
        self.assertFalse(self._order(quotation_cancelled=True).is_demand)

    def test_sap_created_decides_whether_sap_already_holds_the_commitment(self):
        """Since July 2026 OMS posts Sales Orders, which commit stock in SAP. So a
        pushed order must not be reserved again locally or it counts twice; an
        unpushed one is demand SAP has never been told about."""
        self.assertTrue(self._order(sap_created=True).committed_in_sap)
        self.assertFalse(self._order(sap_created=False).committed_in_sap)


class RouterTests(TestCase):
    """The credential in use is a superuser. Read-only must be structural."""

    def setUp(self):
        self.router = OmsReadOnlyRouter()

    def test_the_oms_database_is_never_migrated(self):
        self.assertIs(self.router.allow_migrate("oms_orders", "order_processing"), False)
        self.assertIsNone(self.router.allow_migrate("default", "order_processing"))

    def test_no_relation_may_span_into_oms(self):
        ours, theirs = OmsSyncRun(), OmsSyncRun()
        ours._state.db, theirs._state.db = "default", "oms_orders"
        self.assertIs(self.router.allow_relation(ours, theirs), False)

    def test_no_model_routes_to_oms_for_reads_or_writes(self):
        self.assertIsNone(self.router.db_for_read(OmsOrder))
        self.assertIsNone(self.router.db_for_write(OmsOrder))


@override_settings(OMS_CATEGORY_WAREHOUSE=WAREHOUSES)
class CommandTests(TestCase):
    def test_the_command_refuses_to_run_when_oms_is_unreachable(self):
        from django.core.management.base import CommandError

        with mock.patch("order_processing.management.commands.sync_oms_orders.ping",
                        return_value=(False, "connection refused")):
            with self.assertRaises(CommandError):
                call_command("sync_oms_orders")

    def test_check_tests_connectivity_without_syncing(self):
        with mock.patch("order_processing.management.commands.sync_oms_orders.ping",
                        return_value=(True, "2278 orders visible")):
            call_command("sync_oms_orders", "--check", verbosity=0)
        self.assertEqual(OmsSyncRun.objects.count(), 0)


# ─────────────────────────────────────────────────────────────────────────────
# Phase 3-4 — availability
# ─────────────────────────────────────────────────────────────────────────────

from .integrations.sap.inventory import StockLine, StockSnapshot  # noqa: E402
from .services import availability  # noqa: E402

SAP_MAP = {"OIL": "JIVO_OIL"}


def _snapshot(warehouse="GP-FG", error="", **items):
    """A fake SAP reading. `items` is item_code -> (on_hand, committed)."""
    snap = StockSnapshot(warehouse_code=warehouse, company_code="JIVO_OIL", error=error)
    for code, (on_hand, committed) in items.items():
        snap.lines[code] = StockLine(
            item_code=code, warehouse_code=warehouse,
            on_hand=Decimal(on_hand), committed=Decimal(committed),
        )
    return snap


@override_settings(OMS_CATEGORY_WAREHOUSE=WAREHOUSES, OMS_CATEGORY_SAP_COMPANY=SAP_MAP,
                   OMS_SHIPPING_STATUSES=["COMPLETED"], OMS_PIPELINE_STATUSES=["APPROVED"],
                   # Pinned empty: without this the suite reads whatever sourcing
                   # the deployment's .env happens to define, and these tests are
                   # about the booking warehouse alone. SourcingGroupTests covers
                   # the other case explicitly.
                   OMS_WAREHOUSE_SOURCING={})
class AvailabilityTests(TestCase):
    def _order(self, *, qty="100", sap_created=True, status="COMPLETED", **line_kwargs):
        # Keep the line internally consistent unless a test deliberately breaks
        # it: boxes = qty / pcs. Changing qty alone would trip the quantity guard
        # and every availability answer would come back UNKNOWN.
        line_kwargs.setdefault("qty", Decimal(qty))
        pcs = line_kwargs.setdefault("pcs", Decimal("20"))
        if "boxes" not in line_kwargs:
            line_kwargs["boxes"] = (Decimal(qty) / pcs).quantize(Decimal("0.01"))
        with mock.patch.object(order_sync.reader, "fetch_orders",
                               return_value=[order_row(1, sap_created=sap_created,
                                                       status_code=status)]), \
             mock.patch.object(order_sync.reader, "fetch_lines",
                               return_value=[line_row(10, 1, **line_kwargs)]):
            order_sync.sync_orders()
        return OmsOrder.objects.get(oms_order_id=1)

    def _check(self, order, snapshot):
        with mock.patch.object(availability.inventory, "fetch_stock", return_value=snapshot):
            return availability.check_order(order)

    def test_enough_stock_is_available(self):
        result = self._check(self._order(qty="100"), _snapshot(FG0000379=("500", "0")))
        line = result.lines[0]
        self.assertEqual(line.available, Decimal("500"))
        self.assertEqual(line.allocatable, Decimal("100"))
        self.assertEqual(line.short, Decimal("0"))
        self.assertEqual(result.verdict, availability.Verdict.AVAILABLE)

    def test_committed_stock_is_not_available_stock(self):
        """OITW.IsCommited holds SAP's own open sales orders. Ignoring it promises
        stock that is already spoken for."""
        result = self._check(self._order(qty="100"), _snapshot(FG0000379=("500", "450")))
        line = result.lines[0]
        self.assertEqual(line.available, Decimal("50"))
        self.assertEqual(line.short, Decimal("50"))
        self.assertEqual(result.verdict, availability.Verdict.PARTIAL)

    def test_no_free_stock_is_short_not_partial(self):
        result = self._check(self._order(qty="100"), _snapshot(FG0000379=("500", "500")))
        self.assertEqual(result.lines[0].allocatable, Decimal("0"))
        self.assertEqual(result.verdict, availability.Verdict.SHORT)

    def test_negative_sap_availability_reads_as_none_not_as_a_credit(self):
        result = self._check(self._order(qty="10"), _snapshot(FG0000379=("100", "150")))
        self.assertEqual(result.lines[0].available, Decimal("0"))

    def test_orders_already_in_sap_are_not_counted_twice(self):
        """This is the double-count trap. OMS posts Sales Orders, so a pushed
        order is ALREADY inside IsCommited -- subtracting it again would invent a
        shortage that does not exist."""
        # Another pushed order for the same item, same warehouse.
        other = OmsOrder.objects.create(
            oms_order_id=99, order_number="ORD-OTHER", customer_code="C",
            oms_status="COMPLETED", sap_created=True,
        )
        OmsOrderLine.objects.create(
            order=other, oms_line_id=990, item_code="FG0000379",
            quantity=Decimal("400"), warehouse_code="GP-FG", category="OIL",
        )
        result = self._check(self._order(qty="100"), _snapshot(FG0000379=("500", "0")))
        self.assertEqual(result.lines[0].local_demand, Decimal("0"))
        self.assertEqual(result.lines[0].available, Decimal("500"))

    def test_orders_not_yet_in_sap_are_netted_off_locally(self):
        """The mirror image: ~273 orders never reached SAP, so IsCommited knows
        nothing about them. Ignoring those promises the same stock twice."""
        other = OmsOrder.objects.create(
            oms_order_id=99, order_number="ORD-OTHER", customer_code="C",
            oms_status="COMPLETED", sap_created=False,
        )
        OmsOrderLine.objects.create(
            order=other, oms_line_id=990, item_code="FG0000379",
            quantity=Decimal("400"), warehouse_code="GP-FG", category="OIL",
        )
        result = self._check(self._order(qty="100"), _snapshot(FG0000379=("500", "0")))
        line = result.lines[0]
        self.assertEqual(line.local_demand, Decimal("400"))
        self.assertEqual(line.available, Decimal("100"))
        self.assertTrue(any("not yet in SAP" in n for n in line.notes))

    def test_an_order_never_reserves_against_itself(self):
        order = self._order(qty="100", sap_created=False)
        result = self._check(order, _snapshot(FG0000379=("500", "0")))
        self.assertEqual(result.lines[0].local_demand, Decimal("0"))

    def test_sap_being_down_is_unknown_never_zero(self):
        """A zero would read as 'nothing in stock' and could trigger production
        for goods sitting in the warehouse."""
        result = self._check(self._order(qty="100"),
                             _snapshot(error="HANA unreachable"))
        self.assertEqual(result.lines[0].verdict, availability.Verdict.UNKNOWN)
        self.assertEqual(result.verdict, availability.Verdict.UNKNOWN)
        self.assertEqual(result.lines[0].short, Decimal("0"))

    def test_an_item_sap_has_never_stocked_is_unknown_not_zero(self):
        result = self._check(self._order(qty="100"), _snapshot())   # no rows at all
        self.assertEqual(result.lines[0].verdict, availability.Verdict.UNKNOWN)

    def test_a_line_with_an_untrustworthy_quantity_gets_no_stock_answer(self):
        """Its quantity may be in the wrong unit entirely -- see the two OMS
        conventions -- so any availability figure would be fiction."""
        order = self._order(qty="40", pcs=Decimal("16"), boxes=Decimal("640"))
        result = self._check(order, _snapshot(FG0000379=("5000", "0")))
        self.assertEqual(result.lines[0].verdict, availability.Verdict.UNKNOWN)
        self.assertTrue(any("inconsistent" in n for n in result.lines[0].notes))

    def test_beverages_reports_why_it_cannot_be_checked(self):
        order = self._order(category="BEVERAGES")
        result = availability.check_order(order)
        self.assertEqual(result.verdict, availability.Verdict.UNKNOWN)
        self.assertTrue(result.errors)

    def test_the_order_verdict_is_the_worst_of_its_lines(self):
        """A shipment is not partially dispatchable because one line is fine."""
        with mock.patch.object(order_sync.reader, "fetch_orders", return_value=[order_row(1)]), \
             mock.patch.object(order_sync.reader, "fetch_lines",
                               return_value=[line_row(10, 1, qty=Decimal("10"),
                                                      pcs=Decimal("10"), boxes=Decimal("1")),
                                             line_row(11, 1, item_code="FG0000042",
                                                      qty=Decimal("100"), pcs=Decimal("10"),
                                                      boxes=Decimal("10"))]):
            order_sync.sync_orders()
        order = OmsOrder.objects.get(oms_order_id=1)
        snap = _snapshot(FG0000379=("500", "0"), FG0000042=("0", "0"))
        result = self._check(order, snap)
        self.assertEqual(result.verdict, availability.Verdict.PARTIAL)

    def test_sap_company_resolution_is_decided_by_category(self):
        """OMS company '1' carries both OIL and BEVERAGES, so the company code
        alone cannot pick the SAP database."""
        self.assertEqual(availability.sap_company_for("1", "OIL"), "JIVO_OIL")

    def test_an_unmapped_category_never_falls_back_to_the_company_map(self):
        """Item codes are NOT unique across SAP companies: FG0000324 is sesame oil
        in JIVO_OIL and a 500ml water bottle in JIVO_BEVERAGES. Falling back would
        answer a water order with oil stock -- a real number for the wrong
        product, which is worse than no answer."""
        with override_settings(OMS_CATEGORY_SAP_COMPANY={"OIL": "JIVO_OIL"},
                               OMS_COMPANY_SAP_COMPANY={"1": "JIVO_OIL"}):
            self.assertEqual(availability.sap_company_for("1", "BEVERAGES"), "")

    def test_the_company_map_still_covers_a_line_with_no_category(self):
        with override_settings(OMS_CATEGORY_SAP_COMPANY={},
                               OMS_COMPANY_SAP_COMPANY={"2": "JIVO_MART"}):
            self.assertEqual(availability.sap_company_for("2", ""), "JIVO_MART")
            self.assertEqual(availability.sap_company_for("9", ""), "")

    def test_pending_orders_excludes_rejected_and_cancelled(self):
        self._order(status="COMPLETED")
        OmsOrder.objects.create(oms_order_id=50, order_number="R", customer_code="C",
                                oms_status="REJECTED")
        OmsOrder.objects.create(oms_order_id=51, order_number="C", customer_code="C",
                                oms_status="COMPLETED", quotation_cancelled=True)
        self.assertEqual([o.oms_order_id for o in availability.pending_orders()], [1])


@override_settings(OMS_CATEGORY_WAREHOUSE=WAREHOUSES, OMS_CATEGORY_SAP_COMPANY=SAP_MAP,
                   OMS_SHIPPING_STATUSES=["COMPLETED"], OMS_PIPELINE_STATUSES=[],
                   OMS_WAREHOUSE_SOURCING={"GP-FG": ["BH-PF"]})
class SourcingGroupTests(TestCase):
    """The stock is at Bahadurgarh; the orders are booked against GP-FG."""

    def _order(self, qty="100"):
        boxes = (Decimal(qty) / Decimal("20")).quantize(Decimal("0.01"))
        with mock.patch.object(order_sync.reader, "fetch_orders", return_value=[order_row(1)]), \
             mock.patch.object(order_sync.reader, "fetch_lines",
                               return_value=[line_row(10, 1, qty=Decimal(qty),
                                                      pcs=Decimal("20"), boxes=boxes)]):
            order_sync.sync_orders()
        return OmsOrder.objects.get(oms_order_id=1)

    def _check(self, order, booking, supply):
        def fake(company, codes, warehouse):
            return booking if warehouse == "GP-FG" else supply
        with mock.patch.object(availability.inventory, "fetch_stock", side_effect=fake):
            return availability.check_order(order)

    def test_stock_in_a_supplying_warehouse_makes_the_order_fulfillable(self):
        """GP-FG holds 21,557 against 229,583 committed while BH-PF holds 171,582.
        Checking the booking warehouse alone reports SHORT for goods that exist."""
        result = self._check(self._order("100"),
                             booking=_snapshot(FG0000379=("0", "0")),
                             supply=_snapshot("BH-PF", FG0000379=("500", "0")))
        line = result.lines[0]
        self.assertEqual(line.available, Decimal("0"))          # truly none here
        self.assertEqual(line.elsewhere, {"BH-PF": Decimal("500")})
        self.assertEqual(line.available_in_group, Decimal("500"))
        self.assertEqual(line.short, Decimal("0"))
        self.assertEqual(result.verdict, availability.Verdict.AVAILABLE)

    def test_a_promise_met_elsewhere_says_a_transfer_is_needed(self):
        """An answer that hides the transfer is not an instruction anyone can act on."""
        result = self._check(self._order("100"),
                             booking=_snapshot(FG0000379=("0", "0")),
                             supply=_snapshot("BH-PF", FG0000379=("500", "0")))
        self.assertTrue(any("Needs transfer" in n for n in result.lines[0].notes))

    def test_the_group_can_still_be_short(self):
        result = self._check(self._order("1000"),
                             booking=_snapshot(FG0000379=("100", "0")),
                             supply=_snapshot("BH-PF", FG0000379=("200", "0")))
        line = result.lines[0]
        self.assertEqual(line.available_in_group, Decimal("300"))
        self.assertEqual(line.short, Decimal("700"))
        self.assertEqual(result.verdict, availability.Verdict.PARTIAL)

    @override_settings(OMS_WAREHOUSE_SOURCING={})
    def test_without_a_sourcing_group_the_answer_stays_what_sap_says(self):
        """Whether another warehouse may serve an order implies a transfer, which
        is an operational decision this code must not make on its own."""
        result = self._check(self._order("100"),
                             booking=_snapshot(FG0000379=("0", "0")),
                             supply=_snapshot("BH-PF", FG0000379=("500", "0")))
        self.assertEqual(result.lines[0].elsewhere, {})
        self.assertEqual(result.verdict, availability.Verdict.SHORT)


# ─────────────────────────────────────────────────────────────────────────────
# Phase 5-6 — the processing engine and production requirements
# ─────────────────────────────────────────────────────────────────────────────

from .models import (  # noqa: E402
    ProductionRequirement,
    RequirementSource,
    RequirementStatus,
    StockCheck,
)
from .services import processing  # noqa: E402


@override_settings(OMS_CATEGORY_WAREHOUSE=WAREHOUSES, OMS_CATEGORY_SAP_COMPANY=SAP_MAP,
                   OMS_SHIPPING_STATUSES=["COMPLETED"], OMS_PIPELINE_STATUSES=[],
                   OMS_WAREHOUSE_SOURCING={})
class ProcessingEngineTests(TestCase):
    def _mirror(self, oid, qty, *, item="FG0000379", status="COMPLETED", delivery="2026-08-13"):
        boxes = (Decimal(qty) / Decimal("20")).quantize(Decimal("0.01"))
        with mock.patch.object(order_sync.reader, "fetch_orders",
                               return_value=[order_row(oid, status_code=status,
                                                       delivery_date=delivery)]), \
             mock.patch.object(order_sync.reader, "fetch_lines",
                               return_value=[line_row(oid * 10, oid, item_code=item,
                                                      qty=Decimal(qty), pcs=Decimal("20"),
                                                      boxes=boxes)]):
            order_sync.sync_orders()
        return OmsOrder.objects.get(oms_order_id=oid)

    def _process(self, order, snapshot):
        with mock.patch.object(availability.inventory, "fetch_stock", return_value=snapshot):
            return processing.process_order(order)

    def test_a_fully_covered_order_is_ready_and_raises_nothing(self):
        order, check, result = self._process(self._mirror(1, "100"),
                                             _snapshot(FG0000379=("500", "0")))
        self.assertEqual(order.state, OrderState.READY_FOR_FULFILLMENT)
        self.assertEqual(result.verdict, availability.Verdict.AVAILABLE)
        self.assertFalse(ProductionRequirement.objects.exists())
        self.assertEqual(check.verdict, "AVAILABLE")

    def test_a_shortfall_raises_a_production_requirement(self):
        order, _c, _r = self._process(self._mirror(1, "500"),
                                      _snapshot(FG0000379=("100", "0")))
        self.assertEqual(order.state, OrderState.PRODUCTION_REQUIRED)
        req = ProductionRequirement.objects.get()
        self.assertEqual(req.item_code, "FG0000379")
        self.assertEqual(req.quantity, Decimal("400"))
        self.assertEqual(req.status, RequirementStatus.REQUIRED)
        self.assertEqual(req.needed_by, date(2026, 8, 13))

    def test_processing_twice_does_not_double_the_requirement(self):
        """Rule 8. Keyed on the line, so a re-run replaces its contribution."""
        order = self._mirror(1, "500")
        snap = _snapshot(FG0000379=("100", "0"))
        self._process(order, snap)
        self._process(order, snap)
        self.assertEqual(ProductionRequirement.objects.count(), 1)
        self.assertEqual(ProductionRequirement.objects.get().quantity, Decimal("400"))
        self.assertEqual(RequirementSource.objects.count(), 1)

    def test_three_orders_short_of_one_sku_become_one_requirement(self):
        """Three orders short of the same SKU are one thing to produce."""
        snap = _snapshot(FG0000379=("0", "0"))
        for oid, qty in ((1, "100"), (2, "200"), (3, "300")):
            self._process(self._mirror(oid, qty), snap)
        req = ProductionRequirement.objects.get()
        self.assertEqual(req.quantity, Decimal("600"))
        self.assertEqual(req.sources.count(), 3)

    def test_a_requirement_shrinks_when_stock_arrives(self):
        """Re-running after stock moves must CORRECT the requirement, not inflate
        it — that is what a daily re-check is for."""
        order = self._mirror(1, "500")
        self._process(order, _snapshot(FG0000379=("100", "0")))
        self.assertEqual(ProductionRequirement.objects.get().quantity, Decimal("400"))
        self._process(order, _snapshot(FG0000379=("450", "0")))
        self.assertEqual(ProductionRequirement.objects.get().quantity, Decimal("50"))

    def test_a_requirement_is_retired_once_nothing_needs_it(self):
        """Otherwise yesterday's shortage keeps the factory making something
        nobody ordered."""
        order = self._mirror(1, "500")
        self._process(order, _snapshot(FG0000379=("100", "0")))
        self._process(order, _snapshot(FG0000379=("5000", "0")))
        req = ProductionRequirement.objects.get()
        self.assertEqual(req.status, RequirementStatus.CANCELLED)
        self.assertEqual(req.sources.count(), 0)
        self.assertEqual(order.state, OrderState.READY_FOR_FULFILLMENT)

    def test_a_cancelled_order_stops_driving_production(self):
        order = self._mirror(1, "500")
        self._process(order, _snapshot(FG0000379=("0", "0")))
        self.assertEqual(ProductionRequirement.objects.get().quantity, Decimal("500"))

        order.oms_status = "REJECTED"
        order.save(update_fields=["oms_status"])
        self._process(order, _snapshot(FG0000379=("0", "0")))
        self.assertEqual(order.state, OrderState.CANCELLED)
        self.assertEqual(ProductionRequirement.objects.get().status,
                         RequirementStatus.CANCELLED)

    def test_a_requirement_shared_by_two_orders_survives_one_being_cancelled(self):
        snap = _snapshot(FG0000379=("0", "0"))
        first, second = self._mirror(1, "100"), self._mirror(2, "200")
        self._process(first, snap)
        self._process(second, snap)
        self.assertEqual(ProductionRequirement.objects.get().quantity, Decimal("300"))

        first.oms_status = "REJECTED"
        first.save(update_fields=["oms_status"])
        self._process(first, snap)
        req = ProductionRequirement.objects.get()
        self.assertEqual(req.status, RequirementStatus.REQUIRED)
        self.assertEqual(req.quantity, Decimal("200"))

    def test_an_unknown_answer_is_not_treated_as_fulfillable(self):
        """A stock read that failed is not permission to ship."""
        order, _c, _r = self._process(self._mirror(1, "100"),
                                      _snapshot(error="HANA down"))
        self.assertEqual(order.state, OrderState.STOCK_CHECKED)
        self.assertFalse(ProductionRequirement.objects.exists())

    def test_the_check_is_stored_with_its_working(self):
        """A requirement raised last Tuesday has to be explainable by last
        Tuesday's stock, not today's."""
        order, check, _r = self._process(self._mirror(1, "500"),
                                         _snapshot(FG0000379=("100", "20")))
        line = check.lines.get()
        self.assertEqual(line.on_hand, Decimal("100"))
        self.assertEqual(line.committed_in_sap, Decimal("20"))
        self.assertEqual(line.available, Decimal("80"))
        self.assertEqual(line.short, Decimal("420"))
        self.assertEqual(StockCheck.objects.count(), 1)

    def test_the_queue_survives_one_order_failing(self):
        self._mirror(1, "100")
        self._mirror(2, "100")
        calls = {"n": 0}

        def flaky(order, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom")
            return availability.OrderAvailability(
                order_id=order.oms_order_id, order_number=order.order_number,
                company_code="1",
            )

        with mock.patch.object(processing.availability, "check_order", side_effect=flaky):
            tally = processing.process_pending()
        self.assertEqual(tally.get("FAILED"), 1)
        self.assertTrue(ProcessingEvent.objects.filter(
            event="ORDER_PROCESS_FAILED", result="FAILED").exists())

    def test_every_decision_is_auditable(self):
        self._process(self._mirror(1, "500"), _snapshot(FG0000379=("100", "0")))
        event = ProcessingEvent.objects.get(event="ORDER_PROCESSED")
        self.assertEqual(event.new_state, OrderState.PRODUCTION_REQUIRED)
        # 500 needed against 100 free is PARTIAL -- but the STATE is
        # PRODUCTION_REQUIRED, because something still has to be made.
        self.assertEqual(event.detail["verdict"], "PARTIAL")
        self.assertEqual(event.detail["requirements_created"], 1)


# ─────────────────────────────────────────────────────────────────────────────
# Phase 7-8 — BOM explosion, material and procurement planning
# ─────────────────────────────────────────────────────────────────────────────

from .integrations.sap.bom import BomUnavailable  # noqa: E402
from .models import (  # noqa: E402
    MaterialRequirement,
    ProcurementRequirement,
    ProcurementStatus,
)
from .services import material_planning  # noqa: E402


class BomExplosionTests(TestCase):
    """The arithmetic the specification works through: 60 units of A needing
    X=2, Y=1, Z=0.5 gives 120 / 60 / 30."""

    def _components(self, mapping):
        return mock.patch.object(
            material_planning.bom_reader, "fetch_components", side_effect=
            lambda company, codes: {c: mapping[c] for c in codes if c in mapping},
        )

    def test_a_single_level_bom_multiplies_out(self):
        from .integrations.sap import bom as bom_module
        recipe = {"FG-A": [
            {"item_code": "RM-X", "quantity_per_unit": Decimal("2"), "warehouse": ""},
            {"item_code": "RM-Y", "quantity_per_unit": Decimal("1"), "warehouse": ""},
            {"item_code": "RM-Z", "quantity_per_unit": Decimal("0.5"), "warehouse": ""},
        ]}
        with mock.patch.object(bom_module, "fetch_components",
                               side_effect=lambda c, codes: {k: recipe[k] for k in codes if k in recipe}):
            totals, missing = bom_module.explode("JIVO_OIL", "FG-A", Decimal("60"))
        self.assertEqual(totals, {"RM-X": Decimal("120"), "RM-Y": Decimal("60"),
                                  "RM-Z": Decimal("30")})
        self.assertEqual(missing, [])

    def test_a_product_with_no_bom_is_reported_not_treated_as_needing_nothing(self):
        """An empty component list would silently say 'no materials required' and
        the shortfall would evaporate between production and procurement."""
        from .integrations.sap import bom as bom_module
        with mock.patch.object(bom_module, "fetch_components", return_value={}):
            totals, missing = bom_module.explode("JIVO_OIL", "FG-NOBOM", Decimal("10"))
        self.assertEqual(totals, {})
        self.assertEqual(missing, ["FG-NOBOM"])

    def test_a_self_referencing_bom_is_refused_not_followed(self):
        """A cycle would otherwise hang the request forever."""
        from .integrations.sap import bom as bom_module
        recipe = {"FG-A": [{"item_code": "FG-A", "quantity_per_unit": Decimal("1"),
                            "warehouse": ""}]}
        with mock.patch.object(bom_module, "fetch_components",
                               side_effect=lambda c, codes: {k: recipe[k] for k in codes if k in recipe}):
            totals, _missing = bom_module.explode("JIVO_OIL", "FG-A", Decimal("5"), depth=3)
        self.assertEqual(totals, {"FG-A": Decimal("5")})   # one level, then stopped


class MaterialPlanningTests(TestCase):
    def setUp(self):
        self.requirement = ProductionRequirement.objects.create(
            item_code="FG-A", item_name="Product A", warehouse_code="GP-FG",
            sap_company="JIVO_OIL", quantity=Decimal("60"),
            needed_by=date(2026, 8, 20),
        )

    def _plan(self, components, stock, open_po=None, po_fails=False):
        def explode(company, parent, qty, depth=1):
            return ({k: v * qty for k, v in components.items()}, [])
        po = mock.Mock(side_effect=BomUnavailable("po down")) if po_fails \
            else mock.Mock(return_value=open_po or {})
        with mock.patch.object(material_planning.bom_reader, "explode", side_effect=explode), \
             mock.patch.object(material_planning.bom_reader, "fetch_open_po_quantities", po), \
             mock.patch.object(material_planning.inventory, "fetch_stock", return_value=stock):
            return material_planning.plan_materials(self.requirement)

    def test_the_net_requirement_accounts_for_stock_and_open_pos(self):
        """The specification's worked example: required 100, available 40,
        reserved 10, incoming 20 -> net 50."""
        materials, missing, error = self._plan(
            {"RM-X": Decimal("100") / Decimal("60")},          # 100 for 60 units
            _snapshot(**{"RM-X": ("40", "10")}),               # 40 on hand, 10 committed
            open_po={"RM-X": Decimal("20")},
        )
        self.assertEqual((missing, error), ([], ""))
        m = materials[0]
        self.assertEqual(m.gross_required, Decimal("100"))
        self.assertEqual(m.on_hand, Decimal("40"))
        self.assertEqual(m.incoming_po, Decimal("20"))
        self.assertEqual(m.net_required, Decimal("50"))        # 100 - (40-10) - 20

    def test_enough_material_means_nothing_to_buy(self):
        materials, _m, _e = self._plan({"RM-X": Decimal("1")},
                                       _snapshot(**{"RM-X": ("500", "0")}))
        self.assertEqual(materials[0].net_required, Decimal("0"))
        self.assertFalse(materials[0].is_short)

    def test_an_unreadable_po_list_marks_the_net_unusable_rather_than_zero(self):
        """Pretending open POs are zero would over-order every time SAP hiccups."""
        materials, _m, _e = self._plan({"RM-X": Decimal("1")},
                                       _snapshot(**{"RM-X": ("0", "0")}), po_fails=True)
        self.assertFalse(materials[0].stock_known)
        self.assertEqual(materials[0].net_required, Decimal("0"))
        self.assertFalse(materials[0].is_short)

    def test_re_exploding_replaces_rather_than_accumulates(self):
        self._plan({"RM-X": Decimal("1"), "RM-Y": Decimal("1")},
                   _snapshot(**{"RM-X": ("0", "0"), "RM-Y": ("0", "0")}))
        self.assertEqual(MaterialRequirement.objects.count(), 2)
        # The recipe changes to one component; the old line must not linger.
        self._plan({"RM-X": Decimal("1")}, _snapshot(**{"RM-X": ("0", "0")}))
        self.assertEqual(
            list(MaterialRequirement.objects.values_list("item_code", flat=True)), ["RM-X"])

    def test_a_missing_bom_stops_the_explosion_and_says_so(self):
        def explode(company, parent, qty, depth=1):
            return {}, [parent]
        with mock.patch.object(material_planning.bom_reader, "explode", side_effect=explode):
            materials, missing, error = material_planning.plan_materials(self.requirement)
        self.assertEqual(materials, [])
        self.assertEqual(missing, ["FG-A"])
        self.assertTrue(ProcessingEvent.objects.filter(event="BOM_MISSING").exists())


class ProcurementPlanningTests(TestCase):
    def _requirement(self, item, qty="60", warehouse="GP-FG", needed=None):
        return ProductionRequirement.objects.create(
            item_code=item, warehouse_code=warehouse, sap_company="JIVO_OIL",
            quantity=Decimal(qty), needed_by=needed or date(2026, 8, 20),
        )

    def _material(self, requirement, item, net, *, incoming="0"):
        return MaterialRequirement.objects.create(
            requirement=requirement, item_code=item, warehouse_code="GP-FG",
            gross_required=Decimal(net), net_required=Decimal(net),
            incoming_po=Decimal(incoming), stock_known=True,
        )

    def test_a_short_material_becomes_a_procurement_requirement(self):
        self._material(self._requirement("FG-A"), "RM-X", "50")
        material_planning.plan_procurement()
        proc = ProcurementRequirement.objects.get()
        self.assertEqual((proc.item_code, proc.quantity), ("RM-X", Decimal("50")))
        self.assertEqual(proc.status, ProcurementStatus.REQUIRED)

    def test_two_production_runs_needing_one_material_are_one_purchase(self):
        """Two runs needing the same cap are one thing to buy, not two."""
        self._material(self._requirement("FG-A"), "RM-X", "50")
        self._material(self._requirement("FG-B"), "RM-X", "30")
        material_planning.plan_procurement()
        self.assertEqual(ProcurementRequirement.objects.count(), 1)
        self.assertEqual(ProcurementRequirement.objects.get().quantity, Decimal("80"))

    def test_incoming_stock_is_not_counted_once_per_production_run(self):
        """Open POs belong to the ITEM. Summing them per line would count the same
        purchase order twice and under-order."""
        self._material(self._requirement("FG-A"), "RM-X", "50", incoming="20")
        self._material(self._requirement("FG-B"), "RM-X", "30", incoming="20")
        material_planning.plan_procurement()
        self.assertEqual(ProcurementRequirement.objects.get().incoming_po, Decimal("20"))

    def test_the_earliest_need_date_wins(self):
        self._material(self._requirement("FG-A", needed=date(2026, 9, 1)), "RM-X", "10")
        self._material(self._requirement("FG-B", needed=date(2026, 8, 15)), "RM-X", "10")
        material_planning.plan_procurement()
        self.assertEqual(ProcurementRequirement.objects.get().needed_by, date(2026, 8, 15))

    def test_a_procurement_nobody_needs_any_more_is_retired(self):
        """A phantom purchase requirement is worse than none — someone acts on it."""
        material = self._material(self._requirement("FG-A"), "RM-X", "50")
        material_planning.plan_procurement()
        self.assertEqual(ProcurementRequirement.objects.get().status,
                         ProcurementStatus.REQUIRED)
        material.net_required = Decimal("0")
        material.save(update_fields=["net_required"])
        material_planning.plan_procurement()
        self.assertEqual(ProcurementRequirement.objects.get().status,
                         ProcurementStatus.CANCELLED)

    def test_material_with_unknown_stock_never_drives_a_purchase(self):
        m = self._material(self._requirement("FG-A"), "RM-X", "50")
        m.stock_known = False
        m.save(update_fields=["stock_known"])
        material_planning.plan_procurement()
        self.assertFalse(ProcurementRequirement.objects.filter(
            status=ProcurementStatus.REQUIRED).exists())

    def test_replanning_does_not_duplicate(self):
        self._material(self._requirement("FG-A"), "RM-X", "50")
        material_planning.plan_procurement()
        material_planning.plan_procurement()
        self.assertEqual(ProcurementRequirement.objects.filter(
            status=ProcurementStatus.REQUIRED).count(), 1)


@override_settings(OMS_CATEGORY_WAREHOUSE=WAREHOUSES)
class NoWarehouseVisibilityTests(TestCase):
    """A line with no warehouse must READ as a stated gap, not as an empty cell.

    1,641 of 5,300 live lines are in this state — all BEVERAGES, because OMS sends
    no WarehouseCode for that category. An order silently stuck at UNKNOWN with a
    blank warehouse column gives nobody anything to chase.
    """

    def _sync(self, lines):
        with mock.patch.object(order_sync.reader, "fetch_orders", return_value=[order_row(1)]), \
             mock.patch.object(order_sync.reader, "fetch_lines", return_value=lines):
            order_sync.sync_orders()

    def test_a_missing_warehouse_is_labelled_in_words(self):
        self._sync([line_row(10, 1, category="BEVERAGES")])
        line = OmsOrderLine.objects.get()
        self.assertEqual(line.warehouse_code, "")
        self.assertEqual(line.warehouse_label, "NO WAREHOUSE")
        self.assertFalse(line.has_warehouse)

    def test_a_real_warehouse_is_shown_as_itself(self):
        self._sync([line_row(10, 1)])
        line = OmsOrderLine.objects.get()
        self.assertEqual(line.warehouse_label, "GP-FG")
        self.assertTrue(line.has_warehouse)

    def test_the_line_is_flagged_so_it_can_be_found(self):
        self._sync([line_row(10, 1, category="BEVERAGES")])
        self.assertIn(LineIssue.NO_WAREHOUSE.value, OmsOrderLine.objects.get().issues)

    def test_flagged_lines_are_queryable_by_their_issue(self):
        """The filter the report and the UI both use."""
        self._sync([line_row(10, 1), line_row(11, 1, category="BEVERAGES")])
        flagged = OmsOrderLine.objects.with_issue(LineIssue.NO_WAREHOUSE.value)
        self.assertEqual([l.oms_line_id for l in flagged], [11])

    def test_the_serializer_sends_the_label_so_clients_do_not_invent_one(self):
        from .serializers import OmsOrderLineSerializer

        self._sync([line_row(10, 1, category="BEVERAGES")])
        data = OmsOrderLineSerializer(OmsOrderLine.objects.get()).data
        self.assertEqual(data["warehouse_label"], "NO WAREHOUSE")
        self.assertFalse(data["has_warehouse"])
        self.assertEqual(data["warehouse_code"], "")


class ShowLineIssuesCommandTests(TestCase):
    @override_settings(OMS_CATEGORY_WAREHOUSE=WAREHOUSES)
    def test_the_report_runs_and_finds_the_flagged_lines(self):
        with mock.patch.object(order_sync.reader, "fetch_orders", return_value=[order_row(1)]), \
             mock.patch.object(order_sync.reader, "fetch_lines",
                               return_value=[line_row(10, 1, category="BEVERAGES")]):
            order_sync.sync_orders()
        call_command("show_line_issues", verbosity=0)
        call_command("show_line_issues", "--by-item", verbosity=0)
        call_command("show_line_issues", "--issue", "any", verbosity=0)

    def test_the_report_is_quiet_when_nothing_is_flagged(self):
        call_command("show_line_issues", verbosity=0)
