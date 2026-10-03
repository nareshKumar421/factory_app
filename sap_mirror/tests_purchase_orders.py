"""The gate's copy: open purchase orders kept current by what SAP says changed,
and the vendor list, answering the gate's reads when HANA cannot be asked.

No HANA. Taking the copy runs against a fake reader; serving it runs the real
``HanaPOReader`` / ``HanaVendorReader`` with ``HanaConnection.connect`` failing
the way an unreachable HANA does.
"""

from datetime import date, datetime
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from hdbcli import dbapi
from rest_framework.test import APIClient

from company.models import Company, UserCompany, UserRole
from sap_client.context import CompanyContext
from sap_client.dtos import VendorDTO
from sap_client.exceptions import SAPConnectionError, SAPDataError
from sap_client.hana.connection import HanaConnection
from sap_client.hana.po_reader import HanaPOReader
from sap_client.hana.vendor_reader import HanaVendorReader

from . import services
from .models import MirroredPurchaseOrder

NOW = timezone.make_aware(datetime(2026, 10, 3, 10, 0))
HANA_DOWN = dbapi.OperationalError(-10709, "Connection failed (rc=111:Connection refused)")


def po_line(entry, num, supplier, line, item, open_qty, *, fg=0, ordered="100"):
    """A row in ``OPEN_LINE_COLUMNS`` order, plus the finished-goods flag."""
    return [
        num, supplier, f"{supplier} Ltd", item, f"{item} name", Decimal(ordered),
        Decimal(ordered) - Decimal(open_qty), Decimal(open_qty), "KG", Decimal("142.5"),
        entry, line, "GST18", "BH-RM", "", 3, "INV-77", datetime(2026, 9, 28), "OLIVE", fg,
    ]


class FakePOReader:
    """Open POs in SAP for one company, and what the copy asked to read."""

    def __init__(self):
        self.pos = {
            501: ("v1", [po_line(501, 2601, "VEND1", 0, "RM0000002", "40"),
                         po_line(501, 2601, "VEND1", 1, "PM0000001", "10")]),
            502: ("v1", [po_line(502, 2602, "VEND1", 0, "FG0000151", "5", fg=1)]),
            503: ("v1", [po_line(503, 2603, "VEND2", 0, "RM0000009", "70")]),
        }
        self.down = False
        self.read = []

    def open_po_versions(self):
        if self.down:
            raise SAPConnectionError("Unable to connect to SAP HANA.")
        return {entry: version for entry, (version, _) in self.pos.items()}

    def open_po_rows_for(self, entries):
        self.read.extend(entries)
        return [list(row) for entry in entries if entry in self.pos for row in self.pos[entry][1]]


class POCopyTestCase(TestCase):
    def setUp(self):
        self.oil = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        self.sap = FakePOReader()
        patcher = patch("sap_mirror.purchase_orders._reader", return_value=self.sap)
        patcher.start()
        self.addCleanup(patcher.stop)

    def take_copy(self, now=NOW):
        return services.refresh(self.oil, services.PURCHASE_ORDERS, now)

    def hana_down(self):
        return patch.object(HanaConnection, "connect", side_effect=HANA_DOWN)


class TakingThePOCopyTests(POCopyTestCase):
    def test_every_po_with_an_open_line_is_copied(self):
        state = self.take_copy()

        self.assertEqual(state.row_count, 3)
        self.assertEqual(
            sorted(MirroredPurchaseOrder.objects.values_list("doc_num", "supplier_code")),
            [("2601", "VEND1"), ("2602", "VEND1"), ("2603", "VEND2")],
        )

    def test_only_a_changed_po_is_read_again_and_a_closed_one_leaves(self):
        self.take_copy()
        self.sap.read.clear()
        # 501 received against; 503 fully received (no open line left).
        self.sap.pos[501] = ("v2", [po_line(501, 2601, "VEND1", 0, "RM0000002", "15")])
        del self.sap.pos[503]

        self.take_copy(timezone.make_aware(datetime(2026, 10, 3, 10, 15)))

        self.assertEqual(self.sap.read, [501])
        self.assertEqual(sorted(MirroredPurchaseOrder.objects.values_list("doc_entry", flat=True)), [501, 502])

    def test_sap_not_answering_keeps_the_copy(self):
        self.take_copy()
        self.sap.down = True

        state = self.take_copy(timezone.make_aware(datetime(2026, 10, 3, 10, 15)))

        self.assertEqual(state.synced_at, NOW)
        self.assertEqual(MirroredPurchaseOrder.objects.count(), 3)

    def test_open_pos_are_due_at_every_run(self):
        self.assertIsNotNone(services.DATASETS[services.PURCHASE_ORDERS].every)


