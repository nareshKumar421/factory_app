"""Actions on SAP production orders: create, release, close, issue, receive.

Ported from SAP Portal (``backend_v1/routes/sap.js`` ~997–1315). The portal
forwarded whatever the page built; this checks up front what SAP would refuse,
calls SAP last, and records the action after SAP accepted it
(``SapProductionOrderAction``).

SAP has no idempotency key on any of these, and a slow answer leaves the
operator unsure whether it went through. Beyond the confirmation on the screen,
an identical request from the same person within ``REPEAT_WINDOW`` is refused
unless they confirm it (``confirm_repeat``) — the double-click the portal had
no guard against.

Two portal behaviours are deliberately not ported:

* The branch defaulted to 2 ("FACTORY") when the page sent none. Here it is the
  branch of the order's own warehouse (``OWHS.BPLid``), and omitted when SAP
  has none, so SAP applies its own default rather than a guess.
* After a receipt the portal set Complete/Reject by updating SAP's ``IGN1``
  table directly. JI never writes SAP tables, so a receipt is SAP's default,
  Complete (see ``sap_client/docs/sap_portal_port.md``).
"""

import hashlib
import json
import logging
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from sap_client.client import SAPClient

from ..models_sap_orders import SapProductionOrderAction

logger = logging.getLogger(__name__)

REPEAT_WINDOW = timedelta(minutes=2)
QUANTITY_TOLERANCE = Decimal("0.000001")


class SapOrderError(Exception):
    """A request refused before SAP was asked. ``status`` is the HTTP code."""

    def __init__(self, message: str, status: int = 400, **extra):
        super().__init__(message)
        self.status = status
        self.extra = extra


