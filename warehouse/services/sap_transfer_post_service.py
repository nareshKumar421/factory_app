"""Post the actual stock transfer against a SAP-raised transfer request.

Approving an inventory transfer *request* (``ObjType 1250000001``) does not move
anything: it leaves an open ``OWTQ`` whose lines reserve stock until real
inventory transfers (``OWTR``) are posted against them, drawing ``OpenQty``
down until the request closes. SAP allows that in as many parts as it takes —
in Oil this year every posted request carries one to three transfers — so the
caller supplies a quantity per line rather than an all-or-nothing button.

This is the SAP-raised twin of ``transfer_request_service``. That one posts the
app's own requests and is keyed on a ``WarehouseTransferRequest`` row; requests
raised in the SAP client have no such row, so everything here is keyed on the
SAP ``DocEntry`` and read live from HANA.

**Same-branch only.** A branch-crossing move has to travel through an
``*-INT`` warehouse as two separate legs, which the app's own flow models with
a local record to track the leg in between. Rather than half-implement that
against a document we do not own, a cross-branch request is refused by name and
left to SAP. It is a rounding error in practice: of 526 Oil transfer requests
raised this year, exactly one crossed branches.
"""

from decimal import Decimal, InvalidOperation

from django.utils import timezone

from sap_client.client import SAPClient
from sap_client.hana.series_reader import HanaSeriesReader
from sap_client.hana.transfer_request_reader import HanaTransferRequestReader
from sap_client.service_layer.stock_transfer_writer import (
    BASE_TYPE_TRANSFER_REQUEST,
    build_stock_transfer_payload,
)

from .warehouse_scope import assert_manages

ZERO = Decimal("0")

# WTQ1.LineStatus for a line that still owes stock.
LINE_OPEN = "O"


class SapTransferPostError(Exception):
    """Something about the request or the quantities makes the post impossible."""


def _decimal(value, label: str) -> Decimal:
    try:
        quantity = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise SapTransferPostError(f"{label} is not a number: {value!r}")
    if quantity < ZERO:
        raise SapTransferPostError(f"{label} cannot be negative.")
    return quantity


