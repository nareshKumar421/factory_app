from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from barcode.models import BarcodeAuditLog, Box, BoxStatus, Pallet
from company.models import Company
from driver_management.models import Driver
from vehicle_management.models import Vehicle

from .models import (
    BOMLineStatus,
    BOMRequest,
    BOMRequestLine,
    BOMRequestStatus,
)
from .models_bst import (
    BSTPartialTransferApproval,
    BSTPartialTransferStatus,
    BSTReceiveStatus,
    BSTSourceType,
    BSTTransfer,
    BSTTransferDoc,
    BSTTransferItem,
    BSTTransferStatus,
)
from .services.bst_service import BSTError, BSTService, compute_scan_status
from .services.warehouse_service import WarehouseService

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
    # Expected quantity is 1 pc so any single qty-carrying box completes the bill on
    # the quantity gate; box_count stays high as the scan cap the count-based tests
    # rely on (they scan up to 2 boxes / a 2-box pallet). Dedicated completeness tests
    # (BSTScanCompletenessTests) use a realistic short bill to exercise the qty lock.
    "total_quantity": 1.0,
    "lines": [
        {
            "line_num": 0,
            "item_code": "ITM1",
            "item_name": "Item One",
            "quantity": 1.0,
            "uom": "PCS",
            "from_warehouse": "WH-A",
            "to_warehouse": "WH-B",
            "pcs_per_carton": 1.0,
            "box_count": 10,
        }
    ],
}


def make_box(company, barcode, *, item_code="ITM1", pallet=None, warehouse="WH-A",
             status=BoxStatus.ACTIVE, qty=Decimal("1")):
    return Box.objects.create(
        company=company, box_barcode=barcode, item_code=item_code, item_name="Item One",
        batch_number="B1", qty=qty, uom="PCS",
        mfg_date=date(2026, 1, 1), exp_date=date(2027, 1, 1),
        current_warehouse=warehouse, pallet=pallet, status=status,
    )


