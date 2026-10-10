"""A truck's delivery, written to SAP: each bill's A/R invoice marked received.

SAP keeps a delivery on the invoice, not on the truck: ``OINV.U_Recv_Date``
(Receive Date), ``INV1.U_Recvd_Qty`` (Received Qty) on every line, and the signed
proof as the invoice's attachment. Until this, one person typed those in by hand
from scanned bills, weeks after the truck came back. A Delivered or Partially
Delivered tracking update now opens a :class:`TruckDispatchSapReceipt` for every
A/R invoice on the truck, and the SAP posting queue writes each one.

What ``SBO_SP_TRANSACTIONNOTIFICATION`` demands of that write (read from the
procedure itself; the app posts as ``B1i``, whom none of the rules exempt):

* ``130001002 Please Attach its Receiving`` -- a received date with no
  attachment is refused in Oil and Mart (the rule is commented out in
  Beverages). The attachment must arrive in the same update as the date, so the
  proof is uploaded first and the PATCH carries both. A delivery logged without
  a proof waits for one, in every company alike: the proof is what the received
  date stands on.
* ``1300013 Please update the received qty`` -- once the date is set, every line
  needs a received quantity above zero. A bill with an item that came back whole
  therefore cannot be recorded from here; it is left for SAP by hand.
* ``1300014`` -- the received date may not be earlier than the invoice's date.

Unlike the dispatch stamp, the received date is not write-once. Even so, a bill
SAP already shows as received is left exactly as SAP has it: someone entered it
there, and the app has no business overwriting that.

Received quantities are in the bill's own units (``INV1.Quantity``, which is what
the docking copied onto its items): a full delivery receives what left the gate
(the item's dispatched quantity after a short dispatch, else its quantity); a
partial delivery receives what the operator recorded as delivered for each short
item. The docking's items were created from SAP's lines in ``LineNum`` order, so
they are matched to SAP's lines by position, with the item code and quantity
checked -- a bill changed in SAP since it was loaded is refused, not guessed at.
"""

import logging
import os
from decimal import Decimal

import requests
from django.db import transaction
from django.utils import timezone

from gate_core.models import (
    SalesDispatchDocumentType,
    SalesDispatchGateOutStatus,
    TruckDispatchSapReceipt,
    TruckDispatchSapReceiptStatus as ReceiptStatus,
    TruckDispatchStatus,
    TruckDispatchUpdate,
)
from sap_client.exceptions import (
    SAPConnectionError,
    SAPDataError,
    SAPUnavailable,
    SAPValidationError,
)
from sap_postings.models import SapPostingOutcome
from sap_postings.services import Outcome

logger = logging.getLogger(__name__)

KIND = "dispatch_tracking.receive"

DELIVERY_STATUSES = (
    TruckDispatchStatus.DELIVERED,
    TruckDispatchStatus.PARTIALLY_DELIVERED,
)

# A bill with a receipt in one of these is settled with SAP, or on its way: a
# later delivery update on the truck does not open another for it.
SETTLED = (ReceiptStatus.WAITING, ReceiptStatus.POSTED, ReceiptStatus.ALREADY_RECEIVED)
# Never reached SAP: a later delivery update replaces them.
REPLACEABLE = (ReceiptStatus.NEEDS_PROOF, ReceiptStatus.REFUSED, ReceiptStatus.BY_HAND)


class ReceiptError(Exception):
    """Why a bill's delivery cannot be written to SAP as it stands."""


# ---------------------------------------------------------------------------
# opening receipts for a delivery update
# ---------------------------------------------------------------------------

def truck_invoices(arrival):
    """The A/R invoices riding on a truck: every company's, not just the user's.

    The delivery is a fact about the truck -- the timeline it is logged on is
    shared by every company on it -- so every invoice on it is received.
    Stock transfers are left out: SAP has the same fields on them, but nobody
    uses them, and the receiving branch books its own receipt.
    """
    documents = []
    for docking in arrival.gate_outs.all():
        if not docking.is_active or docking.status != SalesDispatchGateOutStatus.DISPATCHED:
            continue
        documents.extend(
            document
            for document in docking.documents.all()
            if document.is_active and document.document_type == SalesDispatchDocumentType.INVOICE
        )
    return documents


def received_date_for(update):
    return update.delivered_date or timezone.localdate(update.occurred_at)


def receipt_lines(document, delivered_by_item):
    """What each item of the bill is recorded as received, in SAP's line order."""
    items = sorted(
        (item for item in document.items.all() if item.is_active),
        key=lambda item: (item.line_num, item.id),
    )
    lines = []
    for item in items:
        if item.id in delivered_by_item:
            received = delivered_by_item[item.id]
        elif item.dispatched_quantity is not None:
            received = item.dispatched_quantity
        else:
            received = item.quantity
        lines.append(
            {
                "item": item.id,
                "item_code": item.item_code,
                "quantity": str(item.quantity),
                "received": str(received),
            }
        )
    return lines


