"""Warehouse transfer requests: raise, approve, post to SAP, hand off to BST.

Ordering matters and is deliberate. The transfer is posted *before* the BST is
created, because BST is already built to validate scans against an existing SAP
document — so the posted `DocEntry` is what seeds it, and nothing in the scan
flow has to change. Shortfalls are then BST's existing problem, which it already
solves with `BSTPartialTransferApproval`.

Approval is app-owned. SAP's own approval procedures do not apply to Service
Layer posts, so there is no second queue anywhere and no draft to chase.
"""

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from company.models import Company
from sap_client.client import SAPClient
from sap_client.exceptions import SAPConnectionError, SAPDataError, SAPValidationError
from sap_client.hana.series_reader import HanaSeriesReader
from sap_client.service_layer.itr_writer import build_transfer_request_payload
from sap_client.service_layer.stock_transfer_writer import (
    BASE_TYPE_STOCK_TRANSFER,
    BASE_TYPE_TRANSFER_REQUEST,
    build_stock_transfer_payload,
)

from ..models_transfer import (
    TransferLineStatus,
    TransferPostingStatus,
    TransferRequestStatus,
    TransferRouteType,
    WarehouseTransferRequest,
    WarehouseTransferRequestLine,
)
from . import transfer_guards as guards
from .transfer_guards import TransferGuardError

logger = logging.getLogger(__name__)


class TransferRequestError(ValueError):
    """A request the app itself refuses — bad state, not a SAP rejection."""


