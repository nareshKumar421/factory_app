"""The dispatch bill copy: kept current by what SAP says changed, and answering
the dispatch reader's reads when HANA cannot be asked.

No HANA. Taking the copy runs against a fake reader (``sap_mirror.bills._reader``);
serving it runs the real ``HanaDispatchBillReader`` with ``HanaConnection.connect``
patched to fail the way an unreachable HANA does.
"""

from datetime import date, datetime
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone
from hdbcli import dbapi

from company.models import Company
from dispatch_plans.hana_reader import HanaDispatchBillReader
from dispatch_plans.serializers import DispatchBillFilterSerializer
from dispatch_plans.services import DispatchPlansService
from sap_client.context import CompanyContext
from sap_client.exceptions import SAPConnectionError, SAPDataError
from sap_client.hana.connection import HanaConnection

from . import bills, services
from .models import MirrorDataset, MirroredBill

NOW = timezone.make_aware(datetime(2026, 9, 30, 10, 0))
HANA_DOWN = dbapi.OperationalError(-10709, "Connection failed (rc=111:Connection refused)")


def bill(entry, num, created, *, warehouse="BH-PC", dispatched=None, branch="Panchkula", time="10:15"):
    return {
        "doc_entry": entry, "doc_num": str(num), "doc_date": created, "create_date": created,
        "create_time": time, "card_code": "CUST001", "card_name": "Sharma Traders",
        "doc_total": 1180.0, "branch_id": 3, "branch_name": branch, "ship_to_code": "GGN",
        "ship_to_address": "Gurugram", "state": "HR", "city": "Gurugram", "bp_gstin": "06ABC",
        "sap_dispatch_date": dispatched, "sap_bilty_no": "", "sap_bilty_date": None,
        "sap_transporter_name": "", "sap_vehicle_no": "", "sap_transporter_invoice": "",
        "sap_lr_number": "", "sap_eway_bill": "", "gst_vehicle_no": "", "gst_transport_date": None,
        "gst_transport_reason": "", "line_count": 1, "total_quantity": 100.0,
        "total_litres": 100.0, "total_boxes": 8.0, "total_loose": 4.0, "total_weight": 92.0,
        "total_line_amount": 1000.0, "total_gross_amount": 1180.0, "warehouses": warehouse,
        "item_summary": "FG0000151 - Olive Oil 1 LTR", "base_refs": "",
    }


def line(warehouse="BH-PC", quantity=100.0):
    return {
        "line_num": 0, "item_code": "FG0000151", "item_name": "Olive Oil 1 LTR",
        "quantity": quantity, "uom": "PCS", "rate": 10.0, "line_total": 1000.0,
        "gross_total": 1180.0, "warehouse_code": warehouse, "base_ref": "", "base_entry": None,
        "base_type": None, "tax_code": "CG+SG@18", "total_litres": 100.0, "total_boxes": 8.0,
        "total_loose": 4.0, "total_weight": 92.0, "sal_factor2": 12.0,
    }


def pickable(entry, num, warehouse="BH-PC"):
    return {
        "doc_entry": entry, "doc_num": str(num), "card_code": "CUST001",
        "card_name": "Sharma Traders", "line_num": 0, "item_code": "FG0000151",
        "item_name": "Olive Oil 1 LTR", "uom": "PCS", "warehouse_code": warehouse,
        "quantity": Decimal("100"), "pcs_per_box": Decimal("12"), "boxes": Decimal("8.333"),
        "litres": Decimal("100"), "gross_weight": Decimal("92.5"), "sal_factor2": Decimal("12"),
        "sal_factor3": Decimal("0"), "dispatched_qty": Decimal("0"),
        "sap_dispatch_date": None, "sap_bilty_no": "",
    }


class FakeReader:
    """SAP's bills for one company, and a count of what the copy asked for."""

    def __init__(self):
        self.bills = {}       # doc_entry -> (bill, lines, pickable, version)
        self.credited = set()
        self.down = False
        self.read = []        # doc entries read in full

    def add(self, entry, num, created, version="2026-09-30|101500", **kwargs):
        warehouse = kwargs.get("warehouse", "BH-PC")
        self.bills[entry] = (
            bill(entry, num, created, **kwargs), [line(warehouse)],
            [pickable(entry, num, warehouse)], version,
        )

    def _check(self):
        if self.down:
            raise SAPConnectionError("Unable to connect to SAP HANA.")

    def bill_versions(self, created_from):
        self._check()
        return {
            entry: data[3] for entry, data in self.bills.items()
            if data[0]["create_date"] >= created_from.isoformat()
        }

    def credited_doc_entries(self, created_from):
        self._check()
        return set(self.credited)

    def list_bills(self, filters):
        self._check()
        self.read.extend(filters["doc_entries"])
        return [dict(self.bills[e][0]) for e in filters["doc_entries"] if e in self.bills]

    def list_bill_lines_for(self, entries):
        return {e: list(self.bills[e][1]) for e in entries if e in self.bills}

    def list_pickable_lines(self, entries):
        return [dict(p) for e in entries if e in self.bills for p in self.bills[e][2]]