def _nothing_received(lines):
    return [line["item_code"] or f"item {line['item']}" for line in lines
            if Decimal(line["received"]) <= 0]


def by_hand_message(item_codes):
    return (
        "SAP takes a received date only with a received quantity above 0 on every "
        f"line, and none of {', '.join(item_codes)} was received. Enter this bill's "
        "receipt in SAP by hand."
    )


NEEDS_PROOF_MESSAGE = "Attach the proof of delivery to send this to SAP."


def open_receipts(update, user):
    """One receipt per invoice on the truck, for a delivery update just logged.

    Runs inside the update's own transaction; nothing here talks to SAP.
    """
    if update.status not in DELIVERY_STATUSES:
        return []

    delivered_by_item = {
        item.item_id: item.qty_delivered
        for line in update.partial_lines.all()
        for item in line.items.all()
    }
    received_date = received_date_for(update)
    opened = []
    for document in truck_invoices(update.arrival):
        earlier = TruckDispatchSapReceipt.objects.filter(
            document=document, is_active=True
        ).exclude(update=update)
        if earlier.filter(status__in=SETTLED).exists():
            continue
        earlier.filter(status__in=REPLACEABLE).update(
            status=ReceiptStatus.SUPERSEDED,
            message=f"Replaced by the {update.get_status_display().lower()} update "
                    f"for {received_date:%d %b %Y}.",
            updated_by=user,
            updated_at=timezone.now(),
        )

        lines = receipt_lines(document, delivered_by_item)
        empty = _nothing_received(lines)
        if empty:
            status, message = ReceiptStatus.BY_HAND, by_hand_message(empty)
        elif not update.proof:
            status, message = ReceiptStatus.NEEDS_PROOF, NEEDS_PROOF_MESSAGE
        else:
            status, message = ReceiptStatus.WAITING, ""
        opened.append(
            TruckDispatchSapReceipt.objects.create(
                update=update,
                document=document,
                status=status,
                received_date=received_date,
                lines=lines,
                message=message,
                created_by=user,
                updated_by=user,
            )
        )
    return opened


# ---------------------------------------------------------------------------
# sending them
# ---------------------------------------------------------------------------

def title_for(receipt):
    vehicle = getattr(receipt.update.arrival.vehicle, "vehicle_number", "") or ""
    title = f"Delivery of bill {receipt.document.sap_doc_num}"
    return f"{title} on {vehicle}" if vehicle else title


def link_for(receipt):
    vehicle = getattr(receipt.update.arrival.vehicle, "vehicle_number", "") or ""
    return f"/dispatch/tracking?search={vehicle}" if vehicle else "/dispatch/tracking"


def send_receipts(update_id, user, *, retry_refused=False):
    """Send an update's receipts that are due for SAP.

    Each is tried now; once SAP has not answered for one, the rest are queued
    for the worker without being tried, so a truck of twenty bills does not hold
    the operator through twenty timeouts.
    """
    from sap_postings import services as sap_postings

    update = (
        TruckDispatchUpdate.objects.select_related("arrival__vehicle")
        .filter(pk=update_id).first()
    )
    if update is None or not update.proof:
        return
    due = [ReceiptStatus.WAITING, ReceiptStatus.NEEDS_PROOF]
    if retry_refused:
        due.append(ReceiptStatus.REFUSED)
    receipts = list(
        update.sap_receipts.filter(is_active=True, status__in=due)
        .select_related("document__company")
        .order_by("id")
    )

    not_answering = False
    for receipt in receipts:
        receipt.update = update
        if receipt.status != ReceiptStatus.WAITING:
            _record(receipt, ReceiptStatus.WAITING, "")
        kwargs = dict(
            kind=KIND,
            company=receipt.document.company,
            source_id=receipt.pk,
            title=title_for(receipt),
            link=link_for(receipt),
            user=user,
        )
        if not_answering:
            sap_postings.queue(
                **kwargs,
                reason="SAP did not answer for an earlier bill on this truck, so this "
                       "one waits for SAP without being tried.",
            )
            continue
        try:
            _posting, outcome = sap_postings.post_now(**kwargs)
        except sap_postings.PostingInProgress:
            continue
        if outcome.kind == SapPostingOutcome.WAITING:
            not_answering = True


def send_after_commit(update_id, user, *, retry_refused=False):
    """Send once the update's transaction has committed -- the receipts must exist
    for the posting to read, and the posting log commits on its own."""
    transaction.on_commit(
        lambda: send_receipts(update_id, user, retry_refused=retry_refused)
    )


