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

import functools
import logging
from datetime import date, datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied

from company.models import Company
from sap_client.client import SAPClient
from sap_client.exceptions import (
    SAPConnectionError,
    SAPDataError,
    SAPOutcomeUnknown,
    SAPValidationError,
)
from sap_client.hana.series_reader import HanaSeriesReader
from sap_client.hana.stock_transfer_reader import HanaStockTransferReader
from sap_client.service_layer.itr_writer import build_transfer_request_payload
from sap_client.service_layer.stock_transfer_writer import (
    BASE_TYPE_STOCK_TRANSFER,
    BASE_TYPE_TRANSFER_REQUEST,
    build_stock_transfer_payload,
)

from ..models_transfer import (
    TransferLineStatus,
    TransferPostingStatus,
    TransferRaisedBy,
    TransferRequestStatus,
    TransferRouteType,
    WarehouseTransferRequest,
    WarehouseTransferRequestLine,
)
from . import transfer_guards as guards
from . import warehouse_scope
from .transfer_reservations import (
    batches_held_by_open_requests,
    reserved_by_open_requests,
)
from .transfer_guards import TransferGuardError

logger = logging.getLogger(__name__)

# The SAP server's clock, which stamps OWTR.CreateTS.
SAP_TIME_ZONE = ZoneInfo('Asia/Kolkata')


class TransferRequestError(ValueError):
    """A request the app itself refuses — bad state, not a SAP rejection."""


def _plain(quantity: Decimal) -> str:
    """`Decimal('60.000')` as "60", never as "6E+1"."""
    return format(quantity.normalize(), 'f')