class BillCopyTestCase(TestCase):
    def setUp(self):
        self.oil = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        self.sap = FakeReader()
        self.sap.add(9001, 626090001, "2026-09-29")
        self.sap.add(9002, 626090002, "2026-09-30", warehouse="BH-BT", time="09:00")
        self.sap.add(9003, 626090003, "2026-09-30", dispatched="2026-09-30")
        patcher = patch("sap_mirror.bills._reader", return_value=self.sap)
        patcher.start()
        self.addCleanup(patcher.stop)

    def take_copy(self, now=NOW):
        return services.refresh(self.oil, services.BILLS, now)

    def copied(self):
        return set(MirroredBill.objects.filter(company=self.oil).values_list("doc_entry", flat=True))


class TakingTheBillCopyTests(BillCopyTestCase):
    def test_the_first_run_copies_every_live_bill_of_the_window(self):
        self.sap.add(8000, 626080000, "2026-08-20")  # older than 30 days

        state = self.take_copy()

        self.assertEqual(state.row_count, 3)
        self.assertEqual(state.synced_at, NOW)
        self.assertEqual(self.copied(), {9001, 9002, 9003})
        row = MirroredBill.objects.get(doc_entry=9002)
        self.assertEqual(row.warehouse_codes, "|BH-BT|")
        self.assertEqual(row.create_date, date(2026, 9, 30))
        self.assertTrue(MirroredBill.objects.get(doc_entry=9003).sap_dispatched)

    def test_only_what_sap_says_changed_is_read_again(self):
        self.take_copy()
        self.sap.read.clear()
        # Stamped as dispatched in SAP: the header's update stamp moves.
        stamped = self.sap.bills[9001]
        self.sap.bills[9001] = (
            {**stamped[0], "sap_dispatch_date": "2026-09-30"}, stamped[1], stamped[2],
            "2026-09-30|113000",
        )
        self.sap.add(9004, 626090004, "2026-09-30")

        self.take_copy(timezone.make_aware(datetime(2026, 9, 30, 11, 45)))

        self.assertEqual(sorted(self.sap.read), [9001, 9004])
        self.assertTrue(MirroredBill.objects.get(doc_entry=9001).sap_dispatched)

    def test_a_bill_sap_no_longer_lists_leaves_the_copy(self):
        self.take_copy()
        del self.sap.bills[9002]  # cancelled

        self.take_copy()

        self.assertEqual(self.copied(), {9001, 9003})

    def test_a_credit_note_marks_the_bill(self):
        self.take_copy()
        self.sap.credited = {9003}

        self.take_copy()

        self.assertTrue(MirroredBill.objects.get(doc_entry=9003).credited)
        self.sap.credited = set()  # the credit note was cancelled
        self.take_copy()
        self.assertFalse(MirroredBill.objects.get(doc_entry=9003).credited)

    def test_sap_not_answering_keeps_the_copy(self):
        self.take_copy()
        self.sap.down = True

        state = self.take_copy(timezone.make_aware(datetime(2026, 9, 30, 10, 15)))

        self.assertEqual(state.synced_at, NOW)
        self.assertIn("the copy was kept", state.last_error)
        self.assertEqual(self.copied(), {9001, 9002, 9003})

    def test_the_bills_are_due_at_every_run_and_the_lists_once_a_night(self):
        state = self.take_copy()
        self.assertFalse(services.is_due(state, NOW + (services.DATASETS[services.BILLS].every / 2),
                                         services.DATASETS[services.BILLS].every))
        self.assertTrue(services.is_due(state, timezone.make_aware(datetime(2026, 9, 30, 10, 15)),
                                        services.DATASETS[services.BILLS].every))


