"""Tests for linking a transfer request to the BST that checks its boxes.

This link used to be set by our own `create_bst` endpoint. The button now hands
off to the ordinary BST screen instead — so the BST is created by code that knows
nothing about transfer requests, and the link has to be recovered from the SAP
document afterwards. Leg 2 of a cross-branch move depends on it, as does the
destination correction that keeps boxes out of the in-transit warehouse.

Needs the database, so these are `TestCase` rather than the DB-free guard tests.
"""

from company.models import Company
from django.test import TestCase

from .models_bst import BSTSourceType, BSTTransfer, BSTTransferDoc, BSTTransferStatus
from .models_transfer import (
    TransferPostingStatus,
    TransferRequestStatus,
    TransferRouteType,
    WarehouseTransferRequest,
)
from .services.transfer_request_service import TransferRequestService

POSTED_DOC_ENTRY = 21337
POSTED_DOC_NUM = "826676783"


class ResolveBSTTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")

    def setUp(self):
        self.service = TransferRequestService("JIVO_OIL")

    # --- helpers -------------------------------------------------------

    def _request(self, **kwargs):
        defaults = dict(
            company=self.company,
            entry_no=WarehouseTransferRequest.generate_entry_no(),
            from_warehouse="BH-PF",
            to_warehouse="BH-BT",
            route_type=TransferRouteType.INTRA_BRANCH,
            from_branch_id=2,
            to_branch_id=2,
            status=TransferRequestStatus.APPROVED,
            posting_status=TransferPostingStatus.POSTED,
            sap_transfer_doc_entry=POSTED_DOC_ENTRY,
            sap_transfer_doc_num=POSTED_DOC_NUM,
        )
        defaults.update(kwargs)
        return WarehouseTransferRequest.objects.create(**defaults)

    def _bst(self, *, doc_entry=POSTED_DOC_ENTRY, to_warehouse="BH-BT", status=None):
        bst = BSTTransfer.objects.create(
            company=self.company,
            entry_no=BSTTransfer.generate_entry_no(),
            source_type=BSTSourceType.STOCK_TRANSFER,
            sap_doc_entry=doc_entry,
            sap_doc_num=POSTED_DOC_NUM,
            sap_from_warehouse="BH-PF",
            sap_to_warehouse=to_warehouse,
            status=status or BSTTransferStatus.SCANNING,
        )
        BSTTransferDoc.objects.create(
            transfer=bst, sap_doc_entry=doc_entry, sap_doc_num=POSTED_DOC_NUM
        )
        return bst

    # --- tests ---------------------------------------------------------

    def test_no_bst_yet_resolves_to_nothing(self):
        request = self._request()
        self.assertIsNone(self.service.resolve_bst(request))
        request.refresh_from_db()
        self.assertIsNone(request.bst_transfer_id)

    def test_unposted_request_resolves_to_nothing(self):
        # Nothing to match on until the transfer has a SAP document.
        request = self._request(
            sap_transfer_doc_entry=None,
            sap_transfer_doc_num="",
            posting_status=TransferPostingStatus.NOT_POSTED,
        )
        self._bst()
        self.assertIsNone(self.service.resolve_bst(request))

    def test_links_by_sap_document(self):
        request = self._request()
        bst = self._bst()
        self.assertEqual(self.service.resolve_bst(request), bst)
        request.refresh_from_db()
        self.assertEqual(request.bst_transfer_id, bst.id)

    def test_an_existing_link_is_returned_untouched(self):
        request = self._request()
        bst = self._bst()
        request.bst_transfer = bst
        request.save(update_fields=["bst_transfer"])
        # A second BST on a different document must not steal the link.
        self._bst(doc_entry=999999)
        self.assertEqual(self.service.resolve_bst(request), bst)

    def test_a_cancelled_bst_is_ignored(self):
        request = self._request()
        self._bst(status=BSTTransferStatus.CANCELLED)
        self.assertIsNone(self.service.resolve_bst(request))

    def test_a_different_document_is_not_matched(self):
        request = self._request()
        self._bst(doc_entry=888888)
        self.assertIsNone(self.service.resolve_bst(request))

    def test_cross_branch_destination_is_corrected(self):
        # Leg 1's SAP document ships into PB-INT, but the boxes physically land
        # at PB-PS. BST settles accepted boxes to its head's sap_to_warehouse,
        # so leaving it as PB-INT would park them in a bookkeeping warehouse.
        request = self._request(
            route_type=TransferRouteType.CROSS_BRANCH,
            to_warehouse="PB-PS",
            to_branch_id=3,
            intransit_warehouse="PB-INT",
            posting_status=TransferPostingStatus.IN_TRANSIT,
        )
        bst = self._bst(to_warehouse="PB-INT")
        self.service.resolve_bst(request)
        bst.refresh_from_db()
        self.assertEqual(bst.sap_to_warehouse, "PB-PS")

    def test_intra_branch_destination_is_left_alone(self):
        request = self._request()
        bst = self._bst(to_warehouse="BH-BT")
        self.service.resolve_bst(request)
        bst.refresh_from_db()
        self.assertEqual(bst.sap_to_warehouse, "BH-BT")

    def test_get_request_links_only_when_asked(self):
        request = self._request()
        bst = self._bst()

        # Internal lookups must not write.
        self.assertIsNone(
            self.service.get_request(request.id).bst_transfer_id
        )
        # The detail read opts in, so the UI shows the BST once it exists.
        self.assertEqual(
            self.service.get_request(request.id, link_bst=True).bst_transfer_id,
            bst.id,
        )


class ReceivedQuantitiesFromLinkedBSTTests(TestCase):
    """Leg 2's quantities must survive the link being recovered, not set."""

    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Jivo Oil", code="JIVO_OIL")

    def test_scan_exempt_line_still_falls_back_to_what_leg_one_moved(self):
        from decimal import Decimal

        from .models_transfer import WarehouseTransferRequestLine

        service = TransferRequestService("JIVO_OIL")
        request = WarehouseTransferRequest.objects.create(
            company=self.company,
            entry_no=WarehouseTransferRequest.generate_entry_no(),
            from_warehouse="BH-PF",
            to_warehouse="PB-PS",
            route_type=TransferRouteType.CROSS_BRANCH,
            intransit_warehouse="PB-INT",
            status=TransferRequestStatus.APPROVED,
            posting_status=TransferPostingStatus.IN_TRANSIT,
            sap_transfer_doc_entry=POSTED_DOC_ENTRY,
        )
        WarehouseTransferRequestLine.objects.create(
            request=request, line_num=0, item_code="PM0000019",
            requested_qty=Decimal("500"), approved_qty=Decimal("500"),
            transferred_qty=Decimal("500"), is_batch_managed=False,
        )
        bst = BSTTransfer.objects.create(
            company=self.company,
            entry_no=BSTTransfer.generate_entry_no(),
            source_type=BSTSourceType.STOCK_TRANSFER,
            sap_doc_entry=POSTED_DOC_ENTRY,
            sap_from_warehouse="BH-PF",
            sap_to_warehouse="PB-PS",
            status=BSTTransferStatus.RECEIVED,
        )
        # No box scans at all — packaging material is never scanned.
        quantities = service.received_quantities_from_bst(request, bst)
        self.assertEqual(quantities, {0: Decimal("500")})