def _keeps_posting_failure(method):
    """Keep SAP's refusal on the request once the posting has rolled back.

    `_post_and_record` writes FAILED and the error as it re-raises, but it runs
    inside the posting's atomic block, so that write rolls back with the rest
    and the request looks as if nobody tried. This sits outside the block and
    writes it again once the rollback is done: a timeout in particular may mean
    SAP committed anyway, and the operator needs the message before retrying.
    """
    @functools.wraps(method)
    def wrapper(self, request_id, *args, **kwargs):
        try:
            return method(self, request_id, *args, **kwargs)
        except (SAPValidationError, SAPDataError, SAPConnectionError) as exc:
            # Only a failure of the post itself; a read that failed before it
            # (series, stock) leaves the request as it was, which is the truth.
            if getattr(exc, "failed_transfer_request_id", None) == request_id:
                WarehouseTransferRequest.objects.filter(pk=request_id).update(
                    posting_status=TransferPostingStatus.FAILED,
                    posting_error=str(exc),
                    updated_at=timezone.now(),
                )
            raise
    return wrapper

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
                      to_warehouse=None, from_warehouse=None,
                      raised_by_side=None, mine=False):
        qs = self.base_queryset()
        if raised_by_side:
            qs = qs.filter(raised_by_side=raised_by_side)
        if mine:
            qs = qs.filter(requested_by=self.user)
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

        The picker offers `free_to_move` — on hand minus what *this app's* own
        open requests already hold — rather than on hand minus SAP's
        `IsCommited`. See `transfer_reservations` for why: `IsCommited` is a
        permanent lien from any open document, including transfer requests
        keyed by hand years ago and never closed, none of which stops a
        warehouse-to-warehouse move.

        SAP's `committed` still travels with the row, as context the operator
        can see rather than a subtraction made on their behalf. The real gate is
        server-side at posting, where batch allocation fails loudly if the stock
        has gone.
        """
        warehouse = (warehouse or "").strip()
        if not warehouse:
            raise TransferRequestError("Pick the source warehouse first.")

        rows = self.client.get_warehouse_stock(
            warehouse, search=search or "", limit=limit
        )
        reserved = reserved_by_open_requests(
            self.company_code, warehouse,
            item_codes=[row["item_code"] for row in rows],
        )
        for row in rows:
            # Dropped rather than left alongside: `available` is SAP's
            # on-hand-minus-committed, and shipping two differently-derived
            # "how much can I take" numbers is how the wrong one gets used.
            row.pop("available", None)
            own = float(reserved.get(row["item_code"], 0))
            row["app_reserved"] = own
            row["free_to_move"] = row["on_hand"] - own
        return rows

    def item_batches(
        self, warehouse: str, item_code: str, *, exclude_request_id: int | None = None
    ) -> dict:
        """Released batches of one item in the source warehouse, for the raise form.

        Each carries how much of it our other open requests were raised
        against, so two requests are not quietly pinned to the same batch.
        That is shown, not refused — the item-level figure works the same way,
        and posting is where a batch that has gone is actually caught.
        """
        warehouse = (warehouse or "").strip()
        item_code = (item_code or "").strip()
        if not warehouse or not item_code:
            raise TransferRequestError("Pick the source warehouse and the item first.")

        is_batch_managed = bool(self.client.batch_managed_flags([item_code]).get(item_code))
        batches = []
        if is_batch_managed:
            held = batches_held_by_open_requests(
                self.company_code, warehouse, item_code,
                exclude_request_id=exclude_request_id,
            )
            batches = [
                {
                    "batch_number": batch["batch_number"],
                    "quantity": batch["quantity"],
                    "in_date": batch["in_date"],
                    "expiry_date": batch["expiry_date"],
                    "production_date": batch["production_date"],
                    "held_by_requests": held.get(batch["batch_number"], Decimal("0")),
                }
                for batch in self._batch_reader().available_batches(item_code, warehouse)
                if batch["status"] == "0"
            ]
        return {
            "item_code": item_code,
            "warehouse": warehouse,
            "is_batch_managed": is_batch_managed,
            "batches": batches,
        }

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
        raised_by_side = data.get('raised_by_side') or TransferRaisedBy.SENDER

        if not raw_lines:
            raise TransferRequestError("Add at least one item to the request.")

        # The raiser must run the side they raise from: only a warehouse's own
        # manager may offer its stock out, and only the receiving warehouse's
        # manager may ask for stock to be sent in. Checked before the route so
        # the answer does not depend on whether the route is a valid one.
        if raised_by_side == TransferRaisedBy.RECEIVER:
            warehouse_scope.assert_can_receive_into(
                self.user, self.company_code, [to_warehouse]
            )
        else:
            warehouse_scope.assert_can_send_from(
                self.user, self.company_code, [from_warehouse]
            )

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
            raised_by_side=raised_by_side,
            requested_by=self.user,
        )

        self._add_lines(request, raw_lines)
        self._post_transfer_request(request)
        logger.info(
            "Transfer request %s raised (%s → %s, %s)",
            request.entry_no, from_warehouse, to_warehouse, request.route_type,
        )
        return request

    def _add_lines(self, request: WarehouseTransferRequest, raw_lines: list) -> None:
        """Write the request's lines, numbered from zero as SAP numbers them."""
        batch_flags = self.client.batch_managed_flags(
            [line.get('item_code') for line in raw_lines]
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
            is_batch_managed = bool(batch_flags.get(item_code))
            chosen = self._chosen_batches(
                item_code,
                line.get('from_warehouse') or request.from_warehouse,
                quantity,
                line.get('batches'),
                is_batch_managed=is_batch_managed,
            )
            WarehouseTransferRequestLine.objects.create(
                request=request,
                line_num=index,
                item_code=item_code,
                item_name=line.get('item_name', ''),
                uom=line.get('uom', ''),
                from_warehouse=line.get('from_warehouse', ''),
                to_warehouse=line.get('to_warehouse', ''),
                requested_qty=quantity,
                is_batch_managed=is_batch_managed,
                chosen_batches=chosen,
            )

    def _chosen_batches(
        self, item_code: str, source: str, quantity: Decimal, raw, *, is_batch_managed: bool
    ) -> list[dict]:
        """Check the batches picked for a line, and return them as stored.

        Nothing picked is the ordinary case: posting takes the oldest. A pick
        must add up to the line exactly and name batches the shelf holds now —
        caught here, while the requester is still at the form, rather than by
        whoever posts it later.
        """
        merged: dict[str, Decimal] = {}
        for entry in raw or []:
            number = str(entry.get('batch_number') or '').strip()
            taking = Decimal(str(entry.get('quantity') or 0))
            if not number:
                raise TransferRequestError(f"A batch picked for {item_code} has no number.")
            if taking > 0:
                merged[number] = merged.get(number, Decimal('0')) + taking
        if not merged:
            return []
        if not is_batch_managed:
            raise TransferRequestError(
                f"{item_code} is not batch-tracked in SAP, so it has no batches to pick."
            )

        total = sum(merged.values(), Decimal('0'))
        if total != quantity:
            raise TransferRequestError(
                f"{item_code}: the batches picked add up to {_plain(total)}, but "
                f"{_plain(quantity)} is asked for."
            )

        from sap_client.hana.batch_stock_reader import InsufficientBatchStock
        try:
            self._batch_reader().check_allocation(
                item_code, source,
                [{'BatchNumber': number, 'Quantity': q} for number, q in merged.items()],
            )
        except InsufficientBatchStock as exc:
            # The requester's to fix, not SAP failing, so a 400 and not a 502.
            raise TransferRequestError(str(exc)) from exc
        return [
            {'batch_number': number, 'quantity': _plain(q)} for number, q in merged.items()
        ]

    def _repick_batches(self, request: WarehouseTransferRequest, raw_lines: list) -> None:
        """Save an edit that changed only which batches the lines are pinned to.

        Batches never reach SAP's request, so this leaves SAP alone. The
        request's `updated_at` still moves, so an approver deciding on the old
        picks is refused like any other stale decision.
        """
        changed = False
        for line, raw in zip(request.lines.all(), raw_lines):
            chosen = self._chosen_batches(
                line.item_code, line.source_warehouse, line.requested_qty,
                raw.get('batches'), is_batch_managed=line.is_batch_managed,
            )
            if chosen != line.chosen_batches:
                line.chosen_batches = chosen
                line.save(update_fields=['chosen_batches', 'updated_at'])
                changed = True
        if changed:
            request.save(update_fields=['updated_at'])

    # ------------------------------------------------------------------
    # 01b — edit, until it is decided
    # ------------------------------------------------------------------

    @transaction.atomic
    def update_request(self, request_id: int, data: dict) -> WarehouseTransferRequest:
        """Change a pending request's items, quantities or remarks.

        Open to the person who raised it until the other side decides — after
        that the quantities are the approver's to trim, and stock may already be
        moving. The route stays as raised: it fixes who decides and which SAP
        branch the request sits in, so a different route is a different request.

        When the lines change, SAP's copy is replaced rather than patched: a new
        transfer request carries the edited lines and the old one is closed.
        Posting ties each transfer line to its request line by number
        (`base_line`), and only a fresh request numbers its lines from zero the
        way the app does. Remarks never reach SAP, so changing only those
        leaves SAP alone.
        """
        request = self._locked(request_id)
        if request.requested_by_id != getattr(self.user, 'pk', None):
            raise PermissionDenied(
                f"Only the person who raised {request.entry_no} can change it."
            )
        if request.status != TransferRequestStatus.PENDING:
            raise TransferRequestError(
                f"{request.entry_no} is already "
                f"{request.get_status_display().lower()}, so it can no longer be changed."
            )

        if 'remarks' in data:
            request.remarks = data['remarks']
            request.save(update_fields=['remarks', 'updated_at'])

        raw_lines = data.get('lines')
        if raw_lines is not None:
            if not raw_lines:
                raise TransferRequestError("Add at least one item to the request.")
            if self._lines_change_sap(request, raw_lines):
                request.lines.all().delete()
                self._add_lines(request, raw_lines)
                self._replace_sap_request(request)
            else:
                self._repick_batches(request, raw_lines)
        return self.get_request(request.pk)

    @staticmethod
    def _lines_change_sap(request: WarehouseTransferRequest, raw_lines: list) -> bool:
        """Whether the edit alters anything SAP's request carries."""
        current = [
            (line.item_code, line.requested_qty, line.from_warehouse, line.to_warehouse)
            for line in request.lines.all()
        ]
        edited = [
            (
                (line.get('item_code') or '').strip(),
                Decimal(str(line.get('quantity') or 0)),
                line.get('from_warehouse') or '',
                line.get('to_warehouse') or '',
            )
            for line in raw_lines
        ]
        return edited != current

    def _replace_sap_request(self, request: WarehouseTransferRequest) -> None:
        """Raise SAP's request afresh from the edited lines, then close the old one.

        In that order, so the stock is never left unreserved in between. If SAP
        will not close the old one, the new one is closed instead and the edit
        refused: rolled back, the app points at the old request again, and that
        is the one still holding the stock.
        """
        superseded = (
            request.sap_request_doc_entry if not request.sap_request_closed_at else None
        )
        superseded_num = request.sap_request_doc_num or superseded
        self._post_transfer_request(request)
        if request.sap_request_closed_at:
            # The old one was already closed — by the stale sweep, or by hand
            # in SAP — so the new request is the first to reserve anything.
            request.sap_request_closed_at = None
            request.save(update_fields=['sap_request_closed_at', 'updated_at'])

        if superseded:
            try:
                self.client.close_transfer_request(superseded)
            except (SAPConnectionError, SAPDataError, SAPValidationError) as exc:
                # A timeout may have closed it anyway, and one closed by hand in
                # SAP refuses a second close — neither should block the edit.
                if self._still_open_in_sap(superseded):
                    self._release_sap_request(request)
                    raise TransferRequestError(
                        f"SAP would not close request {superseded_num}, so the "
                        f"change was not saved: {exc}"
                    ) from exc

        logger.info(
            "Transfer request %s edited; SAP request %s replaced by %s",
            request.entry_no, superseded_num or '—', request.sap_request_doc_num,
        )

    def _still_open_in_sap(self, doc_entry: int) -> bool:
        """Whether SAP still holds a request open. Unknown counts as open."""
        try:
            summary = self.client.summarise_transfer_requests([doc_entry])
        except (SAPConnectionError, SAPDataError):
            return True
        found = summary.get(doc_entry)
        return bool(found and found['is_open'])

    def _locked(self, request_id: int) -> WarehouseTransferRequest:
        """The request, its row held until this transaction ends.

        Editing and deciding both start from "is it still pending?", and an
        edit racing a decision would otherwise see yes on both sides. Only the
        request's own row is locked — not the company it joins to.
        """
        try:
            return (
                WarehouseTransferRequest.objects
                .select_for_update(of=('self',))
                .get(pk=request_id, company__code=self.company_code)
            )
        except WarehouseTransferRequest.DoesNotExist:
            raise TransferRequestError(f"Transfer request {request_id} not found.")

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

        `data['updated_at']` is the request as the approver saw it. The
        requester may edit a pending request, so a decision made on an older
        copy is refused rather than applied, by line number, to other lines.
        """
        self._locked(request_id)
        request = self.get_request(request_id)
        self._assert_can_decide(request)
        if request.status != TransferRequestStatus.PENDING:
            raise TransferRequestError(
                f"{request.entry_no} is already {request.get_status_display().lower()}."
            )
        seen = data.get('updated_at')
        if seen and seen != request.updated_at:
            raise TransferRequestError(
                f"{request.entry_no} was changed after you opened it. Check its "
                f"lines again, then approve."
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
        self._locked(request_id)
        request = self.get_request(request_id)
        # Refusing is the same decision as approving, so it needs the same
        # standing — otherwise anyone could cancel another site's stock move.
        self._assert_can_decide(request)
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

    def _assert_can_decide(self, request: WarehouseTransferRequest) -> None:
        """The side that did not raise the request is the side that decides.

        Stock offered by the sender is accepted by whoever runs the warehouse it
        is coming into. Stock asked for by the receiver is agreed to by whoever
        runs the warehouse it leaves — they are the one handing it over.
        """
        if request.is_asked_for:
            warehouse_scope.assert_can_send_from(
                self.user, self.company_code, [request.from_warehouse]
            )
        else:
            warehouse_scope.assert_can_receive_into(
                self.user, self.company_code, [request.to_warehouse]
            )

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
        self._assert_can_post(request)
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
                # What was picked when raising, cut to what was approved.
                "chosen": [],
                "available": [],
                "error": "",
                # Unlike `error`, nothing the poster cannot fix by picking again.
                "note": "",
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
                entry["chosen"] = line.chosen_split(outstanding)
                if entry["chosen"]:
                    entry["proposed"], entry["note"] = self._propose_chosen(
                        line, entry["chosen"], entry["available"]
                    )
                else:
                    try:
                        entry["proposed"] = reader.allocate_fifo(
                            line.item_code, source, outstanding
                        )
                    except (SAPDataError, SAPValidationError) as exc:
                        # Report the shortfall in the dialog rather than failing
                        # the whole preview — other lines may still be fine.
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

    @staticmethod
    def _propose_chosen(line, chosen: list[dict], available: list[dict]):
        """Start the split from the raise-time pick, as far as the shelf still allows.

        A picked batch may have moved since the request was raised. Proposing
        it anyway would show a split that adds up but cannot post, so each pick
        is capped at what its batch holds now and the gap is said in words —
        the poster then makes up the rest from another batch.
        """
        held = {batch["batch_number"]: batch["quantity"] for batch in available}
        proposed, short = [], []
        for pick in chosen:
            wanted = Decimal(str(pick["Quantity"]))
            there = held.get(pick["BatchNumber"], Decimal("0"))
            take = min(wanted, there)
            if take > 0:
                proposed.append({"BatchNumber": pick["BatchNumber"], "Quantity": float(take)})
            if take < wanted:
                short.append(
                    f"{pick['BatchNumber']} now holds {_plain(there)}, not {_plain(wanted)}"
                    if there > 0 else
                    f"{pick['BatchNumber']} is no longer in {line.source_warehouse}"
                )
        note = ""
        if short:
            note = (
                "Picked when the request was raised, but "
                + "; ".join(short)
                + ". Make up the rest from another batch."
            )
        return proposed, note

    @_keeps_posting_failure
    @transaction.atomic
    def post_transfer(
        self, request_id: int, allocations: dict[int, list[dict]] | None = None
    ) -> WarehouseTransferRequest:
        """Post the approved quantities to SAP as an inventory transfer.

        Intra-branch this is the whole move. Cross-branch it is leg 1, into the
        destination branch's in-transit warehouse; leg 2 waits for receipt.

        `allocations` maps line_num -> a hand-picked batch split. A line not
        given one takes the batches picked when the request was raised, and
        oldest-first when none were, so an operator can override one line and
        leave the rest alone.
        """
        request = self.get_request(request_id)
        self._assert_can_post(request)

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

        adopted = self._adopt_lost_post(request, is_second_leg=False)
        if adopted:
            return adopted

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

        try:
            created = self._post_and_record(request, payload, is_second_leg=False)
        except (SAPValidationError, SAPOutcomeUnknown):
            # A timeout may mean SAP committed anyway, and a post racing one
            # whose reply is still lost is refused as "base document already
            # closed". If a post of ours did go through, that is the answer.
            adopted = self._adopt_after_failure(request, is_second_leg=False)
            if adopted:
                return adopted
            raise
        self._persist_transfer_lines(request, lines)
        return created

    def _assert_can_post(self, request: WarehouseTransferRequest) -> None:
        """Only the two people on the request may move its stock.

        The one who raised it and the one who approved it — nobody else, not
        even another holder of the post permission. Posting is what actually
        moves stock in SAP, so it stays with the people who answer for the
        request rather than with anyone who happens to have the button.
        """
        involved = {request.requested_by_id, request.reviewed_by_id} - {None}
        if getattr(self.user, 'pk', None) not in involved:
            raise PermissionDenied(
                f"Only the person who raised {request.entry_no} or the person "
                f"who approved it can post its stock to SAP."
            )

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

        The caller's split for a line wins, then the one picked when raising,
        then oldest-first. A chosen split is validated against the shelf before
        it is sent — a picked batch can name stock that has since moved, which
        FIFO could never do.
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
                chosen = allocations.get(line.line_num) or line.chosen_split(outstanding)
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

    @_keeps_posting_failure
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
        adopted = self._adopt_lost_post(request, is_second_leg=True)
        if adopted:
            return adopted

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
        try:
            return self._post_and_record(request, payload, is_second_leg=True)
        except (SAPValidationError, SAPOutcomeUnknown):
            adopted = self._adopt_after_failure(request, is_second_leg=True)
            if adopted:
                return adopted
            raise

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
            # The save above rolls back with the posting's transaction;
            # `_keeps_posting_failure` writes it again once it has.
            exc.failed_transfer_request_id = request.pk
            raise

        return self._record_posted(
            request, created.get('DocEntry'), str(created.get('DocNum') or ''),
            is_second_leg=is_second_leg,
        )

    def _record_posted(
        self, request: WarehouseTransferRequest, doc_entry, doc_num: str, *,
        is_second_leg: bool, posted_at=None,
    ) -> WarehouseTransferRequest:
        """Point the request at the SAP transfer that moved its stock."""
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
            request.posted_at = posted_at or timezone.now()
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

    def _adopt_after_failure(
        self, request: WarehouseTransferRequest, *, is_second_leg: bool
    ) -> WarehouseTransferRequest | None:
        """`_adopt_lost_post` once a post has failed — never masking that failure.

        If the lookup cannot reach SAP either, the post's own error is the one
        the operator needs, so the lookup's is logged and dropped.
        """
        try:
            return self._adopt_lost_post(request, is_second_leg=is_second_leg)
        except (SAPConnectionError, SAPDataError) as exc:
            logger.warning(
                "Transfer request %s: could not check SAP for a lost post: %s",
                request.entry_no, exc,
            )
            return None

    def _adopt_lost_post(
        self, request: WarehouseTransferRequest, *, is_second_leg: bool
    ) -> WarehouseTransferRequest | None:
        """Record a transfer SAP already made for this request, if its reply was lost.

        SAP can commit a transfer and never send the answer: the Service Layer
        hangs, the browser gives up at 30 s, a restart kills the worker still
        waiting, and the app's half rolls back. TR-20261003-0002 sat "not
        posted" for four days that way while its 6,900 PCS had moved, and the
        next press of Post was refused because the first had used SAP's request
        up. So before posting, and again when SAP refuses, look for the app's
        own transfer — its comment and its base document name it exactly — and
        record it instead of posting a second.

        Only the app's own document is adopted. A transfer keyed against the
        same request in the SAP client is someone else's decision about what
        moved, and stays theirs to reconcile.
        """
        if is_second_leg:
            base_type, base_entry = BASE_TYPE_STOCK_TRANSFER, request.sap_transfer_doc_entry
            comments = f"App transfer request {request.entry_no} — leg 2"
        else:
            base_type, base_entry = BASE_TYPE_TRANSFER_REQUEST, request.sap_request_doc_entry
            comments = f"App transfer request {request.entry_no}"
        if not base_entry:
            return None

        found = HanaStockTransferReader(self.client.context).find_by_base(
            base_type, base_entry, comments
        )
        if not found:
            return None

        created_at = datetime.combine(
            found['doc_date'],
            time(*divmod(found['create_ts'] // 100, 100), found['create_ts'] % 100),
            tzinfo=SAP_TIME_ZONE,
        )
        logger.warning(
            "Transfer request %s: SAP already holds %s %s from %s; recording it "
            "instead of posting again",
            request.entry_no, "leg 2" if is_second_leg else "transfer",
            found['doc_num'], created_at,
        )
        request = self._record_posted(
            request, found['doc_entry'], found['doc_num'],
            is_second_leg=is_second_leg, posted_at=created_at,
        )
        if not is_second_leg:
            self._persist_transfer_lines(request, [
                {
                    'line_num': line['base_line'],
                    'quantity': line['quantity'],
                    'batches': line['batches'],
                }
                for line in found['lines']
                if line['base_line'] is not None
            ])
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