class TransferRequestService:
    """Everything a warehouse transfer request does, for one company."""

    def __init__(self, company_code: str, user=None):
        self.company_code = company_code
        self.user = user
        self._client = None
        self._branches = None

    # ------------------------------------------------------------------
    # lazily-built collaborators
    # ------------------------------------------------------------------

    @property
    def company(self) -> Company:
        return Company.objects.get(code=self.company_code)

    @property
    def client(self) -> SAPClient:
        if self._client is None:
            self._client = SAPClient(company_code=self.company_code)
        return self._client

    @property
    def branch_map(self) -> dict:
        """Warehouse -> branch, read once per service instance."""
        if self._branches is None:
            self._branches = self.client.get_warehouse_branches()
        return self._branches

    # ------------------------------------------------------------------
    # reads
    # ------------------------------------------------------------------

    def base_queryset(self):
        return (
            WarehouseTransferRequest.objects
            .filter(company__code=self.company_code)
            .select_related('company', 'requested_by', 'reviewed_by', 'posted_by',
                            'bst_transfer')
            .prefetch_related('lines')
        )

    def list_requests(self, *, status=None, posting_status=None,
                      to_warehouse=None, from_warehouse=None):
        qs = self.base_queryset()
        if status:
            qs = qs.filter(status=status)
        if posting_status:
            qs = qs.filter(posting_status=posting_status)
        if to_warehouse:
            qs = qs.filter(to_warehouse=to_warehouse)
        if from_warehouse:
            qs = qs.filter(from_warehouse=from_warehouse)
        return qs

    def available_items(
        self, warehouse: str, *, search: str = "", limit: int = 50
    ) -> list[dict]:
        """Items the source warehouse actually holds, for the request's picker.

        The picker offers `available` (on hand minus committed) rather than raw
        on-hand, because an open request already commits stock at its source —
        offering on-hand would invite two requests to claim the same drums. The
        real gate is still server-side at posting, where batch allocation fails
        loudly if the stock has gone.
        """
        if not (warehouse or "").strip():
            raise TransferRequestError("Pick the source warehouse first.")
        return self.client.get_warehouse_stock(
            warehouse.strip(), search=search or "", limit=limit
        )

    def get_request(
        self, request_id: int, *, link_bst: bool = False
    ) -> WarehouseTransferRequest:
        try:
            request = self.base_queryset().get(pk=request_id)
        except WarehouseTransferRequest.DoesNotExist:
            raise TransferRequestError(f"Transfer request {request_id} not found.")
        if link_bst and not request.bst_transfer_id and request.sap_transfer_doc_entry:
            # Self-healing: the BST is usually made on the BST screen, which
            # cannot know about this request. Opt-in so the write only happens
            # on the detail read, not on every internal lookup.
            self.resolve_bst(request)
        return request

    def pending_approvals(self):
        """What the receiving warehouse has waiting on it."""
        return self.list_requests(status=TransferRequestStatus.PENDING)

    def awaiting_second_leg(self):
        """Cross-branch stock sitting in an in-transit warehouse."""
        return self.list_requests(
            posting_status=TransferPostingStatus.IN_TRANSIT
        ).filter(route_type=TransferRouteType.CROSS_BRANCH)

    # ------------------------------------------------------------------
    # 01 — raise
    # ------------------------------------------------------------------

    @transaction.atomic
    def create_request(self, data: dict) -> WarehouseTransferRequest:
        """Raise a request and mirror it into SAP as a transfer request.

        Posting the ITR is what reserves the stock for the duration of the
        approval, which is the whole reason the pending state is useful: without
        it two approvals can promise the same drums.

        Deliberately atomic over the SAP call: if SAP refuses or is unreachable
        the app request is rolled back too, rather than left sitting without a
        reservation and quietly promising stock it never held.
        """
        from_warehouse = (data.get('from_warehouse') or '').strip()
        to_warehouse = (data.get('to_warehouse') or '').strip()
        raw_lines = data.get('lines') or []

        if not raw_lines:
            raise TransferRequestError("Add at least one item to the request.")

        route = guards.resolve_route(
            from_warehouse=from_warehouse,
            to_warehouse=to_warehouse,
            branch_of=self.branch_map,
        )
        guards.check_route(
            from_warehouse=from_warehouse,
            to_warehouse=to_warehouse,
            route=route,
        )

        item_codes = [line.get('item_code') for line in raw_lines]
        batch_flags = self.client.batch_managed_flags(item_codes)

        request = WarehouseTransferRequest.objects.create(
            company=self.company,
            entry_no=WarehouseTransferRequest.generate_entry_no(),
            from_warehouse=from_warehouse,
            to_warehouse=to_warehouse,
            route_type=(
                TransferRouteType.CROSS_BRANCH if route.is_cross_branch
                else TransferRouteType.INTRA_BRANCH
            ),
            from_branch_id=route.from_branch_id,
            to_branch_id=route.to_branch_id,
            intransit_warehouse=route.intransit_warehouse,
            remarks=data.get('remarks', ''),
            requested_by=self.user,
        )

        for index, line in enumerate(raw_lines):
            item_code = (line.get('item_code') or '').strip()
            quantity = Decimal(str(line.get('quantity') or 0))
            if not item_code:
                raise TransferRequestError(f"Line {index + 1} has no item code.")
            if quantity <= 0:
                raise TransferRequestError(
                    f"{item_code} needs a positive quantity, got {quantity}."
                )
            # Catch it here rather than at posting: a fractional pouch is wrong
            # the moment it is asked for, and SAP will not object later.
            guards.check_whole_units(item_code, quantity, line.get('uom', ''))
            WarehouseTransferRequestLine.objects.create(
                request=request,
                line_num=index,
                item_code=item_code,
                item_name=line.get('item_name', ''),
                uom=line.get('uom', ''),
                from_warehouse=line.get('from_warehouse', ''),
                to_warehouse=line.get('to_warehouse', ''),
                requested_qty=quantity,
                is_batch_managed=bool(batch_flags.get(item_code)),
            )

        self._post_transfer_request(request)
        logger.info(
            "Transfer request %s raised (%s → %s, %s)",
            request.entry_no, from_warehouse, to_warehouse, request.route_type,
        )
        return request

    def _post_transfer_request(self, request: WarehouseTransferRequest) -> None:
        """Mirror the app request into SAP so the stock is reserved."""
        posting_date = timezone.localdate()
        guards.check_posting_date(posting_date)

        series = HanaSeriesReader(self.client.context).resolve_transfer_request(
            posting_date
        )
        payload = build_transfer_request_payload(
            series=series,
            branch_id=request.from_branch_id,
            from_warehouse=request.from_warehouse,
            to_warehouse=request.leg1_destination,
            lines=[
                {
                    'item_code': line.item_code,
                    'quantity': line.requested_qty,
                    'from_warehouse': line.from_warehouse,
                    'to_warehouse': line.to_warehouse or request.leg1_destination,
                }
                for line in request.lines.all()
            ],
            posting_date=posting_date,
            comments=f"App transfer request {request.entry_no}",
        )
        created = self.client.create_transfer_request(payload)
        request.sap_request_doc_entry = created.get('DocEntry')
        request.sap_request_doc_num = str(created.get('DocNum') or '')
        request.save(update_fields=[
            'sap_request_doc_entry', 'sap_request_doc_num', 'updated_at',
        ])

    # ------------------------------------------------------------------
    # 02 — approve / reject
    # ------------------------------------------------------------------

    @transaction.atomic
    def approve(self, request_id: int, data: dict) -> WarehouseTransferRequest:
        """Receiving warehouse accepts the request, in full or in part.

        `data['lines']` is a list of `{"line_num": n, "approved_qty": q}`. Any
        line left out is approved at its requested quantity; a line approved at
        zero is rejected.
        """
        request = self.get_request(request_id)
        if request.status != TransferRequestStatus.PENDING:
            raise TransferRequestError(
                f"{request.entry_no} is already {request.get_status_display().lower()}."
            )

        decisions = {
            int(item['line_num']): Decimal(str(item.get('approved_qty') or 0))
            for item in (data.get('lines') or [])
        }

        any_approved = False
        any_trimmed = False
        for line in request.lines.all():
            approved = decisions.get(line.line_num, line.requested_qty)
            if approved < 0:
                raise TransferRequestError(
                    f"{line.item_code}: approved quantity cannot be negative."
                )
            if approved > line.requested_qty:
                raise TransferRequestError(
                    f"{line.item_code}: cannot approve {approved}, only "
                    f"{line.requested_qty} was requested."
                )
            if approved > 0:
                guards.check_whole_units(line.item_code, approved, line.uom)
            line.approved_qty = approved
            line.status = (
                TransferLineStatus.APPROVED if approved > 0
                else TransferLineStatus.REJECTED
            )
            line.save(update_fields=['approved_qty', 'status', 'updated_at'])

            any_approved = any_approved or approved > 0
            any_trimmed = any_trimmed or approved < line.requested_qty

        if not any_approved:
            return self.reject(
                request_id,
                data.get('reason') or "Every line was approved at zero.",
            )

        request.status = (
            TransferRequestStatus.PARTIALLY_APPROVED if any_trimmed
            else TransferRequestStatus.APPROVED
        )
        request.reviewed_by = self.user
        request.reviewed_at = timezone.now()
        request.save(update_fields=['status', 'reviewed_by', 'reviewed_at', 'updated_at'])
        logger.info("Transfer request %s %s", request.entry_no, request.status)
        return request

    @transaction.atomic
    def reject(self, request_id: int, reason: str) -> WarehouseTransferRequest:
        """Receiving warehouse refuses the request; the reservation is released."""
        request = self.get_request(request_id)
        if request.status != TransferRequestStatus.PENDING:
            raise TransferRequestError(
                f"{request.entry_no} is already {request.get_status_display().lower()}."
            )
        if not (reason or '').strip():
            raise TransferRequestError("A rejection needs a reason.")

        request.lines.update(approved_qty=0, status=TransferLineStatus.REJECTED)
        request.status = TransferRequestStatus.REJECTED
        request.rejection_reason = reason.strip()
        request.reviewed_by = self.user
        request.reviewed_at = timezone.now()
        request.save(update_fields=[
            'status', 'rejection_reason', 'reviewed_by', 'reviewed_at', 'updated_at',
        ])

        self._release_sap_request(request)
        logger.info("Transfer request %s rejected: %s", request.entry_no, reason)
        return request

    def _release_sap_request(self, request: WarehouseTransferRequest) -> None:
        """Close the ITR so it stops reserving stock.

        `Close`, not `Cancel` — SAP refuses Cancel on this entity outright, even
        while the request is open. Failure is logged rather than raised: the
        app-side decision is already recorded, and a stranded reservation is the
        stale sweep's job, not a reason to fail the operator's action.
        """
        if not request.sap_request_doc_entry or request.sap_request_closed_at:
            return
        try:
            self.client.close_transfer_request(request.sap_request_doc_entry)
        except (SAPConnectionError, SAPDataError, SAPValidationError) as exc:
            logger.error(
                "Could not close SAP request %s for %s: %s",
                request.sap_request_doc_entry, request.entry_no, exc,
            )
            return
        request.sap_request_closed_at = timezone.now()
        request.save(update_fields=['sap_request_closed_at', 'updated_at'])

    # ------------------------------------------------------------------
    # 03 — post the transfer
    # ------------------------------------------------------------------

    def allocation_preview(self, request_id: int) -> dict:
        """What batches posting would take, and what else is on the shelf.

        Lets the operator see and change the split before stock moves. FIFO is
        only a default — the floor sometimes needs a specific batch (a customer
        specifying a production date, or clearing short-dated stock first).
        """
        request = self.get_request(request_id)
        destination = request.leg1_destination
        reader = self._batch_reader()

        lines = []
        for line in request.lines.all():
            outstanding = line.outstanding_qty
            if outstanding <= 0:
                continue

            source = line.source_warehouse
            entry = {
                "line_num": line.line_num,
                "item_code": line.item_code,
                "item_name": line.item_name,
                "uom": line.uom,
                "quantity": outstanding,
                "from_warehouse": source,
                "to_warehouse": line.to_warehouse or destination,
                "is_batch_managed": line.is_batch_managed,
                "proposed": [],
                "available": [],
                "error": "",
            }

            if line.is_batch_managed:
                entry["available"] = [
                    {
                        "batch_number": batch["batch_number"],
                        "quantity": batch["quantity"],
                        "in_date": batch["in_date"],
                        "expiry_date": batch["expiry_date"],
                        "production_date": batch["production_date"],
                    }
                    for batch in reader.available_batches(line.item_code, source)
                    if batch["status"] == "0"
                ]
                try:
                    entry["proposed"] = reader.allocate_fifo(
                        line.item_code, source, outstanding
                    )
                except (SAPDataError, SAPValidationError) as exc:
                    # Report the shortfall in the dialog rather than failing the
                    # whole preview — other lines may still be fine.
                    entry["error"] = str(exc)

            lines.append(entry)

        return {
            "entry_no": request.entry_no,
            "from_warehouse": request.from_warehouse,
            "to_warehouse": destination,
            "is_cross_branch": request.is_cross_branch,
            "needs_batches": any(line["is_batch_managed"] for line in lines),
            "lines": lines,
        }

    @transaction.atomic
    def post_transfer(
        self, request_id: int, allocations: dict[int, list[dict]] | None = None
    ) -> WarehouseTransferRequest:
        """Post the approved quantities to SAP as an inventory transfer.

        Intra-branch this is the whole move. Cross-branch it is leg 1, into the
        destination branch's in-transit warehouse; leg 2 waits for receipt.

        `allocations` maps line_num -> a hand-picked batch split. Anything not
        given falls back to oldest-first, so an operator can override one line
        and leave the rest alone.
        """
        request = self.get_request(request_id)

        if not request.is_approved:
            raise TransferRequestError(
                f"{request.entry_no} has not been approved yet."
            )
        if request.posting_status in (
            TransferPostingStatus.IN_TRANSIT, TransferPostingStatus.POSTED
        ):
            raise TransferRequestError(
                f"{request.entry_no} is already posted as SAP document "
                f"{request.sap_transfer_doc_num}."
            )

        destination = request.leg1_destination
        posting_date = timezone.localdate()
        guards.check_posting_date(posting_date)

        lines = self._build_transfer_lines(request, destination, allocations or {})
        if not lines:
            raise TransferRequestError("Nothing approved is left to transfer.")

        guards.check_route(
            from_warehouse=request.from_warehouse,
            to_warehouse=destination,
            route=guards.RouteDecision(
                request.is_cross_branch, request.from_branch_id,
                request.to_branch_id, request.intransit_warehouse,
            ),
        )
        guards.check_lines(
            lines=lines,
            batch_flags={
                line.item_code: line.is_batch_managed for line in request.lines.all()
            },
            has_transfer_request=bool(request.sap_request_doc_entry),
        )

        series = HanaSeriesReader(self.client.context).resolve_stock_transfer(posting_date)
        payload = build_stock_transfer_payload(
            series=series,
            branch_id=request.from_branch_id,
            from_warehouse=request.from_warehouse,
            to_warehouse=destination,
            lines=lines,
            posting_date=posting_date,
            comments=f"App transfer request {request.entry_no}",
            card_code=guards.card_code_for_route(request.from_warehouse, destination),
        )

        created = self._post_and_record(request, payload, is_second_leg=False)
        self._persist_transfer_lines(request, lines)
        return created

    def _batch_reader(self):
        from sap_client.hana.batch_stock_reader import HanaBatchStockReader
        return HanaBatchStockReader(self.client.context)

    def _build_transfer_lines(
        self,
        request: WarehouseTransferRequest,
        destination: str,
        allocations: dict[int, list[dict]] | None = None,
    ) -> list[dict]:
        """Turn approved quantities into SAP lines and choose their batches.

        Oldest-first unless the caller supplied a split for that line, which is
        validated against the shelf before it is sent — a typed batch can name
        stock that has already moved, which FIFO could never do.
        """
        allocations = allocations or {}
        lines: list[dict] = []
        for line in request.lines.all():
            outstanding = line.outstanding_qty
            if outstanding <= 0:
                continue

            source = line.source_warehouse
            entry = {
                'item_code': line.item_code,
                'quantity': outstanding,
                'from_warehouse': source,
                'to_warehouse': line.to_warehouse or destination,
                'line_num': line.line_num,
                'uom': line.uom,
            }

            if line.is_batch_managed:
                chosen = allocations.get(line.line_num)
                if chosen:
                    entry['batches'] = self._batch_reader().check_allocation(
                        line.item_code, source, chosen
                    )
                else:
                    entry['batches'] = self.client.allocate_batches_fifo(
                        line.item_code, source, outstanding
                    )

            # Tie the line back to its request line so SAP draws the reservation
            # down instead of leaving it open alongside the movement.
            if request.sap_request_doc_entry:
                entry['base_type'] = BASE_TYPE_TRANSFER_REQUEST
                entry['base_entry'] = request.sap_request_doc_entry
                entry['base_line'] = line.line_num

            lines.append(entry)
        return lines

    def _persist_transfer_lines(
        self, request: WarehouseTransferRequest, lines: list[dict]
    ) -> None:
        by_line_num = {line['line_num']: line for line in lines}
        for line in request.lines.all():
            sent = by_line_num.get(line.line_num)
            if not sent:
                continue
            line.transferred_qty = line.transferred_qty + Decimal(str(sent['quantity']))
            line.batch_allocation = sent.get('batches') or []
            line.save(update_fields=[
                'transferred_qty', 'batch_allocation', 'updated_at',
            ])

    # ------------------------------------------------------------------
    # 04 — hand off to BST
    # ------------------------------------------------------------------

    @transaction.atomic
    def create_bst(self, request_id: int, data: dict | None = None):
        """Seed a BST from the posted transfer — the "create BST" button.

        BST already validates scans against an existing SAP document, so all this
        does is point it at the transfer we just posted. Nothing in the scan flow
        changes.
        """
        from .bst_service import BSTError, BSTService

        request = self.get_request(request_id)
        data = data or {}

        if not request.sap_transfer_doc_entry:
            raise TransferRequestError(
                f"{request.entry_no} has not been posted to SAP yet, so there is "
                f"no document for the BST to check scans against."
            )
        if request.bst_transfer_id:
            raise TransferRequestError(
                f"{request.entry_no} already has BST "
                f"{request.bst_transfer.entry_no}."
            )

        try:
            bst = BSTService(self.company_code, self.user).create_transfer({
                "sap_doc_entries": [request.sap_transfer_doc_entry],
                "vehicle": data.get("vehicle"),
                "driver": data.get("driver"),
                "requires_gate": data.get("requires_gate", request.is_cross_branch),
                "remarks": data.get("remarks") or f"From {request.entry_no}",
            })
        except BSTError as exc:
            raise TransferRequestError(str(exc)) from exc

        if request.is_cross_branch:
            # Leg 1 ships into the in-transit warehouse, so that is what the SAP
            # document says — but in-transit is a bookkeeping location, not a
            # place, and the boxes physically land at the real destination. Point
            # the BST at where the goods actually end up, so that once leg 2
            # posts, the app and SAP agree on where the stock is.
            bst.sap_to_warehouse = request.to_warehouse
            bst.save(update_fields=["sap_to_warehouse", "updated_at"])

        request.bst_transfer = bst
        request.save(update_fields=["bst_transfer", "updated_at"])
        logger.info(
            "BST %s seeded from transfer request %s (SAP %s)",
            bst.entry_no, request.entry_no, request.sap_transfer_doc_num,
        )
        return bst

    def resolve_bst(self, request: WarehouseTransferRequest):
        """Find the BST that checks this transfer's boxes, and link it.

        The BST is normally created through the ordinary BST screen (so the user
        gets vehicle, driver, gate and document-combining), which means nothing
        sets `bst_transfer` for us. Match on the SAP document instead: BST
        already refuses to let two live transfers share one document, so the
        document identifies the BST unambiguously however it was created.

        Also corrects the destination for a cross-branch move — leg 1's SAP
        document ships into the in-transit warehouse, but the boxes physically
        land at the real destination and BST settles them to its head's
        `sap_to_warehouse`. Without this the app would park them in a
        bookkeeping warehouse.
        """
        from ..models_bst import BSTTransfer, BSTTransferDoc, BSTTransferStatus

        if request.bst_transfer_id:
            return request.bst_transfer
        if not request.sap_transfer_doc_entry:
            return None

        doc = (
            BSTTransferDoc.objects
            .filter(
                sap_doc_entry=request.sap_transfer_doc_entry,
                transfer__company=request.company,
            )
            .exclude(transfer__status=BSTTransferStatus.CANCELLED)
            .select_related("transfer")
            .order_by("-transfer_id")
            .first()
        )
        bst: BSTTransfer | None = doc.transfer if doc else None
        if bst is None:
            return None

        request.bst_transfer = bst
        request.save(update_fields=["bst_transfer", "updated_at"])

        if request.is_cross_branch and bst.sap_to_warehouse != request.to_warehouse:
            bst.sap_to_warehouse = request.to_warehouse
            bst.save(update_fields=["sap_to_warehouse", "updated_at"])
            logger.info(
                "BST %s destination corrected to %s (leg 1 shipped into %s)",
                bst.entry_no, request.to_warehouse, request.intransit_warehouse,
            )

        logger.info(
            "Linked BST %s to transfer request %s via SAP document %s",
            bst.entry_no, request.entry_no, request.sap_transfer_doc_num,
        )
        return bst

    def received_quantities_from_bst(
        self, request: WarehouseTransferRequest, bst
    ) -> dict[int, Decimal]:
        """What the receiver actually accepted, per request line.

        Accepted box scans are the truth for barcoded stock. Packaging material
        is scan-exempt and so has no scans at all — for those lines fall back to
        what leg 1 moved, otherwise a PM line would settle as zero received.
        """
        from ..models_bst import BSTReceiveStatus

        accepted: dict[str, Decimal] = {}
        scanned_items: set[str] = set()
        for scan in bst.box_scans.all():
            scanned_items.add(scan.item_code)
            if scan.receive_status == BSTReceiveStatus.ACCEPTED:
                accepted[scan.item_code] = (
                    accepted.get(scan.item_code, Decimal('0'))
                    + Decimal(str(scan.quantity or 0))
                )

        quantities: dict[int, Decimal] = {}
        for line in request.lines.all():
            if line.item_code in scanned_items:
                quantities[line.line_num] = accepted.get(line.item_code, Decimal('0'))
            else:
                quantities[line.line_num] = line.transferred_qty
        return quantities

    def settle_after_receipt(self, bst):
        """Post leg 2 once a cross-branch BST has been received.

        Returns the request when a leg was posted, otherwise None. A SAP failure
        is recorded on the request and NOT raised: the boxes are physically
        received either way, and refusing the receipt would leave the warehouse
        unable to close a shipment that has already arrived. The request is left
        in FAILED for a retry through the normal endpoint.
        """
        candidates = self.base_queryset().filter(
            route_type=TransferRouteType.CROSS_BRANCH,
            sap_transfer_doc_entry__isnull=False,
            sap_leg2_doc_entry__isnull=True,
        )
        # Prefer the explicit link, but fall back to the SAP document the BST was
        # built from — a BST created through the ordinary BST screen never set
        # `bst_transfer`, and leg 2 must still fire.
        request = candidates.filter(bst_transfer=bst).first()
        if request is None:
            doc_entries = set(
                bst.docs.values_list('sap_doc_entry', flat=True)
            ) | {bst.sap_doc_entry}
            request = candidates.filter(
                sap_transfer_doc_entry__in=[d for d in doc_entries if d]
            ).first()
            if request is not None:
                self.resolve_bst(request)
        if request is None:
            return None

        try:
            return self.post_second_leg(
                request.id, self.received_quantities_from_bst(request, bst)
            )
        except (TransferRequestError, TransferGuardError,
                SAPValidationError, SAPDataError, SAPConnectionError) as exc:
            logger.error(
                "Leg 2 failed for %s after BST %s was received: %s",
                request.entry_no, bst.entry_no, exc,
            )
            request.refresh_from_db()
            request.posting_status = TransferPostingStatus.FAILED
            request.posting_error = str(exc)
            request.save(update_fields=[
                'posting_status', 'posting_error', 'updated_at',
            ])
            return request

    # ------------------------------------------------------------------
    # 05 — the second leg
    # ------------------------------------------------------------------

    @transaction.atomic
    def post_second_leg(
        self, request_id: int, received: dict[int, Decimal] | None = None
    ) -> WarehouseTransferRequest:
        """Move cross-branch stock out of in-transit into its real destination.

        `received` maps line_num -> quantity actually received; anything omitted
        uses what leg 1 moved. A short receipt simply leaves the remainder in the
        in-transit warehouse, which is exactly where in-transit shortfall
        belongs — no correcting document needed.
        """
        request = self.get_request(request_id)

        if not request.is_cross_branch:
            raise TransferRequestError(
                f"{request.entry_no} stays inside one branch, so it has no second leg."
            )
        # Gate on the documents rather than the status, so a leg 2 that failed
        # (status FAILED, not IN_TRANSIT) can still be retried once whatever SAP
        # objected to has been fixed.
        if not request.sap_transfer_doc_entry:
            raise TransferRequestError(
                f"{request.entry_no} has no first leg posted — nothing is in transit."
            )
        if request.sap_leg2_doc_entry:
            raise TransferRequestError(
                f"{request.entry_no} already completed as SAP document "
                f"{request.sap_leg2_doc_num}."
            )

        posting_date = timezone.localdate()
        guards.check_posting_date(posting_date)
        received = received or {}

        lines: list[dict] = []
        for line in request.lines.all():
            moved = Decimal(str(received.get(line.line_num, line.transferred_qty)))
            if moved <= 0:
                continue
            entry = {
                'item_code': line.item_code,
                'quantity': moved,
                'from_warehouse': request.intransit_warehouse,
                'to_warehouse': line.to_warehouse or request.to_warehouse,
                'line_num': line.line_num,
                'uom': line.uom,
            }
            if line.is_batch_managed:
                entry['batches'] = self.client.allocate_batches_fifo(
                    line.item_code, request.intransit_warehouse, moved
                )
            if request.sap_transfer_doc_entry:
                entry['base_type'] = BASE_TYPE_STOCK_TRANSFER
                entry['base_entry'] = request.sap_transfer_doc_entry
                entry['base_line'] = line.line_num
            lines.append(entry)

        if not lines:
            raise TransferRequestError("Nothing was received, so there is nothing to post.")

        # Read the real branches rather than assuming both sides match — that
        # assumption is exactly what the 6700001 guard exists to catch, so
        # feeding it two copies of the same value would disable it.
        branches = self.branch_map
        guards.check_route(
            from_warehouse=request.intransit_warehouse,
            to_warehouse=request.to_warehouse,
            route=guards.RouteDecision(
                False,
                branches.get(request.intransit_warehouse),
                branches.get(request.to_warehouse),
                request.intransit_warehouse,
            ),
            is_second_leg=True,
        )
        guards.check_lines(
            lines=lines,
            batch_flags={
                line.item_code: line.is_batch_managed for line in request.lines.all()
            },
            has_transfer_request=True,
        )

        series = HanaSeriesReader(self.client.context).resolve_stock_transfer(posting_date)
        payload = build_stock_transfer_payload(
            series=series,
            branch_id=request.to_branch_id,
            from_warehouse=request.intransit_warehouse,
            to_warehouse=request.to_warehouse,
            lines=lines,
            posting_date=posting_date,
            comments=f"App transfer request {request.entry_no} — leg 2",
        )
        return self._post_and_record(request, payload, is_second_leg=True)

    # ------------------------------------------------------------------
    # posting mechanics
    # ------------------------------------------------------------------

    def _post_and_record(
        self, request: WarehouseTransferRequest, payload: dict, *, is_second_leg: bool
    ) -> WarehouseTransferRequest:
        try:
            created = self.client.create_stock_transfer(payload)
        except (SAPValidationError, SAPDataError, SAPConnectionError) as exc:
            # Record the refusal rather than losing it — a connection error in
            # particular may mean SAP committed anyway, and the operator needs
            # to see the message before trying again.
            request.posting_status = TransferPostingStatus.FAILED
            request.posting_error = str(exc)
            request.save(update_fields=[
                'posting_status', 'posting_error', 'updated_at',
            ])
            raise

        doc_entry = created.get('DocEntry')
        doc_num = str(created.get('DocNum') or '')

        if is_second_leg:
            request.sap_leg2_doc_entry = doc_entry
            request.sap_leg2_doc_num = doc_num
            request.posting_status = TransferPostingStatus.POSTED
            fields = ['sap_leg2_doc_entry', 'sap_leg2_doc_num']
        else:
            request.sap_transfer_doc_entry = doc_entry
            request.sap_transfer_doc_num = doc_num
            request.posting_status = (
                TransferPostingStatus.IN_TRANSIT if request.is_cross_branch
                else TransferPostingStatus.POSTED
            )
            request.posted_by = self.user
            request.posted_at = timezone.now()
            fields = [
                'sap_transfer_doc_entry', 'sap_transfer_doc_num',
                'posted_by', 'posted_at',
            ]

        request.posting_error = ''
        request.save(update_fields=fields + [
            'posting_status', 'posting_error', 'updated_at',
        ])
        logger.info(
            "Transfer request %s posted %s as SAP %s",
            request.entry_no, "leg 2" if is_second_leg else "leg 1", doc_num,
        )
        return request

    # ------------------------------------------------------------------
    # verification
    # ------------------------------------------------------------------

    def verify_batches(self, request_id: int) -> list[str]:
        """Compare the batch split we sent against what SAP recorded in IBT1.

        Reads IBT1 rather than the document: a Service Layer GET returns an
        empty `BatchNumbers` list even for documents that carry batches, so a
        check against the API would report every batch transfer as unallocated.
        """
        request = self.get_request(request_id)
        if not request.sap_transfer_doc_entry:
            return []

        expected: dict[tuple[int, str], Decimal] = {}
        for line in request.lines.all():
            for batch in (line.batch_allocation or []):
                key = (line.line_num, batch.get('BatchNumber'))
                expected[key] = (
                    expected.get(key, Decimal('0'))
                    + Decimal(str(batch.get('Quantity') or 0))
                )
        if not expected:
            return []

        return self._batch_reader().verify_allocation(
            request.sap_transfer_doc_entry, expected
        )

    def reconcile(self, *, include_settled: bool = False, limit: int = 500) -> dict:
        """Where the app and SAP disagree about transfers. See the reconciler."""
        from .transfer_reconciliation import TransferReconciler
        return TransferReconciler(self).run(
            include_settled=include_settled, limit=limit
        )
