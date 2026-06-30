"""Service layer for the Branch Stock Transfer (BST) sender flow.

Create a BST from a SAP stock-transfer document, scan the boxes/pallets being
moved, and dispatch it. Ownership of boxes is NOT changed here — that happens on
the receiving side when the destination warehouse accepts the boxes.
"""

import logging
from decimal import Decimal

from django.db import transaction
from django.db.models import Count
from django.utils import timezone

from barcode.models import Box, BoxStatus, Pallet
from barcode.services.scan_service import ScanService
from company.models import Company
from sap_client.client import SAPClient

from ..models_bst import (
    BSTBoxScan,
    BSTReceiveStatus,
    BSTTransfer,
    BSTTransferItem,
    BSTTransferStatus,
)

logger = logging.getLogger(__name__)


class BSTError(ValueError):
    """Domain error surfaced to the API as a 400."""


# Transfer states in which a box is still committed to a BST (used as a soft
# lock so the same box can't be put on two BSTs at once).
IN_FLIGHT_STATUSES = (
    BSTTransferStatus.DRAFT,
    BSTTransferStatus.SCANNING,
    BSTTransferStatus.DISPATCHED,
    BSTTransferStatus.AWAITING_GATE_OUT,
    BSTTransferStatus.GATED_OUT,
    BSTTransferStatus.IN_TRANSIT,
    BSTTransferStatus.AWAITING_GATE_IN,
    BSTTransferStatus.GATED_IN,
    BSTTransferStatus.ARRIVED,
    BSTTransferStatus.RECEIVING,
)

# Sender may still edit/scan only while in these states.
EDITABLE_STATUSES = (BSTTransferStatus.DRAFT, BSTTransferStatus.SCANNING)