def _record(receipt, status, message, *, posted=False):
    receipt.status = status
    receipt.message = (message or "")[:4000]
    fields = ["status", "message", "updated_at"]
    if posted:
        receipt.posted_at = timezone.now()
        fields.append("posted_at")
    receipt.save(update_fields=fields)


def match_lines(receipt_lines_, sap_lines):
    """``[(LineNum, received)]`` for the PATCH: the receipt's lines on SAP's.

    Position for position, because the docking's items were made from SAP's
    lines in ``LineNum`` order; the item code and quantity must agree, or the
    bill has changed in SAP since it was loaded and nothing is guessed.
    """
    if len(receipt_lines_) != len(sap_lines):
        raise ReceiptError(
            f"The bill has {len(sap_lines)} lines in SAP but {len(receipt_lines_)} "
            "were loaded on the truck. It has changed in SAP since; enter its "
            "receipt in SAP by hand."
        )
    matched = []
    for ours, theirs in zip(receipt_lines_, sap_lines):
        if (ours["item_code"] or "") != theirs["item_code"] or Decimal(
            ours["quantity"]
        ) != theirs["quantity"]:
            raise ReceiptError(
                f"Line {theirs['line_num']} is {theirs['item_code']} x "
                f"{theirs['quantity']:g} in SAP but {ours['item_code']} x "
                f"{Decimal(ours['quantity']):g} on the truck. The bill has changed in "
                "SAP since it was loaded; enter its receipt in SAP by hand."
            )
        matched.append((theirs["line_num"], Decimal(ours["received"])))
    return matched


