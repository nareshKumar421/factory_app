"""Service layer for the Branch Stock Transfer (BST) sender flow.

Create a BST from a SAP stock-transfer document, scan the boxes/pallets being
moved, and dispatch it. Ownership of boxes is NOT changed here — that happens on
the receiving side when the destination warehouse accepts the boxes.
"""

import logging
import math
import re
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
    reassign_pallets_to_company,
    requires_item_code_remap,
    resolve_destination_item_code_map,
)
from barcode.services.scan_service import ScanService
from company.models import Company
from sap_client.client import SAPClient

from ..models_bst import (
    BSTBoxScan,
    BSTPartialTransferApproval,
    BSTPartialTransferStatus,
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


def _norm_code(value) -> str:
    return str(value or "").strip().upper()


# Packaging-material item-code prefix. PM lines don't require box scanning on a
# BST (they're not barcode-tracked), so they never gate sealing: a PM line is
# never counted short, and a PM-only bill needs no scanning at all. Mirrors the
# visible-prefix rule used for weighment (`gate_core.services.weighment_rules`).
PM_ITEM_CODE_PREFIX = "PM"


def is_pm_item_code(item_code) -> bool:
    """True when an item code identifies packaging material (``PM`` prefix)."""
    return bool(item_code) and str(item_code).strip().upper().startswith(PM_ITEM_CODE_PREFIX)


def bst_requires_scanning(transfer: "BSTTransfer") -> bool:
    """True when the bill has at least one non-PM line, i.e. scanning is required.

    A PM-only bill needs no scanning to seal/dispatch; any other line requires it.
    Reads the (prefetched) ``items`` relation so it adds no query in the gate path.
    """
    return any(not is_pm_item_code(item.item_code) for item in transfer.items.all())


def compute_scan_status(transfer: "BSTTransfer") -> dict:
    """Scanned-vs-expected QUANTITY status for a BST — the sender's completeness gate.

    Mirrors the sales-dispatch rule (``gate_core.services.sales_dispatch_gatepass``:
    ``load_scan_status`` / ``has_unscanned_bill_lines``). Expected quantity per item
    comes from the BST's SAP lines (``items``); scanned quantity per item from the box
    scans (``BSTBoxScan.quantity``, copied from each box's own piece count at scan
    time). Completeness is judged on QUANTITY — ground truth on both sides — so a box
    whose qty is wrong (e.g. a 4-pack labelled as 1 PCS) is caught as a shortfall even
    though the box COUNT looks right. Falls back to the box-COUNT estimate only when no
    scan carries a quantity (legacy / quantity-less scans), so we never invent a
    shortfall from absent quantity data.

    Aggregated by item code across every document in the entry — the same grain the
    scan cap uses (``BSTService.scan``). Reads only the (prefetched) ``items`` /
    ``box_scans`` relations, so it adds no queries in the detail serializer.

    Returns Decimals for exact comparison; ``scan_status_payload`` JSON-ifies it.
    """
    expected_qty: dict = {}
    expected_boxes: dict = {}
    names: dict = {}
    uoms: dict = {}
    for item in transfer.items.all():
        code = _norm_code(item.item_code)
        expected_qty[code] = expected_qty.get(code, Decimal("0")) + Decimal(str(item.quantity or 0))
        expected_boxes[code] = expected_boxes.get(code, 0) + int(item.expected_boxes or 0)
        names.setdefault(code, item.item_name)
        uoms.setdefault(code, item.uom)

    scanned_qty: dict = {}
    scanned_boxes: dict = {}
    uses_quantity = False
    for scan in transfer.box_scans.all():
        code = _norm_code(scan.item_code)
        qty = Decimal(str(scan.quantity or 0))
        scanned_qty[code] = scanned_qty.get(code, Decimal("0")) + qty
        scanned_boxes[code] = scanned_boxes.get(code, 0) + 1
        if qty > 0:
            uses_quantity = True
        names.setdefault(code, scan.item_name)
        uoms.setdefault(code, scan.uom)

    total_scanned_boxes = sum(scanned_boxes.values())
    has_scans = total_scanned_boxes > 0

    items_out: list[dict] = []
    short_items: list[dict] = []
    for code in expected_qty.keys() | scanned_qty.keys():
        eq = expected_qty.get(code, Decimal("0"))
        sq = scanned_qty.get(code, Decimal("0"))
        eb = expected_boxes.get(code, 0)
        sb = scanned_boxes.get(code, 0)
        requires_scan = not is_pm_item_code(code)
        if uses_quantity and eq > 0:
            is_complete = sq >= eq
            is_over = sq > eq
            is_short = sq < eq
        else:
            is_complete = eb > 0 and sb >= eb
            is_over = eb > 0 and sb > eb
            is_short = eb > 0 and sb < eb
        if not requires_scan:
            # Packaging material isn't scanned: never short, always counted
            # complete, so it can't dirty the completeness gate or block sealing.
            is_short = False
            is_complete = True
        row = {
            "item_code": code,
            "item_name": names.get(code, ""),
            "uom": uoms.get(code, ""),
            "expected_qty": eq,
            "scanned_qty": sq,
            "expected_boxes": eb,
            "scanned_boxes": sb,
            "is_complete": is_complete,
            "is_over": is_over,
            "requires_scan": requires_scan,
        }
        items_out.append(row)
        # Only bill lines (expected > 0) with a genuine deficit count as short; a
        # surplus on one line never offsets a shortfall on another.
        if is_short:
            short_items.append(row)

    items_out.sort(key=lambda r: r["item_code"])
    short_items.sort(key=lambda r: r["item_code"])
    # A PM-only bill needs no scanning at all; any non-PM line requires it. Read
    # off the expected (bill) lines only — a stray PM scan doesn't change the rule.
    requires_scanning = any(not is_pm_item_code(code) for code in expected_qty.keys())
    return {
        "is_partial": has_scans and bool(short_items),
        "has_scans": has_scans,
        "requires_scanning": requires_scanning,
        "uses_quantity": uses_quantity,
        "scanned_qty": sum(scanned_qty.values(), Decimal("0")),
        "expected_qty": sum(expected_qty.values(), Decimal("0")),
        "scanned_boxes": total_scanned_boxes,
        "expected_boxes": sum(expected_boxes.values()),
        "short_items": short_items,
        "items": items_out,
    }


def _fmt_qty(value: Decimal) -> str:
    """Trim trailing zeros for display (3441.000 -> 3441)."""
    text = format(Decimal(value).normalize(), "f")
    return text


def scan_status_payload(transfer: "BSTTransfer") -> dict:
    """JSON-safe form of :func:`compute_scan_status` for the API."""
    status = compute_scan_status(transfer)

    def _row(row: dict) -> dict:
        return {
            "item_code": row["item_code"],
            "item_name": row["item_name"],
            "uom": row["uom"],
            "expected_qty": _fmt_qty(row["expected_qty"]),
            "scanned_qty": _fmt_qty(row["scanned_qty"]),
            "expected_boxes": row["expected_boxes"],
            "scanned_boxes": row["scanned_boxes"],
            "is_complete": row["is_complete"],
            "is_over": row["is_over"],
            "requires_scan": row["requires_scan"],
        }

    return {
        "is_partial": status["is_partial"],
        "has_scans": status["has_scans"],
        "requires_scanning": status["requires_scanning"],
        "uses_quantity": status["uses_quantity"],
        "scanned_qty": _fmt_qty(status["scanned_qty"]),
        "expected_qty": _fmt_qty(status["expected_qty"]),
        "scanned_boxes": status["scanned_boxes"],
        "expected_boxes": status["expected_boxes"],
        "short_items": [_row(r) for r in status["short_items"]],
        "items": [_row(r) for r in status["items"]],
    }


def latest_partial_transfer_request(transfer: "BSTTransfer"):
    """The most recent partial-transfer approval request for a transfer, or None.

    Reads the (prefetched) reverse relation so it adds no query in the serializer."""
    requests = list(transfer.partial_transfer_requests.all())
    if not requests:
        return None
    # The relation's default ordering is newest-first; be explicit in case the
    # prefetch was re-ordered.
    return max(requests, key=lambda r: (r.requested_at, r.id))


def partial_transfer_approved(transfer: "BSTTransfer") -> bool:
    """True when an admin has approved sealing this transfer with a short scan."""
    return any(
        r.status == BSTPartialTransferStatus.APPROVED
        for r in transfer.partial_transfer_requests.all()
    )


def partial_transfer_state(transfer: "BSTTransfer") -> dict | None:
    """Serializer-friendly snapshot of the transfer's latest partial-transfer request."""
    request = latest_partial_transfer_request(transfer)
    if request is None:
        return None
    return {
        "id": request.id,
        "status": request.status,
        "is_pending": request.status == BSTPartialTransferStatus.PENDING,
        "is_approved": request.status == BSTPartialTransferStatus.APPROVED,
        "reason": request.reason,
        "review_notes": request.review_notes,
        "scanned_qty": _fmt_qty(request.scanned_qty),
        "expected_qty": _fmt_qty(request.expected_qty),
        "requested_by_name": (
            getattr(request.requested_by, "full_name", "")
            or getattr(request.requested_by, "email", "")
            if request.requested_by else ""
        ),
        "requested_at": request.requested_at,
        "reviewed_by_name": (
            getattr(request.reviewed_by, "full_name", "")
            or getattr(request.reviewed_by, "email", "")
            if request.reviewed_by else ""
        ),
        "reviewed_at": request.reviewed_at,
    }


def scan_shortfall_message(status: dict) -> str:
    short = ", ".join(
        f"{r['item_code']} {_fmt_qty(r['scanned_qty'])}/{_fmt_qty(r['expected_qty'])} {r['uom']}".strip()
        for r in status["short_items"]
    )
    return (
        f"Scanned quantity is short of the bill: {short}. Scan the missing boxes, "
        f"or get a partial-transfer approval before sealing."
    )


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
#
# Exception — *live* internal transfers (see `BSTService._is_live`): an
# intra-company STOCK_TRANSFER with no gate step goes receivable the moment the
# sender scans its first box (→ IN_TRANSIT), so the destination can scan
# concurrently. The sender keeps scanning through IN_TRANSIT / RECEIVING until it
# is *sealed* by `approve()` — see `BSTService._live_editable`.
EDITABLE_STATUSES = (BSTTransferStatus.DRAFT, BSTTransferStatus.SCANNING)

# Statuses in which a live internal transfer stays sender-editable (until sealed).
LIVE_EDITABLE_STATUSES = (
    BSTTransferStatus.SCANNING,
    BSTTransferStatus.IN_TRANSIT,
    BSTTransferStatus.RECEIVING,
)

# Destination warehouse may receive while in these states. A gated transfer is
# NOT receivable until the destination gate marks it in (→ ARRIVED); a non-gated
# transfer is receivable straight from IN_TRANSIT.
#
# PARTIALLY_RECEIVED is receivable too: a partial receipt is resumable — the
# remaining boxes can still be received and the transfer re-finalized (→ RECEIVED
# once everything lands). Without this a premature/accidental "Finalize receipt"
# would strand the shipment with no in-app way to continue receiving it.
RECEIVABLE_STATUSES = (
    BSTTransferStatus.IN_TRANSIT,
    BSTTransferStatus.ARRIVED,
    BSTTransferStatus.RECEIVING,
    BSTTransferStatus.PARTIALLY_RECEIVED,
)

# Statuses shown on the receiver's Incoming board: everything still receivable
# PLUS already-finalized receipts, so the board doubles as history and a
# finalized transfer doesn't just vanish. The receive PAGE still gates actions on
# RECEIVABLE_STATUSES, so a RECEIVED/CLOSED row is view-only.
INCOMING_VIEW_STATUSES = RECEIVABLE_STATUSES + (
    BSTTransferStatus.RECEIVED,
    BSTTransferStatus.CLOSED,
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

    # Pack size embedded in an item name, e.g. "OIL 1L 12 PCS" -> 12. Mirrors the
    # dispatch flow's _PACK_SIZE_PATTERN (gate_core.services.sales_dispatch_gatepass)
    # and the frontend, so a BST invoice's box count matches the dispatch scan page.
    _PACK_SIZE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:PCS?|SETS?|TINS?|BOTTLES?)\b", re.IGNORECASE)

    @classmethod
    def _boxes_from_pack_size(cls, quantity, item_name) -> int:
        """Fallback box count when SAP carries no box total for an invoice line:
        quantity / pack-size parsed from the item name. Returns 0 when neither is
        known (a genuine data gap), mirroring the dispatch flow's per-line rule."""
        try:
            qty = float(quantity or 0)
        except (TypeError, ValueError):
            return 0
        matches = cls._PACK_SIZE_RE.findall(item_name or "")
        if qty <= 0 or not matches:
            return 0
        try:
            pack = float(matches[-1])
        except (TypeError, ValueError):
            return 0
        if pack <= 0:
            return 0
        return int(math.ceil(qty / pack))

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
        # The BST page is search-driven: the user types the full SAP invoice number
        # and we look it up exactly. There is no date filter on that page, so an
        # invoice must be findable by its number regardless of age — hence the exact
        # DocNum lookup (no date window, no row cap) instead of a windowed list.
        # With no search term there is nothing to show yet.
        term = (search or "").strip()
        if not term:
            return []
        from gate_core.services.sales_dispatch_documents import SalesDispatchDocumentService
        svc = SalesDispatchDocumentService(self.company)
        docs = svc.list_documents(
            BSTSourceType.INVOICE,
            {"invoice_doc_num": term},
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
                # Prefer the SAP box total; fall back to quantity / pack-size (from
                # the item name) so the scan page isn't left with 0 boxes when the
                # U_UNE_TOTB custom field isn't maintained.
                box_count = self._box_count(item.get("total_boxes")) or self._boxes_from_pack_size(
                    item.get("quantity"), item.get("item_name"),
                )
                lines.append({
                    "line_num": item["line_num"],
                    "item_code": item["item_code"] or "",
                    "item_name": item["item_name"] or "",
                    "quantity": float(item["quantity"] or 0),
                    "uom": item["uom"] or "",
                    "from_warehouse": item.get("warehouse_code", "") or "",
                    "to_warehouse": "",
                    "pcs_per_carton": 0,
                    "box_count": box_count,
                })
        source_whs = sorted({ln["from_warehouse"] for ln in lines if ln["from_warehouse"]})
        # Head total mirrors the summed line box counts when we have lines (so it
        # reflects the same fallback), else the SAP header value.
        total_boxes = (
            sum(ln["box_count"] for ln in lines)
            if lines
            else self._box_count(doc.get("total_boxes"))
        )
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
            "total_boxes": total_boxes,
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
            .prefetch_related(
                "docs__items", "items__doc",
                "box_scans__scanned_by", "box_scans__received_by",
                "partial_transfer_requests__requested_by",
                "partial_transfer_requests__reviewed_by",
            )
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
        # Combined documents may come from DIFFERENT source warehouses (e.g.
        # virtual warehouses on one dock). STOCK_TRANSFER documents must still
        # share the same destination warehouse; INVOICE documents must still be
        # for the same customer (they settle to one destination company).
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

        # A SAP document backs at most one live BST: reject any doc already used
        # by another non-cancelled transfer of this company, so the same invoice /
        # stock-transfer can't be shipped on two BSTs.
        clash = (
            BSTTransferDoc.objects
            .filter(sap_doc_entry__in=ordered_entries, transfer__company=self.company)
            .exclude(transfer__status=BSTTransferStatus.CANCELLED)
            .select_related("transfer")
            .first()
        )
        if clash:
            raise BSTError(
                f"SAP document {clash.sap_doc_num or clash.sap_doc_entry} is already "
                f"used by {clash.transfer.entry_no}. Cancel that BST to reuse it."
            )

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
            # Invoices may now ship from DIFFERENT source warehouses (virtual
            # warehouses resolving to the same physical dock) — the source-route
            # lock is lifted. They must still be for one customer.
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
            # Combined documents may now come from DIFFERENT source warehouses
            # (e.g. virtual warehouses that resolve to the same physical dock) —
            # the source-route lock is lifted. They must still settle to one
            # destination warehouse, since the receive-side move sends every
            # accepted box to the head's `sap_to_warehouse`.
            to_warehouses = {(sap.get("to_warehouse") or "") for sap in saps}
            if len(to_warehouses) > 1:
                raise BSTError(
                    "All selected documents must share the same destination warehouse."
                )

        # The head mirrors the first (primary) document for quick display; every
        # document is a `docs` row. When the combined documents span several
        # source warehouses the head can't name a single one, so leave it blank
        # ("multiple") — the per-document rows still carry each source warehouse.
        primary = saps[0]
        head_from_warehouse = primary.get("from_warehouse") or ""
        if len({(sap.get("from_warehouse") or "") for sap in saps}) > 1:
            head_from_warehouse = ""
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
            sap_from_warehouse=head_from_warehouse,
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

    @staticmethod
    def _is_live(transfer: BSTTransfer) -> bool:
        """A *live* internal transfer: an intra-company STOCK_TRANSFER with no
        gate step. These go receivable the instant the sender scans the first box
        (→ IN_TRANSIT) so the destination can accept/reject concurrently while the
        sender is still scanning. INVOICE (cross-company) and gated transfers keep
        the classic sequential flow (scan → approve → [gate] → receive)."""
        return (
            transfer.source_type == BSTSourceType.STOCK_TRANSFER
            and not transfer.requires_gate
        )

    def _live_editable(self, transfer: BSTTransfer) -> bool:
        """True while the sender of a live transfer may still add/remove boxes:
        through IN_TRANSIT / RECEIVING, until `approve()` *seals* it
        (`scan_approved_at`). Sealing is what finally stops the sender."""
        return (
            self._is_live(transfer)
            and transfer.scan_approved_at is None
            and transfer.status in LIVE_EDITABLE_STATUSES
        )

    def _ensure_editable(self, transfer: BSTTransfer) -> None:
        if transfer.status in EDITABLE_STATUSES or self._live_editable(transfer):
            return
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

    def _validate_box(
        self, box: Box, transfer: BSTTransfer, allowed_items: set, source_whs: set
    ) -> None:
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
        # ...and the box must be physically at one of the transfer's source
        # warehouses. A BST can combine documents from several (virtual) source
        # warehouses, so we check membership of the whole set rather than a single
        # head warehouse. Falls back to the head when no line carries a warehouse.
        allowed_whs = source_whs or (
            {transfer.sap_from_warehouse} if transfer.sap_from_warehouse else set()
        )
        if allowed_whs and box.current_warehouse not in allowed_whs:
            raise BSTError(
                f"{box.box_barcode} is at {box.current_warehouse or '—'}, "
                f"not a source warehouse of this transfer "
                f"({', '.join(sorted(allowed_whs))})."
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
                raise BSTError(self.scanner.explain_scan_miss(barcode_raw)["message"])
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
                raise BSTError(self.scanner.explain_scan_miss(barcode_raw)["message"])
            return "BOX", box

        # Company-scoped lookup missed: distinguish a box owned by another
        # company / a wrong-state box / a genuinely unknown barcode instead of
        # a bare "does not exist" (which is wrong ~99% of the time in practice).
        raise BSTError(self.scanner.explain_scan_miss(barcode_raw)["message"])

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
        source_whs: set[str] = set()
        for item in transfer.items.all():
            limits[item.item_code] = limits.get(item.item_code, 0) + (item.expected_boxes or 0)
            if item.from_warehouse:
                source_whs.add(item.from_warehouse)
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
            self._validate_box(box, transfer, allowed_items, source_whs)
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

        # Live internal transfer: the first scanned box makes the shipment
        # receivable immediately (→ IN_TRANSIT), so the destination sees the boxes
        # and can start scanning while the sender is still going. The sender stays
        # editable (see `_live_editable`) until they seal it via `approve()`.
        if created and self._is_live(transfer) and transfer.status == BSTTransferStatus.SCANNING:
            now = timezone.now()
            transfer.status = BSTTransferStatus.IN_TRANSIT
            transfer.dispatched_by = self.user
            transfer.dispatched_at = now
            transfer.save(update_fields=["status", "dispatched_by", "dispatched_at", "updated_at"])

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
        scan = transfer.box_scans.filter(id=scan_id).first()
        if not scan:
            raise BSTError("Scan not found on this transfer.")
        # On a live transfer the receiver may already have acted on this box; the
        # sender must not yank a box the destination has accepted/rejected.
        if scan.receive_status != BSTReceiveStatus.PENDING:
            raise BSTError(
                "This box was already received at the destination and can't be removed."
            )
        scan.delete()
        # A live transfer went IN_TRANSIT on its first scan. If the sender clears
        # every (still-unreceived) box, drop it back to SCANNING so it leaves the
        # destination's incoming board until scanning resumes.
        if (
            self._is_live(transfer)
            and transfer.status == BSTTransferStatus.IN_TRANSIT
            and transfer.scan_approved_at is None
            and not transfer.box_scans.exists()
        ):
            transfer.status = BSTTransferStatus.SCANNING
            transfer.dispatched_by = None
            transfer.dispatched_at = None
            transfer.save(update_fields=["status", "dispatched_by", "dispatched_at", "updated_at"])

    # ==================================================================
    # Approve / cancel
    # ==================================================================

    @transaction.atomic
    def approve(self, transfer: BSTTransfer) -> BSTTransfer:
        """Warehouse review: the scanning is confirmed correct.

        For a **live** internal transfer this is optional — the shipment is
        already receivable from its first scan. Approving just *seals* the send
        (the sender stops scanning) and stamps who confirmed it; it never rewinds
        or blocks the receiver.

        For a **non-live** transfer (any INVOICE, or a gated STOCK_TRANSFER) this
        is the dispatch trigger: gated → wait for the gate to mark it out;
        otherwise → straight in transit (receivable)."""
        transfer = self._lock(transfer)
        # A PM-only bill needs no scanning (packaging material isn't barcode-tracked),
        # so it can be sealed with zero boxes. Any non-PM line still requires a scan.
        if bst_requires_scanning(transfer) and not transfer.box_scans.exists():
            raise BSTError("Scan at least one box before approving.")

        # Completeness gate — mirror the sales-dispatch lock: the scanned QUANTITY
        # per item must reach the bill, else sealing is blocked. Catches a box whose
        # qty is wrong (right box count, short pieces) that the box-count cap misses.
        # An admin partial-transfer approval (commit B) releases the shortfall.
        scan_status = compute_scan_status(transfer)
        if scan_status["is_partial"] and not partial_transfer_approved(transfer):
            raise BSTError(scan_shortfall_message(scan_status))

        now = timezone.now()

        if self._is_live(transfer):
            if transfer.scan_approved_at:
                raise BSTError("This BST has already been approved.")
            if transfer.status not in LIVE_EDITABLE_STATUSES:
                raise BSTError("This BST can no longer be approved.")
            transfer.scan_approved_by = self.user
            transfer.scan_approved_at = now
            fields = ["scan_approved_by", "scan_approved_at", "updated_at"]
            # Normally the first scan already flipped it in transit; cover the
            # (rare) case where it's still SCANNING at approval time.
            if transfer.status == BSTTransferStatus.SCANNING:
                transfer.status = BSTTransferStatus.IN_TRANSIT
                transfer.dispatched_by = self.user
                transfer.dispatched_at = now
                fields += ["status", "dispatched_by", "dispatched_at"]
            transfer.save(update_fields=fields)
            return transfer

        if transfer.status not in EDITABLE_STATUSES:
            raise BSTError("This BST has already been approved.")
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
    # Partial-transfer approval (seal a short scan with admin sign-off)
    # ==================================================================

    @transaction.atomic
    def request_partial_transfer(self, transfer: BSTTransfer, reason: str) -> BSTPartialTransferApproval:
        """Operator raises a request to seal this transfer despite a short scan."""
        transfer = self._lock(transfer)
        reason = (reason or "").strip()
        if not reason:
            raise BSTError("A reason is required to request partial-transfer approval.")
        if not transfer.box_scans.exists():
            raise BSTError("Scan at least one box before requesting approval.")

        status = compute_scan_status(transfer)
        if not status["is_partial"]:
            raise BSTError("This transfer is fully scanned — no approval is needed.")
        if partial_transfer_approved(transfer):
            raise BSTError("This transfer already has an approved partial-transfer request.")

        existing = transfer.partial_transfer_requests.filter(
            status=BSTPartialTransferStatus.PENDING
        ).first()
        if existing:
            return existing

        return BSTPartialTransferApproval.objects.create(
            company=transfer.company,
            transfer=transfer,
            scanned_qty=status["scanned_qty"],
            expected_qty=status["expected_qty"],
            reason=reason,
            requested_by=self.user,
        )

    def partial_transfer_queryset(self, *, status=None):
        """Admin review queue for this company's BST partial-transfer requests."""
        qs = (
            BSTPartialTransferApproval.objects
            .filter(company=self.company)
            .select_related("transfer", "requested_by", "reviewed_by")
            .order_by("-requested_at", "-id")
        )
        if status:
            qs = qs.filter(status=str(status).upper())
        return qs

    def get_partial_transfer_request(self, request_id: int) -> BSTPartialTransferApproval:
        try:
            return self.partial_transfer_queryset().get(id=request_id)
        except BSTPartialTransferApproval.DoesNotExist as exc:
            raise BSTError("Partial-transfer request not found.") from exc

    def latest_partial_transfer_for(self, transfer: BSTTransfer):
        return transfer.partial_transfer_requests.order_by("-requested_at", "-id").first()

    @transaction.atomic
    def review_partial_transfer(
        self, request_id: int, *, approve: bool, notes: str = "",
    ) -> BSTPartialTransferApproval:
        request = self.get_partial_transfer_request(request_id)
        if not request.is_pending:
            raise BSTError(f"This request is already {request.get_status_display().lower()}.")
        notes = (notes or "").strip()
        if not approve and not notes:
            raise BSTError("A note is required when rejecting a request.")
        request.mark_reviewed(
            status=(
                BSTPartialTransferStatus.APPROVED if approve
                else BSTPartialTransferStatus.REJECTED
            ),
            reviewer=self.user,
            notes=notes,
        )
        return request

    # ==================================================================
    # Receiver side (current company == destination)
    # ==================================================================

    def _incoming_base(self, statuses):
        return (
            BSTTransfer.objects
            .filter(self._receivable_scope(), status__in=statuses)
            .select_related("company", "destination_company", "vehicle", "driver")
            .annotate(
                scanned_box_count=Count("box_scans", distinct=True),
                item_count=Count("items", distinct=True),
                doc_count=Count("docs", distinct=True),
            )
            .order_by("-dispatched_at", "-created_at")
        )

    def incoming_queryset(self):
        """Transfers dispatched and still awaiting receipt by this company — its
        own intra-company transfers plus cross-company invoices addressed to it.
        The receivable set only (drives what can actually be received)."""
        return self._incoming_base(RECEIVABLE_STATUSES)

    def incoming_view_queryset(self):
        """Incoming board listing: the receivable set PLUS already-finalized
        (RECEIVED/CLOSED) receipts, so finalized transfers stay visible as
        history instead of disappearing off the dashboard."""
        return self._incoming_base(INCOMING_VIEW_STATUSES)

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
        resolved_pallet_code = ""
        if entity_type == "PALLET" and entity_id:
            is_pallet = True
            pallet = Pallet.objects.filter(id=entity_id).first()
            resolved_pallet_code = pallet.pallet_id if pallet else barcode_raw
            barcodes = list(
                (pallet.boxes.values_list("box_barcode", flat=True)) if pallet else []
            )
            if not barcodes:
                raise BSTError("Pallet has no boxes.")
        elif entity_type == "BOX" and entity_id:
            box = Box.objects.filter(id=entity_id).first()
            barcodes = [box.box_barcode] if box else []
        else:
            # A pallet that already changed hands (e.g. accepted → moved to the
            # destination company) no longer resolves via the source-scoped
            # ScanService, so re-scanning it to reject/accept the rest would miss.
            # Fall back to this transfer's own scans: if the raw value is a
            # pallet_code recorded on the transfer, receive every box under it.
            pallet_boxes = list(
                transfer.box_scans.filter(pallet_code=barcode_raw)
                .values_list("box_barcode", flat=True)
            )
            if pallet_boxes:
                is_pallet = True
                resolved_pallet_code = barcode_raw
                barcodes = pallet_boxes
            else:
                # A sender-scanned box barcode still matches by raw value even if
                # the box already changed hands.
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

        # Settle cross-company ownership the instant a box/pallet is decided:
        # accepting hands it to the destination company; rejecting a
        # previously-accepted one returns it (and its now-empty pallet) to the
        # source. Intra-company STOCK_TRANSFERs never change owner, so skip them.
        if transfer.source_type == BSTSourceType.INVOICE and updated:
            decided_boxes = list(
                Box.objects.select_related("company")
                .filter(id__in=[s.box_id for s in updated if s.box_id])
            )
            pallet_codes = [resolved_pallet_code] if is_pallet and resolved_pallet_code else None
            if decision == BSTReceiveStatus.ACCEPTED:
                self._hand_to_destination(transfer, decided_boxes, pallet_codes=pallet_codes)
            else:
                self._return_to_source(transfer, decided_boxes, pallet_codes=pallet_codes)

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
        # Idempotent for a resumed (PARTIALLY_RECEIVED → RECEIVED) re-finalize: skip
        # boxes already sitting at the destination, so we don't record a second
        # TRANSFER movement for a box settled on an earlier completion.
        if to_warehouse:
            boxes = [b for b in boxes if b.current_warehouse != to_warehouse]
        if not boxes:
            return
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
        """Cross-company: hand ownership of the accepted boxes (and their pallets)
        to the destination company on receive-complete. Thin wrapper over the
        shared handoff; see :meth:`_hand_to_destination`."""
        self._hand_to_destination(transfer, boxes)

    def _hand_to_destination(
        self, transfer: BSTTransfer, boxes: list, *, pallet_codes=None
    ) -> int:
        """Hand ownership of ``boxes`` AND their pallets to the transfer's
        destination company — the cross-company (INVOICE) intercompany handoff.

        Reuses the shared ``box_ownership`` move (with the JIVO OIL⇄MART
        item-code remap where catalogues differ). The warehouse is left
        unchanged, matching the standalone intercompany-transfer flow; each moved
        box is recorded on the barcode audit log.

        ``pallet_codes`` names extra pallets (by license plate / ``pallet_id``) to
        move even when none of ``boxes`` links to them — used by the per-pallet
        putaway hook so the container follows its contents into Mart.

        Idempotent: boxes/pallets already owned by the destination are skipped, so
        a resumed finalize, a putaway retry, or a backfill never double-moves or
        double-logs. Returns the number of boxes moved.
        """
        destination = transfer.destination_company
        if destination is None:
            raise BSTError("This invoice transfer has no destination company.")
        source = transfer.company

        # Only move what isn't already in the destination company (idempotent).
        boxes = [b for b in boxes if b.company_id != destination.id]

        # Pallets to move: the parents of these boxes, plus any explicitly named
        # license plates. Skip pallets already owned by the destination.
        pallet_pks = {b.pallet_id for b in boxes if b.pallet_id}
        codes = {c.strip() for c in (pallet_codes or ()) if c and c.strip()}
        pallet_filter = Q(id__in=pallet_pks)
        if codes:
            pallet_filter |= Q(pallet_id__in=codes)
        pallets = list(
            Pallet.objects.select_for_update()
            .filter(pallet_filter)
            .exclude(company_id=destination.id)
        ) if (pallet_pks or codes) else []

        if not boxes and not pallets:
            return 0

        # Lock the boxes for the ownership update.
        if boxes:
            boxes = list(
                Box.objects.select_for_update().select_related("company")
                .filter(id__in=[b.id for b in boxes])
            )

        # Re-resolve the item-code map at settle time (authoritative — the
        # catalogue may have changed since create). Covers box and pallet codes.
        try:
            item_code_map = resolve_destination_item_code_map(
                source, destination,
                [b.item_code for b in boxes] + [p.item_code for p in pallets],
            )
        except ItemCodeMappingError as exc:
            raise BSTError(str(exc)) from exc

        reassign_boxes_to_company(boxes, destination, item_code_map=item_code_map or None)
        reassign_pallets_to_company(pallets, destination, item_code_map=item_code_map or None)

        if boxes:
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
        return len(boxes)

    def _return_to_source(
        self, transfer: BSTTransfer, boxes: list, *, pallet_codes=None
    ) -> int:
        """Reverse of :meth:`_hand_to_destination`: return ``boxes`` (and, once a
        pallet holds no box at the destination, that pallet) to the SOURCE
        company. Used when a previously-accepted box/pallet is rejected.

        Reverses the JIVO OIL⇄MART item-code remap (destination catalogue back to
        source). Idempotent: only boxes actually sitting in the destination are
        moved, so re-rejecting is a no-op. Returns the number of boxes returned.
        """
        destination = transfer.destination_company
        if destination is None:
            return 0
        source = transfer.company

        # Only reverse what actually sits in the destination (i.e. was accepted).
        boxes = [b for b in boxes if b.company_id == destination.id]
        if boxes:
            boxes = list(
                Box.objects.select_for_update().select_related("company")
                .filter(id__in=[b.id for b in boxes])
            )
        if not boxes:
            return 0

        # Reverse item-code remap (destination catalogue -> source catalogue).
        try:
            box_map = resolve_destination_item_code_map(
                destination, source, [b.item_code for b in boxes],
            )
        except ItemCodeMappingError as exc:
            raise BSTError(str(exc)) from exc
        reassign_boxes_to_company(boxes, source, item_code_map=box_map or None)

        # A pallet returns to the source only once it no longer holds ANY box at
        # the destination (a partially-rejected pallet stays put).
        codes = {c.strip() for c in (pallet_codes or ()) if c and c.strip()}
        pallet_pks = {b.pallet_id for b in boxes if b.pallet_id}
        pallet_filter = Q(id__in=pallet_pks)
        if codes:
            pallet_filter |= Q(pallet_id__in=codes)
        pallets = list(
            Pallet.objects.select_for_update()
            .filter(pallet_filter, company_id=destination.id)
        ) if (pallet_pks or codes) else []
        returning = [
            p for p in pallets
            if not Box.objects.filter(pallet_id=p.id, company_id=destination.id).exists()
        ]
        if returning:
            try:
                pallet_map = resolve_destination_item_code_map(
                    destination, source, [p.item_code for p in returning],
                )
            except ItemCodeMappingError as exc:
                raise BSTError(str(exc)) from exc
            reassign_pallets_to_company(returning, source, item_code_map=pallet_map or None)

        BarcodeAuditLog.objects.bulk_create([
            BarcodeAuditLog(
                box=box,
                barcode=box.box_barcode,
                transaction_type=BarcodeAuditTransactionType.TRANSFER_REVERSED,
                from_company=destination,
                to_company=source,
                user=self.user,
                notes=f"BST {transfer.entry_no} reject after accept (invoice {transfer.sap_doc_num})",
            )
            for box in boxes
        ])
        return len(boxes)

    @transaction.atomic
    def receive_complete(self, transfer: BSTTransfer) -> BSTTransfer:
        transfer = self._lock(transfer, as_receiver=True)
        self._ensure_receivable(transfer)

        # Hard block on a live transfer: the receiver may accept/reject boxes while
        # the sender is still scanning, but must NOT finalize until the sender has
        # sealed the send (`scan_approved_at`, set by "Finish sending"/approve).
        # Otherwise boxes not yet scanned would be wrongly recorded as short.
        # (Non-live transfers are always sealed before they become receivable.)
        if self._is_live(transfer) and transfer.scan_approved_at is None:
            raise BSTError(
                "The sender hasn't finished sending yet. They must seal the "
                "transfer (Finish sending) before the receipt can be finalized."
            )

        scans = list(transfer.box_scans.all())
        dispatched = [s for s in scans if not s.is_unexpected]
        accepted = sum(1 for s in scans if s.receive_status == BSTReceiveStatus.ACCEPTED)
        pending = sum(1 for s in dispatched if s.receive_status == BSTReceiveStatus.PENDING)
        rejected = sum(1 for s in dispatched if s.receive_status == BSTReceiveStatus.REJECTED)

        # A PM-only bill is sealed with no boxes (packaging material isn't scanned),
        # so there's nothing to accept/reject — finalize it straight to RECEIVED
        # rather than blocking on the "decide at least one box" guard below.
        if not dispatched and not bst_requires_scanning(transfer):
            transfer.status = BSTTransferStatus.RECEIVED
            transfer.received_by = self.user
            transfer.received_at = timezone.now()
            transfer.save(
                update_fields=["status", "received_by", "received_at", "updated_at"]
            )
            return transfer

        # Guard: finalizing an untouched receipt is meaningless and would silently
        # strand the whole shipment as PARTIALLY_RECEIVED (the incident this fixes).
        # Require at least one box to have been decided (accepted or rejected).
        if accepted == 0 and rejected == 0:
            raise BSTError(
                "Accept or reject at least one box before finalizing this receipt."
            )

        self._apply_accepted_moves(transfer)

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