class BSTService:

    def __init__(self, company_code: str, user=None):
        self.company_code = company_code
        self.user = user
        self._company = None
        self._scan_service = None

    @property
    def company(self) -> Company:
        if self._company is None:
            self._company = Company.objects.get(code=self.company_code)
        return self._company

    @property
    def scanner(self) -> ScanService:
        if self._scan_service is None:
            self._scan_service = ScanService(self.company_code)
        return self._scan_service

    # ==================================================================
    # SAP stock-transfer lookup
    # ==================================================================

    def list_sap_transfers(self, *, search=None, from_date=None, to_date=None, limit=50) -> list[dict]:
        client = SAPClient(self.company_code)
        return client.list_stock_transfers(
            search=search, from_date=from_date, to_date=to_date, limit=limit,
        )

    def get_sap_transfer(self, doc_entry: int) -> dict:
        client = SAPClient(self.company_code)
        transfer = client.get_stock_transfer(doc_entry)
        if not transfer:
            raise BSTError(f"SAP stock transfer {doc_entry} was not found.")
        return transfer

    # ==================================================================
    # Querysets
    # ==================================================================

    def list_queryset(self):
        """Lean queryset for list views (outgoing for this company)."""
        return (
            BSTTransfer.objects
            .filter(company=self.company)
            .select_related("company", "to_company", "vehicle", "driver")
            .annotate(
                scanned_box_count=Count("box_scans", distinct=True),
                item_count=Count("items", distinct=True),
            )
            .order_by("-created_at")
        )

    def detail_queryset(self):
        return (
            BSTTransfer.objects
            .select_related("company", "to_company", "vehicle", "driver",
                            "created_by", "dispatched_by", "received_by")
            .prefetch_related("items", "box_scans__scanned_by", "box_scans__received_by")
        )

    def get_transfer(self, transfer_id: int) -> BSTTransfer:
        try:
            return self.detail_queryset().get(id=transfer_id, company=self.company)
        except BSTTransfer.DoesNotExist as exc:
            raise BSTError("BST transfer not found.") from exc

    # ==================================================================
    # Create
    # ==================================================================

    @transaction.atomic
    def create_transfer(self, data: dict) -> BSTTransfer:
        to_company = data["to_company"]
        if to_company.id == self.company.id:
            raise BSTError("Source and destination company cannot be the same.")

        sap = self.get_sap_transfer(data["sap_doc_entry"])

        transfer = BSTTransfer.objects.create(
            company=self.company,
            to_company=to_company,
            entry_no=BSTTransfer.generate_entry_no(),
            sap_doc_entry=sap["doc_entry"],
            sap_doc_num=str(sap.get("doc_num") or ""),
            sap_doc_date=sap.get("doc_date"),
            sap_from_warehouse=sap.get("from_warehouse") or "",
            sap_to_warehouse=sap.get("to_warehouse") or "",
            sap_reference=sap.get("reference") or "",
            invoice_no=data.get("invoice_no", ""),
            vehicle=data["vehicle"],
            driver=data["driver"],
            requires_gate=data.get("requires_gate", False),
            remarks=data.get("remarks", ""),
            status=BSTTransferStatus.SCANNING,
            created_by=self.user,
        )

        items = [
            BSTTransferItem(
                transfer=transfer,
                line_num=line["line_num"],
                item_code=line.get("item_code", ""),
                item_name=line.get("item_name", ""),
                quantity=Decimal(str(line.get("quantity") or 0)),
                uom=line.get("uom", ""),
                from_warehouse=line.get("from_warehouse", ""),
                to_warehouse=line.get("to_warehouse", ""),
            )
            for line in sap.get("lines", [])
        ]
        BSTTransferItem.objects.bulk_create(items)
        return transfer

    @transaction.atomic
    def update_transfer(self, transfer: BSTTransfer, data: dict) -> BSTTransfer:
        if transfer.status not in EDITABLE_STATUSES:
            raise BSTError("This BST can no longer be edited.")
        for field in ("vehicle", "driver", "invoice_no", "requires_gate", "remarks"):
            if field in data:
                setattr(transfer, field, data[field])
        transfer.save()
        return transfer

    # ==================================================================
    # Scanning
    # ==================================================================

    def _ensure_editable(self, transfer: BSTTransfer) -> None:
        if transfer.status not in EDITABLE_STATUSES:
            raise BSTError("This BST is no longer open for scanning.")

    def _box_locked_elsewhere(self, box: Box, transfer: BSTTransfer) -> bool:
        return (
            BSTBoxScan.objects
            .filter(box=box, transfer__status__in=IN_FLIGHT_STATUSES)
            .exclude(transfer_id=transfer.id)
            .exists()
        )

    def _validate_box(self, box: Box) -> None:
        if box.company_id != self.company.id:
            raise BSTError(f"{box.box_barcode} does not belong to {self.company.code}.")
        if box.status not in (BoxStatus.ACTIVE, BoxStatus.PARTIAL):
            raise BSTError(f"{box.box_barcode} is not active.")
        if box.dispatched_at or box.status == BoxStatus.DISPATCHED:
            raise BSTError(f"{box.box_barcode} is already dispatched.")

    def _create_scan(self, transfer: BSTTransfer, box: Box) -> BSTBoxScan:
        return BSTBoxScan.objects.create(
            transfer=transfer,
            box=box,
            pallet=box.pallet,
            box_barcode=box.box_barcode,
            item_code=box.item_code,
            item_name=box.item_name,
            batch_number=box.batch_number,
            quantity=box.qty,
            uom=box.uom,
            warehouse_code=box.current_warehouse,
            pallet_code=box.pallet.pallet_id if box.pallet else "",
            scanned_by=self.user,
        )

    def _resolve_scan(self, barcode_raw: str):
        """Return ('BOX', Box) or ('PALLET', [Box, ...]) for a scanned code."""
        lookup = self.scanner.lookup_barcode(barcode_raw)
        entity_type = lookup.get("entity_type")
        entity_id = lookup.get("entity_id")

        if entity_type == "PALLET" and entity_id:
            pallet = Pallet.objects.filter(id=entity_id).first()
            if not pallet:
                raise BSTError("Pallet barcode does not exist.")
            boxes = list(
                pallet.boxes
                .filter(status__in=(BoxStatus.ACTIVE, BoxStatus.PARTIAL), dispatched_at__isnull=True)
                .order_by("id")
            )
            if not boxes:
                raise BSTError(f"{pallet.pallet_id} has no active boxes to transfer.")
            return "PALLET", boxes

        if entity_type == "BOX" and entity_id:
            box = Box.objects.select_related("pallet").filter(id=entity_id).first()
            if not box:
                raise BSTError("Barcode does not exist.")
            return "BOX", box

        raise BSTError("Barcode does not exist.")

    @transaction.atomic
    def scan(self, transfer: BSTTransfer, barcode_raw: str) -> dict:
        """Scan a box or pallet onto the transfer. Returns a result summary."""
        self._ensure_editable(transfer)
        barcode_raw = str(barcode_raw or "").strip()
        if not barcode_raw:
            raise BSTError("Barcode is required.")

        kind, payload = self._resolve_scan(barcode_raw)
        boxes = payload if kind == "PALLET" else [payload]

        existing = set(
            transfer.box_scans.filter(
                box_barcode__in=[b.box_barcode for b in boxes]
            ).values_list("box_barcode", flat=True)
        )

        created = []
        duplicates = []
        for box in boxes:
            if box.box_barcode in existing:
                duplicates.append(box.box_barcode)
                continue
            self._validate_box(box)
            if self._box_locked_elsewhere(box, transfer):
                raise BSTError(f"{box.box_barcode} is already on another active BST.")
            created.append(self._create_scan(transfer, box))

        return {
            "kind": kind,
            "created": created,
            "created_count": len(created),
            "duplicate_count": len(duplicates),
            "duplicates": duplicates,
        }

    def scan_batch(self, transfer: BSTTransfer, barcodes: list[str]) -> dict:
        """Scan many codes; never fails the whole call — reports per-code errors."""
        self._ensure_editable(transfer)
        saved, failed = [], []
        for raw in barcodes:
            try:
                result = self.scan(transfer, raw)
                saved.append({"barcode": raw, "created_count": result["created_count"],
                              "duplicate_count": result["duplicate_count"]})
            except BSTError as exc:
                failed.append({"barcode": raw, "reason": str(exc)})
        return {"saved": saved, "failed": failed}

    @transaction.atomic
    def remove_scan(self, transfer: BSTTransfer, scan_id: int) -> None:
        self._ensure_editable(transfer)
        deleted, _ = transfer.box_scans.filter(id=scan_id).delete()
        if not deleted:
            raise BSTError("Scan not found on this transfer.")

    # ==================================================================
    # Dispatch / cancel
    # ==================================================================

    @transaction.atomic
    def dispatch(self, transfer: BSTTransfer) -> BSTTransfer:
        if transfer.status not in EDITABLE_STATUSES:
            raise BSTError("This BST has already been dispatched.")
        if not transfer.box_scans.exists():
            raise BSTError("Scan at least one box before dispatching.")
        transfer.status = (
            BSTTransferStatus.AWAITING_GATE_OUT
            if transfer.requires_gate
            else BSTTransferStatus.IN_TRANSIT
        )
        transfer.dispatched_by = self.user
        transfer.dispatched_at = timezone.now()
        transfer.save(update_fields=["status", "dispatched_by", "dispatched_at", "updated_at"])
        return transfer

    @transaction.atomic
    def cancel(self, transfer: BSTTransfer, reason: str = "") -> BSTTransfer:
        terminal = (
            BSTTransferStatus.RECEIVED,
            BSTTransferStatus.PARTIALLY_RECEIVED,
            BSTTransferStatus.CLOSED,
            BSTTransferStatus.CANCELLED,
        )
        if transfer.status in terminal:
            raise BSTError("This BST can no longer be cancelled.")
        if transfer.box_scans.filter(receive_status=BSTReceiveStatus.ACCEPTED).exists():
            raise BSTError("Some boxes have already been received; cannot cancel.")
        transfer.status = BSTTransferStatus.CANCELLED
        transfer.cancel_reason = reason or ""
        transfer.cancelled_by = self.user
        transfer.cancelled_at = timezone.now()
        transfer.save(update_fields=["status", "cancel_reason", "cancelled_by", "cancelled_at", "updated_at"])
        return transfer
