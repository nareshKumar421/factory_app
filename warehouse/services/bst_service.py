"""Service layer for the Branch Stock Transfer (BST) sender flow.

Create a BST from a SAP stock-transfer document, scan the boxes/pallets being
moved, and dispatch it. Ownership of boxes is NOT changed here — that happens on
the receiving side when the destination warehouse accepts the boxes.
"""

import logging
import math
from decimal import Decimal

from django.db import transaction
from django.db.models import Count, F, Q
from django.utils import timezone

from barcode.models import (
    BarcodeAuditLog,
    BarcodeAuditTransactionType,
    Box,
    BoxMovement,
    BoxMovementType,
    BoxStatus,
    Pallet,
)
from barcode.services.box_ownership import (
    ItemCodeMappingError,
    reassign_boxes_to_company,
    requires_item_code_remap,
    resolve_destination_item_code_map,
)
from barcode.services.scan_service import ScanService
from company.models import Company
from sap_client.client import SAPClient

from ..models_bst import (
    BSTBoxScan,
    BSTReceiveStatus,
    BSTSourceType,
    BSTTransfer,
    BSTTransferDoc,
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

# Statuses shown on the gate-out board: transfers still awaiting gate-out plus
# those already gated out. The board doubles as history so the gate can see
# previously dispatched vehicles, not just the pending queue.
GATE_OUTWARD_VIEW_STATUSES = (
    BSTTransferStatus.AWAITING_GATE_OUT,
    BSTTransferStatus.GATED_OUT,
    BSTTransferStatus.IN_TRANSIT,
    BSTTransferStatus.AWAITING_GATE_IN,
    BSTTransferStatus.GATED_IN,
    BSTTransferStatus.ARRIVED,
    BSTTransferStatus.RECEIVING,
    BSTTransferStatus.RECEIVED,
    BSTTransferStatus.PARTIALLY_RECEIVED,
    BSTTransferStatus.CLOSED,
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

    # ------------------------------------------------------------------
    # SAP source documents — stock transfers OR invoices (dispatch bills).
    #
    # An invoice-sourced BST is a cross-company sale (e.g. JIVO OIL → JIVO MART);
    # we reuse the dispatch flow's invoice reader (SalesDispatchDocumentService)
    # and normalize invoices into the same shape as stock transfers so the create
    # path and the picker treat them uniformly. `from_warehouse` is the invoice's
    # source warehouse; there is no `to_warehouse` (the destination is a company).
    # ------------------------------------------------------------------

    @staticmethod
    def _is_invoice(document_type) -> bool:
        return str(document_type or "").strip().upper() == BSTSourceType.INVOICE

    @staticmethod
    def _box_count(value) -> int:
        if value in (None, ""):
            return 0
        try:
            return int(math.ceil(float(value)))
        except (TypeError, ValueError):
            return 0

    def list_sap_documents(self, *, document_type=None, search=None,
                           from_date=None, to_date=None, limit=50) -> list[dict]:
        if self._is_invoice(document_type):
            return self._list_sap_invoices(
                search=search, from_date=from_date, to_date=to_date, limit=limit,
            )
        return self.list_sap_transfers(
            search=search, from_date=from_date, to_date=to_date, limit=limit,
        )

    def get_sap_document(self, doc_entry: int, *, document_type=None) -> dict:
        if self._is_invoice(document_type):
            return self._get_sap_invoice(doc_entry)
        return self.get_sap_transfer(doc_entry)

    def _list_sap_invoices(self, *, search=None, from_date=None, to_date=None, limit=50) -> list[dict]:
        from gate_core.services.sales_dispatch_documents import SalesDispatchDocumentService
        svc = SalesDispatchDocumentService(self.company)
        docs = svc.list_documents(
            BSTSourceType.INVOICE,
            {
                "search": search or "",
                "from_date": from_date,
                "to_date": to_date,
                "limit": limit or 50,
            },
        )
        return [self._normalize_invoice(doc, with_lines=False) for doc in docs]

    def _get_sap_invoice(self, doc_entry: int) -> dict:
        from gate_core.services.sales_dispatch_documents import SalesDispatchDocumentService
        svc = SalesDispatchDocumentService(self.company)
        doc = svc.get_document(BSTSourceType.INVOICE, doc_entry)
        if not doc:
            raise BSTError(f"SAP invoice {doc_entry} was not found.")
        return self._normalize_invoice(doc, with_lines=True)

    def _normalize_invoice(self, doc: dict, *, with_lines: bool) -> dict:
        """Reshape a SalesDispatchDocumentService invoice into the BST doc shape."""
        from gate_core.services.sales_dispatch_documents import SalesDispatchDocumentService
        lines = []
        if with_lines:
            for item in SalesDispatchDocumentService.iter_items(doc):
                lines.append({
                    "line_num": item["line_num"],
                    "item_code": item["item_code"] or "",
                    "item_name": item["item_name"] or "",
                    "quantity": float(item["quantity"] or 0),
                    "uom": item["uom"] or "",
                    "from_warehouse": item.get("warehouse_code", "") or "",
                    "to_warehouse": "",
                    "pcs_per_carton": 0,
                    "box_count": self._box_count(item.get("total_boxes")),
                })
        source_whs = sorted({ln["from_warehouse"] for ln in lines if ln["from_warehouse"]})
        return {
            "document_type": BSTSourceType.INVOICE,
            "doc_entry": int(doc["doc_entry"]),
            "doc_num": str(doc.get("doc_num") or ""),
            "doc_date": doc.get("doc_date"),
            "from_warehouse": source_whs[0] if len(source_whs) == 1 else "",
            "to_warehouse": "",
            "warehouses": doc.get("warehouses") or ", ".join(source_whs),
            "comments": "",
            "reference": str(doc.get("base_refs") or ""),
            "card_code": str(doc.get("card_code") or ""),
            "card_name": str(doc.get("card_name") or ""),
            "line_count": doc.get("line_count") or len(lines),
            "total_quantity": float(doc.get("total_quantity") or 0),
            "total_boxes": self._box_count(doc.get("total_boxes")),
            "lines": lines,
        }

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
                doc_count=Count("docs", distinct=True),
            )
            .order_by("-created_at")
        )

    def detail_queryset(self):
        return (
            BSTTransfer.objects
            .select_related("company", "vehicle", "driver",
                            "created_by", "scan_approved_by", "dispatched_by", "received_by")
            .prefetch_related("docs__items", "items__doc", "box_scans__scanned_by", "box_scans__received_by")
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
        # A BST entry can combine several SAP documents on one physical shipment.
        # STOCK_TRANSFER documents must share the same source + destination
        # warehouse; INVOICE documents must share the same source warehouse and
        # the same customer (they settle to one destination company).
        document_type = data.get("document_type") or BSTSourceType.STOCK_TRANSFER
        is_invoice = self._is_invoice(document_type)

        doc_entries = data.get("sap_doc_entries") or []
        # Dedupe while preserving the order the user added them.
        seen: set[int] = set()
        ordered_entries: list[int] = []
        for doc_entry in doc_entries:
            if doc_entry not in seen:
                seen.add(doc_entry)
                ordered_entries.append(doc_entry)
        if not ordered_entries:
            raise BSTError("Select at least one SAP document.")

        saps = [
            self.get_sap_document(doc_entry, document_type=document_type)
            for doc_entry in ordered_entries
        ]

        destination = None
        if is_invoice:
            destination = data.get("destination_company")
            if destination is None:
                raise BSTError("Select the destination company for an invoice transfer.")
            if destination.id == self.company.id:
                raise BSTError("The destination company must differ from the source company.")
            if len({(sap.get("from_warehouse") or "") for sap in saps}) > 1:
                raise BSTError("All selected invoices must ship from the same source warehouse.")
            if len({(sap.get("card_code") or "") for sap in saps}) > 1:
                raise BSTError("All selected invoices must be for the same customer.")
            # Fail early (at create) if the cross-company item-code mapping is
            # missing/ambiguous, rather than surprising the receiver at accept time.
            item_codes = [
                line.get("item_code") for sap in saps for line in sap.get("lines", [])
            ]
            try:
                resolve_destination_item_code_map(self.company, destination, item_codes)
            except ItemCodeMappingError as exc:
                raise BSTError(str(exc)) from exc
        else:
            from_warehouses = {(sap.get("from_warehouse") or "") for sap in saps}
            to_warehouses = {(sap.get("to_warehouse") or "") for sap in saps}
            if len(from_warehouses) > 1 or len(to_warehouses) > 1:
                raise BSTError(
                    "All selected documents must share the same source and destination warehouse."
                )

        # The head mirrors the first (primary) document for quick display; the
        # shared route lives on the head, and every document is a `docs` row.
        primary = saps[0]
        transfer = BSTTransfer.objects.create(
            company=self.company,
            entry_no=BSTTransfer.generate_entry_no(),
            source_type=BSTSourceType.INVOICE if is_invoice else BSTSourceType.STOCK_TRANSFER,
            destination_company=destination,
            customer_code=str(primary.get("card_code") or "") if is_invoice else "",
            customer_name=str(primary.get("card_name") or "") if is_invoice else "",
            sap_doc_entry=primary["doc_entry"],
            sap_doc_num=str(primary.get("doc_num") or ""),
            sap_doc_date=primary.get("doc_date"),
            sap_from_warehouse=primary.get("from_warehouse") or "",
            sap_to_warehouse=primary.get("to_warehouse") or "",
            sap_reference=primary.get("reference") or "",
            invoice_no=data.get("invoice_no")
            or (str(primary.get("doc_num") or "") if is_invoice else ""),
            vehicle=data.get("vehicle"),
            driver=data.get("driver"),
            requires_gate=data.get("requires_gate", False),
            remarks=data.get("remarks", ""),
            status=BSTTransferStatus.SCANNING,
            created_by=self.user,
        )

        items = []
        for sap in saps:
            doc = BSTTransferDoc.objects.create(
                transfer=transfer,
                sap_doc_entry=sap["doc_entry"],
                sap_doc_num=str(sap.get("doc_num") or ""),
                sap_doc_date=sap.get("doc_date"),
                sap_reference=sap.get("reference") or "",
                invoice_no=str(sap.get("doc_num") or "") if is_invoice else "",
            )
            for line in sap.get("lines", []):
                items.append(
                    BSTTransferItem(
                        transfer=transfer,
                        doc=doc,
                        line_num=line["line_num"],
                        item_code=line.get("item_code", ""),
                        item_name=line.get("item_name", ""),
                        quantity=Decimal(str(line.get("quantity") or 0)),
                        uom=line.get("uom", ""),
                        from_warehouse=line.get("from_warehouse", ""),
                        to_warehouse=line.get("to_warehouse", ""),
                        # Box count from the SAP bill (line qty ÷ pieces-per-carton).
                        expected_boxes=int(line.get("box_count") or 0),
                    )
                )
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

    def _receivable_scope(self) -> Q:
        """Transfers this company may receive: its own intra-company
        STOCK_TRANSFERs, plus INVOICE transfers addressed to it as the
        destination company (the receiving side of a cross-company sale)."""
        return (
            Q(company=self.company, source_type=BSTSourceType.STOCK_TRANSFER)
            | Q(destination_company=self.company, source_type=BSTSourceType.INVOICE)
        )

    def _lock(self, transfer: BSTTransfer, *, as_receiver: bool = False) -> BSTTransfer:
        """Re-fetch the transfer with a row lock so a check-then-act mutation
        (scan / approve / receive / gate-out / cancel) can't race a concurrent
        one. Must be called inside a transaction. Sender actions re-scope to the
        owning company; receiver actions re-scope to the receivable set — which
        for an INVOICE transfer is the destination company, not the owner."""
        qs = BSTTransfer.objects.select_for_update()
        if as_receiver:
            return qs.get(self._receivable_scope(), pk=transfer.pk)
        return qs.get(pk=transfer.pk, company=self.company)

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
        # Scanning is restricted to the SAP bill: the box's item must be on it.
        if box.item_code not in allowed_items:
            raise BSTError(
                f"{box.box_barcode} (item {box.item_code}) is not on this transfer's bill."
            )
        # ...and the box must be physically at the transfer's source warehouse.
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
        transfer = self._lock(transfer)
        self._ensure_editable(transfer)
        barcode_raw = str(barcode_raw or "").strip()
        if not barcode_raw:
            raise BSTError("Barcode is required.")

        kind, payload = self._resolve_scan(barcode_raw)
        boxes = payload if kind == "PALLET" else [payload]

        # Bill limits — scanning is restricted to the combined bill of every
        # document in the entry: only their items, and no more boxes than the
        # total box count for each item summed across all documents.
        limits: dict[str, int] = {}
        for item in transfer.items.all():
            limits[item.item_code] = limits.get(item.item_code, 0) + (item.expected_boxes or 0)
        allowed_items = set(limits.keys())
        scanned_boxes: dict[str, int] = {}
        for code in transfer.box_scans.values_list("item_code", flat=True):
            scanned_boxes[code] = scanned_boxes.get(code, 0) + 1

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
            # Over-count guard: keep the item's scanned boxes within the bill's box count.
            limit = limits.get(box.item_code, 0)  # membership guaranteed by _validate_box
            already = scanned_boxes.get(box.item_code, 0)
            if limit and already + 1 > limit:
                raise BSTError(
                    f"{box.box_barcode}: all {limit} box(es) for {box.item_code} "
                    f"are already scanned."
                )
            scanned_boxes[box.item_code] = already + 1
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
        transfer = self._lock(transfer)
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
        transfer = self._lock(transfer)
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
        transfer = self._lock(transfer)
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
        """Transfers dispatched and awaiting receipt by this company — its own
        intra-company transfers plus cross-company invoices addressed to it."""
        return (
            BSTTransfer.objects
            .filter(self._receivable_scope(), status__in=RECEIVABLE_STATUSES)
            .select_related("company", "destination_company", "vehicle", "driver")
            .annotate(
                scanned_box_count=Count("box_scans", distinct=True),
                item_count=Count("items", distinct=True),
                doc_count=Count("docs", distinct=True),
            )
            .order_by("-dispatched_at", "-created_at")
        )

    def get_incoming_transfer(self, transfer_id: int) -> BSTTransfer:
        try:
            return self.detail_queryset().get(self._receivable_scope(), id=transfer_id)
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
        never dispatched on this transfer is rejected — receiving is restricted to
        the dispatched set.
        """
        transfer = self._lock(transfer, as_receiver=True)
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

        is_pallet = False
        if entity_type == "PALLET" and entity_id:
            is_pallet = True
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
        existing = {
            s.box_barcode: s
            for s in transfer.box_scans.filter(box_barcode__in=barcodes)
        }
        # Receiving is restricted to the boxes the sender dispatched on this
        # transfer. A pallet is just a container — receive the boxes on it that
        # belong to this transfer and ignore the rest; but an explicitly scanned
        # box (or raw barcode) that wasn't dispatched here is an error.
        matched = [c for c in barcodes if c in existing]
        if is_pallet:
            if not matched:
                raise BSTError("None of this pallet's boxes were dispatched on this transfer.")
        else:
            missing = [c for c in barcodes if c not in existing]
            if missing:
                raise BSTError(
                    f"{', '.join(missing)} was not dispatched on this transfer."
                )
        updated = []
        for code in matched:
            scan = existing[code]
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
            "unexpected": [],
        }

    def _apply_accepted_moves(self, transfer: BSTTransfer) -> None:
        """Settle the accepted boxes. How they settle depends on `source_type`:
        a STOCK_TRANSFER only changes the warehouse (same company); an INVOICE
        hands ownership to the destination company."""
        accepted = list(
            transfer.box_scans
            .filter(receive_status=BSTReceiveStatus.ACCEPTED, box__isnull=False)
            .select_related("box")
        )
        boxes = [s.box for s in accepted]
        if not boxes:
            return

        if transfer.source_type == BSTSourceType.INVOICE:
            self._apply_accepted_invoice_moves(transfer, boxes)
        else:
            self._apply_accepted_transfer_moves(transfer, boxes)

    def _apply_accepted_transfer_moves(self, transfer: BSTTransfer, boxes: list) -> None:
        """Intra-company: the box's company never changes, only `current_warehouse`."""
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

    def _apply_accepted_invoice_moves(self, transfer: BSTTransfer, boxes: list) -> None:
        """Cross-company: hand ownership of the accepted boxes to the destination
        company, reusing the shared box_ownership handoff (with the JIVO MART
        item-code remap where catalogues differ). The warehouse is left unchanged,
        matching the standalone intercompany-transfer flow; the company handoff is
        recorded on the barcode audit log."""
        destination = transfer.destination_company
        if destination is None:
            raise BSTError("This invoice transfer has no destination company.")
        source = transfer.company

        # Lock the boxes for the ownership update and re-resolve the item-code map
        # at settle time (authoritative — stock/catalogue may have changed since
        # create).
        boxes = list(
            Box.objects.select_for_update().select_related("company")
            .filter(id__in=[b.id for b in boxes])
        )
        try:
            item_code_map = resolve_destination_item_code_map(
                source, destination, [b.item_code for b in boxes],
            )
        except ItemCodeMappingError as exc:
            raise BSTError(str(exc)) from exc

        reassign_boxes_to_company(boxes, destination, item_code_map=item_code_map or None)

        BarcodeAuditLog.objects.bulk_create([
            BarcodeAuditLog(
                box=box,
                barcode=box.box_barcode,
                transaction_type=BarcodeAuditTransactionType.TRANSFER_COMPLETED,
                from_company=source,
                to_company=destination,
                user=self.user,
                notes=f"BST {transfer.entry_no} (invoice {transfer.sap_doc_num})",
            )
            for box in boxes
        ])

    @transaction.atomic
    def receive_complete(self, transfer: BSTTransfer) -> BSTTransfer:
        transfer = self._lock(transfer, as_receiver=True)
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

    def gate_outwards_view_queryset(self, from_date=None, to_date=None):
        """Gate-out board: the live queue (awaiting gate-out, always shown) plus
        transfers already gated out. This is a gate view, so the history is
        filtered by `gated_out_at` — when the gate person marked the vehicle
        out — not by warehouse approval. Pending entries sort on top, then the
        most recent gate-outs."""
        done = Q(gated_out_at__isnull=False)
        if from_date:
            done &= Q(gated_out_at__date__gte=from_date)
        if to_date:
            done &= Q(gated_out_at__date__lte=to_date)
        return (
            BSTTransfer.objects
            .filter(company=self.company, requires_gate=True,
                    status__in=GATE_OUTWARD_VIEW_STATUSES)
            .filter(Q(status=BSTTransferStatus.AWAITING_GATE_OUT) | done)
            .select_related("company", "vehicle", "driver")
            .annotate(scanned_box_count=Count("box_scans", distinct=True),
                      item_count=Count("items", distinct=True))
            .order_by(F("gated_out_at").desc(nulls_first=True))
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
        transfer = self._lock(transfer)
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
        transfer = self._lock(transfer, as_receiver=True)
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
