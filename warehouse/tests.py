from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from barcode.models import Box, BoxStatus, Pallet
from company.models import Company
from driver_management.models import Driver
from vehicle_management.models import Vehicle

from .models_bst import BSTReceiveStatus, BSTTransferStatus
from .services.bst_service import BSTError, BSTService

User = get_user_model()


# BST is intra-company: a SAP stock transfer moves stock between two warehouses
# (WH-A → WH-B) of the same company.
FAKE_SAP_TRANSFER = {
    "doc_entry": 555,
    "doc_num": "1001",
    "doc_date": date(2026, 6, 1),
    "from_warehouse": "WH-A",
    "to_warehouse": "WH-B",
    "reference": "REF-1",
    "comments": "",
    "line_count": 1,
    "total_quantity": 10.0,
    "lines": [
        {
            "line_num": 0,
            "item_code": "ITM1",
            "item_name": "Item One",
            "quantity": 10.0,
            "uom": "PCS",
            "from_warehouse": "WH-A",
            "to_warehouse": "WH-B",
            "pcs_per_carton": 1.0,
            "box_count": 10,
        }
    ],
}


def make_box(company, barcode, *, item_code="ITM1", pallet=None, warehouse="WH-A",
             status=BoxStatus.ACTIVE):
    return Box.objects.create(
        company=company, box_barcode=barcode, item_code=item_code, item_name="Item One",
        batch_number="B1", qty=Decimal("1"), uom="PCS",
        mfg_date=date(2026, 1, 1), exp_date=date(2027, 1, 1),
        current_warehouse=warehouse, pallet=pallet, status=status,
    )


class BSTSenderFlowTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        self.other = Company.objects.create(name="Jivo Mart", code="JIVO_MART")
        self.user = User.objects.create(
            email="wh@example.com", full_name="WH User", employee_code="EMP1",
        )
        self.vehicle = Vehicle.objects.create(vehicle_number="HR-01-1234")
        self.driver = Driver.objects.create(
            name="Driver A", mobile_no="9990001111", license_no="DL-1",
        )
        self.svc = BSTService(self.company.code, self.user)

    def _create_transfer(self, requires_gate=False):
        data = {
            "sap_doc_entries": [555],
            "vehicle": self.vehicle if requires_gate else None,
            "driver": self.driver if requires_gate else None,
            "invoice_no": "INV-9",
            "requires_gate": requires_gate,
            "remarks": "",
        }
        with patch("warehouse.services.bst_service.SAPClient") as sap:
            sap.return_value.get_stock_transfer.return_value = dict(FAKE_SAP_TRANSFER)
            return self.svc.create_transfer(data)

    def test_create_snapshots_sap_lines_and_warehouses(self):
        transfer = self._create_transfer()
        self.assertTrue(transfer.entry_no.startswith("BST-"))
        self.assertEqual(transfer.status, BSTTransferStatus.SCANNING)
        self.assertEqual(transfer.sap_doc_num, "1001")
        self.assertEqual(transfer.sap_from_warehouse, "WH-A")
        self.assertEqual(transfer.sap_to_warehouse, "WH-B")
        self.assertEqual(transfer.items.count(), 1)

    def test_create_internal_without_vehicle_or_driver(self):
        transfer = self._create_transfer(requires_gate=False)
        self.assertIsNone(transfer.vehicle)
        self.assertIsNone(transfer.driver)

    def test_create_combines_multiple_documents(self):
        doc1 = dict(FAKE_SAP_TRANSFER)
        doc2 = {
            **FAKE_SAP_TRANSFER,
            "doc_entry": 556,
            "doc_num": "1002",
            "lines": [
                dict(FAKE_SAP_TRANSFER["lines"][0], item_code="ITM2",
                     item_name="Item Two", box_count=5),
            ],
        }
        mapping = {555: doc1, 556: doc2}
        data = {
            "sap_doc_entries": [555, 556], "vehicle": None, "driver": None,
            "invoice_no": "", "requires_gate": False, "remarks": "",
        }
        with patch("warehouse.services.bst_service.SAPClient") as sap:
            sap.return_value.get_stock_transfer.side_effect = lambda de: mapping[de]
            transfer = self.svc.create_transfer(data)
        self.assertEqual(transfer.docs.count(), 2)
        self.assertEqual(transfer.items.count(), 2)
        # The head mirrors the first (primary) document.
        self.assertEqual(transfer.sap_doc_num, "1001")

    def test_create_rejects_documents_with_different_route(self):
        doc1 = dict(FAKE_SAP_TRANSFER)
        doc2 = {**FAKE_SAP_TRANSFER, "doc_entry": 557, "to_warehouse": "WH-C"}
        mapping = {555: doc1, 557: doc2}
        data = {
            "sap_doc_entries": [555, 557], "vehicle": None, "driver": None,
            "invoice_no": "", "requires_gate": False, "remarks": "",
        }
        with patch("warehouse.services.bst_service.SAPClient") as sap:
            sap.return_value.get_stock_transfer.side_effect = lambda de: mapping[de]
            with self.assertRaises(BSTError):
                self.svc.create_transfer(data)

    def test_scan_across_multiple_documents(self):
        # Two documents, different items, same route → one combined bill.
        doc1 = dict(FAKE_SAP_TRANSFER)
        doc2 = {
            **FAKE_SAP_TRANSFER,
            "doc_entry": 556,
            "doc_num": "1002",
            "lines": [
                dict(FAKE_SAP_TRANSFER["lines"][0], item_code="ITM2",
                     item_name="Item Two", box_count=10),
            ],
        }
        mapping = {555: doc1, 556: doc2}
        data = {
            "sap_doc_entries": [555, 556], "vehicle": None, "driver": None,
            "invoice_no": "", "requires_gate": False, "remarks": "",
        }
        with patch("warehouse.services.bst_service.SAPClient") as sap:
            sap.return_value.get_stock_transfer.side_effect = lambda de: mapping[de]
            transfer = self.svc.create_transfer(data)
        make_box(self.company, "BOX-A", item_code="ITM1")
        make_box(self.company, "BOX-B", item_code="ITM2")
        self.assertEqual(self.svc.scan(transfer, "BOX-A")["created_count"], 1)
        self.assertEqual(self.svc.scan(transfer, "BOX-B")["created_count"], 1)

    def test_scan_box_and_duplicate(self):
        transfer = self._create_transfer()
        make_box(self.company, "BOX-1")
        result = self.svc.scan(transfer, "BOX-1")
        self.assertEqual(result["created_count"], 1)
        again = self.svc.scan(transfer, "BOX-1")
        self.assertEqual(again["created_count"], 0)
        self.assertEqual(again["duplicate_count"], 1)
        self.assertEqual(transfer.box_scans.count(), 1)

    def test_scan_pallet_expands_to_boxes(self):
        transfer = self._create_transfer()
        pallet = Pallet.objects.create(
            company=self.company, pallet_id="PLT-1", item_code="ITM1",
            batch_number="B1", total_qty=Decimal("2"), uom="PCS",
            mfg_date=date(2026, 1, 1), exp_date=date(2027, 1, 1), current_warehouse="WH-A",
        )
        make_box(self.company, "BOX-P1", pallet=pallet)
        make_box(self.company, "BOX-P2", pallet=pallet)
        result = self.svc.scan(transfer, "PLT-1")
        self.assertEqual(result["kind"], "PALLET")
        self.assertEqual(result["created_count"], 2)

    def test_scan_rejects_other_company_box(self):
        transfer = self._create_transfer()
        make_box(self.other, "BOX-X")
        with self.assertRaises(BSTError):
            self.svc.scan(transfer, "BOX-X")

    def test_scan_rejects_item_not_on_transfer(self):
        # Scanning is restricted to the SAP bill — off-bill items are blocked.
        transfer = self._create_transfer()
        make_box(self.company, "BOX-OFF-BILL", item_code="ITM2")  # not on the SAP doc
        with self.assertRaises(BSTError):
            self.svc.scan(transfer, "BOX-OFF-BILL")

    def test_scan_rejects_over_bill_box_count(self):
        # The bill expects 1 box for the item; a second box exceeds it.
        small = dict(FAKE_SAP_TRANSFER)
        small["lines"] = [dict(FAKE_SAP_TRANSFER["lines"][0], box_count=1)]
        data = {
            "sap_doc_entries": [555], "vehicle": None, "driver": None,
            "invoice_no": "INV-9", "requires_gate": False, "remarks": "",
        }
        with patch("warehouse.services.bst_service.SAPClient") as sap:
            sap.return_value.get_stock_transfer.return_value = small
            transfer = self.svc.create_transfer(data)
        self.assertEqual(transfer.items.get(item_code="ITM1").expected_boxes, 1)
        make_box(self.company, "BOX-Q1")
        make_box(self.company, "BOX-Q2")
        self.assertEqual(self.svc.scan(transfer, "BOX-Q1")["created_count"], 1)
        with self.assertRaises(BSTError):
            self.svc.scan(transfer, "BOX-Q2")

    def test_scan_rejects_box_not_at_source_warehouse(self):
        transfer = self._create_transfer()
        make_box(self.company, "BOX-WRONG-WH", warehouse="WH-Z")  # not WH-A
        with self.assertRaises(BSTError):
            self.svc.scan(transfer, "BOX-WRONG-WH")

    def test_box_locked_to_another_active_bst(self):
        t1 = self._create_transfer()
        make_box(self.company, "BOX-L")
        self.svc.scan(t1, "BOX-L")
        t2 = self._create_transfer()
        with self.assertRaises(BSTError):
            self.svc.scan(t2, "BOX-L")

    def test_approve_requires_scans(self):
        transfer = self._create_transfer()
        with self.assertRaises(BSTError):
            self.svc.approve(transfer)

    def test_approve_non_gated_goes_in_transit(self):
        transfer = self._create_transfer(requires_gate=False)
        make_box(self.company, "BOX-D")
        self.svc.scan(transfer, "BOX-D")
        self.svc.approve(transfer)
        transfer.refresh_from_db()
        self.assertEqual(transfer.status, BSTTransferStatus.IN_TRANSIT)
        self.assertIsNotNone(transfer.dispatched_at)

    def test_approve_gated_awaits_gate_out(self):
        transfer = self._create_transfer(requires_gate=True)
        make_box(self.company, "BOX-G")
        self.svc.scan(transfer, "BOX-G")
        self.svc.approve(transfer)
        transfer.refresh_from_db()
        self.assertEqual(transfer.status, BSTTransferStatus.AWAITING_GATE_OUT)

    def test_cannot_scan_after_approve(self):
        transfer = self._create_transfer()
        make_box(self.company, "BOX-A1")
        self.svc.scan(transfer, "BOX-A1")
        self.svc.approve(transfer)
        make_box(self.company, "BOX-A2")
        with self.assertRaises(BSTError):
            self.svc.scan(transfer, "BOX-A2")

    def test_cancel_blocks_after_accept(self):
        transfer = self._create_transfer()
        make_box(self.company, "BOX-C")
        self.svc.scan(transfer, "BOX-C")
        scan = transfer.box_scans.first()
        scan.receive_status = BSTReceiveStatus.ACCEPTED
        scan.save(update_fields=["receive_status"])
        with self.assertRaises(BSTError):
            self.svc.cancel(transfer, "changed mind")


class BSTReceiverFlowTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        self.sender = User.objects.create(
            email="src@example.com", full_name="Src User", employee_code="EMP-S",
        )
        self.receiver = User.objects.create(
            email="dst@example.com", full_name="Dst User", employee_code="EMP-D",
        )
        # Sender works the source warehouse, receiver the destination — same company.
        self.src_svc = BSTService(self.company.code, self.sender)
        self.dst_svc = BSTService(self.company.code, self.receiver)

    def _dispatched_transfer(self, barcodes):
        data = {
            "sap_doc_entries": [555], "vehicle": None, "driver": None,
            "invoice_no": "INV-1", "requires_gate": False, "remarks": "",
        }
        with patch("warehouse.services.bst_service.SAPClient") as sap:
            sap.return_value.get_stock_transfer.return_value = dict(FAKE_SAP_TRANSFER)
            transfer = self.src_svc.create_transfer(data)
        for code in barcodes:
            make_box(self.company, code)
            self.src_svc.scan(transfer, code)
        self.src_svc.approve(transfer)
        return transfer

    def test_incoming_lists_dispatched_transfers(self):
        transfer = self._dispatched_transfer(["BOX-1", "BOX-2"])
        incoming = self.dst_svc.incoming_queryset()
        self.assertEqual(incoming.count(), 1)
        self.assertEqual(incoming.first().id, transfer.id)

    def test_receive_accept_moves_box_to_destination_warehouse(self):
        transfer = self._dispatched_transfer(["BOX-1"])
        self.dst_svc.receive_scan(transfer, "BOX-1", decision="ACCEPTED")
        transfer.refresh_from_db()
        self.assertEqual(transfer.status, BSTTransferStatus.RECEIVING)
        self.dst_svc.receive_complete(transfer)
        transfer.refresh_from_db()
        self.assertEqual(transfer.status, BSTTransferStatus.RECEIVED)
        box = Box.objects.get(box_barcode="BOX-1")
        # Company never changes; warehouse moves to the SAP to-warehouse.
        self.assertEqual(box.company_id, self.company.id)
        self.assertEqual(box.current_warehouse, "WH-B")
        self.assertTrue(box.movements.filter(movement_type="TRANSFER").exists())

    def test_partial_receive_marks_partially_received(self):
        transfer = self._dispatched_transfer(["BOX-1", "BOX-2"])
        self.dst_svc.receive_scan(transfer, "BOX-1", decision="ACCEPTED")
        self.dst_svc.receive_scan(transfer, "BOX-2", decision="REJECTED", reject_reason="damaged")
        self.dst_svc.receive_complete(transfer)
        transfer.refresh_from_db()
        self.assertEqual(transfer.status, BSTTransferStatus.PARTIALLY_RECEIVED)
        # Accepted box moved warehouse; rejected box stayed put.
        self.assertEqual(Box.objects.get(box_barcode="BOX-1").current_warehouse, "WH-B")
        self.assertEqual(Box.objects.get(box_barcode="BOX-2").current_warehouse, "WH-A")

    def test_short_box_keeps_partially_received(self):
        transfer = self._dispatched_transfer(["BOX-1", "BOX-2"])
        self.dst_svc.receive_scan(transfer, "BOX-1", decision="ACCEPTED")
        self.dst_svc.receive_complete(transfer)
        transfer.refresh_from_db()
        self.assertEqual(transfer.status, BSTTransferStatus.PARTIALLY_RECEIVED)

    def test_receive_rejects_undispatched_box(self):
        # Receiving is restricted to the dispatched set — a box the sender never
        # sent on this transfer is rejected, not recorded.
        transfer = self._dispatched_transfer(["BOX-1"])
        make_box(self.company, "BOX-EXTRA")
        with self.assertRaises(BSTError):
            self.dst_svc.receive_scan(transfer, "BOX-EXTRA", decision="ACCEPTED")
        self.assertFalse(transfer.box_scans.filter(box_barcode="BOX-EXTRA").exists())

    def test_receive_pallet_accepts_only_dispatched_boxes(self):
        # A pallet may hold boxes that weren't dispatched on this transfer;
        # scanning it at receive accepts the dispatched ones and ignores the rest
        # (rather than hard-failing the whole scan).
        data = {
            "sap_doc_entries": [555], "vehicle": None, "driver": None,
            "invoice_no": "INV-1", "requires_gate": False, "remarks": "",
        }
        with patch("warehouse.services.bst_service.SAPClient") as sap:
            sap.return_value.get_stock_transfer.return_value = dict(FAKE_SAP_TRANSFER)
            transfer = self.src_svc.create_transfer(data)
        pallet = Pallet.objects.create(
            company=self.company, pallet_id="PLT-R1", item_code="ITM1",
            batch_number="B1", total_qty=Decimal("2"), uom="PCS",
            mfg_date=date(2026, 1, 1), exp_date=date(2027, 1, 1), current_warehouse="WH-A",
        )
        make_box(self.company, "BOX-PD", pallet=pallet)   # dispatched
        make_box(self.company, "BOX-ND", pallet=pallet)   # not dispatched
        self.src_svc.scan(transfer, "BOX-PD")             # dispatch only one box
        self.src_svc.approve(transfer)
        result = self.dst_svc.receive_scan(transfer, "PLT-R1", decision="ACCEPTED")
        self.assertEqual(result["updated_count"], 1)
        self.assertEqual(
            transfer.box_scans.get(box_barcode="BOX-PD").receive_status,
            BSTReceiveStatus.ACCEPTED,
        )
        self.assertFalse(transfer.box_scans.filter(box_barcode="BOX-ND").exists())

    def test_receive_scan_rejected_when_not_in_transit(self):
        data = {
            "sap_doc_entries": [555], "vehicle": None, "driver": None,
            "invoice_no": "", "requires_gate": False, "remarks": "",
        }
        with patch("warehouse.services.bst_service.SAPClient") as sap:
            sap.return_value.get_stock_transfer.return_value = dict(FAKE_SAP_TRANSFER)
            transfer = self.src_svc.create_transfer(data)
        with self.assertRaises(BSTError):
            self.dst_svc.receive_scan(transfer, "BOX-1")


class BSTGateFlowTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        self.user = User.objects.create(
            email="gate@example.com", full_name="Gate User", employee_code="EMP-G",
        )
        self.vehicle = Vehicle.objects.create(vehicle_number="HR-03-3333")
        self.driver = Driver.objects.create(
            name="Driver C", mobile_no="9990003333", license_no="DL-3",
        )
        self.svc = BSTService(self.company.code, self.user)

    def _gated_dispatched(self):
        data = {
            "sap_doc_entries": [555], "vehicle": self.vehicle, "driver": self.driver,
            "invoice_no": "INV-G", "requires_gate": True, "remarks": "",
        }
        with patch("warehouse.services.bst_service.SAPClient") as sap:
            sap.return_value.get_stock_transfer.return_value = dict(FAKE_SAP_TRANSFER)
            transfer = self.svc.create_transfer(data)
        make_box(self.company, "BOX-G1")
        self.svc.scan(transfer, "BOX-G1")
        self.svc.approve(transfer)
        return transfer

    def test_gated_approval_then_gate_out_goes_in_transit(self):
        transfer = self._gated_dispatched()
        transfer.refresh_from_db()
        # Approved by the warehouse, now waiting for the gate to mark it out.
        self.assertEqual(transfer.status, BSTTransferStatus.AWAITING_GATE_OUT)
        self.assertIsNotNone(transfer.scan_approved_at)
        self.assertIsNone(transfer.dispatched_at)  # not dispatched until gate-out
        self.assertEqual(self.svc.gate_outwards_queryset().count(), 1)
        self.assertEqual(self.svc.incoming_queryset().count(), 0)

        self.svc.mark_gate_out(transfer)
        transfer.refresh_from_db()
        self.assertEqual(transfer.status, BSTTransferStatus.IN_TRANSIT)
        self.assertIsNotNone(transfer.gated_out_at)
        self.assertIsNotNone(transfer.dispatched_at)
        # Now it's in transit and receivable; gate-out list is empty.
        self.assertEqual(self.svc.gate_outwards_queryset().count(), 0)
        self.assertEqual(self.svc.incoming_queryset().count(), 1)

    def test_gate_out_board_keeps_history_filtered_by_gate_out_date(self):
        transfer = self._gated_dispatched()
        far_future = (timezone.now() + timedelta(days=30)).date()

        # Awaiting entries are the live queue: always on the board, whatever the
        # date filter is.
        self.assertEqual(self.svc.gate_outwards_view_queryset().count(), 1)
        self.assertEqual(
            self.svc.gate_outwards_view_queryset(
                from_date=far_future, to_date=far_future,
            ).count(),
            1,
        )

        self.svc.mark_gate_out(transfer)
        transfer.refresh_from_db()
        gate_day = timezone.localdate(transfer.gated_out_at)

        # Unlike the pending-only queue, the board keeps gated-out vehicles as
        # history.
        self.assertEqual(self.svc.gate_outwards_queryset().count(), 0)
        self.assertEqual(self.svc.gate_outwards_view_queryset().count(), 1)

        # History is filtered by the gate-out day (when the gate acted).
        self.assertEqual(
            self.svc.gate_outwards_view_queryset(
                from_date=gate_day, to_date=gate_day,
            ).count(),
            1,
        )
        self.assertEqual(
            self.svc.gate_outwards_view_queryset(
                from_date=far_future, to_date=far_future,
            ).count(),
            0,
        )

    def test_mark_gate_out_rejected_when_not_awaiting(self):
        # A non-gated transfer never reaches AWAITING_GATE_OUT.
        data = {
            "sap_doc_entries": [555], "vehicle": None, "driver": None,
            "invoice_no": "", "requires_gate": False, "remarks": "",
        }
        with patch("warehouse.services.bst_service.SAPClient") as sap:
            sap.return_value.get_stock_transfer.return_value = dict(FAKE_SAP_TRANSFER)
            transfer = self.svc.create_transfer(data)
        make_box(self.company, "BOX-N1")
        self.svc.scan(transfer, "BOX-N1")
        self.svc.approve(transfer)
        with self.assertRaises(BSTError):
            self.svc.mark_gate_out(transfer)