class BOMReRequestTests(TestCase):
    """Production re-requests the un-approved remainder of a partial/rejected BOM request."""

    def setUp(self):
        from production_execution.models import ProductionLine, ProductionRun

        self.company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        self.user = User.objects.create(
            email="prod@example.com", full_name="Prod User", employee_code="EMP-P",
        )
        self.line = ProductionLine.objects.create(company=self.company, name="Line 1")
        self.run = ProductionRun.objects.create(
            company=self.company, run_number=1, date=date(2026, 7, 25),
            line=self.line, required_qty=Decimal("100"),
            warehouse_approval_status="PARTIALLY_APPROVED", status="IN_PROGRESS",
        )
        self.svc = WarehouseService(self.company.code)

    def _make_request(self, status, lines):
        """lines = [(item_code, required, approved, line_status)]"""
        req = BOMRequest.objects.create(
            company=self.company, production_run=self.run,
            required_qty=Decimal("100"), status=status, requested_by=self.user,
        )
        for idx, (code, required, approved, line_status) in enumerate(lines):
            BOMRequestLine.objects.create(
                bom_request=req, item_code=code, item_name=f"Item {code}",
                per_unit_qty=Decimal("1"), required_qty=Decimal(str(required)),
                approved_qty=Decimal(str(approved)), base_line=idx, uom="KG",
                status=line_status,
            )
        return req

    def test_partial_approval_re_request_carries_only_shortfall(self):
        source = self._make_request(
            BOMRequestStatus.PARTIALLY_APPROVED,
            [
                ("A", 100, 100, BOMLineStatus.APPROVED),  # fully approved -> excluded
                ("B", 100, 60, BOMLineStatus.APPROVED),   # short by 40
                ("C", 100, 0, BOMLineStatus.REJECTED),    # rejected -> full 100
            ],
        )
        follow_up = self.svc.re_request_bom_shortfall(source.id, self.user)

        self.assertEqual(follow_up.status, BOMRequestStatus.PENDING)
        self.assertEqual(follow_up.parent_request_id, source.id)
        self.assertEqual(follow_up.production_run_id, self.run.id)

        lines = {l.item_code: l for l in follow_up.lines.all()}
        self.assertNotIn("A", lines)  # fully approved item not re-requested
        self.assertEqual(lines["B"].required_qty, Decimal("40.000"))
        self.assertEqual(lines["C"].required_qty, Decimal("100.000"))
        self.assertTrue(all(l.status == BOMLineStatus.PENDING for l in lines.values()))

        # Original request and its approved quantities are left intact.
        source.refresh_from_db()
        self.assertEqual(source.status, BOMRequestStatus.PARTIALLY_APPROVED)
        self.assertEqual(source.lines.get(item_code="B").approved_qty, Decimal("60.000"))

    def test_rejected_request_can_be_re_requested_in_full(self):
        source = self._make_request(
            BOMRequestStatus.REJECTED,
            [("A", 100, 0, BOMLineStatus.REJECTED)],
        )
        follow_up = self.svc.re_request_bom_shortfall(source.id, self.user)
        self.assertEqual(follow_up.lines.get(item_code="A").required_qty, Decimal("100.000"))

    def test_run_status_unchanged_so_run_stays_startable(self):
        source = self._make_request(
            BOMRequestStatus.PARTIALLY_APPROVED,
            [("B", 100, 60, BOMLineStatus.APPROVED)],
        )
        self.svc.re_request_bom_shortfall(source.id, self.user)
        self.run.refresh_from_db()
        self.assertEqual(self.run.warehouse_approval_status, "PARTIALLY_APPROVED")

    def test_fully_approved_request_cannot_be_re_requested(self):
        source = self._make_request(
            BOMRequestStatus.APPROVED,
            [("A", 100, 100, BOMLineStatus.APPROVED)],
        )
        with self.assertRaises(ValueError):
            self.svc.re_request_bom_shortfall(source.id, self.user)

    def test_nothing_outstanding_raises(self):
        # A "partial" request whose lines are actually all fully satisfied.
        source = self._make_request(
            BOMRequestStatus.PARTIALLY_APPROVED,
            [("A", 100, 100, BOMLineStatus.APPROVED)],
        )
        with self.assertRaises(ValueError):
            self.svc.re_request_bom_shortfall(source.id, self.user)

    def test_blocked_when_a_request_is_already_pending(self):
        source = self._make_request(
            BOMRequestStatus.PARTIALLY_APPROVED,
            [("B", 100, 60, BOMLineStatus.APPROVED)],
        )
        self.svc.re_request_bom_shortfall(source.id, self.user)
        # A second re-request while the first follow-up is still pending is blocked.
        with self.assertRaises(ValueError):
            self.svc.re_request_bom_shortfall(source.id, self.user)

    def test_blocked_when_source_is_not_the_latest_request(self):
        source = self._make_request(
            BOMRequestStatus.PARTIALLY_APPROVED,
            [("B", 100, 60, BOMLineStatus.APPROVED)],
        )
        # A newer request exists for the run -> the old one is stale.
        self._make_request(
            BOMRequestStatus.PARTIALLY_APPROVED,
            [("B", 40, 30, BOMLineStatus.APPROVED)],
        )
        with self.assertRaises(ValueError):
            self.svc.re_request_bom_shortfall(source.id, self.user)

    def test_blocked_for_completed_run(self):
        self.run.status = "COMPLETED"
        self.run.save(update_fields=["status"])
        source = self._make_request(
            BOMRequestStatus.PARTIALLY_APPROVED,
            [("B", 100, 60, BOMLineStatus.APPROVED)],
        )
        with self.assertRaises(ValueError):
            self.svc.re_request_bom_shortfall(source.id, self.user)


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

    def test_create_rejects_sap_document_already_used_by_another_bst(self):
        # A SAP document backs at most one live BST — reusing it on a second BST
        # is blocked (unless the first was cancelled).
        self._create_transfer()  # uses doc 555
        data = {
            "sap_doc_entries": [555], "vehicle": None, "driver": None,
            "invoice_no": "", "requires_gate": False, "remarks": "",
        }
        with patch("warehouse.services.bst_service.SAPClient") as sap:
            sap.return_value.get_stock_transfer.return_value = dict(FAKE_SAP_TRANSFER)
            with self.assertRaisesMessage(BSTError, "already"):
                self.svc.create_transfer(data)

    def test_create_allows_reusing_document_from_a_cancelled_bst(self):
        # Cancelling frees the document for a fresh BST.
        first = self._create_transfer()  # uses doc 555
        first.status = BSTTransferStatus.CANCELLED
        first.save(update_fields=["status"])
        second = self._create_transfer()  # doc 555 again — now allowed
        self.assertNotEqual(second.id, first.id)
        self.assertEqual(second.sap_doc_entry, 555)

    def test_create_rejects_documents_with_different_destination(self):
        # Destination must still match — the receive-side move sends every box to
        # the head's destination, so mixing destinations would misroute stock.
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

    def test_create_combines_documents_from_different_source_warehouses(self):
        # Virtual source warehouses differ, but the destination is shared — allowed.
        # The head source warehouse is blanked ("multiple") and each line keeps its own.
        doc1 = dict(FAKE_SAP_TRANSFER)
        doc2 = {
            **FAKE_SAP_TRANSFER,
            "doc_entry": 558,
            "doc_num": "1003",
            "from_warehouse": "WH-A2",
            "lines": [
                dict(FAKE_SAP_TRANSFER["lines"][0], item_code="ITM2",
                     item_name="Item Two", from_warehouse="WH-A2"),
            ],
        }
        mapping = {555: doc1, 558: doc2}
        data = {
            "sap_doc_entries": [555, 558], "vehicle": None, "driver": None,
            "invoice_no": "", "requires_gate": False, "remarks": "",
        }
        with patch("warehouse.services.bst_service.SAPClient") as sap:
            sap.return_value.get_stock_transfer.side_effect = lambda de: mapping[de]
            transfer = self.svc.create_transfer(data)
        self.assertEqual(transfer.docs.count(), 2)
        self.assertEqual(transfer.sap_from_warehouse, "")  # blanked: multiple sources
        self.assertEqual(transfer.sap_to_warehouse, "WH-B")
        # A box at either source warehouse can be scanned onto the combined bill.
        make_box(self.company, "BOX-S1", item_code="ITM1", warehouse="WH-A")
        make_box(self.company, "BOX-S2", item_code="ITM2", warehouse="WH-A2")
        self.svc.scan(transfer, "BOX-S1")
        self.svc.scan(transfer, "BOX-S2")
        self.assertEqual(transfer.box_scans.count(), 2)

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
        # A second, independent BST on its own SAP document (a doc backs only one
        # BST, so t2 can't reuse t1's).
        data = {
            "sap_doc_entries": [556], "vehicle": None, "driver": None,
            "invoice_no": "", "requires_gate": False, "remarks": "",
        }
        with patch("warehouse.services.bst_service.SAPClient") as sap:
            sap.return_value.get_stock_transfer.return_value = {
                **FAKE_SAP_TRANSFER, "doc_entry": 556, "doc_num": "1002",
            }
            t2 = self.svc.create_transfer(data)
        with self.assertRaises(BSTError):
            self.svc.scan(t2, "BOX-L")

    def test_approve_requires_scans(self):
        transfer = self._create_transfer()
        with self.assertRaises(BSTError):
            self.svc.approve(transfer)

    # -- PM (packaging material) scan exemption ------------------------------

    def _create_pm_transfer(self, extra_lines=None):
        """A BST whose bill is a PM line, plus any `extra_lines` (dicts)."""
        lines = [
            dict(FAKE_SAP_TRANSFER["lines"][0], line_num=0,
                 item_code="PM0000235", item_name="Carton Box"),
        ]
        lines.extend(extra_lines or [])
        doc = {**FAKE_SAP_TRANSFER, "line_count": len(lines), "lines": lines}
        data = {
            "sap_doc_entries": [555], "vehicle": None, "driver": None,
            "invoice_no": "", "requires_gate": False, "remarks": "",
        }
        with patch("warehouse.services.bst_service.SAPClient") as sap:
            sap.return_value.get_stock_transfer.return_value = doc
            return self.svc.create_transfer(data)

    def test_pm_only_transfer_approves_without_scans(self):
        # A PM-only bill needs no scanning: approve seals it straight to transit.
        transfer = self._create_pm_transfer()
        status = compute_scan_status(transfer)
        self.assertFalse(status["requires_scanning"])
        self.assertFalse(status["is_partial"])
        self.assertTrue(all(not r["requires_scan"] for r in status["items"]))
        self.svc.approve(transfer)
        transfer.refresh_from_db()
        self.assertEqual(transfer.status, BSTTransferStatus.IN_TRANSIT)

    def test_mixed_bill_still_requires_scanning_non_pm(self):
        # A non-PM line makes scanning required, but the PM line is never short.
        extra = [dict(FAKE_SAP_TRANSFER["lines"][0], line_num=1, item_code="ITM1")]
        transfer = self._create_pm_transfer(extra_lines=extra)
        self.assertTrue(compute_scan_status(transfer)["requires_scanning"])
        with self.assertRaises(BSTError):
            self.svc.approve(transfer)  # nothing scanned yet
        # Scanning just the non-PM box completes the bill (PM never counts short).
        make_box(self.company, "BOX-M", item_code="ITM1")
        self.svc.scan(transfer, "BOX-M")
        status = compute_scan_status(transfer)
        self.assertFalse(status["is_partial"])
        self.svc.approve(transfer)
        transfer.refresh_from_db()
        self.assertEqual(transfer.status, BSTTransferStatus.IN_TRANSIT)

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

    # -- Live internal transfer (no gate) -----------------------------------

    def test_live_first_scan_goes_in_transit(self):
        # A non-gated STOCK_TRANSFER becomes receivable the instant the first box
        # is scanned — no approve needed to unlock the destination.
        transfer = self._create_transfer(requires_gate=False)
        self.assertEqual(transfer.status, BSTTransferStatus.SCANNING)
        make_box(self.company, "BOX-L1")
        self.svc.scan(transfer, "BOX-L1")
        transfer.refresh_from_db()
        self.assertEqual(transfer.status, BSTTransferStatus.IN_TRANSIT)
        self.assertIsNotNone(transfer.dispatched_at)

    def test_live_sender_can_keep_scanning_after_going_in_transit(self):
        # Going in transit on the first scan must NOT stop the sender adding more.
        transfer = self._create_transfer(requires_gate=False)
        make_box(self.company, "BOX-L1")
        make_box(self.company, "BOX-L2")
        self.svc.scan(transfer, "BOX-L1")
        transfer.refresh_from_db()
        self.assertEqual(transfer.status, BSTTransferStatus.IN_TRANSIT)
        self.assertEqual(self.svc.scan(transfer, "BOX-L2")["created_count"], 1)
        self.assertEqual(transfer.box_scans.count(), 2)

    def test_gated_scan_stays_scanning(self):
        # A gated transfer is NOT live: scanning does not make it receivable; it
        # still waits for approve → gate-out.
        transfer = self._create_transfer(requires_gate=True)
        make_box(self.company, "BOX-GT")
        self.svc.scan(transfer, "BOX-GT")
        transfer.refresh_from_db()
        self.assertEqual(transfer.status, BSTTransferStatus.SCANNING)

    def test_live_approve_seals_and_blocks_further_scan(self):
        # Approve on a live transfer is an optional seal: it stamps the approver,
        # keeps it in transit, and stops the sender from scanning more.
        transfer = self._create_transfer(requires_gate=False)
        make_box(self.company, "BOX-S1")
        self.svc.scan(transfer, "BOX-S1")
        self.svc.approve(transfer)
        transfer.refresh_from_db()
        self.assertEqual(transfer.status, BSTTransferStatus.IN_TRANSIT)
        self.assertIsNotNone(transfer.scan_approved_at)
        make_box(self.company, "BOX-S2")
        with self.assertRaises(BSTError):
            self.svc.scan(transfer, "BOX-S2")

    def test_remove_last_live_scan_reverts_to_scanning(self):
        # Clearing every (unreceived) box on a live transfer pulls it back off the
        # incoming board (→ SCANNING) until scanning resumes.
        transfer = self._create_transfer(requires_gate=False)
        make_box(self.company, "BOX-R1")
        self.svc.scan(transfer, "BOX-R1")
        transfer.refresh_from_db()
        self.assertEqual(transfer.status, BSTTransferStatus.IN_TRANSIT)
        scan = transfer.box_scans.get(box_barcode="BOX-R1")
        self.svc.remove_scan(transfer, scan.id)
        transfer.refresh_from_db()
        self.assertEqual(transfer.status, BSTTransferStatus.SCANNING)
        self.assertIsNone(transfer.dispatched_at)


