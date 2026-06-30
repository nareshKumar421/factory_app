from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from barcode.models import Box, BoxStatus, Pallet
from company.models import Company
from driver_management.models import Driver
from vehicle_management.models import Vehicle

from .models_bst import BSTReceiveStatus, BSTTransfer, BSTTransferStatus
from .services.bst_service import BSTError, BSTService

User = get_user_model()


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


class BSTSenderFlowTests(TestCase):
    def setUp(self):
        self.source = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        self.dest = Company.objects.create(name="Jivo Mart", code="JIVO_MART")
        self.user = User.objects.create(
            email="wh@example.com", full_name="WH User", employee_code="EMP1",
        )
        self.vehicle = Vehicle.objects.create(vehicle_number="HR-01-1234")
        self.driver = Driver.objects.create(
            name="Driver A", mobile_no="9990001111", license_no="DL-1",
        )
        self.svc = BSTService(self.source.code, self.user)

    # ---- helpers -------------------------------------------------------

    def _box(self, barcode, *, company=None, pallet=None, status=BoxStatus.ACTIVE):
        return Box.objects.create(
            company=company or self.source,
            box_barcode=barcode,
            item_code="ITM1",
            item_name="Item One",
            batch_number="B1",
            qty=Decimal("1"),
            uom="PCS",
            mfg_date=date(2026, 1, 1),
            exp_date=date(2027, 1, 1),
            current_warehouse="WH-A",
            pallet=pallet,
            status=status,
        )

    def _create_transfer(self, requires_gate=False):
        data = {
            "sap_doc_entry": 555,
            "to_company": self.dest,
            "vehicle": self.vehicle,
            "driver": self.driver,
            "invoice_no": "INV-9",
            "requires_gate": requires_gate,
            "remarks": "",
        }
        with patch("warehouse.services.bst_service.SAPClient") as sap:
            sap.return_value.get_stock_transfer.return_value = dict(FAKE_SAP_TRANSFER)
            return self.svc.create_transfer(data)

    # ---- tests ---------------------------------------------------------

    def test_create_snapshots_sap_lines(self):
        transfer = self._create_transfer()
        self.assertTrue(transfer.entry_no.startswith("BST-"))
        self.assertEqual(transfer.status, BSTTransferStatus.SCANNING)
        self.assertEqual(transfer.sap_doc_num, "1001")
        self.assertEqual(transfer.items.count(), 1)
        self.assertEqual(transfer.items.first().item_code, "ITM1")

    def test_create_internal_without_vehicle_or_driver(self):
        # Non-gated (internal) transfer needs no vehicle/driver.
        data = {
            "sap_doc_entry": 555, "to_company": self.dest,
            "vehicle": None, "driver": None,
            "invoice_no": "", "requires_gate": False, "remarks": "",
        }
        with patch("warehouse.services.bst_service.SAPClient") as sap:
            sap.return_value.get_stock_transfer.return_value = dict(FAKE_SAP_TRANSFER)
            transfer = self.svc.create_transfer(data)
        self.assertIsNone(transfer.vehicle)
        self.assertIsNone(transfer.driver)
        self.assertFalse(transfer.requires_gate)

    def test_create_rejects_same_company(self):
        data = {
            "sap_doc_entry": 555, "to_company": self.source,
            "vehicle": self.vehicle, "driver": self.driver,
            "invoice_no": "", "requires_gate": False, "remarks": "",
        }
        with self.assertRaises(BSTError):
            self.svc.create_transfer(data)

    def test_scan_box_and_duplicate(self):
        transfer = self._create_transfer()
        self._box("BOX-1")
        result = self.svc.scan(transfer, "BOX-1")
        self.assertEqual(result["created_count"], 1)
        self.assertEqual(transfer.box_scans.count(), 1)
        # Re-scanning the same box is a no-op duplicate.
        again = self.svc.scan(transfer, "BOX-1")
        self.assertEqual(again["created_count"], 0)
        self.assertEqual(again["duplicate_count"], 1)
        self.assertEqual(transfer.box_scans.count(), 1)

    def test_scan_pallet_expands_to_boxes(self):
        transfer = self._create_transfer()
        pallet = Pallet.objects.create(
            company=self.source, pallet_id="PLT-1", item_code="ITM1",
            batch_number="B1", total_qty=Decimal("2"), uom="PCS",
            mfg_date=date(2026, 1, 1), exp_date=date(2027, 1, 1),
            current_warehouse="WH-A",
        )
        self._box("BOX-P1", pallet=pallet)
        self._box("BOX-P2", pallet=pallet)
        result = self.svc.scan(transfer, "PLT-1")
        self.assertEqual(result["kind"], "PALLET")
        self.assertEqual(result["created_count"], 2)

    def test_scan_rejects_other_company_box(self):
        transfer = self._create_transfer()
        self._box("BOX-X", company=self.dest)
        with self.assertRaises(BSTError):
            self.svc.scan(transfer, "BOX-X")

    def test_box_locked_to_another_active_bst(self):
        t1 = self._create_transfer()
        self._box("BOX-L")
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
        self._box("BOX-D")
        self.svc.scan(transfer, "BOX-D")
        self.svc.dispatch(transfer)
        transfer.refresh_from_db()
        self.assertEqual(transfer.status, BSTTransferStatus.IN_TRANSIT)
        self.assertIsNotNone(transfer.dispatched_at)

    def test_dispatch_gated_awaits_gate_out(self):
        transfer = self._create_transfer(requires_gate=True)
        self._box("BOX-G")
        self.svc.scan(transfer, "BOX-G")
        self.svc.dispatch(transfer)
        transfer.refresh_from_db()
        self.assertEqual(transfer.status, BSTTransferStatus.AWAITING_GATE_OUT)

    def test_cannot_scan_after_dispatch(self):
        transfer = self._create_transfer()
        self._box("BOX-A1")
        self.svc.scan(transfer, "BOX-A1")
        self.svc.dispatch(transfer)
        self._box("BOX-A2")
        with self.assertRaises(BSTError):
            self.svc.scan(transfer, "BOX-A2")

    def test_cancel_blocks_after_accept(self):
        transfer = self._create_transfer()
        self._box("BOX-C")
        self.svc.scan(transfer, "BOX-C")
        scan = transfer.box_scans.first()
        scan.receive_status = BSTReceiveStatus.ACCEPTED
        scan.save(update_fields=["receive_status"])
        with self.assertRaises(BSTError):
            self.svc.cancel(transfer, "changed mind")


