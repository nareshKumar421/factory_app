# raw_material_gatein/services/po_repoint.py

"""Move a received PO onto a different open PO for the same vendor.

Distinct from *replacing* a PO (``POReceiptReplaceAPI``), which is for a gate
operator who booked the wrong PO before QC: it throws the items away and starts
again from SAP. This one is for a PO that was right at gate-in and has since run
out — another truck's GRPO consumed the open quantity while this one was being
unloaded and inspected. The material, its quantities and its QC are all correct;
only the PO line the GRPO will draw on is wrong.

So this keeps every ``POItemReceipt`` (and with it every arrival slip, inspection
and accepted/rejected quantity) and rewrites just the SAP linkage: the receipt's
PO number and DocEntry, and each item's line number and line terms. It is allowed
right up until a GRPO actually posts, which is the point at which the linkage
stops being an intention and becomes an accounting fact.
"""

import logging
from decimal import Decimal

from django.db import transaction

from gate_core.enums import GateEntryStatus
from sap_client.client import SAPClient

from .validations import (
    _format_qty,
    as_qty,
    is_over_receipt_exempt,
    over_receipt_ceiling,
)

logger = logging.getLogger(__name__)


class RepointError(Exception):
    """A repoint that must not go ahead, with an operator-readable reason."""


def repoint_block_reason(po_receipt):
    """Why this receipt cannot be repointed (``None`` if it can).

    Deliberately shorter than ``_po_receipt_replace_block_reason``: a completed
    entry and a finished QC inspection are *expected* here — that is when the
    problem shows up — and neither is invalidated by changing which PO line the
    receipt draws on. Only a posted GRPO is final.

    ``entry.is_locked`` is *not* checked, unlike every gate-side rule. Completing
    an RM entry locks it, so every entry that can reach this state is locked and
    the check would refuse all of them. The lock guards the gate entry record —
    which a repoint never writes to — not the receipt's SAP linkage, which the
    GRPO screen goes on changing long after the lock is set.
    """
    entry = po_receipt.vehicle_entry

    if entry.status == GateEntryStatus.CANCELLED:
        return "This gate entry is cancelled and cannot be modified."

    if (
        po_receipt.grpo_postings.filter(status="POSTED").exists()
        or po_receipt.merged_grpo_postings.filter(status="POSTED").exists()
    ):
        return (
            "This PO has already been posted to GRPO. Correct it in SAP instead — "
            "changing the linkage here would not move the posted receipt."
        )

    for item in po_receipt.items.all():
        if item.grpo_lines.filter(grpo_posting__status="POSTED").exists():
            return "This PO has posted GRPO lines and cannot be repointed."

    return None


def _match_new_po_lines(po_receipt, new_po):
    """``{POItemReceipt: POItemDTO}`` — where each received item goes on the new PO.

    Matched on item code, not line number: the replacement PO is a fresh document
    and its lines are in whatever order purchase entered them. Where the same item
    appears on more than one open line, the one with the most open quantity is
    taken, since that is the line most likely to hold the whole receipt.
    """
    lines_by_item = {}
    for line in new_po.items:
        lines_by_item.setdefault(line.po_item_code, []).append(line)

    matched = {}
    missing = []
    for item in po_receipt.items.all():
        candidates = lines_by_item.get(item.po_item_code)
        if not candidates:
            missing.append(item.po_item_code)
            continue
        matched[item] = max(candidates, key=lambda line: line.remaining_qty or 0)

    if missing:
        raise RepointError(
            f"PO {new_po.po_number} has no open line for "
            + ", ".join(sorted(set(missing)))
            + ". The replacement PO has to cover every item on this receipt."
        )

    return matched


def _validate_capacity(matched, exempt):
    """Refuse a repoint onto a PO that has no room either — it would only move the
    failure from one posting attempt to the next."""
    if exempt:
        return

    problems = []
    for item, line in matched.items():
        remaining = Decimal(str(line.remaining_qty or 0))
        received = Decimal(str(item.received_qty))
        if received > over_receipt_ceiling(remaining):
            problems.append(
                f"{item.po_item_code}: {_format_qty(received)} received but only "
                f"{_format_qty(remaining)} open on line {line.line_num}"
            )

    if problems:
        raise RepointError(
            "The replacement PO does not have room for this receipt either — "
            + "; ".join(problems)
            + "."
        )


def _refresh_pending_draft_payloads(po_receipt, matched):
    """Repoint the saved GRPO drafts' line terms too.

    A draft saves the unit price and tax code the operator saw, and posts them
    verbatim. Left alone after a repoint it would post the *old* PO's price
    against the new PO's line — which SAP accepts, and which nobody would notice.
    """
    from grpo.models import GRPOStatus

    terms_by_item_id = {
        item.id: {
            "unit_price": float(line.rate) if line.rate is not None else None,
            "tax_code": line.tax_code,
            "gl_account": line.account_code,
        }
        for item, line in matched.items()
    }

    postings = (
        po_receipt.merged_grpo_postings.exclude(status=GRPOStatus.POSTED)
        | po_receipt.grpo_postings.exclude(status=GRPOStatus.POSTED)
    ).distinct()

    updated = 0
    for posting in postings:
        payload = posting.request_payload
        if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
            continue

        changed = False
        for line in payload["items"]:
            terms = terms_by_item_id.get(line.get("po_item_receipt_id"))
            if not terms:
                continue
            for field, value in terms.items():
                if value not in (None, "") and line.get(field) != value:
                    line[field] = value
                    changed = True

        if changed:
            posting.request_payload = payload
            posting.save(update_fields=["request_payload", "updated_at"])
            updated += 1

    return updated