class SapTransferPostService:
    """List SAP transfer requests still owing stock, and post transfers for them."""

    def __init__(self, company_code: str, user):
        self.company_code = company_code
        self.user = user
        self.client = SAPClient(company_code=company_code)

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def _reader(self) -> HanaTransferRequestReader:
        return HanaTransferRequestReader(self.client.context)

    def list_awaiting_transfer(self, limit: int = 200) -> list[dict]:
        """Approved requests with stock still owed, newest first.

        Company-wide for the same reason the approval queue is: a request's two
        warehouses have different managers and whoever must post it is often
        neither, so filtering by the caller's warehouses would hide rows nobody
        could then find. Whether the caller may *post* each one is reported per
        row instead.
        """
        reader = self._reader()
        rows = reader.list_open_requests(limit=limit)
        branches = self.client.get_warehouse_branches()
        manageable = self._manageable_warehouses()

        out = []
        for row in rows:
            detail = reader.get_request(row["doc_entry"])
            if detail is None:
                continue
            open_lines = [
                line for line in detail["lines"] if line["line_status"] == LINE_OPEN
            ]
            if not open_lines:
                continue

            from_whs = row["from_warehouse"]
            to_whs = row["to_warehouse"]
            from_branch = branches.get(from_whs)
            to_branch = branches.get(to_whs)
            cross_branch = (
                from_branch is not None
                and to_branch is not None
                and from_branch != to_branch
            )
            # Posting moves stock OUT of the source, so it is the source
            # warehouse's manager who may do it.
            may_post = manageable is None or from_whs.upper() in manageable

            out.append({
                "doc_entry": row["doc_entry"],
                "doc_num": row["doc_num"],
                "doc_date": row["doc_date"].isoformat() if row["doc_date"] else None,
                "from_warehouse": from_whs,
                "to_warehouse": to_whs,
                "comments": row["comments"] or None,
                "age_days": row["age_days"],
                "cross_branch": cross_branch,
                "can_post": bool(may_post and not cross_branch),
                # Named so the page can say why, rather than just greying out.
                "blocked_reason": self._blocked_reason(cross_branch, may_post, from_whs),
                "lines": [
                    {
                        "line_num": line["line_num"],
                        "item_code": line["item_code"],
                        "item_name": line["item_name"],
                        "uom": line["uom"],
                        "quantity": str(line["quantity"]),
                        "open_quantity": str(line["open_quantity"]),
                        "served_quantity": str(line["served_quantity"]),
                        "from_warehouse": line["from_warehouse"] or from_whs,
                        "to_warehouse": line["to_warehouse"] or to_whs,
                    }
                    for line in open_lines
                ],
            })
        return out

    @staticmethod
    def _blocked_reason(cross_branch: bool, may_post: bool, from_whs: str):
        if cross_branch:
            return (
                "This request crosses SAP branches, so the stock has to move in two "
                "legs through an in-transit warehouse. Post it in SAP."
            )
        if not may_post:
            return f"You do not manage {from_whs}, the warehouse the stock leaves."
        return None

    def _manageable_warehouses(self):
        """Upper-cased warehouse codes the caller manages, or None if unrestricted."""
        from warehouse.models_manager import UserWarehouse

        if getattr(self.user, "is_superuser", False):
            return None
        return {
            code.strip().upper()
            for code in UserWarehouse.objects.filter(
                user=self.user, company__code=self.company_code, is_active=True
            ).values_list("warehouse_code", flat=True)
            if code
        }

    # ------------------------------------------------------------------
    # Post
    # ------------------------------------------------------------------

    def post_transfer(self, doc_entry: int, quantities: dict) -> dict:
        """Create one inventory transfer against request ``doc_entry``.

        ``quantities`` maps ``WTQ1.LineNum`` to how much to move now. A line
        left out, or set to zero, is simply not moved — the request stays open
        for it. Every quantity is checked against the line's live ``OpenQty``
        rather than anything the browser sent, because a concurrent transfer in
        SAP may already have taken part of it.
        """
        reader = self._reader()
        request = reader.get_request(int(doc_entry))
        if request is None:
            raise SapTransferPostError(
                f"Transfer request {doc_entry} was not found in SAP."
            )
        if request.get("cancelled"):
            raise SapTransferPostError("That transfer request is cancelled in SAP.")
        if not request.get("is_open"):
            raise SapTransferPostError(
                "That transfer request has nothing left to transfer."
            )

        from_whs = request.get("from_warehouse") or ""
        to_whs = request.get("to_warehouse") or ""

        branches = self.client.get_warehouse_branches()
        from_branch = branches.get(from_whs)
        to_branch = branches.get(to_whs)
        if from_branch is not None and to_branch is not None and from_branch != to_branch:
            raise SapTransferPostError(
                f"{from_whs} and {to_whs} are in different SAP branches, so this "
                "move needs two legs through an in-transit warehouse. Post it in SAP."
            )

        # Moving stock out is the source warehouse's call.
        assert_manages(
            self.user, self.company_code, [from_whs],
            action="post a transfer out of this warehouse",
        )

        lines = self._build_lines(request, quantities)
        if not lines:
            raise SapTransferPostError(
                "Set a quantity on at least one line before posting."
            )

        posting_date = timezone.localdate()
        series = HanaSeriesReader(self.client.context).resolve_stock_transfer(posting_date)
        payload = build_stock_transfer_payload(
            series=series,
            branch_id=from_branch,
            from_warehouse=from_whs,
            to_warehouse=to_whs,
            lines=lines,
            posting_date=posting_date,
            comments=f"Against SAP transfer request {request.get('doc_num') or doc_entry}",
        )
        created = self.client.create_stock_transfer(payload)

        # Re-read so the caller sees what SAP now says is still owed, rather
        # than what we hoped it would say.
        after = reader.get_request(int(doc_entry))
        remaining = sum(
            (line["open_quantity"] for line in (after or {}).get("lines", [])), ZERO
        )
        return {
            "doc_entry": created.get("DocEntry"),
            "doc_num": created.get("DocNum"),
            "request_doc_entry": int(doc_entry),
            "request_closed": bool(after and not after.get("is_open")),
            "remaining_quantity": str(remaining),
            "lines_moved": len(lines),
        }

    def _build_lines(self, request: dict, quantities: dict) -> list[dict]:
        """Validate the asked-for quantities and choose batches for each line."""
        by_line = {line["line_num"]: line for line in request["lines"]}
        batch_flags = self.client.batch_managed_flags(
            [line["item_code"] for line in request["lines"]]
        )

        lines: list[dict] = []
        for raw_line_num, raw_quantity in (quantities or {}).items():
            try:
                line_num = int(raw_line_num)
            except (TypeError, ValueError):
                raise SapTransferPostError(f"Not a line number: {raw_line_num!r}")

            line = by_line.get(line_num)
            if line is None:
                raise SapTransferPostError(
                    f"Line {line_num} is not on transfer request "
                    f"{request.get('doc_num') or request.get('doc_entry')}."
                )

            quantity = _decimal(raw_quantity, f"Quantity for line {line_num}")
            if quantity <= ZERO:
                continue  # deliberately skipped, not an error
            if line["line_status"] != LINE_OPEN:
                raise SapTransferPostError(
                    f"Line {line_num} ({line['item_code']}) is already closed in SAP."
                )
            if quantity > line["open_quantity"]:
                raise SapTransferPostError(
                    f"Line {line_num} ({line['item_code']}) has only "
                    f"{line['open_quantity']} left to transfer, not {quantity}."
                )

            source = line["from_warehouse"] or request.get("from_warehouse") or ""
            entry = {
                "item_code": line["item_code"],
                "quantity": quantity,
                "from_warehouse": source,
                "to_warehouse": (
                    line["to_warehouse"] or request.get("to_warehouse") or ""
                ),
                "line_num": line_num,
                "uom": line["uom"],
                # Tie the movement to its request line so SAP draws the
                # reservation down instead of leaving it open alongside.
                "base_type": BASE_TYPE_TRANSFER_REQUEST,
                "base_entry": int(request["doc_entry"]),
                "base_line": line_num,
            }
            if batch_flags.get(line["item_code"]):
                entry["batches"] = self.client.allocate_batches_fifo(
                    line["item_code"], source, quantity
                )
            lines.append(entry)
        return lines