def _fingerprint(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def _refuse_repeat(company, user, action: str, order_doc_entry, payload: dict, confirm_repeat: bool):
    if confirm_repeat:
        return
    since = timezone.now() - REPEAT_WINDOW
    fingerprint = _fingerprint(payload)
    recent = SapProductionOrderAction.objects.filter(
        company=company, action=action, created_by=user, created_at__gte=since,
    )
    # A create has no order yet (SAP assigns it), so it matches on the payload alone.
    if order_doc_entry is not None:
        recent = recent.filter(order_doc_entry=order_doc_entry)
    recent = recent.values_list("payload", flat=True)
    if any(_fingerprint(previous) == fingerprint for previous in recent):
        raise SapOrderError(
            "You posted exactly this to SAP less than two minutes ago. Check SAP before posting "
            "it again, or confirm that you mean to post it twice.",
            status=409,
            code="REPEAT_POST",
        )


def _record(company, user, action, *, order_doc_entry=None, item_code="", quantity=None, result=None, payload=None):
    result = result or {}
    return SapProductionOrderAction.objects.create(
        company=company,
        action=action,
        order_doc_entry=order_doc_entry,
        item_code=item_code or "",
        quantity=quantity,
        sap_doc_entry=result.get("DocEntry") or None,
        sap_doc_num=int(result["DocNum"]) if str(result.get("DocNum") or "").isdigit() else None,
        pending_approval_draft=result.get("draft_entry") if result.get("pending_approval") else None,
        payload=payload or {},
        created_by=user,
    )


def _order(client: SAPClient, doc_entry: int) -> dict:
    order = client.sap_production_order(doc_entry)
    if order is None:
        raise SapOrderError(f"Production order {doc_entry} was not found in SAP.", status=404)
    return order


def _date(value):
    return value.isoformat() if value else None


# ---------------------------------------------------------------------------
# Create / release / close
# ---------------------------------------------------------------------------


def create_order(company, user, data: dict, confirm_repeat: bool = False) -> dict:
    """POST ProductionOrders. Without lines SAP builds them from the item's BOM."""
    payload = {
        "ItemNo": data["item_code"],
        "PlannedQuantity": float(data["planned_quantity"]),
        "DueDate": _date(data["due_date"]),
    }
    if data.get("start_date"):
        payload["StartDate"] = _date(data["start_date"])
    if data.get("warehouse"):
        payload["Warehouse"] = data["warehouse"]
    if data.get("remarks"):
        payload["Remarks"] = data["remarks"]
    if data.get("release"):
        payload["ProductionOrderStatus"] = "boposReleased"
    lines = data.get("lines") or []
    if lines:
        payload["ProductionOrderLines"] = [
            {
                "ItemNo": line["item_code"],
                "PlannedQuantity": float(line["planned_quantity"]),
                **({"Warehouse": line["warehouse"]} if line.get("warehouse") else {}),
                "VisualOrder": index,
            }
            for index, line in enumerate(lines)
        ]
    _refuse_repeat(company, user, SapProductionOrderAction.Action.CREATE, None, payload, confirm_repeat)
    result = SAPClient(company_code=company.code).create_production_order(payload)
    _record(
        company, user, SapProductionOrderAction.Action.CREATE,
        order_doc_entry=result.get("DocEntry"), item_code=data["item_code"],
        quantity=data["planned_quantity"], result=result, payload=payload,
    )
    return {"doc_entry": result.get("DocEntry"), "doc_num": result.get("DocNum")}


def release_order(company, user, doc_entry: int) -> None:
    client = SAPClient(company_code=company.code)
    order = _order(client, doc_entry)
    if order["status"] != "P":
        raise SapOrderError(f"Only a planned order can be released; this one is {order['status_label'].lower()}.")
    client.release_production_order(doc_entry)
    _record(company, user, SapProductionOrderAction.Action.RELEASE, order_doc_entry=doc_entry,
            item_code=order["item_code"])


def close_order(company, user, doc_entry: int) -> None:
    client = SAPClient(company_code=company.code)
    order = _order(client, doc_entry)
    if order["status"] != "R":
        raise SapOrderError(f"Only a released order can be closed; this one is {order['status_label'].lower()}.")
    client.close_production_order(doc_entry)
    _record(company, user, SapProductionOrderAction.Action.CLOSE, order_doc_entry=doc_entry,
            item_code=order["item_code"])


# ---------------------------------------------------------------------------
# Issue / receipt
# ---------------------------------------------------------------------------


def _batch_rows(batches, quantity: Decimal, what: str) -> list[dict]:
    rows = [b for b in batches or [] if b.get("batch_number") and Decimal(str(b.get("quantity") or 0)) > 0]
    total = sum((Decimal(str(b["quantity"])) for b in rows), Decimal("0"))
    if abs(total - quantity) > QUANTITY_TOLERANCE:
        raise SapOrderError(f"{what} is batch managed: its batches must add up to {quantity} (they add up to {total}).")
    return [{"BatchNumber": b["batch_number"], "Quantity": float(b["quantity"])} for b in rows]


def issue(company, user, doc_entry: int, data: dict, confirm_repeat: bool = False) -> dict:
    """InventoryGenExits consuming component lines of a released order."""
    client = SAPClient(company_code=company.code)
    order = _order(client, doc_entry)
    if order["status"] != "R":
        raise SapOrderError("Materials can be issued only to a released order.")
    lines_by_num = {line["line_num"]: line for line in order["lines"]}
    document_lines = []
    for requested in data["lines"]:
        line = lines_by_num.get(requested["line_num"])
        if line is None:
            raise SapOrderError(f"Line {requested['line_num']} is not on production order {doc_entry}.")
        quantity = Decimal(str(requested["quantity"]))
        entry = {
            "BaseType": 202,
            "BaseEntry": int(doc_entry),
            "BaseLine": int(requested["line_num"]),
            "Quantity": float(quantity),
        }
        warehouse = requested.get("warehouse") or line["warehouse"]
        if warehouse:
            entry["WarehouseCode"] = warehouse
        if line["batch_managed"]:
            entry["BatchNumbers"] = _batch_rows(requested.get("batches"), quantity, line["item_code"])
        document_lines.append(entry)
    payload = {"DocumentLines": document_lines}
    if order.get("branch_id"):
        payload["BPL_IDAssignedToInvoice"] = order["branch_id"]
    if data.get("posting_date"):
        payload["DocDate"] = _date(data["posting_date"])
    if data.get("remarks"):
        payload["Comments"] = data["remarks"]
    _refuse_repeat(company, user, SapProductionOrderAction.Action.ISSUE, doc_entry, payload, confirm_repeat)
    result = client.issue_for_production(payload)
    _record(
        company, user, SapProductionOrderAction.Action.ISSUE, order_doc_entry=doc_entry,
        item_code=order["item_code"],
        quantity=sum((Decimal(str(line["quantity"])) for line in data["lines"]), Decimal("0")),
        result=result, payload=payload,
    )
    return result


def receipt(company, user, doc_entry: int, data: dict, confirm_repeat: bool = False) -> dict:
    """InventoryGenEntries receiving the finished product of a released order."""
    client = SAPClient(company_code=company.code)
    order = _order(client, doc_entry)
    if order["status"] != "R":
        raise SapOrderError("Finished goods can be received only against a released order.")
    quantity = Decimal(str(data["quantity"]))
    line = {"BaseType": 202, "BaseEntry": int(doc_entry), "Quantity": float(quantity)}
    warehouse = data.get("warehouse") or order["warehouse"]
    if warehouse:
        line["WarehouseCode"] = warehouse
    if client.batch_managed_flags([order["item_code"]]).get(order["item_code"]):
        if not data.get("batch_number"):
            raise SapOrderError(f"{order['item_code']} is batch managed: give the batch number to receive into.")
        line["BatchNumbers"] = [{"BatchNumber": data["batch_number"], "Quantity": float(quantity)}]
    payload = {"DocumentLines": [line]}
    if order.get("branch_id"):
        payload["BPL_IDAssignedToInvoice"] = order["branch_id"]
    if data.get("posting_date"):
        payload["DocDate"] = _date(data["posting_date"])
    if data.get("remarks"):
        payload["Comments"] = data["remarks"]
    _refuse_repeat(company, user, SapProductionOrderAction.Action.RECEIPT, doc_entry, payload, confirm_repeat)
    result = client.receipt_from_production(payload)
    _record(
        company, user, SapProductionOrderAction.Action.RECEIPT, order_doc_entry=doc_entry,
        item_code=order["item_code"], quantity=quantity, result=result, payload=payload,
    )
    return result