@transaction.atomic
def repoint_po_receipt(po_receipt, *, new_po_number, reason, company_code, user=None):
    """Point ``po_receipt`` at ``new_po_number``, keeping its items and their QC.

    Returns a summary dict. Raises ``RepointError`` with a reason an operator can
    act on; the transaction rolls back and nothing is touched.
    """
    from ..models import POReceipt, POReplacementLog

    new_po_number = (new_po_number or "").strip()
    reason = (reason or "").strip()
    if not new_po_number:
        raise RepointError("A replacement PO number is required.")
    if not reason:
        raise RepointError("A reason is required to repoint a PO.")
    if new_po_number == po_receipt.po_number:
        raise RepointError(f"This receipt is already on PO {new_po_number}.")

    block_reason = repoint_block_reason(po_receipt)
    if block_reason:
        raise RepointError(block_reason)

    entry = po_receipt.vehicle_entry
    if (
        POReceipt.objects.filter(vehicle_entry=entry, po_number=new_po_number)
        .exclude(id=po_receipt.id)
        .exists()
    ):
        raise RepointError(
            f"PO {new_po_number} is already on this gate entry as a separate receipt."
        )

    new_po = SAPClient(company_code=company_code).get_open_po_by_number(new_po_number)
    if new_po is None:
        raise RepointError(
            f"PO {new_po_number} was not found in SAP with any open line."
        )

    # A different vendor would change who the A/P invoice is against, which is a
    # different correction entirely (and is what Replace PO is for).
    if (new_po.supplier_code or "").strip() != (po_receipt.supplier_code or "").strip():
        raise RepointError(
            f"PO {new_po_number} belongs to {new_po.supplier_code}, not this "
            f"receipt's vendor {po_receipt.supplier_code}."
        )

    matched = _match_new_po_lines(po_receipt, new_po)
    _validate_capacity(
        matched,
        is_over_receipt_exempt(company_code, po_receipt.supplier_code),
    )

    old_po_number = po_receipt.po_number
    old_doc_entry = po_receipt.sap_doc_entry

    po_receipt.po_number = new_po.po_number
    po_receipt.sap_doc_entry = new_po.doc_entry
    po_receipt.branch_id = new_po.branch_id
    po_receipt.vendor_ref = new_po.vendor_ref or ""
    po_receipt.po_date = new_po.doc_date
    po_receipt.updated_by = user
    po_receipt.save()

    moved_lines = []
    for item, line in matched.items():
        moved_lines.append(
            {
                "po_item_code": item.po_item_code,
                "received_qty": str(item.received_qty),
                "old_line_num": item.sap_line_num,
                "new_line_num": line.line_num,
            }
        )
        item.sap_line_num = line.line_num
        # The line terms follow the line: ordered quantity, price, tax code and
        # G/L all belong to the PO being drawn on, not the one that ran out.
        item.ordered_qty = as_qty(line.ordered_qty)
        item.unit_price = line.rate or None
        item.tax_code = line.tax_code
        item.warehouse_code = line.warehouse_code
        item.gl_account = line.account_code
        item.variety = line.variety or item.variety
        item.updated_by = user
        item.save()

    drafts_updated = _refresh_pending_draft_payloads(po_receipt, matched)

    POReplacementLog.objects.create(
        vehicle_entry=entry,
        old_po_number=old_po_number,
        old_supplier_code=po_receipt.supplier_code,
        old_supplier_name=po_receipt.supplier_name,
        new_po_number=new_po.po_number,
        new_supplier_code=po_receipt.supplier_code,
        new_supplier_name=po_receipt.supplier_name,
        supplier_changed=False,
        reason=f"Repointed to an open PO (items and QC kept): {reason}",
        created_by=user,
    )

    logger.info(
        "Repointed PO receipt %s on %s: %s (DocEntry %s) -> %s (DocEntry %s)",
        po_receipt.id, entry.entry_no, old_po_number, old_doc_entry,
        new_po.po_number, new_po.doc_entry,
    )

    return {
        "po_receipt_id": po_receipt.id,
        "gate_entry": entry.entry_no,
        "old_po_number": old_po_number,
        "old_doc_entry": old_doc_entry,
        "new_po_number": new_po.po_number,
        "new_doc_entry": new_po.doc_entry,
        "lines": moved_lines,
        "drafts_updated": drafts_updated,
    }