class BSTReceiverFlowTests(TestCase):
    def setUp(self):
        self.source = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        self.dest = Company.objects.create(name="Jivo Mart", code="JIVO_MART")
        self.sender = User.objects.create(
            email="src@example.com", full_name="Src User", employee_code="EMP-S",
        )
        self.receiver = User.objects.create(
            email="dst@example.com", full_name="Dst User", employee_code="EMP-D",
        )
        self.vehicle = Vehicle.objects.create(vehicle_number="HR-02-2222")
        self.driver = Driver.objects.create(
            name="Driver B", mobile_no="9990002222", license_no="DL-2",
        )
        self.src_svc = BSTService(self.source.code, self.sender)
        self.dst_svc = BSTService(self.dest.code, self.receiver)

    def _box(self, barcode):
        return Box.objects.create(
            company=self.source, box_barcode=barcode, item_code="ITM1",
            item_name="Item One", batch_number="B1", qty=Decimal("1"), uom="PCS",
            mfg_date=date(2026, 1, 1), exp_date=date(2027, 1, 1),
            current_warehouse="WH-A",
        )

    def _dispatched_transfer(self, barcodes):
        data = {
            "sap_doc_entry": 555, "to_company": self.dest,
            "vehicle": self.vehicle, "driver": self.driver,
            "invoice_no": "INV-1", "requires_gate": False, "remarks": "",
        }
        with patch("warehouse.services.bst_service.SAPClient") as sap:
            sap.return_value.get_stock_transfer.return_value = dict(FAKE_SAP_TRANSFER)
            transfer = self.src_svc.create_transfer(data)
        for code in barcodes:
            self._box(code)
            self.src_svc.scan(transfer, code)
        self.src_svc.dispatch(transfer)
        return transfer

    def test_incoming_lists_for_destination_only(self):
        transfer = self._dispatched_transfer(["BOX-1", "BOX-2"])
        self.assertEqual(self.dst_svc.incoming_queryset().count(), 1)
        # The source company does not see it as incoming.
        self.assertEqual(self.src_svc.incoming_queryset().count(), 0)
        self.assertEqual(self.dst_svc.incoming_queryset().first().id, transfer.id)

    def test_receive_accept_moves_box_company_and_warehouse(self):
        transfer = self._dispatched_transfer(["BOX-1"])
        self.dst_svc.receive_scan(transfer, "BOX-1", decision="ACCEPTED")
        transfer.refresh_from_db()
        self.assertEqual(transfer.status, BSTTransferStatus.RECEIVING)
        self.dst_svc.receive_complete(transfer)
        transfer.refresh_from_db()
        self.assertEqual(transfer.status, BSTTransferStatus.RECEIVED)
        box = Box.objects.get(box_barcode="BOX-1")
        self.assertEqual(box.company_id, self.dest.id)
        self.assertEqual(box.current_warehouse, "WH-B")
        self.assertTrue(box.movements.filter(movement_type="TRANSFER").exists())

    def test_partial_receive_marks_partially_received(self):
        transfer = self._dispatched_transfer(["BOX-1", "BOX-2"])
        self.dst_svc.receive_scan(transfer, "BOX-1", decision="ACCEPTED")
        self.dst_svc.receive_scan(transfer, "BOX-2", decision="REJECTED", reject_reason="damaged")
        self.dst_svc.receive_complete(transfer)
        transfer.refresh_from_db()
        self.assertEqual(transfer.status, BSTTransferStatus.PARTIALLY_RECEIVED)
        # Accepted box moved; rejected box stayed with source.
        self.assertEqual(Box.objects.get(box_barcode="BOX-1").company_id, self.dest.id)
        self.assertEqual(Box.objects.get(box_barcode="BOX-2").company_id, self.source.id)

    def test_short_box_keeps_partially_received(self):
        transfer = self._dispatched_transfer(["BOX-1", "BOX-2"])
        self.dst_svc.receive_scan(transfer, "BOX-1", decision="ACCEPTED")
        # BOX-2 never received.
        self.dst_svc.receive_complete(transfer)
        transfer.refresh_from_db()
        self.assertEqual(transfer.status, BSTTransferStatus.PARTIALLY_RECEIVED)

    def test_unexpected_box_flagged(self):
        transfer = self._dispatched_transfer(["BOX-1"])
        self._box("BOX-EXTRA")
        result = self.dst_svc.receive_scan(transfer, "BOX-EXTRA", decision="ACCEPTED")
        self.assertIn("BOX-EXTRA", result["unexpected"])
        self.assertTrue(
            transfer.box_scans.get(box_barcode="BOX-EXTRA").is_unexpected
        )

    def test_receive_scan_rejected_when_not_in_transit(self):
        # A transfer still being scanned by the sender is not receivable.
        data = {
            "sap_doc_entry": 555, "to_company": self.dest,
            "vehicle": self.vehicle, "driver": self.driver,
            "invoice_no": "", "requires_gate": False, "remarks": "",
        }
        with patch("warehouse.services.bst_service.SAPClient") as sap:
            sap.return_value.get_stock_transfer.return_value = dict(FAKE_SAP_TRANSFER)
            transfer = self.src_svc.create_transfer(data)
        with self.assertRaises(BSTError):
            self.dst_svc.receive_scan(transfer, "BOX-1")


