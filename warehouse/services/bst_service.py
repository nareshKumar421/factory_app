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

from barcode.models import (
    Box,
    BoxMovement,
    BoxMovementType,
    BoxStatus,
    Pallet,
)
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

# Destination warehouse may receive while in these states. A gated transfer is
# NOT receivable until the destination gate marks it in (→ ARRIVED); a non-gated
# transfer is receivable straight from IN_TRANSIT.
RECEIVABLE_STATUSES = (
    BSTTransferStatus.IN_TRANSIT,
    BSTTransferStatus.ARRIVED,
    BSTTransferStatus.RECEIVING,
)


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
            .select_related("company", "vehicle", "driver")
            .annotate(
                scanned_box_count=Count("box_scans", distinct=True),
                item_count=Count("items", distinct=True),
            )
            .order_by("-created_at")
        )

    def detail_queryset(self):
        return (
            BSTTransfer.objects
            .select_related("company", "vehicle", "driver",
                            "created_by", "scan_approved_by", "dispatched_by", "received_by")
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
        # BST is intra-company: the source + destination warehouses both come
        # from the SAP stock-transfer document.
        sap = self.get_sap_transfer(data["sap_doc_entry"])

        transfer = BSTTransfer.objects.create(
            company=self.company,
            entry_no=BSTTransfer.generate_entry_no(),
            sap_doc_entry=sap["doc_entry"],
            sap_doc_num=str(sap.get("doc_num") or ""),
            sap_doc_date=sap.get("doc_date"),
            sap_from_warehouse=sap.get("from_warehouse") or "",
            sap_to_warehouse=sap.get("to_warehouse") or "",
            sap_reference=sap.get("reference") or "",
            invoice_no=data.get("invoice_no", ""),
            vehicle=data.get("vehicle"),
            driver=data.get("driver"),
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

    def _validate_box(self, box: Box, transfer: BSTTransfer, allowed_items: set) -> None:
        if box.company_id != self.company.id:
            raise BSTError(f"{box.box_barcode} does not belong to {self.company.code}.")
        if box.status not in (BoxStatus.ACTIVE, BoxStatus.PARTIAL):
            raise BSTError(f"{box.box_barcode} is not active.")
        if box.dispatched_at or box.status == BoxStatus.DISPATCHED:
            raise BSTError(f"{box.box_barcode} is already dispatched.")
        # The box must hold one of the items on this BST document...
        if box.item_code not in allowed_items:
            raise BSTError(
                f"{box.box_barcode} (item {box.item_code}) is not part of this transfer."
            )
        # ...and be physically at the transfer's source warehouse.
        from_wh = transfer.sap_from_warehouse or ""
        if from_wh and box.current_warehouse != from_wh:
            raise BSTError(
                f"{box.box_barcode} is at {box.current_warehouse or '—'}, "
                f"not the source warehouse {from_wh}."
            )

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

        allowed_items = set(transfer.items.values_list("item_code", flat=True))
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
            self._validate_box(box, transfer, allowed_items)
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
    # Approve / cancel
    # ==================================================================

    @transaction.atomic
    def approve(self, transfer: BSTTransfer) -> BSTTransfer:
        """Warehouse review: the scanning is confirmed correct. If a vehicle is
        involved the transfer waits for the gate to mark it out; otherwise it
        goes straight in transit (receivable)."""
        if transfer.status not in EDITABLE_STATUSES:
            raise BSTError("This BST has already been approved.")
        if not transfer.box_scans.exists():
            raise BSTError("Scan at least one box before approving.")

        now = timezone.now()
        transfer.scan_approved_by = self.user
        transfer.scan_approved_at = now
        fields = ["status", "scan_approved_by", "scan_approved_at", "updated_at"]

        if transfer.requires_gate:
            # Hand off to the gate, which dispatches it when the vehicle leaves.
            transfer.status = BSTTransferStatus.AWAITING_GATE_OUT
        else:
            transfer.status = BSTTransferStatus.IN_TRANSIT
            transfer.dispatched_by = self.user
            transfer.dispatched_at = now
            fields += ["dispatched_by", "dispatched_at"]

        transfer.save(update_fields=fields)
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

    # ==================================================================
    # Receiver side (current company == destination)
    # ==================================================================

    def incoming_queryset(self):
        """This company's transfers that are dispatched and awaiting receipt at
        the destination warehouse."""
        return (
            BSTTransfer.objects
            .filter(company=self.company, status__in=RECEIVABLE_STATUSES)
            .select_related("company", "vehicle", "driver")
            .annotate(
                scanned_box_count=Count("box_scans", distinct=True),
                item_count=Count("items", distinct=True),
            )
            .order_by("-dispatched_at", "-created_at")
        )

    def get_incoming_transfer(self, transfer_id: int) -> BSTTransfer:
        try:
            return self.detail_queryset().get(id=transfer_id, company=self.company)
        except BSTTransfer.DoesNotExist as exc:
            raise BSTError("Incoming BST transfer not found.") from exc

    def _ensure_receivable(self, transfer: BSTTransfer) -> None:
        if transfer.status not in RECEIVABLE_STATUSES:
            raise BSTError("This BST is not open for receiving.")

    @transaction.atomic
    def receive_scan(
        self,
        transfer: BSTTransfer,
        barcode_raw: str,
        *,
        decision: str = BSTReceiveStatus.ACCEPTED,
        reject_reason: str = "",
    ) -> dict:
        """Resolve a receive scan (box or pallet) against the dispatched boxes.

        Each matched box is stamped ACCEPTED or REJECTED. A scanned box the sender
        never dispatched is recorded as an unexpected accepted/rejected row.
        """
        self._ensure_receivable(transfer)
        if decision not in (BSTReceiveStatus.ACCEPTED, BSTReceiveStatus.REJECTED):
            raise BSTError("Decision must be ACCEPTED or REJECTED.")
        barcode_raw = str(barcode_raw or "").strip()
        if not barcode_raw:
            raise BSTError("Barcode is required.")

        # Resolve within the company (BST is intra-company).
        lookup = ScanService(transfer.company.code).lookup_barcode(barcode_raw)
        entity_type = lookup.get("entity_type")
        entity_id = lookup.get("entity_id")

        if entity_type == "PALLET" and entity_id:
            pallet = Pallet.objects.filter(id=entity_id).first()
            barcodes = list(
                (pallet.boxes.values_list("box_barcode", flat=True)) if pallet else []
            )
            if not barcodes:
                raise BSTError("Pallet has no boxes.")
        elif entity_type == "BOX" and entity_id:
            box = Box.objects.filter(id=entity_id).first()
            barcodes = [box.box_barcode] if box else []
        else:
            # Fall back to the raw value so a sender-scanned barcode still matches
            # even if the box already changed hands.
            barcodes = [barcode_raw]

        now = timezone.now()
        updated, unexpected = [], []
        existing = {
            s.box_barcode: s
            for s in transfer.box_scans.filter(box_barcode__in=barcodes)
        }
        for code in barcodes:
            scan = existing.get(code)
            if scan is None:
                scan = BSTBoxScan(
                    transfer=transfer,
                    box_barcode=code,
                    is_unexpected=True,
                )
                unexpected.append(code)
            scan.receive_status = decision
            scan.reject_reason = reject_reason if decision == BSTReceiveStatus.REJECTED else ""
            scan.received_by = self.user
            scan.received_at = now
            scan.save()
            updated.append(scan)

        if transfer.status != BSTTransferStatus.RECEIVING:
            transfer.status = BSTTransferStatus.RECEIVING
            transfer.save(update_fields=["status", "updated_at"])

        return {
            "decision": decision,
            "updated_count": len(updated),
            "unexpected": unexpected,
        }

    def _apply_accepted_moves(self, transfer: BSTTransfer) -> None:
        """Move accepted boxes to the destination warehouse (intra-company —
        the box's company never changes, only its `current_warehouse`)."""
        accepted = list(
            transfer.box_scans
            .filter(receive_status=BSTReceiveStatus.ACCEPTED, box__isnull=False)
            .select_related("box")
        )
        boxes = [s.box for s in accepted]
        if not boxes:
            return

        to_warehouse = transfer.sap_to_warehouse or ""
        movements = [
            BoxMovement(
                company=transfer.company,
                box=box,
                movement_type=BoxMovementType.TRANSFER,
                from_warehouse=box.current_warehouse,
                to_warehouse=to_warehouse,
                performed_by=self.user,
            )
            for box in boxes
        ]
        if to_warehouse:
            Box.objects.filter(id__in=[b.id for b in boxes]).update(current_warehouse=to_warehouse)
        BoxMovement.objects.bulk_create(movements)

    @transaction.atomic
    def receive_complete(self, transfer: BSTTransfer) -> BSTTransfer:
        self._ensure_receivable(transfer)

        self._apply_accepted_moves(transfer)

        scans = list(transfer.box_scans.all())
        dispatched = [s for s in scans if not s.is_unexpected]
        accepted = sum(1 for s in scans if s.receive_status == BSTReceiveStatus.ACCEPTED)
        pending = sum(1 for s in dispatched if s.receive_status == BSTReceiveStatus.PENDING)
        rejected = sum(1 for s in dispatched if s.receive_status == BSTReceiveStatus.REJECTED)

        fully = accepted == len(dispatched) and rejected == 0 and pending == 0 and accepted > 0
        transfer.status = (
            BSTTransferStatus.RECEIVED if fully else BSTTransferStatus.PARTIALLY_RECEIVED
        )
        transfer.received_by = self.user
        transfer.received_at = timezone.now()
        transfer.save(update_fields=["status", "received_by", "received_at", "updated_at"])
        return transfer

    # ==================================================================
    # Gate side (only for transfers that require a gate movement)
    # ==================================================================

    def gate_outwards_queryset(self):
        """Dispatched, gated transfers leaving this company, awaiting gate-out."""
        return (
            BSTTransfer.objects
            .filter(company=self.company, status=BSTTransferStatus.AWAITING_GATE_OUT)
            .select_related("company", "vehicle", "driver")
            .annotate(scanned_box_count=Count("box_scans", distinct=True),
                      item_count=Count("items", distinct=True))
            .order_by("dispatched_at")
        )

    def gate_inwards_queryset(self):
        """Gated transfers for this company awaiting gate-in at the destination."""
        return (
            BSTTransfer.objects
            .filter(company=self.company, status=BSTTransferStatus.AWAITING_GATE_IN)
            .select_related("company", "vehicle", "driver")
            .annotate(scanned_box_count=Count("box_scans", distinct=True),
                      item_count=Count("items", distinct=True))
            .order_by("gated_out_at")
        )

    @transaction.atomic
    def mark_gate_out(self, transfer: BSTTransfer) -> BSTTransfer:
        if transfer.status != BSTTransferStatus.AWAITING_GATE_OUT:
            raise BSTError("This BST is not awaiting gate-out.")
        now = timezone.now()
        # The vehicle physically leaves now → it goes in transit (a destination
        # gate-in step can be added later; for now gate-out makes it receivable).
        transfer.status = BSTTransferStatus.IN_TRANSIT
        transfer.gated_out_by = self.user
        transfer.gated_out_at = now
        transfer.dispatched_by = self.user
        transfer.dispatched_at = now
        transfer.save(update_fields=[
            "status", "gated_out_by", "gated_out_at",
            "dispatched_by", "dispatched_at", "updated_at",
        ])
        return transfer

    @transaction.atomic
    def mark_gate_in(self, transfer: BSTTransfer) -> BSTTransfer:
        if transfer.status != BSTTransferStatus.AWAITING_GATE_IN:
            raise BSTError("This BST is not awaiting gate-in.")
        transfer.status = BSTTransferStatus.ARRIVED
        transfer.gated_in_by = self.user
        transfer.gated_in_at = timezone.now()
        transfer.save(update_fields=["status", "gated_in_by", "gated_in_at", "updated_at"])
        return transfer

    def get_outward_transfer(self, transfer_id: int) -> BSTTransfer:
        try:
            return self.detail_queryset().get(id=transfer_id, company=self.company)
        except BSTTransfer.DoesNotExist as exc:
            raise BSTError("BST transfer not found.") from exc
