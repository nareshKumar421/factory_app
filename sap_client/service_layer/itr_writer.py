"""Post inventory transfer requests (OWTQ) to SAP via the Service Layer.

A request moves no stock but it does reserve it: an open line adds to
`OITW."IsCommited"` at the source and `OITW."OnOrder"` at the destination. That
reservation is the point — it stops two approvals promising the same stock while
each waits for a decision.

The corresponding obligation: SAP never expires a request. Whatever the app
raises, the app must also retire — always with `close()`, which is the only
retirement SAP allows here (see below).

Two behaviours established by posting into `TEST_JIVO_OIL_HANADB`, both of which
contradict what the OData catalogue implies:

* **`Cancel` is not supported on this entity.** SAP answers `-5006 "The
  requested action is not supported for this object."` even for a brand-new
  request with every line still open. `Close` is the only way to retire one: it
  sets `DocStatus='C'`, closes the lines, zeroes `OpenQty` and releases the
  reservation, while leaving `CANCELED='N'`. So a request the app rejected is
  indistinguishable in SAP from one that was closed for any other reason — the
  app stays the system of record for *why*.
* **Cancelling the transfer does not reopen the request.** The reversing
  document restores the stock but does not restore `OpenQty`, so a consumed
  request stays consumed. A retry after a cancelled transfer needs a new
  request, not a re-post against the old one.
"""

import logging
from datetime import date
from typing import Optional

from .transfer_base import TransferDocumentWriter
from ..exceptions import SAPValidationError

logger = logging.getLogger(__name__)


class InventoryTransferRequestWriter(TransferDocumentWriter):
    entity_set = "InventoryTransferRequests"
    label = "inventory transfer request"

    def cancel(self, doc_entry: int) -> None:
        """Not available — SAP rejects Cancel on this entity with -5006.

        Raised as a programming error rather than passed through to SAP so the
        caller is told what to do instead.
        """
        raise SAPValidationError(
            "SAP does not support cancelling an inventory transfer request "
            f"(tried on DocEntry {doc_entry}). Use close() instead — it "
            "releases the reservation and closes the lines."
        )


def build_transfer_request_payload(
    *,
    series: int,
    branch_id: Optional[int],
    from_warehouse: str,
    to_warehouse: str,
    lines: list[dict],
    posting_date: Optional[date] = None,
    due_date: Optional[date] = None,
    comments: str = "",
    journal_memo: str = "Inventory Transfer Request -",
) -> dict:
    """Assemble an `InventoryTransferRequests` payload.

    Lines take `item_code`, `quantity`, and optional per-line
    `from_warehouse`/`to_warehouse`. No batch allocation here — a request only
    states what is wanted; batches are chosen when the transfer is posted.
    """
    posting_date = posting_date or date.today()
    stamp = posting_date.isoformat()
    due_stamp = (due_date or posting_date).isoformat()

    payload: dict = {
        "DocDate": stamp,
        "TaxDate": stamp,
        "DueDate": due_stamp,
        "Series": int(series),
        "FromWarehouse": from_warehouse,
        "ToWarehouse": to_warehouse,
        "JournalMemo": journal_memo,
        "Comments": comments,
        "StockTransferLines": [
            {
                "ItemCode": line["item_code"],
                "Quantity": line["quantity"],
                "FromWarehouseCode": line.get("from_warehouse") or from_warehouse,
                "WarehouseCode": line.get("to_warehouse") or to_warehouse,
            }
            for line in lines
        ],
    }
    if branch_id is not None:
        payload["BPLID"] = int(branch_id)

    return payload
