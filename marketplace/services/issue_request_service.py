"""Warehouse issue request for a batch's consolidated stock list.

Mirrors ``warehouse.services.warehouse_service`` BOM-Request semantics
(per-line partial approval → issue → receive) but scoped to the marketplace
consolidated stock list. Per ``MARKETPLACE_FLIPKART_SHEET_FLOW.md`` §4.3
**decision A**, the *issue* step is an internal picking/accountability record —
SAP stock is decremented once, later, by the dispatch Delivery Note. (Switching
to decision B = post a SAP stock transfer here is isolated to ``_post_issue``.)
"""
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from ..models import (
    MarketplaceIssueLine,
    MarketplaceIssueLineStatus,
    MarketplaceIssueRequest,
    MarketplaceIssueStatus,
    OrderImportBatch,
)
from .batch_resolve_service import build_stock_list
from .errors import MarketplaceError


def _snapshot_stock(company, warehouse_code, item_codes):
    """Best-effort on-hand snapshot from SAP. Returns {} when SAP is unreachable
    (dev/test/simulate) so the flow stays usable offline."""
    if not warehouse_code or not item_codes:
        return {}
    try:
        from warehouse.services.warehouse_service import WarehouseService

        rows = WarehouseService(company.code).get_stock_for_items(
            list(item_codes), warehouse_code
        )
        return {str(r["item_code"]).upper(): Decimal(str(r.get("available", 0))) for r in rows}
    except Exception:  # noqa: BLE001 — SAP/HANA optional; never block on it
        return {}


@transaction.atomic
def create_from_batch(batch, *, warehouse_code, user):
    """Create a SENT issue request from the batch's consolidated stock list."""
    stock = build_stock_list(batch)
    if stock["unmapped_skus"]:
        raise MarketplaceError(
            "Resolve all unmapped SKUs before sending to the warehouse.",
            code="UNMAPPED_SKUS", status_code=409, detail=stock["unmapped_skus"],
        )
    if not stock["lines"]:
        raise MarketplaceError(
            "The batch resolves to no stock lines.", code="EMPTY_STOCK_LIST", status_code=400,
        )

    request = MarketplaceIssueRequest.objects.create(
        company=batch.company, channel=batch.channel, batch=batch,
        sap_warehouse_code=warehouse_code or "",
        status=MarketplaceIssueStatus.SENT, created_by=user,
    )
    on_hand = _snapshot_stock(batch.company, warehouse_code, [l["item_code"] for l in stock["lines"]])
    for line in stock["lines"]:
        MarketplaceIssueLine.objects.create(
            request=request,
            item_code=line["item_code"], item_name=line["item_name"],
            component_type=line["component_type"], uom=line["uom"],
            required_qty=line["required_quantity"],
            available_stock=on_hand.get(line["item_code"].upper(), Decimal("0")),
            source_skus=line["source_skus"],
        )

    batch.status = OrderImportBatch.Status.REQUESTED
    batch.save(update_fields=["status"])
    return request


def _roll_up_status(request):
    lines = list(request.lines.all())
    approved = [l for l in lines if l.status in (
        MarketplaceIssueLineStatus.APPROVED, MarketplaceIssueLineStatus.PARTIALLY_APPROVED
    )]
    if not approved:
        return MarketplaceIssueStatus.REJECTED
    fully = all(
        l.status == MarketplaceIssueLineStatus.APPROVED and l.approved_qty >= l.required_qty
        for l in lines
    )
    return MarketplaceIssueStatus.APPROVED if fully else MarketplaceIssueStatus.PARTIALLY_APPROVED


@transaction.atomic
def review(request, *, decisions, user):
    """Apply per-line approve/reject. ``decisions`` = [{line_id, approved_qty, status, reason}]."""
    if request.status not in (
        MarketplaceIssueStatus.SENT, MarketplaceIssueStatus.APPROVED,
        MarketplaceIssueStatus.PARTIALLY_APPROVED,
    ):
        raise MarketplaceError(
            f"Cannot review a request in state {request.status}.",
            code="INVALID_STATE", status_code=409,
        )
    by_id = {l.id: l for l in request.lines.all()}
    for d in decisions:
        line = by_id.get(d.get("line_id"))
        if line is None:
            continue
        decision = (d.get("status") or "").upper()
        if decision == MarketplaceIssueLineStatus.REJECTED:
            line.approved_qty = Decimal("0")
            line.status = MarketplaceIssueLineStatus.REJECTED
            line.reject_reason = (d.get("reason") or "")[:255]
        else:
            qty = Decimal(str(d.get("approved_qty", line.required_qty)))
            if qty <= 0:
                raise MarketplaceError(
                    f"Approved qty must be > 0 for {line.item_code}.",
                    code="INVALID_QTY", status_code=400,
                )
            if qty > line.required_qty:
                raise MarketplaceError(
                    f"Approved qty exceeds required for {line.item_code}.",
                    code="INVALID_QTY", status_code=400,
                )
            line.approved_qty = qty
            line.status = (
                MarketplaceIssueLineStatus.APPROVED if qty >= line.required_qty
                else MarketplaceIssueLineStatus.PARTIALLY_APPROVED
            )
            line.reject_reason = ""
        line.save()

    request.status = _roll_up_status(request)
    request.reviewed_by = user
    request.reviewed_at = timezone.now()
    request.updated_by = user
    request.save(update_fields=["status", "reviewed_by", "reviewed_at", "updated_by", "updated_at"])
    return request


@transaction.atomic
def reject(request, *, reason, user):
    request.lines.update(
        approved_qty=Decimal("0"), status=MarketplaceIssueLineStatus.REJECTED,
        reject_reason=(reason or "")[:255],
    )
    request.status = MarketplaceIssueStatus.REJECTED
    request.reject_reason = (reason or "")[:255]
    request.reviewed_by = user
    request.reviewed_at = timezone.now()
    request.save()
    return request


def _post_issue(request):
    """Decision A: internal issue, no SAP doc. (Decision B would post a stock
    transfer here and append to ``request.sap_issue_doc_entries``.)"""
    return []


@transaction.atomic
def issue(request, *, user):
    """Mark the approved quantities as physically issued to packing."""
    if request.status not in (
        MarketplaceIssueStatus.APPROVED, MarketplaceIssueStatus.PARTIALLY_APPROVED
    ):
        raise MarketplaceError(
            "Only an approved request can be issued.", code="INVALID_STATE", status_code=409,
        )
    for line in request.lines.all():
        line.issued_qty = line.approved_qty
        line.save(update_fields=["issued_qty"])
    docs = _post_issue(request)
    if docs:
        request.sap_issue_doc_entries = (request.sap_issue_doc_entries or []) + docs
    request.status = MarketplaceIssueStatus.ISSUED
    request.updated_by = user
    request.save()
    request.batch.status = OrderImportBatch.Status.ISSUED
    request.batch.save(update_fields=["status"])
    return request


@transaction.atomic
def receive(request, *, receipts=None, user=None):
    """Packing confirms receipt. ``receipts`` = optional [{line_id, received_qty}];
    defaults to received == issued."""
    by_id = {l.id: l for l in request.lines.all()}
    receipt_map = {r["line_id"]: Decimal(str(r["received_qty"])) for r in (receipts or [])}
    for line in by_id.values():
        line.received_qty = receipt_map.get(line.id, line.issued_qty)
        line.save(update_fields=["received_qty"])
    request.status = MarketplaceIssueStatus.RECEIVED
    request.updated_by = user
    request.save(update_fields=["status", "updated_by", "updated_at"])
    return request
