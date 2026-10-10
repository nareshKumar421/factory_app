"""The Service Layer bodies for each step, as SAP's own rules need them.

What each field is for (read from live SAP, 2026-10-09):

* The order carries the month's series (``PRO``+MMYY). SAP's default series is
  a 2024 one, so leaving it out would number the order into the wrong period.
* Its lines are the BOM's, line for line: SAP refuses a standard order whose
  components, per-piece quantities or line count differ from the BOM (errors
  20205, 20206, 2020041, 2020014).
* Every line carries ``U_WASTAGE_QUANTITY``. The header's ``U_WASTAGE``
  defaults to 'Y', and SAP refuses an order with 'Y' and an empty wastage on
  any line (20200001). Oil records none, so it is 0.
* ``CustomerCode`` is the WIP vendor Oil's order screen fills by formatted
  search.
* Issue and receipt lines carry the variety (``CostingCode``, dimension 1) and
  the same code in ``U_SchemeAgst``, which the SAP screen copies from it.
  SAP refuses an Oil issue line without a variety (60003).
* The receipt's batch carries the production and expiry dates.
* Every document's ``Comments`` starts with the entry's reference, so a retry
  can find what an earlier try posted.
"""

from decimal import Decimal

from .constants import COMMENTS_MAX_LENGTH


def _date(value) -> str:
    return value.isoformat()


def _number(value) -> float:
    return float(value if isinstance(value, Decimal) else Decimal(str(value)))


def comments(entry) -> str:
    text = entry.sap_reference
    if entry.remarks:
        text = f"{text} {entry.remarks}"
    return text[:COMMENTS_MAX_LENGTH]


def order_payload(entry, *, series: int, card_code: str = "") -> dict:
    """``POST ProductionOrders``: a planned standard order with the BOM's lines.

    SAP creates production orders only as planned; the order step releases it next.
    """
    payload = {
        "ItemNo": entry.item_code,
        "ProductionOrderType": "bopotStandard",
        "ProductionOrderStatus": "boposPlanned",
        "PlannedQuantity": _number(entry.quantity),
        "PostingDate": _date(entry.posting_date),
        "StartDate": _date(entry.posting_date),
        "DueDate": _date(entry.posting_date),
        "Warehouse": entry.warehouse,
        "Series": int(series),
        "Remarks": comments(entry),
        "U_WASTAGE": "Y",
        "ProductionOrderLines": order_lines(
            {
                "item_code": line.item_code,
                "item_type": line.item_type,
                "base_quantity": line.base_quantity,
                "planned_quantity": line.planned_quantity,
                "warehouse": line.warehouse,
                "issue_method": line.issue_method,
            }
            for line in entry.lines.all()
        ),
    }
    if card_code:
        payload["CustomerCode"] = card_code
    return payload


def order_lines(lines) -> list[dict]:
    """The order's lines as SAP takes them: the BOM's, each with wastage 0."""
    return [
        {
            "ItemNo": line["item_code"],
            "ItemType": "pit_Resource" if line["item_type"] == "resource" else "pit_Item",
            "BaseQuantity": _number(line["base_quantity"]),
            "PlannedQuantity": _number(line["planned_quantity"]),
            "Warehouse": line["warehouse"],
            "ProductionOrderIssueType": "im_Backflush" if line["issue_method"] == "B" else "im_Manual",
            "U_WASTAGE_QUANTITY": 0,
        }
        for line in lines
    ]


#: The Service Layer replaces a collection sent in a PATCH only when asked;
#: otherwise the new BOM's lines would be added beside the old product's.
REPLACE_COLLECTIONS = {"B1S-ReplaceCollectionsOnPatch": "true"}


def replan_payload(built: dict, *, reference: str) -> dict:
    """``PATCH ProductionOrders(n)`` of a planned order: a new product, quantity
    or date, and the new product's BOM as its lines (sent with
    :data:`REPLACE_COLLECTIONS`)."""
    remarks = f"{reference} {built['remarks']}" if built.get("remarks") else reference
    return {
        "ItemNo": built["item_code"],
        "PlannedQuantity": _number(built["quantity"]),
        "PostingDate": _date(built["posting_date"]),
        "StartDate": _date(built["posting_date"]),
        "DueDate": _date(built["posting_date"]),
        "Warehouse": built["warehouse"],
        "Remarks": remarks[:COMMENTS_MAX_LENGTH],
        "ProductionOrderLines": order_lines(built["lines"]),
    }


def unrelease_payload() -> dict:
    """``PATCH ProductionOrders(n)``: back to planned, so it can be changed."""
    return {"ProductionOrderStatus": "boposPlanned"}


def issue_payload(entry, *, series: int, branch: int | None, lines: list[dict], doc_date) -> dict:
    """``POST InventoryGenExits`` against the order.

    ``lines``: ``[{"line": ProductionOrderEntryLine, "quantity": Decimal,
    "batches": [{"BatchNumber", "Quantity"}]}]``, one per order line.
    """
    document_lines = []
    for row in lines:
        line = row["line"]
        document_line = {
            "BaseType": 202,
            "BaseEntry": int(entry.sap_order_entry),
            "BaseLine": int(line.sap_line_num),
            "Quantity": _number(row["quantity"]),
            "WarehouseCode": line.warehouse,
            "CostingCode": entry.variety,
            "U_SchemeAgst": entry.variety,
        }
        if row.get("batches"):
            document_line["BatchNumbers"] = [
                {"BatchNumber": b["BatchNumber"], "Quantity": _number(b["Quantity"])}
                for b in row["batches"]
            ]
        document_lines.append(document_line)
    payload = {
        "Series": int(series),
        "DocDate": _date(doc_date),
        "Comments": comments(entry),
        "DocumentLines": document_lines,
    }
    if branch:
        payload["BPL_IDAssignedToInvoice"] = int(branch)
    return payload


def receipt_payload(entry, *, series: int, branch: int | None, batch_managed: bool, doc_date) -> dict:
    """``POST InventoryGenEntries``: the finished goods, complete, into the BOM's warehouse."""
    line = {
        "BaseType": 202,
        "BaseEntry": int(entry.sap_order_entry),
        "Quantity": _number(entry.quantity),
        "WarehouseCode": entry.warehouse,
        "TransactionType": "botrntComplete",
        "CostingCode": entry.variety,
        "U_SchemeAgst": entry.variety,
    }
    if batch_managed:
        line["BatchNumbers"] = [
            {
                "BatchNumber": entry.batch_number,
                "Quantity": _number(entry.quantity),
                "ManufacturingDate": _date(entry.mfg_date),
                "ExpiryDate": _date(entry.expiry_date),
            }
        ]
    payload = {
        "Series": int(series),
        "DocDate": _date(doc_date),
        "Comments": comments(entry),
        "DocumentLines": [line],
    }
    if branch:
        payload["BPL_IDAssignedToInvoice"] = int(branch)
    return payload


def close_payload(closing_date) -> dict:
    """``PATCH ProductionOrders(n)``: closed on the Close step's date, which is
    the date of the variance SAP posts."""
    return {"ProductionOrderStatus": "boposClosed", "ClosingDate": _date(closing_date)}


def release_payload() -> dict:
    """``PATCH ProductionOrders(n)``: released, so materials can be issued to it."""
    return {"ProductionOrderStatus": "boposReleased"}