class ServingTheBillCopyTests(BillCopyTestCase):
    def setUp(self):
        super().setUp()
        self.take_copy()
        down = patch.object(HanaConnection, "connect", side_effect=HANA_DOWN)
        down.start()
        self.addCleanup(down.stop)
        self.reader = HanaDispatchBillReader(CompanyContext("JIVO_OIL"))

    def window(self, **filters):
        return {"date_from": date(2026, 9, 1), "date_to": date(2026, 9, 30), **filters}

    def test_the_bill_list_comes_from_the_copy_newest_first_and_says_how_old_it_is(self):
        rows = self.reader.list_bills(self.window())

        self.assertEqual([r["doc_entry"] for r in rows], [9003, 9002, 9001])
        self.assertEqual(rows[0]["sap_copy_as_of"], timezone.localtime(NOW).isoformat())
        self.assertEqual(rows[0]["total_litres"], 100.0)

    def test_the_lists_filters_are_honoured(self):
        ids = lambda **f: [r["doc_entry"] for r in self.reader.list_bills(self.window(**f))]

        self.assertEqual(ids(warehouse="bh-bt"), [9002])
        self.assertEqual(ids(exclude_sap_dispatched=True), [9002, 9001])
        self.assertEqual(ids(branch="panchkula"), [9003, 9002, 9001])
        self.assertEqual(ids(branch="3"), [9003, 9002, 9001])
        self.assertEqual(ids(limit=1), [9003])
        self.assertEqual(
            [r["doc_entry"] for r in self.reader.list_bills(
                {"date_from": date(2026, 9, 29), "date_to": date(2026, 9, 29)})],
            [9001],
        )
        MirroredBill.objects.filter(doc_entry=9001).update(credited=True)
        self.assertEqual(ids(exclude_credited=True), [9003, 9002])

    def test_a_bill_by_number_comes_with_its_lines(self):
        found = self.reader.get_bill_by_number("626090002")

        self.assertEqual(found["doc_entry"], 9002)
        self.assertEqual(found["items"][0]["warehouse_code"], "BH-BT")

    def test_picking_lines_keep_their_types(self):
        lines = self.reader.list_pickable_lines([9002, 9001])

        self.assertEqual([l["doc_entry"] for l in lines], [9001, 9002])
        self.assertEqual(lines[0]["boxes"], Decimal("8.333"))
        self.assertIsInstance(lines[0]["gross_weight"], Decimal)

    def test_a_bill_the_copy_does_not_hold_is_still_sap_being_down(self):
        with self.assertRaises(SAPConnectionError):
            self.reader.get_bill_by_number("626089999")
        with self.assertRaises(SAPConnectionError):
            self.reader.list_bills_by_doc_entries([9001, 7777])
        with self.assertRaises(SAPConnectionError):
            self.reader.list_pickable_lines([9001, 7777])
        with self.assertRaises(SAPConnectionError):
            self.reader.list_bills({"date_from": date(2026, 7, 1), "date_to": date(2026, 7, 31)})

    def test_the_copy_job_never_reads_the_copy(self):
        with self.assertRaises(SAPConnectionError):
            HanaDispatchBillReader(CompanyContext("JIVO_OIL"), use_copy=False).list_bills(self.window())

    def test_the_dispatch_service_works_from_the_copy(self):
        filters = DispatchBillFilterSerializer(
            data={"date_from": "2026-09-01", "date_to": "2026-09-30"}
        )
        self.assertTrue(filters.is_valid(), filters.errors)

        result = DispatchPlansService(company_code="JIVO_OIL").get_bills(filters.validated_data)

        self.assertEqual(
            sorted(row["doc_entry"] for row in result["data"]), [9001, 9002, 9003]
        )


class RefusedQueryTests(BillCopyTestCase):
    def test_a_refused_query_is_not_answered_from_the_copy(self):
        self.take_copy()

        class Refusing:
            def cursor(self):
                return self

            def execute(self, *args):
                raise dbapi.ProgrammingError(260, "invalid column name")

            def close(self):
                pass

        with patch.object(HanaConnection, "connect", return_value=Refusing()):
            with self.assertRaises(SAPDataError):
                HanaDispatchBillReader(CompanyContext("JIVO_OIL")).list_bills(
                    {"date_from": date(2026, 9, 1), "date_to": date(2026, 9, 30)}
                )

    def test_no_copy_yet_is_sap_being_down(self):
        MirrorDataset.objects.all().delete()
        with patch.object(HanaConnection, "connect", side_effect=HANA_DOWN):
            with self.assertRaises(SAPConnectionError):
                HanaDispatchBillReader(CompanyContext("JIVO_OIL")).list_bills(
                    {"date_from": date(2026, 9, 1), "date_to": date(2026, 9, 30)}
                )
