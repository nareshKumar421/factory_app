"""What *this app* has already promised out of a warehouse.

`OITW."IsCommited"` is the obvious number to net off on-hand, and it is the
wrong one. It counts every open document in SAP: sales orders raised against a
production warehouse, production-order components, and above all inventory
transfer requests nobody ever closed. SAP never expires a request, so those
liens are permanent — on 2026-09-21, 730 open request lines in Oil alone held
3.69M units, 3.30M of it (89%) on requests more than 90 days old. That is over
half of every commitment in the company, and some of it dates to 2024.

None of it stops a warehouse-to-warehouse move. SAP posts the transfer whatever
is committed, so netting `IsCommited` off made the app stricter than SAP for no
protection at all, against a figure poisoned by dead paperwork. It is what made
the picker offer 692 PCS of FG0000011 out of BH-PF while the SAP client showed
2,924 sitting there.

What the picker genuinely must not do is let two of *our own* requests claim the
same drums. That needs only our own open requests netted off, which is what this
module counts — from the app's own tables, so stale paper elsewhere in SAP can
never eat into what a warehouse is allowed to move. The real gate is still
server-side at posting, where batch allocation fails loudly if the stock has
gone.
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from django.db.models import Q

from ..models_transfer import (
    TransferLineStatus,
    TransferPostingStatus,
    TransferRequestStatus,
    WarehouseTransferRequestLine,
)

# Statuses where the request still expects to take stock out. REJECTED and
# CANCELLED release their reservation, so they hold nothing.
RESERVING_STATUSES = (
    TransferRequestStatus.PENDING,
    TransferRequestStatus.APPROVED,
    TransferRequestStatus.PARTIALLY_APPROVED,
)

# Once a leg is posted the stock has physically left the source, so `OnHand`
# already reflects it — subtracting again would double-count. Cross-branch
# counts here too: IN_TRANSIT means leg 1 moved the stock out.
SHIPPED_POSTING_STATUSES = (
    TransferPostingStatus.IN_TRANSIT,
    TransferPostingStatus.POSTED,
)


def _open_lines(company_code: str, warehouse: str):
    """Lines of this app's open requests that still take stock out of `warehouse`."""
    return (
        WarehouseTransferRequestLine.objects
        .filter(
            request__company__code=company_code,
            request__status__in=RESERVING_STATUSES,
        )
        .exclude(request__posting_status__in=SHIPPED_POSTING_STATUSES)
        .exclude(status=TransferLineStatus.REJECTED)
        # A line may name its own source — 387 live SAP documents ship from more
        # than one warehouse — and falls back to the request's when it does not.
        .filter(
            Q(from_warehouse=warehouse)
            | Q(from_warehouse="", request__from_warehouse=warehouse)
        )
    )


def _still_promised(requested, approved, transferred) -> Decimal:
    # The receiving warehouse may have approved less than was asked for;
    # once it has decided, its number is the one that holds stock.
    promised = approved if approved > 0 else requested
    return promised - transferred


def reserved_by_open_requests(
    company_code: str, warehouse: str, item_codes=None
) -> dict[str, Decimal]:
    """Item code -> quantity this app's own open requests hold at `warehouse`.

    Pass `item_codes` to scope the query to the picker's page of stock rather
    than every item the warehouse has ever been asked for.
    """
    warehouse = (warehouse or "").strip()
    if not warehouse:
        return {}

    lines = _open_lines(company_code, warehouse)
    if item_codes is not None:
        item_codes = list(item_codes)
        if not item_codes:
            return {}
        lines = lines.filter(item_code__in=item_codes)

    reserved: dict[str, Decimal] = defaultdict(Decimal)
    for item_code, requested, approved, transferred in lines.values_list(
        "item_code", "requested_qty", "approved_qty", "transferred_qty"
    ):
        outstanding = _still_promised(requested, approved, transferred)
        if outstanding > 0:
            reserved[item_code] += outstanding
    return dict(reserved)


def batches_held_by_open_requests(
    company_code: str, warehouse: str, item_code: str, *, exclude_request_id=None
) -> dict[str, Decimal]:
    """Batch number -> quantity of it our open requests were raised against.

    Only requests that picked their batches count; the rest take oldest first
    when posted and hold no batch in particular. Pass `exclude_request_id` when
    editing a request, so its own picks are not reported back as someone
    else's.
    """
    warehouse = (warehouse or "").strip()
    if not warehouse or not item_code:
        return {}

    lines = _open_lines(company_code, warehouse).filter(item_code=item_code)
    if exclude_request_id:
        lines = lines.exclude(request_id=exclude_request_id)

    held: dict[str, Decimal] = defaultdict(Decimal)
    for line in lines:
        if not line.chosen_batches:
            continue
        outstanding = _still_promised(
            line.requested_qty, line.approved_qty, line.transferred_qty
        )
        for split in line.chosen_split(outstanding):
            held[split["BatchNumber"]] += Decimal(str(split["Quantity"]))
    return dict(held)