class SapReceiveHandler:
    """Writes one bill's delivery to its invoice. Safe to send again: SAP is read
    back first, and a received date already there is never written over."""

    kind = KIND

    def send(self, posting):
        receipt = (
            TruckDispatchSapReceipt.objects.select_related(
                "update", "document__company"
            ).filter(pk=posting.source_id).first()
        )
        if receipt is None or not receipt.is_active:
            return Outcome.rejected("The delivery record no longer exists.")
        if receipt.status in (ReceiptStatus.SUPERSEDED, ReceiptStatus.BY_HAND):
            return Outcome.rejected(receipt.message or receipt.get_status_display())
        if receipt.status in (ReceiptStatus.POSTED, ReceiptStatus.ALREADY_RECEIVED):
            return Outcome.posted(receipt.message or receipt.get_status_display())
        if not receipt.update.proof:
            _record(receipt, ReceiptStatus.NEEDS_PROOF, NEEDS_PROOF_MESSAGE)
            return Outcome.rejected(NEEDS_PROOF_MESSAGE)

        document = receipt.document
        try:
            return self._send(receipt, document)
        except (SAPConnectionError, requests.RequestException) as exc:
            message = f"SAP could not be reached ({exc}); the bill is not marked received yet."
            _record(receipt, ReceiptStatus.WAITING, message)
            return Outcome.waiting(message)
        except SAPDataError as exc:
            from sap_mirror.services import hana_unreachable

            if hana_unreachable(exc):
                message = f"SAP could not be read ({exc}); the bill is not marked received yet."
                _record(receipt, ReceiptStatus.WAITING, message)
                return Outcome.waiting(message)
            _record(receipt, ReceiptStatus.REFUSED, str(exc))
            return Outcome.rejected(str(exc))
        except (SAPValidationError, ReceiptError) as exc:
            _record(receipt, ReceiptStatus.REFUSED, str(exc))
            return Outcome.rejected(str(exc))
        except OSError as exc:
            # The proof's file is gone from the server (a media folder lost in
            # a deploy): nothing SAP can be sent until it is attached again.
            message = f"The proof of delivery could not be read ({exc}); attach it again."
            _record(receipt, ReceiptStatus.REFUSED, message)
            return Outcome.rejected(message)

    @staticmethod
    def _read_state(document):
        from dispatch_plans.hana_reader import HanaDispatchBillReader
        from sap_client.context import CompanyContext

        reader = HanaDispatchBillReader(CompanyContext(document.company.code), use_copy=False)
        return reader.invoice_receive_state(document.sap_doc_entry)

    def _send(self, receipt, document):
        company_code = document.company.code
        doc_num = document.sap_doc_num
        state = self._read_state(document)
        if state is None:
            raise ReceiptError(f"Bill {doc_num} is not in SAP for {document.company.name}.")
        if state["cancelled"]:
            raise ReceiptError(f"Bill {doc_num} is cancelled in SAP.")

        if state["received_date"]:
            ours = (
                receipt.sap_attachment_entry is not None
                and state["attachment_entry"] == receipt.sap_attachment_entry
                and state["received_date"] == receipt.received_date
            )
            if ours:
                # Written by an earlier try whose answer never came back.
                message = f"Bill {doc_num} marked received on {receipt.received_date:%d %b %Y}."
                _record(receipt, ReceiptStatus.POSTED, message, posted=True)
                return Outcome.posted(message, result={"doc_nums": [doc_num]})
            message = (
                f"SAP already shows bill {doc_num} received on "
                f"{state['received_date']:%d %b %Y}; left as SAP has it."
            )
            _record(receipt, ReceiptStatus.ALREADY_RECEIVED, message)
            return Outcome.posted(message, result={"doc_nums": [doc_num]})

        if state["doc_date"] and receipt.received_date < state["doc_date"]:
            raise ReceiptError(
                f"SAP will not take a received date ({receipt.received_date:%d %b %Y}) "
                f"earlier than the bill's own date ({state['doc_date']:%d %b %Y})."
            )

        lines = match_lines(receipt.lines, state["lines"])
        empty = [str(line_num) for line_num, received in lines if received <= 0]
        if empty:
            message = by_hand_message(_nothing_received(receipt.lines))
            _record(receipt, ReceiptStatus.BY_HAND, message)
            return Outcome.rejected(message)

        attachment_entry = self._attach_proof(receipt, document, state)

        payload = {
            "U_Recv_Date": receipt.received_date.isoformat(),
            "DocumentLines": [
                {"LineNum": line_num, "U_Recvd_Qty": float(received)}
                for line_num, received in lines
            ],
        }
        if not state["attachment_entry"]:
            payload["AttachmentEntry"] = attachment_entry
        self._patch_invoice(company_code, document.sap_doc_entry, payload)

        message = f"Bill {doc_num} marked received on {receipt.received_date:%d %b %Y}."
        _record(receipt, ReceiptStatus.POSTED, message, posted=True)
        return Outcome.posted(
            message,
            result={"doc_nums": [doc_num], "attachment_entry": attachment_entry},
            detail={"doc_num": doc_num, "payload": payload},
        )

    @staticmethod
    def _attach_proof(receipt, document, state):
        """The proof, as an attachment of the invoice. Uploaded once: the entry is
        kept on the receipt the moment SAP gives it, so a retry reuses it."""
        if receipt.sap_attachment_entry is not None:
            return receipt.sap_attachment_entry

        from sap_client.client import SAPClient

        proof = receipt.update.proof
        extension = os.path.splitext(proof.name)[1]
        filename = f"{document.sap_doc_num}_delivery_{receipt.pk}{extension}"
        client = SAPClient(document.company.code)
        if state["attachment_entry"]:
            # The invoice already carries attachments (Mart's marketplace sheets):
            # the proof joins them rather than replacing the link.
            client.add_line_to_existing_attachment(
                absolute_entry=state["attachment_entry"],
                file_path=proof.path,
                filename=filename,
            )
            entry = state["attachment_entry"]
        else:
            entry = client.upload_attachment(file_path=proof.path, filename=filename).get(
                "AbsoluteEntry"
            )
            if not entry:
                raise SAPDataError("SAP did not say where it kept the proof of delivery.")
        receipt.sap_attachment_entry = int(entry)
        receipt.save(update_fields=["sap_attachment_entry", "updated_at"])
        return receipt.sap_attachment_entry

    @staticmethod
    def _patch_invoice(company_code, doc_entry, payload):
        """The PATCH, with SAP not answering told apart from SAP refusing."""
        from sap_client import health
        from sap_client.context import CompanyContext
        from sap_client.service_layer.auth import ServiceLayerSession

        sl = CompanyContext(company_code).service_layer
        health.guard_service_layer()
        try:
            cookies = ServiceLayerSession(sl).login()
        except requests.RequestException as exc:
            raise SAPUnavailable(f"SAP Service Layer did not answer the login: {exc}") from exc
        try:
            response = requests.patch(
                f"{sl['base_url']}/b1s/v2/Invoices({int(doc_entry)})",
                json=payload,
                cookies=cookies,
                timeout=(10, 180),
                verify=False,
            )
        except requests.RequestException as exc:
            raise SAPUnavailable(f"SAP Service Layer did not answer: {exc}") from exc
        if response.status_code in (502, 503, 504):
            raise SAPUnavailable(f"SAP Service Layer is not available ({response.status_code}).")
        if response.status_code not in (200, 204):
            raise SAPValidationError(_sap_message(response))


def _sap_message(response):
    try:
        error = response.json().get("error", {})
        message = error.get("message")
        if isinstance(message, dict):
            message = message.get("value")
        return str(message or response.text)[:500]
    except Exception:  # noqa: BLE001
        return f"HTTP {response.status_code}: {response.text[:300]}"