class BSTGateFlowTests(TestCase):
    def setUp(self):
        self.source = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        self.dest = Company.objects.create(name="Jivo Mart", code="JIVO_MART")
        self.user = User.objects.create(
            email="gate@example.com", full_name="Gate User", employee_code="EMP-G",
        )
        self.vehicle = Vehicle.objects.create(vehicle_number="HR-03-3333")
        self.driver = Driver.objects.create(
            name="Driver C", mobile_no="9990003333", license_no="DL-3",
        )
        self.src_svc = BSTService(self.source.code, self.user)
        self.dst_svc = BSTService(self.dest.code, self.user)

    def _gated_dispatched(self):
        data = {
            "sap_doc_entry": 555, "to_company": self.dest,
            "vehicle": self.vehicle, "driver": self.driver,
            "invoice_no": "INV-G", "requires_gate": True, "remarks": "",
        }
        with patch("warehouse.services.bst_service.SAPClient") as sap:
            sap.return_value.get_stock_transfer.return_value = dict(FAKE_SAP_TRANSFER)
            transfer = self.src_svc.create_transfer(data)
        Box.objects.create(
            company=self.source, box_barcode="BOX-G1", item_code="ITM1",
            item_name="Item One", batch_number="B1", qty=Decimal("1"), uom="PCS",
            mfg_date=date(2026, 1, 1), exp_date=date(2027, 1, 1), current_warehouse="WH-A",
        )
        self.src_svc.scan(transfer, "BOX-G1")
        self.src_svc.dispatch(transfer)
        return transfer

    def test_gated_lifecycle_out_then_in(self):
        transfer = self._gated_dispatched()
        transfer.refresh_from_db()
        self.assertEqual(transfer.status, BSTTransferStatus.AWAITING_GATE_OUT)

        # Source gate sees it as an expected outward and is not receivable yet.
        self.assertEqual(self.src_svc.gate_outwards_queryset().count(), 1)
        self.assertEqual(self.dst_svc.incoming_queryset().count(), 0)

        self.src_svc.mark_gate_out(transfer)
        transfer.refresh_from_db()
        self.assertEqual(transfer.status, BSTTransferStatus.AWAITING_GATE_IN)
        self.assertIsNotNone(transfer.gated_out_at)

        # Destination gate now sees it as an expected inward; still not receivable.
        self.assertEqual(self.dst_svc.gate_inwards_queryset().count(), 1)
        self.assertEqual(self.dst_svc.incoming_queryset().count(), 0)

        self.dst_svc.mark_gate_in(transfer)
        transfer.refresh_from_db()
        self.assertEqual(transfer.status, BSTTransferStatus.ARRIVED)
        self.assertIsNotNone(transfer.gated_in_at)

        # After gate-in it becomes receivable.
        self.assertEqual(self.dst_svc.incoming_queryset().count(), 1)

    def test_mark_gate_in_rejected_before_gate_out(self):
        transfer = self._gated_dispatched()
        with self.assertRaises(BSTError):
            self.dst_svc.mark_gate_in(transfer)
