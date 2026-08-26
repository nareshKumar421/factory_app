"""Post inventory transfers (OWTR) to SAP via the Service Layer.

The payload shape below was proven by posting into `TEST_JIVO_OIL_HANADB`:
DocEntry 21327 (plain) and 21328 (with a two-batch split, which landed in `IBT1`
exactly as sent). Both were then cancelled and the stock restored.

Notes that matter and are easy to get wrong:

* lines are `StockTransferLines`, **not** `DocumentLines`
* `FromWarehouse` maps to `OWTR."Filler"`; the header pair is only a default and
  each line carries its own `FromWarehouseCode`/`WarehouseCode`, which genuinely
  differ in production (387 multi-source and 108 multi-destination documents)
* `Series` must be the one for the posting month — resolve it, never hardcode
* send no prices: `Price` stays 0 and SAP derives `StockPrice` itself
* no bin allocations — bins are switched off in all three companies
"""

import logging
from datetime import date
from typing import Optional

from .transfer_base import TransferDocumentWriter

logger = logging.getLogger(__name__)

# SAP BaseType for a line copied from an inventory transfer request
BASE_TYPE_TRANSFER_REQUEST = 1250000001
# SAP BaseType for a line copied from another inventory transfer (leg 2 of a
# cross-branch move, out of the in-transit warehouse)
BASE_TYPE_STOCK_TRANSFER = 67


class StockTransferWriter(TransferDocumentWriter):
    entity_set = "StockTransfers"
    label = "inventory transfer"


def build_stock_transfer_payload(
    *,
    series: int,
    branch_id: Optional[int],
    from_warehouse: str,
    to_warehouse: str,
    lines: list[dict],
    posting_date: Optional[date] = None,
    comments: str = "",
    journal_memo: str = "Stock Transfers -",
    card_code: str = "",
) -> dict:
    """Assemble a `StockTransfers` payload.

    `lines` items accept:
        item_code      (str, required)
        quantity       (Decimal/float, required)
        from_warehouse (str, optional — defaults to the header)
        to_warehouse   (str, optional — defaults to the header)
        batches        (list of {"BatchNumber", "Quantity"}, optional)
        base_type / base_entry / base_line  (optional link to ITR or leg 1)
    """
    posting_date = posting_date or date.today()
    stamp = posting_date.isoformat()

    payload: dict = {
        "DocDate": stamp,
        "TaxDate": stamp,
        "DueDate": stamp,
        "Series": int(series),
        "FromWarehouse": from_warehouse,
        "ToWarehouse": to_warehouse,
        "JournalMemo": journal_memo,
        "Comments": comments,
        "StockTransferLines": [],
    }
    if branch_id is not None:
        payload["BPLID"] = int(branch_id)
    if card_code:
        # Only set where SAP demands it — e.g. production wastage into BH-GR
        # requires CUSTA000940, and that card is invalid on any other route.
        payload["CardCode"] = card_code

    for index, line in enumerate(lines):
        entry: dict = {
            "ItemCode": line["item_code"],
            "Quantity": line["quantity"],
            "FromWarehouseCode": line.get("from_warehouse") or from_warehouse,
            "WarehouseCode": line.get("to_warehouse") or to_warehouse,
        }

        base_type = line.get("base_type")
        if base_type is not None:
            entry["BaseType"] = int(base_type)
            entry["BaseEntry"] = int(line["base_entry"])
            entry["BaseLine"] = int(line["base_line"])

        batches = line.get("batches") or []
        if batches:
            # BaseLineNumber ties each split to its own transfer line.
            entry["BatchNumbers"] = [
                {
                    "BatchNumber": batch["BatchNumber"],
                    "Quantity": batch["Quantity"],
                    "BaseLineNumber": index,
                }
                for batch in batches
            ]

        payload["StockTransferLines"].append(entry)

    return payload