class BSTScanCompletenessTests(TestCase):
    """The sender's quantity gate: approve() is blocked while the scanned QUANTITY is
    short of the bill, mirroring the sales-dispatch lock. Catches a box whose piece
    count is wrong (right box count, short pieces) that the box-count cap misses."""

    def setUp(self):
        self.company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        self.user = User.objects.create(
            email="wh@example.com", full_name="WH User", employee_code="EMP1",
        )
        self.svc = BSTService(self.company.code, self.user)

    def _bill(self, *, quantity=10.0, box_count=10, pcs_per_carton=1.0):
        doc = {
            **FAKE_SAP_TRANSFER,
            "total_quantity": quantity,
            "lines": [
                dict(
                    FAKE_SAP_TRANSFER["lines"][0],
                    quantity=quantity,
                    box_count=box_count,
                    pcs_per_carton=pcs_per_carton,
                ),
            ],
        }
        data = {
            "sap_doc_entries": [555], "vehicle": None, "driver": None,
            "invoice_no": "INV-9", "requires_gate": False, "remarks": "",
        }
        with patch("warehouse.services.bst_service.SAPClient") as sap:
            sap.return_value.get_stock_transfer.return_value = doc
            return self.svc.create_transfer(data)

    def test_approve_blocks_on_quantity_shortfall(self):
        # Bill is 10 pcs; a single 1-pc box is scanned (box count looks fine, pieces
        # are short). Sealing must be blocked.
        transfer = self._bill(quantity=10.0, box_count=10)
        make_box(self.company, "BOX-SHORT", qty=Decimal("1"))
        self.svc.scan(transfer, "BOX-SHORT")
        with self.assertRaises(BSTError) as ctx:
            self.svc.approve(transfer)
        self.assertIn("short", str(ctx.exception).lower())
        transfer.refresh_from_db()
        self.assertEqual(transfer.status, BSTTransferStatus.IN_TRANSIT)  # live, unsealed
        self.assertIsNone(transfer.scan_approved_at)

    def test_approve_passes_when_full_quantity_scanned(self):
        # One box carrying the full 10 pcs satisfies the bill on quantity.
        transfer = self._bill(quantity=10.0, box_count=10)
        make_box(self.company, "BOX-FULL", qty=Decimal("10"))
        self.svc.scan(transfer, "BOX-FULL")
        self.svc.approve(transfer)
        transfer.refresh_from_db()
        self.assertIsNotNone(transfer.scan_approved_at)

    def test_scan_status_reports_shortfall(self):
        transfer = self._bill(quantity=10.0, box_count=10)
        make_box(self.company, "BOX-1", qty=Decimal("4"))
        self.svc.scan(transfer, "BOX-1")
        status = compute_scan_status(transfer)
        self.assertTrue(status["is_partial"])
        self.assertTrue(status["uses_quantity"])
        self.assertEqual(status["scanned_qty"], Decimal("4"))
        self.assertEqual(status["expected_qty"], Decimal("10"))
        self.assertEqual([r["item_code"] for r in status["short_items"]], ["ITM1"])

    def test_surplus_quantity_is_not_short(self):
        # A box carrying more than the line (a bigger carton covering a smaller line)
        # reads complete, never blocked.
        transfer = self._bill(quantity=10.0, box_count=10)
        make_box(self.company, "BOX-BIG", qty=Decimal("12"))
        self.svc.scan(transfer, "BOX-BIG")
        status = compute_scan_status(transfer)
        self.assertFalse(status["is_partial"])
        self.svc.approve(transfer)  # does not raise

    def test_box_count_fallback_when_scans_carry_no_quantity(self):
        # Legacy / quantity-less scans (qty 0) fall back to the box-count estimate:
        # fewer boxes than the bill's box_count is short.
        transfer = self._bill(quantity=10.0, box_count=2)
        make_box(self.company, "BOX-Z1", qty=Decimal("0"))
        self.svc.scan(transfer, "BOX-Z1")
        status = compute_scan_status(transfer)
        self.assertFalse(status["uses_quantity"])
        self.assertTrue(status["is_partial"])  # 1 of 2 boxes, no qty to trust
        with self.assertRaises(BSTError):
            self.svc.approve(transfer)

    # -- Partial-transfer approval (seal a short scan with admin sign-off) ----

    def test_request_partial_transfer_creates_pending(self):
        transfer = self._bill(quantity=10.0, box_count=10)
        make_box(self.company, "BOX-1", qty=Decimal("1"))
        self.svc.scan(transfer, "BOX-1")
        req = self.svc.request_partial_transfer(transfer, "one box damaged, sending rest")
        self.assertEqual(req.status, BSTPartialTransferStatus.PENDING)
        self.assertEqual(req.scanned_qty, Decimal("1.000"))
        self.assertEqual(req.expected_qty, Decimal("10.000"))

    def test_request_partial_transfer_requires_reason(self):
        transfer = self._bill(quantity=10.0, box_count=10)
        make_box(self.company, "BOX-1", qty=Decimal("1"))
        self.svc.scan(transfer, "BOX-1")
        with self.assertRaises(BSTError):
            self.svc.request_partial_transfer(transfer, "  ")

    def test_request_partial_transfer_rejected_when_complete(self):
        transfer = self._bill(quantity=10.0, box_count=10)
        make_box(self.company, "BOX-FULL", qty=Decimal("10"))
        self.svc.scan(transfer, "BOX-FULL")
        with self.assertRaises(BSTError):
            self.svc.request_partial_transfer(transfer, "not needed")

    def test_request_is_idempotent_while_pending(self):
        transfer = self._bill(quantity=10.0, box_count=10)
        make_box(self.company, "BOX-1", qty=Decimal("1"))
        self.svc.scan(transfer, "BOX-1")
        first = self.svc.request_partial_transfer(transfer, "short")
        second = self.svc.request_partial_transfer(transfer, "short again")
        self.assertEqual(first.id, second.id)
        self.assertEqual(transfer.partial_transfer_requests.count(), 1)

    def test_approved_partial_transfer_unlocks_approve(self):
        transfer = self._bill(quantity=10.0, box_count=10)
        make_box(self.company, "BOX-1", qty=Decimal("1"))
        self.svc.scan(transfer, "BOX-1")
        req = self.svc.request_partial_transfer(transfer, "short, urgent")
        # Still blocked while pending.
        with self.assertRaises(BSTError):
            self.svc.approve(transfer)
        # Admin approves → sealing is released.
        self.svc.review_partial_transfer(req.id, approve=True, notes="ok")
        self.svc.approve(transfer)
        transfer.refresh_from_db()
        self.assertIsNotNone(transfer.scan_approved_at)

    def test_rejected_partial_transfer_keeps_lock(self):
        transfer = self._bill(quantity=10.0, box_count=10)
        make_box(self.company, "BOX-1", qty=Decimal("1"))
        self.svc.scan(transfer, "BOX-1")
        req = self.svc.request_partial_transfer(transfer, "short")
        self.svc.review_partial_transfer(req.id, approve=False, notes="scan the rest")
        with self.assertRaises(BSTError):
            self.svc.approve(transfer)

    def test_reject_requires_note(self):
        transfer = self._bill(quantity=10.0, box_count=10)
        make_box(self.company, "BOX-1", qty=Decimal("1"))
        self.svc.scan(transfer, "BOX-1")
        req = self.svc.request_partial_transfer(transfer, "short")
        with self.assertRaises(BSTError):
            self.svc.review_partial_transfer(req.id, approve=False, notes="")

    def test_detail_serializer_exposes_scan_status_and_partial(self):
        from warehouse.serializers_bst import BSTTransferDetailSerializer

        transfer = self._bill(quantity=10.0, box_count=10)
        make_box(self.company, "BOX-1", qty=Decimal("4"))
        self.svc.scan(transfer, "BOX-1")
        self.svc.request_partial_transfer(transfer, "two boxes held back")

        data = BSTTransferDetailSerializer(self.svc.get_transfer(transfer.id)).data
        self.assertTrue(data["scan_status"]["is_partial"])
        self.assertEqual(data["scan_status"]["scanned_qty"], "4")
        self.assertEqual(data["scan_status"]["expected_qty"], "10")
        self.assertEqual([i["item_code"] for i in data["scan_status"]["short_items"]], ["ITM1"])
        self.assertIsNotNone(data["partial_transfer"])
        self.assertTrue(data["partial_transfer"]["is_pending"])


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

    def test_finalized_receipt_stays_on_incoming_board_but_not_receivable(self):
        # A finalized receipt must not vanish off the Incoming board: it drops out
        # of the receivable set (no more actions) but stays in the board view.
        transfer = self._dispatched_transfer(["BOX-1"])
        self.dst_svc.receive_scan(transfer, "BOX-1", decision="ACCEPTED")
        self.dst_svc.receive_complete(transfer)
        transfer.refresh_from_db()
        self.assertEqual(transfer.status, BSTTransferStatus.RECEIVED)
        self.assertEqual(self.dst_svc.incoming_queryset().filter(id=transfer.id).count(), 0)
        self.assertEqual(self.dst_svc.incoming_view_queryset().filter(id=transfer.id).count(), 1)

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

    def test_finalize_untouched_receipt_is_rejected(self):
        # Finalizing with nothing accepted/rejected must not silently strand the
        # whole shipment as PARTIALLY_RECEIVED (the incident this guards against).
        transfer = self._dispatched_transfer(["BOX-1", "BOX-2"])
        with self.assertRaises(BSTError):
            self.dst_svc.receive_complete(transfer)
        transfer.refresh_from_db()
        self.assertEqual(transfer.status, BSTTransferStatus.IN_TRANSIT)
        self.assertIsNone(transfer.received_at)

    def test_partial_receipt_is_resumable_to_received(self):
        # A partial receipt can be resumed: accept the rest and re-finalize → the
        # transfer completes as RECEIVED, and the already-settled box isn't moved
        # (or TRANSFER-logged) a second time.
        transfer = self._dispatched_transfer(["BOX-1", "BOX-2"])
        self.dst_svc.receive_scan(transfer, "BOX-1", decision="ACCEPTED")
        self.dst_svc.receive_complete(transfer)
        transfer.refresh_from_db()
        self.assertEqual(transfer.status, BSTTransferStatus.PARTIALLY_RECEIVED)

        # Resume: the transfer is still receivable, so accept the remaining box.
        self.dst_svc.receive_scan(transfer, "BOX-2", decision="ACCEPTED")
        self.dst_svc.receive_complete(transfer)
        transfer.refresh_from_db()
        self.assertEqual(transfer.status, BSTTransferStatus.RECEIVED)
        # BOX-1 settled once, not twice, despite the second finalize.
        self.assertEqual(
            Box.objects.get(box_barcode="BOX-1").movements.filter(movement_type="TRANSFER").count(),
            1,
        )

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
        # A freshly-created transfer with no scans is not yet receivable — the
        # destination can only act once the sender has scanned something.
        data = {
            "sap_doc_entries": [555], "vehicle": None, "driver": None,
            "invoice_no": "", "requires_gate": False, "remarks": "",
        }
        with patch("warehouse.services.bst_service.SAPClient") as sap:
            sap.return_value.get_stock_transfer.return_value = dict(FAKE_SAP_TRANSFER)
            transfer = self.src_svc.create_transfer(data)
        with self.assertRaises(BSTError):
            self.dst_svc.receive_scan(transfer, "BOX-1")

    # -- Live concurrent send + receive (no approve, no gate) ---------------

    def _live_transfer(self, barcodes):
        """Create a live internal transfer and scan boxes WITHOUT approving."""
        data = {
            "sap_doc_entries": [555], "vehicle": None, "driver": None,
            "invoice_no": "INV-L", "requires_gate": False, "remarks": "",
        }
        with patch("warehouse.services.bst_service.SAPClient") as sap:
            sap.return_value.get_stock_transfer.return_value = dict(FAKE_SAP_TRANSFER)
            transfer = self.src_svc.create_transfer(data)
        for code in barcodes:
            make_box(self.company, code)
            self.src_svc.scan(transfer, code)
        return transfer

    def test_incoming_lists_live_transfer_before_approve(self):
        # The destination sees the shipment as soon as the sender starts scanning,
        # without any approve/dispatch step.
        transfer = self._live_transfer(["BOX-1"])
        incoming = self.dst_svc.incoming_queryset()
        self.assertEqual([t.id for t in incoming], [transfer.id])

    def test_receiver_can_scan_while_sender_still_scanning(self):
        # Sender scans BOX-1 (→ in transit); receiver accepts it; sender then adds
        # BOX-2 concurrently; receiver accepts that too. Finalizing is only allowed
        # once the sender seals the send (approve).
        transfer = self._live_transfer(["BOX-1"])
        self.dst_svc.receive_scan(transfer, "BOX-1", decision="ACCEPTED")
        transfer.refresh_from_db()
        self.assertEqual(transfer.status, BSTTransferStatus.RECEIVING)

        make_box(self.company, "BOX-2")
        self.assertEqual(self.src_svc.scan(transfer, "BOX-2")["created_count"], 1)
        self.dst_svc.receive_scan(transfer, "BOX-2", decision="ACCEPTED")
        self.src_svc.approve(transfer)  # sender seals the send
        self.dst_svc.receive_complete(transfer)
        transfer.refresh_from_db()
        self.assertEqual(transfer.status, BSTTransferStatus.RECEIVED)
        self.assertEqual(Box.objects.get(box_barcode="BOX-2").current_warehouse, "WH-B")

    def test_receiver_cannot_finalize_until_sender_seals(self):
        # The hard block: while the sender is still scanning (unsealed), the
        # receiver can accept boxes but must not finalize.
        transfer = self._live_transfer(["BOX-1"])
        self.dst_svc.receive_scan(transfer, "BOX-1", decision="ACCEPTED")
        with self.assertRaises(BSTError):
            self.dst_svc.receive_complete(transfer)
        transfer.refresh_from_db()
        self.assertIsNone(transfer.received_at)
        self.assertEqual(transfer.status, BSTTransferStatus.RECEIVING)

        # Once the sender seals it, finalizing succeeds.
        self.src_svc.approve(transfer)
        self.dst_svc.receive_complete(transfer)
        transfer.refresh_from_db()
        self.assertEqual(transfer.status, BSTTransferStatus.RECEIVED)

    def test_sender_cannot_remove_box_receiver_accepted(self):
        transfer = self._live_transfer(["BOX-1"])
        self.dst_svc.receive_scan(transfer, "BOX-1", decision="ACCEPTED")
        scan = transfer.box_scans.get(box_barcode="BOX-1")
        with self.assertRaises(BSTError):
            self.src_svc.remove_scan(transfer, scan.id)
        self.assertTrue(transfer.box_scans.filter(id=scan.id).exists())


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


