"""Actions on SAP production orders: create, release, close, issue, receive.

Ported from SAP Portal (``backend_v1/routes/sap.js`` ~997–1315). The portal
forwarded whatever the page built; this checks up front what SAP would refuse,
calls SAP last, and records every action (``SapProductionOrderAction``).

SAP has no idempotency key on any of these, and a slow answer leaves the
operator unsure whether it went through. So a create, issue or receipt
(:func:`_post_once`):

* is **claimed before SAP is asked** — a ``POSTING`` row, committed, which a
  partial unique constraint allows once per payload. A second identical
  posting while the first is in flight (two tabs, two operators) is refused
  (409 ``POSTING_IN_PROGRESS``) without reaching SAP;
* ends ``DONE``, ``FAILED`` when SAP refused it (nothing was posted, so it does
  not count as a repeat) or ``UNKNOWN`` when SAP did not answer — a timed-out
  write may still have committed;
* is refused (409) when it repeats a ``DONE`` posting of the same person within
  ``REPEAT_WINDOW`` (``REPEAT_POST``) or an ``UNKNOWN`` one of anybody within
  ``UNCERTAIN_WINDOW`` (``UNCERTAIN_POST``), unless the operator confirms
  (``confirm_repeat``) after checking SAP.

A ``POSTING`` row older than ``POSTING_STALE_AFTER`` (the process died
mid-call) is retired to ``UNKNOWN`` so it cannot block that payload for ever.

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

from django.db import IntegrityError, transaction
from django.utils import timezone

from sap_client.client import SAPClient
from sap_client.exceptions import SAPValidationError

from ..models_sap_orders import SapProductionOrderAction

logger = logging.getLogger(__name__)

REPEAT_WINDOW = timedelta(minutes=2)
# Long enough to cover checking SAP after a timeout, short enough not to block
# a genuine second posting of the same quantity later in the shift.
UNCERTAIN_WINDOW = timedelta(minutes=30)
# Past the Service Layer's 120 s write timeout (entity_client.WRITE_TIMEOUT_SECONDS).
POSTING_STALE_AFTER = timedelta(minutes=5)
QUANTITY_TOLERANCE = Decimal("0.000001")

Outcome = SapProductionOrderAction.Outcome


class SapOrderError(Exception):
    """A request refused before SAP was asked. ``status`` is the HTTP code."""

    def __init__(self, message: str, status: int = 400, **extra):
        super().__init__(message)
        self.status = status
        self.extra = extra


def _fingerprint(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def _retire_stale_postings(company) -> None:
    """A POSTING row older than any SAP call can take was left by a dead process:
    SAP may or may not have posted it, which is exactly UNKNOWN."""
    SapProductionOrderAction.objects.filter(
        company=company, outcome=Outcome.POSTING,
        created_at__lt=timezone.now() - POSTING_STALE_AFTER,
    ).update(outcome=Outcome.UNKNOWN, error="The app stopped before SAP answered.")


def _refuse_repeat(company, user, action: str, fingerprint: str, confirm_repeat: bool):
    same = SapProductionOrderAction.objects.filter(company=company, action=action, fingerprint=fingerprint)
    if same.filter(outcome=Outcome.POSTING).exists():
        raise SapOrderError(
            "This exact posting is being sent to SAP right now. Wait for it to finish, then "
            "check the order before posting again.",
            status=409,
            code="POSTING_IN_PROGRESS",
        )
    if confirm_repeat:
        return
    now = timezone.now()
    unanswered = same.filter(outcome=Outcome.UNKNOWN, created_at__gte=now - UNCERTAIN_WINDOW).first()
    if unanswered is not None:
        at = timezone.localtime(unanswered.created_at).strftime("%H:%M")
        raise SapOrderError(
            f"This exact posting was sent to SAP at {at} and SAP did not answer, so it may have "
            "gone through. Check the order in SAP, then confirm to post it again.",
            status=409,
            code="UNCERTAIN_POST",
        )
    if same.filter(outcome=Outcome.DONE, created_by=user, created_at__gte=now - REPEAT_WINDOW).exists():
        raise SapOrderError(
            "You posted exactly this to SAP less than two minutes ago. Check SAP before posting "
            "it again, or confirm that you mean to post it twice.",
            status=409,
            code="REPEAT_POST",
        )


def _post_once(company, user, action, payload: dict, send, *, order_doc_entry=None, item_code="",
               quantity=None, confirm_repeat=False) -> dict:
    """Claim the payload, send it to SAP, record how it ended; returns SAP's answer."""
    fingerprint = _fingerprint(payload)
    _retire_stale_postings(company)
    _refuse_repeat(company, user, action, fingerprint, confirm_repeat)
    try:
        with transaction.atomic():
            row = SapProductionOrderAction.objects.create(
                company=company, action=action, order_doc_entry=order_doc_entry,
                item_code=item_code or "", quantity=quantity, payload=payload,
                fingerprint=fingerprint, outcome=Outcome.POSTING, created_by=user,
            )
    except IntegrityError:
        # Another request claimed the same payload between the check and here.
        raise SapOrderError(
            "This exact posting is being sent to SAP right now. Wait for it to finish, then "
            "check the order before posting again.",
            status=409,
            code="POSTING_IN_PROGRESS",
        )
    try:
        result = send(payload) or {}
    except SAPValidationError as e:
        # SAP answered, and refused: nothing was posted.
        row.outcome, row.error = Outcome.FAILED, str(e)[:500]
        row.save(update_fields=["outcome", "error", "updated_at"])
        raise
    except Exception as e:
        # No answer (timeout, lost connection) or an answer we could not read:
        # SAP may have posted it.
        row.outcome, row.error = Outcome.UNKNOWN, str(e)[:500] or type(e).__name__
        row.save(update_fields=["outcome", "error", "updated_at"])
        raise
    row.outcome = Outcome.DONE
    row.order_doc_entry = order_doc_entry or result.get("DocEntry") or None
    row.sap_doc_entry = result.get("DocEntry") or None
    row.sap_doc_num = int(result["DocNum"]) if str(result.get("DocNum") or "").isdigit() else None
    row.pending_approval_draft = result.get("draft_entry") if result.get("pending_approval") else None
    row.save()
    return result


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
    client = SAPClient(company_code=company.code)
    result = _post_once(
        company, user, SapProductionOrderAction.Action.CREATE, payload, client.create_production_order,
        item_code=data["item_code"], quantity=data["planned_quantity"], confirm_repeat=confirm_repeat,
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
    return _post_once(
        company, user, SapProductionOrderAction.Action.ISSUE, payload, client.issue_for_production,
        order_doc_entry=doc_entry, item_code=order["item_code"],
        quantity=sum((Decimal(str(line["quantity"])) for line in data["lines"]), Decimal("0")),
        confirm_repeat=confirm_repeat,
    )


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
    return _post_once(
        company, user, SapProductionOrderAction.Action.RECEIPT, payload, client.receipt_from_production,
        order_doc_entry=doc_entry, item_code=order["item_code"], quantity=quantity,
        confirm_repeat=confirm_repeat,
    )
