from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

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
            "sap_doc_entry": 555,
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
        transfer = self._create_transfer()
        make_box(self.company, "BOX-WRONG-ITEM", item_code="ITM2")  # not on the SAP doc
        with self.assertRaises(BSTError):
            self.svc.scan(transfer, "BOX-WRONG-ITEM")

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

    def test_dispatch_requires_scans(self):
        transfer = self._create_transfer()
        with self.assertRaises(BSTError):
            self.svc.dispatch(transfer)

    def test_dispatch_non_gated_goes_in_transit(self):
        transfer = self._create_transfer(requires_gate=False)
        make_box(self.company, "BOX-D")
        self.svc.scan(transfer, "BOX-D")
        self.svc.dispatch(transfer)
        transfer.refresh_from_db()
        self.assertEqual(transfer.status, BSTTransferStatus.IN_TRANSIT)
        self.assertIsNotNone(transfer.dispatched_at)

    def test_dispatch_gated_awaits_gate_out(self):
        transfer = self._create_transfer(requires_gate=True)
        make_box(self.company, "BOX-G")
        self.svc.scan(transfer, "BOX-G")
        self.svc.dispatch(transfer)
        transfer.refresh_from_db()
        self.assertEqual(transfer.status, BSTTransferStatus.AWAITING_GATE_OUT)

    def test_cannot_scan_after_dispatch(self):
        transfer = self._create_transfer()
        make_box(self.company, "BOX-A1")
        self.svc.scan(transfer, "BOX-A1")
        self.svc.dispatch(transfer)
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
            "sap_doc_entry": 555, "vehicle": None, "driver": None,
            "invoice_no": "INV-1", "requires_gate": False, "remarks": "",
        }
        with patch("warehouse.services.bst_service.SAPClient") as sap:
            sap.return_value.get_stock_transfer.return_value = dict(FAKE_SAP_TRANSFER)
            transfer = self.src_svc.create_transfer(data)
        for code in barcodes:
            make_box(self.company, code)
            self.src_svc.scan(transfer, code)
        self.src_svc.dispatch(transfer)
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

    def test_unexpected_box_flagged(self):
        transfer = self._dispatched_transfer(["BOX-1"])
        make_box(self.company, "BOX-EXTRA")
        result = self.dst_svc.receive_scan(transfer, "BOX-EXTRA", decision="ACCEPTED")
        self.assertIn("BOX-EXTRA", result["unexpected"])
        self.assertTrue(transfer.box_scans.get(box_barcode="BOX-EXTRA").is_unexpected)

    def test_receive_scan_rejected_when_not_in_transit(self):
        data = {
            "sap_doc_entry": 555, "vehicle": None, "driver": None,
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
            "sap_doc_entry": 555, "vehicle": self.vehicle, "driver": self.driver,
            "invoice_no": "INV-G", "requires_gate": True, "remarks": "",
        }
        with patch("warehouse.services.bst_service.SAPClient") as sap:
            sap.return_value.get_stock_transfer.return_value = dict(FAKE_SAP_TRANSFER)
            transfer = self.svc.create_transfer(data)
        make_box(self.company, "BOX-G1")
        self.svc.scan(transfer, "BOX-G1")
        self.svc.dispatch(transfer)
        return transfer

    def test_gated_lifecycle_out_then_in(self):
        transfer = self._gated_dispatched()
        transfer.refresh_from_db()
        self.assertEqual(transfer.status, BSTTransferStatus.AWAITING_GATE_OUT)
        self.assertEqual(self.svc.gate_outwards_queryset().count(), 1)
        self.assertEqual(self.svc.incoming_queryset().count(), 0)

        self.svc.mark_gate_out(transfer)
        transfer.refresh_from_db()
        self.assertEqual(transfer.status, BSTTransferStatus.AWAITING_GATE_IN)
        self.assertIsNotNone(transfer.gated_out_at)
        self.assertEqual(self.svc.gate_inwards_queryset().count(), 1)
        self.assertEqual(self.svc.incoming_queryset().count(), 0)

        self.svc.mark_gate_in(transfer)
        transfer.refresh_from_db()
        self.assertEqual(transfer.status, BSTTransferStatus.ARRIVED)
        self.assertIsNotNone(transfer.gated_in_at)
        self.assertEqual(self.svc.incoming_queryset().count(), 1)

    def test_mark_gate_in_rejected_before_gate_out(self):
        transfer = self._gated_dispatched()
        with self.assertRaises(BSTError):
            self.svc.mark_gate_in(transfer)