class ServingThePOCopyTests(POCopyTestCase):
    def setUp(self):
        super().setUp()
        self.take_copy()
        self.reader = HanaPOReader(CompanyContext("JIVO_OIL"))

    def test_a_suppliers_open_pos_come_from_the_copy(self):
        with self.hana_down():
            pos = self.reader.get_open_pos("VEND1")

        self.assertEqual([p.po_number for p in pos], ["2601", "2602"])
        first = pos[0]
        self.assertEqual((first.doc_entry, first.branch_id, first.vendor_ref), (501, 3, "INV-77"))
        self.assertEqual(first.doc_date, date(2026, 9, 28))
        self.assertEqual(
            [(i.po_item_code, i.remaining_qty, i.received_qty) for i in first.items],
            [("RM0000002", 40.0, 60.0), ("PM0000001", 10.0, 90.0)],
        )

    def test_finished_goods_pos_keep_only_finished_goods_lines(self):
        with self.hana_down():
            pos = self.reader.get_open_finished_goods_pos("VEND1")

        self.assertEqual([(p.po_number, [i.po_item_code for i in p.items]) for p in pos],
                         [("2602", ["FG0000151"])])

    def test_a_supplier_with_nothing_open_is_an_empty_answer(self):
        with self.hana_down():
            self.assertEqual(self.reader.get_open_pos("VEND9"), [])

    def test_a_po_by_number(self):
        with self.hana_down():
            po = self.reader.get_open_po_by_number("2603")

        self.assertEqual((po.supplier_code, po.items[0].remaining_qty), ("VEND2", 70.0))

    def test_a_po_number_the_copy_does_not_hold_is_still_sap_being_down(self):
        with self.hana_down(), self.assertRaises(SAPConnectionError):
            self.reader.get_open_po_by_number("9999")

    def test_a_refused_query_is_not_answered_from_the_copy(self):
        class Refusing:
            def cursor(self):
                return self

            def execute(self, *args):
                raise dbapi.ProgrammingError(260, "invalid column name")

            def close(self):
                pass

        with patch.object(HanaConnection, "connect", return_value=Refusing()):
            with self.assertRaises(SAPDataError):
                self.reader.get_open_pos("VEND1")

    def test_the_copy_job_never_reads_the_copy(self):
        with self.hana_down(), self.assertRaises(SAPConnectionError):
            HanaPOReader(CompanyContext("JIVO_OIL"), use_copy=False).get_open_pos("VEND1")

    def test_the_gates_po_list_answers_with_hana_down(self):
        user = get_user_model().objects.create(email="gate@example.com", full_name="Gate Guard")
        UserCompany.objects.create(
            user=user, company=self.oil, role=UserRole.objects.create(name="Security"),
            is_active=True,
        )
        api = APIClient()
        api.force_authenticate(user)

        with self.hana_down():
            response = api.get(
                "/api/v1/po/open-pos/?supplier_code=VEND2", HTTP_COMPANY_CODE="JIVO_OIL"
            )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual([po["po_number"] for po in response.json()], ["2603"])


class VendorCopyTests(TestCase):
    def setUp(self):
        self.oil = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        patcher = patch("sap_mirror.services._fetch_vendors", return_value=[
            {"vendor_code": "VEND2", "vendor_name": "Zeta Oils"},
            {"vendor_code": "VEND1", "vendor_name": "Alpha Packaging"},
        ])
        patcher.start()
        self.addCleanup(patcher.stop)
        services.refresh(self.oil, services.VENDORS, NOW)

    def test_the_vendor_list_comes_from_the_copy_in_name_order(self):
        with patch.object(HanaConnection, "connect", side_effect=HANA_DOWN):
            vendors = HanaVendorReader(CompanyContext("JIVO_OIL")).get_active_vendors()

        self.assertEqual(vendors, [
            VendorDTO(vendor_code="VEND1", vendor_name="Alpha Packaging"),
            VendorDTO(vendor_code="VEND2", vendor_name="Zeta Oils"),
        ])

    def test_it_is_taken_once_a_night(self):
        self.assertIsNone(services.DATASETS[services.VENDORS].every)
