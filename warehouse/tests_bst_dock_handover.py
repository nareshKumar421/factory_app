"""Dock handover: the destination takes pallets off a gated invoice BST at the dock.

The case it exists for (BST-20261005-0007): Oil scanned a Mart invoice onto a
truck, but Mart needed one pallet at once, to dispatch from BH-PF that afternoon.
The floor ICBT'd it, which broke the BST. Now Mart takes that pallet off the BST
at the dock; the rest still goes out through the gate and is received the
normal way.
"""
from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from barcode.models import BarcodeAuditLog, Box, BoxStatus, Pallet
from company.models import Company, UserCompany, UserRole

from .models_bst import (
    BSTReceiveStatus,
    BSTSourceType,
    BSTTransfer,
    BSTTransferDoc,
    BSTTransferItem,
    BSTTransferStatus,
)
from .serializers_bst import BSTTransferDetailSerializer, BSTTransferListSerializer
from .services.bst_service import BSTError, BSTService, dock_handover_open
from .tests import assign_test_warehouses, make_box

User = get_user_model()


class BSTDockHandoverTests(TestCase):
    def setUp(self):
        self.oil = Company.objects.create(name="Acme", code="ACME")
        self.mart = Company.objects.create(name="Beta", code="BETA")
        self.sender = User.objects.create(email="dk-src@example.com", full_name="Src", employee_code="EMP-DS")
        self.receiver = User.objects.create(email="dk-dst@example.com", full_name="Dst", employee_code="EMP-DD")
        assign_test_warehouses(self.sender, self.oil)
        assign_test_warehouses(self.receiver, self.mart)
        self.src = BSTService(self.oil.code, self.sender)
        self.dst = BSTService(self.mart.code, self.receiver)

    def _transfer(self, *, requires_gate=True, source_type=BSTSourceType.INVOICE, boxes=4):
        transfer = BSTTransfer.objects.create(
            company=self.oil, entry_no=BSTTransfer.generate_entry_no(),
            source_type=source_type,
            destination_company=self.mart if source_type == BSTSourceType.INVOICE else None,
            sap_doc_entry=900, sap_doc_num="INV-900",
            sap_from_warehouse="WH-A", sap_to_warehouse="" if source_type == BSTSourceType.INVOICE else "WH-B",
            requires_gate=requires_gate, status=BSTTransferStatus.SCANNING, created_by=self.sender,
        )
        doc = BSTTransferDoc.objects.create(transfer=transfer, sap_doc_entry=900, sap_doc_num="INV-900")
        BSTTransferItem.objects.create(
            transfer=transfer, doc=doc, line_num=0, item_code="ITM1", item_name="Item One",
            quantity=Decimal(boxes), uom="PCS", from_warehouse="WH-A",
            to_warehouse="" if source_type == BSTSourceType.INVOICE else "WH-B",
            expected_boxes=boxes,
        )
        return transfer

    def _pallet(self, code, barcodes):
        pallet = Pallet.objects.create(
            company=self.oil, pallet_id=code, item_code="ITM1", batch_number="B1",
            total_qty=Decimal(len(barcodes)), uom="PCS",
            mfg_date=date(2026, 1, 1), exp_date=date(2027, 1, 1), current_warehouse="WH-A",
        )
        for barcode in barcodes:
            make_box(self.oil, barcode, pallet=pallet)
        return pallet

    def _loaded(self, **kwargs):
        """A gated invoice with two 2-box pallets scanned on: PLT-DOCK (Mart wants it
        now) and PLT-TRUCK (rides out on the truck)."""
        transfer = self._transfer(**kwargs)
        self._pallet("PLT-DOCK", ["BOX-D1", "BOX-D2"])
        self._pallet("PLT-TRUCK", ["BOX-T1", "BOX-T2"])
        self.src.scan(transfer, "PLT-DOCK")
        self.src.scan(transfer, "PLT-TRUCK")
        transfer.refresh_from_db()
        return transfer

    def _statuses(self, transfer):
        return dict(transfer.box_scans.values_list("box_barcode", "receive_status"))

    # -- handing over ----------------------------------------------------------

    def test_handover_gives_the_pallet_to_the_destination_and_leaves_the_rest(self):
        transfer = self._loaded()
        result = self.dst.hand_over_at_dock(transfer, "PLT-DOCK")

        self.assertEqual(result["updated_count"], 2)
        self.assertEqual(self._statuses(transfer), {
            "BOX-D1": BSTReceiveStatus.ACCEPTED, "BOX-D2": BSTReceiveStatus.ACCEPTED,
            "BOX-T1": BSTReceiveStatus.PENDING, "BOX-T2": BSTReceiveStatus.PENDING,
        })
        self.assertEqual(
            set(Box.objects.filter(company=self.mart).values_list("box_barcode", flat=True)),
            {"BOX-D1", "BOX-D2"},
        )
        self.assertEqual(Pallet.objects.get(pallet_id="PLT-DOCK").company_id, self.mart.id)
        self.assertEqual(Pallet.objects.get(pallet_id="PLT-TRUCK").company_id, self.oil.id)
        self.assertTrue(BarcodeAuditLog.objects.filter(
            barcode="BOX-D1", transaction_type="TRANSFER_COMPLETED", to_company=self.mart,
        ).exists())
        # The truck still owns the transfer: status untouched, nothing dispatched.
        transfer.refresh_from_db()
        self.assertEqual(transfer.status, BSTTransferStatus.SCANNING)
        self.assertIsNone(transfer.dispatched_at)

    def test_handing_over_a_single_box(self):
        transfer = self._loaded()
        self.dst.hand_over_at_dock(transfer, "BOX-D1")
        self.assertEqual(self._statuses(transfer)["BOX-D1"], BSTReceiveStatus.ACCEPTED)
        self.assertEqual(self._statuses(transfer)["BOX-D2"], BSTReceiveStatus.PENDING)

    def test_handing_over_twice_changes_nothing(self):
        transfer = self._loaded()
        self.dst.hand_over_at_dock(transfer, "PLT-DOCK")
        # The pallet is the destination's now, so it resolves through the transfer's
        # own scans — and every box on it is already handed over.
        result = self.dst.hand_over_at_dock(transfer, "PLT-DOCK")
        self.assertEqual((result["updated_count"], result["unchanged_count"]), (0, 2))

    def test_a_box_not_on_the_bst_is_refused(self):
        transfer = self._loaded()
        make_box(self.oil, "BOX-ELSEWHERE")
        with self.assertRaises(BSTError):
            self.dst.hand_over_at_dock(transfer, "BOX-ELSEWHERE")

    def test_open_while_awaiting_gate_out(self):
        transfer = self._loaded()
        self.src.approve(transfer)
        transfer.refresh_from_db()
        self.assertEqual(transfer.status, BSTTransferStatus.AWAITING_GATE_OUT)
        self.dst.hand_over_at_dock(transfer, "PLT-DOCK")
        transfer.refresh_from_db()
        self.assertEqual(transfer.status, BSTTransferStatus.AWAITING_GATE_OUT)

    # -- when it is closed -------------------------------------------------

    def test_closed_once_the_truck_has_gated_out(self):
        transfer = self._loaded()
        self.src.approve(transfer)
        self.src.mark_gate_out(transfer)
        transfer.refresh_from_db()
        self.assertFalse(dock_handover_open(transfer))
        with self.assertRaises(BSTError) as ctx:
            self.dst.hand_over_at_dock(transfer, "PLT-DOCK")
        self.assertEqual(ctx.exception.code, "DOCK_CLOSED")

    def test_not_for_a_bst_without_a_truck(self):
        # A no-truck invoice is receivable from its first scan already.
        transfer = self._loaded(requires_gate=False)
        with self.assertRaises(BSTError) as ctx:
            self.dst.hand_over_at_dock(transfer, "PLT-DOCK")
        self.assertEqual(ctx.exception.code, "DOCK_NOT_APPLICABLE")

    def test_not_for_an_intra_company_stock_transfer(self):
        transfer = self._transfer(source_type=BSTSourceType.STOCK_TRANSFER)
        self.assertFalse(dock_handover_open(transfer))

    def test_only_the_destination_company_can_hand_over(self):
        transfer = self._loaded()
        with self.assertRaises(BSTTransfer.DoesNotExist):
            self.src.hand_over_at_dock(transfer, "PLT-DOCK")

    def test_normal_receive_is_still_refused_before_gate_out(self):
        transfer = self._loaded()
        with self.assertRaises(BSTError):
            self.dst.receive_scan(transfer, "PLT-TRUCK", decision="ACCEPTED")

    # -- the sender's side -------------------------------------------------

    def test_sender_cannot_remove_or_cancel_what_was_handed_over(self):
        transfer = self._loaded()
        self.dst.hand_over_at_dock(transfer, "PLT-DOCK")
        scan = transfer.box_scans.get(box_barcode="BOX-D1")
        with self.assertRaises(BSTError):
            self.src.remove_scan(transfer, scan.id)
        with self.assertRaises(BSTError):
            self.src.cancel(transfer, "changed my mind")

    def test_sender_keeps_scanning_after_a_handover(self):
        transfer = self._loaded(boxes=5)
        self.dst.hand_over_at_dock(transfer, "PLT-DOCK")
        make_box(self.oil, "BOX-LATE")
        self.assertEqual(self.src.scan(transfer, "BOX-LATE")["created_count"], 1)

    # -- the whole journey --------------------------------------------------

    def test_rest_is_received_after_the_truck_and_the_bst_closes_fully(self):
        transfer = self._loaded()
        self.dst.hand_over_at_dock(transfer, "PLT-DOCK")
        self.src.approve(transfer)
        self.src.mark_gate_out(transfer)
        self.dst.receive_scan(transfer, "PLT-TRUCK", decision="ACCEPTED")
        self.dst.receive_complete(transfer)

        transfer.refresh_from_db()
        self.assertEqual(transfer.status, BSTTransferStatus.RECEIVED)
        self.assertEqual(Box.objects.filter(company=self.mart).count(), 4)

    # -- undo -----------------------------------------------------------------

    def test_undo_puts_the_pallet_back_on_the_truck(self):
        transfer = self._loaded()
        self.dst.hand_over_at_dock(transfer, "PLT-DOCK")
        result = self.dst.undo_dock_handover(transfer, "PLT-DOCK")

        self.assertEqual(result["updated_count"], 2)
        self.assertEqual(set(self._statuses(transfer).values()), {BSTReceiveStatus.PENDING})
        scan = transfer.box_scans.get(box_barcode="BOX-D1")
        self.assertIsNone(scan.received_at)
        self.assertEqual(Box.objects.filter(company=self.oil).count(), 4)
        self.assertEqual(Pallet.objects.get(pallet_id="PLT-DOCK").company_id, self.oil.id)
        # And the sender can cancel again, since nothing is received any more.
        self.src.cancel(transfer, "")

    def test_undo_is_refused_once_the_destination_has_loaded_it(self):
        transfer = self._loaded()
        self.dst.hand_over_at_dock(transfer, "PLT-DOCK")
        Box.objects.filter(box_barcode="BOX-D1").update(status=BoxStatus.INSIDE_VEHICLE)
        with self.assertRaises(BSTError) as ctx:
            self.dst.undo_dock_handover(transfer, "PLT-DOCK")
        self.assertEqual(ctx.exception.code, "MOVED_ON")
        self.assertEqual(self._statuses(transfer)["BOX-D2"], BSTReceiveStatus.ACCEPTED)

    def test_undo_is_refused_once_the_destination_has_moved_it(self):
        transfer = self._loaded()
        self.dst.hand_over_at_dock(transfer, "PLT-DOCK")
        Box.objects.filter(box_barcode="BOX-D1").update(current_warehouse="WH-Z")
        with self.assertRaises(BSTError):
            self.dst.undo_dock_handover(transfer, "BOX-D1")

    # -- what the receiver's screens see ---------------------------------------

    def test_incoming_board_lists_it_at_the_dock_once_a_box_is_on(self):
        transfer = self._transfer()
        board = self.dst.incoming_view_queryset
        self.assertFalse(board().filter(pk=transfer.pk).exists())  # nothing scanned yet

        self._pallet("PLT-DOCK", ["BOX-D1"])
        self.src.scan(transfer, "PLT-DOCK")
        rows = list(board().filter(pk=transfer.pk))
        self.assertEqual(len(rows), 1)
        self.assertTrue(BSTTransferListSerializer(rows[0]).data["dock_handover_open"])
        # Not part of the receivable set: only the dock action is open.
        self.assertFalse(self.dst.incoming_queryset().filter(pk=transfer.pk).exists())

    def test_incoming_board_leaves_out_a_gated_stock_transfer(self):
        transfer = self._transfer(source_type=BSTSourceType.STOCK_TRANSFER)
        make_box(self.oil, "BOX-S1")
        self.src.scan(transfer, "BOX-S1")
        self.assertFalse(
            BSTService(self.oil.code, self.sender).incoming_view_queryset().filter(pk=transfer.pk).exists(),
        )

    def test_detail_says_whether_the_dock_is_open(self):
        transfer = self._loaded()
        self.assertTrue(BSTTransferDetailSerializer(transfer).data["dock_handover_open"])
        self.src.approve(transfer)
        self.src.mark_gate_out(transfer)
        transfer.refresh_from_db()
        self.assertFalse(BSTTransferDetailSerializer(transfer).data["dock_handover_open"])

    def test_endpoint_hands_over_and_undoes(self):
        transfer = self._loaded()
        UserCompany.objects.create(
            user=self.receiver, company=self.mart, role=UserRole.objects.create(name="Store"),
        )
        client = APIClient()
        client.force_authenticate(user=self.receiver)
        client.credentials(HTTP_COMPANY_CODE=self.mart.code)
        url = f"/api/v1/warehouse/bst/{transfer.id}/dock-handover/"

        res = client.post(url, {"barcode_raw": "PLT-DOCK"}, format="json")
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual((res.data["action"], res.data["updated_count"]), ("handover", 2))
        detail = client.get(f"/api/v1/warehouse/bst/incoming/{transfer.id}/")
        self.assertTrue(detail.data["dock_handover_open"])
        self.assertEqual(detail.data["accepted_count"], 2)

        res = client.post(url, {"barcode_raw": "PLT-DOCK", "undo": True}, format="json")
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.data["action"], "undo")
        self.assertEqual(set(self._statuses(transfer).values()), {BSTReceiveStatus.PENDING})