class BSTInvoiceFlowTests(TestCase):
    """Invoice-sourced BST — a cross-company sale (e.g. JIVO OIL → JIVO MART).

    Unlike a stock-transfer BST, on receipt the accepted boxes change *company*
    (ownership handoff), not just their warehouse.
    """

    def setUp(self):
        self.sender = User.objects.create(
            email="isrc@example.com", full_name="Src", employee_code="EMP-IS",
        )
        self.receiver = User.objects.create(
            email="idst@example.com", full_name="Dst", employee_code="EMP-ID",
        )

    def _dispatched_invoice_transfer(self, source, destination, barcodes, *, item_code="ITM1"):
        """Build, scan and approve an INVOICE BST directly (no SAP round-trip)."""
        src_svc = BSTService(source.code, self.sender)
        transfer = BSTTransfer.objects.create(
            company=source,
            entry_no=BSTTransfer.generate_entry_no(),
            source_type=BSTSourceType.INVOICE,
            destination_company=destination,
            customer_code=destination.code,
            customer_name=destination.name,
            sap_doc_entry=900, sap_doc_num="INV-900",
            sap_from_warehouse="WH-A", sap_to_warehouse="",
            status=BSTTransferStatus.SCANNING, created_by=self.sender,
        )
        doc = BSTTransferDoc.objects.create(
            transfer=transfer, sap_doc_entry=900, sap_doc_num="INV-900", invoice_no="INV-900",
        )
        # Expected quantity matches the boxes scanned (1 pc each) so the sender's
        # completeness gate passes — these tests exercise the receive/ownership flow,
        # not the quantity lock (see BSTScanCompletenessTests for that).
        BSTTransferItem.objects.create(
            transfer=transfer, doc=doc, line_num=0, item_code=item_code,
            item_name="Item One", quantity=Decimal(str(len(barcodes))), uom="PCS",
            from_warehouse="WH-A", to_warehouse="", expected_boxes=len(barcodes),
        )
        for code in barcodes:
            make_box(source, code, item_code=item_code)
            src_svc.scan(transfer, code)
        src_svc.approve(transfer)
        return transfer

    def test_invoice_receipt_moves_box_ownership_to_destination_company(self):
        source = Company.objects.create(name="Acme", code="ACME")
        destination = Company.objects.create(name="Beta", code="BETA")
        transfer = self._dispatched_invoice_transfer(source, destination, ["BOX-1"])

        # The destination company sees it as incoming; the source (owner) does not.
        self.assertEqual(
            BSTService(destination.code, self.receiver).incoming_queryset().count(), 1,
        )
        self.assertEqual(
            BSTService(source.code, self.sender).incoming_queryset().count(), 0,
        )

        dst_svc = BSTService(destination.code, self.receiver)
        dst_svc.receive_scan(transfer, "BOX-1", decision="ACCEPTED")
        dst_svc.receive_complete(transfer)

        transfer.refresh_from_db()
        self.assertEqual(transfer.status, BSTTransferStatus.RECEIVED)
        box = Box.objects.get(box_barcode="BOX-1")
        self.assertEqual(box.company_id, destination.id)  # ownership moved
        self.assertTrue(
            BarcodeAuditLog.objects.filter(
                box=box, transaction_type="TRANSFER_COMPLETED",
                from_company=source, to_company=destination,
            ).exists()
        )

    def test_invoice_reject_keeps_box_with_source_company(self):
        source = Company.objects.create(name="Acme", code="ACME")
        destination = Company.objects.create(name="Beta", code="BETA")
        transfer = self._dispatched_invoice_transfer(source, destination, ["BOX-1", "BOX-2"])

        dst_svc = BSTService(destination.code, self.receiver)
        dst_svc.receive_scan(transfer, "BOX-1", decision="ACCEPTED")
        dst_svc.receive_scan(transfer, "BOX-2", decision="REJECTED", reject_reason="damaged")
        dst_svc.receive_complete(transfer)

        # Accepted box changed company; rejected box stayed with the source.
        self.assertEqual(Box.objects.get(box_barcode="BOX-1").company_id, destination.id)
        self.assertEqual(Box.objects.get(box_barcode="BOX-2").company_id, source.id)

    def test_invoice_accept_moves_ownership_immediately_at_scan(self):
        # Ownership is handed to the destination the instant a box is accepted —
        # no receive_complete needed.
        source = Company.objects.create(name="Acme", code="ACME")
        destination = Company.objects.create(name="Beta", code="BETA")
        transfer = self._dispatched_invoice_transfer(source, destination, ["BOX-1"])

        BSTService(destination.code, self.receiver).receive_scan(
            transfer, "BOX-1", decision="ACCEPTED",
        )
        self.assertEqual(Box.objects.get(box_barcode="BOX-1").company_id, destination.id)

    def test_invoice_reject_after_accept_returns_box_to_source(self):
        # Rejecting a box that was already accepted returns its ownership to the
        # source company (the reversal), and logs it.
        source = Company.objects.create(name="Acme", code="ACME")
        destination = Company.objects.create(name="Beta", code="BETA")
        transfer = self._dispatched_invoice_transfer(source, destination, ["BOX-1"])
        dst_svc = BSTService(destination.code, self.receiver)

        dst_svc.receive_scan(transfer, "BOX-1", decision="ACCEPTED")
        self.assertEqual(Box.objects.get(box_barcode="BOX-1").company_id, destination.id)

        dst_svc.receive_scan(transfer, "BOX-1", decision="REJECTED", reject_reason="changed mind")
        box = Box.objects.get(box_barcode="BOX-1")
        self.assertEqual(box.company_id, source.id)  # returned to source
        self.assertTrue(
            BarcodeAuditLog.objects.filter(
                box=box, transaction_type="TRANSFER_REVERSED",
                from_company=destination, to_company=source,
            ).exists()
        )

    def test_invoice_accept_then_reject_moves_pallet_with_its_boxes(self):
        # The pallet follows its boxes: accepting a palletised box hands the box
        # AND its pallet to the destination; rejecting it (emptying the pallet at
        # the destination) returns both to the source.
        source = Company.objects.create(name="Acme", code="ACME")
        destination = Company.objects.create(name="Beta", code="BETA")
        transfer = self._dispatched_invoice_transfer(source, destination, ["BOX-1"])
        pallet = Pallet.objects.create(
            company=source, pallet_id="PLT-INV-1", item_code="ITM1", batch_number="B1",
            total_qty=Decimal("1"), uom="PCS", mfg_date=date(2026, 1, 1),
            exp_date=date(2027, 1, 1), current_warehouse="WH-A",
        )
        box = Box.objects.get(box_barcode="BOX-1")
        box.pallet = pallet
        box.save(update_fields=["pallet"])
        dst_svc = BSTService(destination.code, self.receiver)

        dst_svc.receive_scan(transfer, "BOX-1", decision="ACCEPTED")
        pallet.refresh_from_db()
        self.assertEqual(pallet.company_id, destination.id)
        self.assertEqual(Box.objects.get(box_barcode="BOX-1").company_id, destination.id)

        dst_svc.receive_scan(transfer, "BOX-1", decision="REJECTED", reject_reason="wrong")
        pallet.refresh_from_db()
        self.assertEqual(pallet.company_id, source.id)
        self.assertEqual(Box.objects.get(box_barcode="BOX-1").company_id, source.id)

    def test_invoice_pallet_barcode_accept_then_reject_via_transfer_fallback(self):
        # Scanning a whole pallet by its barcode accepts every box on it; once
        # accepted the pallet has moved to the destination, so a follow-up reject
        # can no longer resolve it via the source-scoped scanner — the
        # transfer-scoped pallet_code fallback must still return it to the source.
        source = Company.objects.create(name="Acme", code="ACME")
        destination = Company.objects.create(name="Beta", code="BETA")
        transfer = self._dispatched_invoice_transfer(source, destination, ["BOX-1", "BOX-2"])
        pallet = Pallet.objects.create(
            company=source, pallet_id="PLT-INV-9", item_code="ITM1", batch_number="B1",
            total_qty=Decimal("2"), uom="PCS", mfg_date=date(2026, 1, 1),
            exp_date=date(2027, 1, 1), current_warehouse="WH-A",
        )
        Box.objects.filter(box_barcode__in=["BOX-1", "BOX-2"]).update(pallet=pallet)
        transfer.box_scans.filter(box_barcode__in=["BOX-1", "BOX-2"]).update(pallet_code="PLT-INV-9")
        dst_svc = BSTService(destination.code, self.receiver)

        dst_svc.receive_scan(transfer, "PLT-INV-9", decision="ACCEPTED")
        pallet.refresh_from_db()
        self.assertEqual(pallet.company_id, destination.id)
        self.assertEqual(Box.objects.filter(pallet=pallet, company=destination).count(), 2)

        dst_svc.receive_scan(transfer, "PLT-INV-9", decision="REJECTED", reject_reason="wrong pallet")
        pallet.refresh_from_db()
        self.assertEqual(pallet.company_id, source.id)
        self.assertEqual(Box.objects.filter(pallet=pallet, company=source).count(), 2)

    @patch("barcode.services.box_ownership.OitmItemService")
    def test_invoice_receipt_remaps_item_code_for_jivo_mart(self, mock_oitm):
        source = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")
        destination = Company.objects.create(name="Jivo Mart", code="JIVO_MART")
        mock_oitm.return_value.find_item_codes_by_oil_item_code.return_value = ["MART-1"]
        transfer = self._dispatched_invoice_transfer(
            source, destination, ["BOX-1"], item_code="OIL-1",
        )

        dst_svc = BSTService(destination.code, self.receiver)
        dst_svc.receive_scan(transfer, "BOX-1", decision="ACCEPTED")
        dst_svc.receive_complete(transfer)

        box = Box.objects.get(box_barcode="BOX-1")
        self.assertEqual(box.company_id, destination.id)
        self.assertEqual(box.item_code, "MART-1")  # remapped to the JIVO MART catalogue

    def test_create_invoice_transfer_snapshots_customer_and_destination(self):
        from gate_core.services.sales_dispatch_documents import SalesDispatchDocumentService

        source = Company.objects.create(name="Acme", code="ACME")
        destination = Company.objects.create(name="Beta", code="BETA")
        fake_doc = {
            "doc_entry": 900, "doc_num": "INV-900", "doc_date": date(2026, 6, 1),
            "card_code": "BETA", "card_name": "Beta Co",
            "base_refs": "", "warehouses": "WH-A",
            "line_count": 1, "total_quantity": 10, "total_boxes": 10,
            "items": [
                {"line_num": 0, "item_code": "ITM1", "item_name": "Item One",
                 "quantity": 10, "uom": "PCS", "warehouse_code": "WH-A", "total_boxes": 10},
            ],
        }
        data = {
            "document_type": "INVOICE", "sap_doc_entries": [900],
            "destination_company": destination, "vehicle": None, "driver": None,
            "invoice_no": "", "requires_gate": False, "remarks": "",
        }
        with patch.object(SalesDispatchDocumentService, "get_document", return_value=fake_doc):
            transfer = BSTService(source.code, self.sender).create_transfer(data)

        self.assertEqual(transfer.source_type, BSTSourceType.INVOICE)
        self.assertEqual(transfer.destination_company_id, destination.id)
        self.assertEqual(transfer.customer_code, "BETA")
        self.assertEqual(transfer.customer_name, "Beta Co")
        self.assertEqual(transfer.sap_from_warehouse, "WH-A")
        self.assertEqual(transfer.invoice_no, "INV-900")
        self.assertEqual(transfer.items.get(item_code="ITM1").expected_boxes, 10)

    def test_create_invoice_box_count_falls_back_to_pack_size(self):
        # When SAP carries no box total (U_UNE_TOTB unmaintained -> total_boxes 0),
        # the box count is derived from quantity / pack-size parsed from the item
        # name, so the scan page isn't stuck at 0 boxes.
        from gate_core.services.sales_dispatch_documents import SalesDispatchDocumentService

        source = Company.objects.create(name="Acme", code="ACME")
        destination = Company.objects.create(name="Beta", code="BETA")
        fake_doc = {
            "doc_entry": 901, "doc_num": "INV-901", "doc_date": date(2026, 6, 1),
            "card_code": "BETA", "card_name": "Beta Co", "warehouses": "WH-A",
            "line_count": 1, "total_quantity": 24, "total_boxes": 0,
            "items": [
                {"line_num": 0, "item_code": "ITM1", "item_name": "OIL 1L 12 PCS",
                 "quantity": 24, "uom": "PCS", "warehouse_code": "WH-A", "total_boxes": 0},
            ],
        }
        data = {
            "document_type": "INVOICE", "sap_doc_entries": [901],
            "destination_company": destination, "vehicle": None, "driver": None,
            "invoice_no": "", "requires_gate": False, "remarks": "",
        }
        with patch.object(SalesDispatchDocumentService, "get_document", return_value=fake_doc):
            transfer = BSTService(source.code, self.sender).create_transfer(data)

        # 24 pcs / 12 pcs-per-carton = 2 boxes.
        self.assertEqual(transfer.items.get(item_code="ITM1").expected_boxes, 2)
