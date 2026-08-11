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
