"""The late PO link: attaching SAP PO lines to a fixed-asset gate entry.

The gate records a fixed-asset arrival with free text (``fixed_asset_gatein``)
and does not know the PO, and that wizard stays exactly as it is. The PO is
linked afterwards, from the Fixed Asset GRPO screen, by someone holding
``can_link_fixed_asset_po`` or ``can_post_fixed_asset_grpo``.

WHAT A LINK IS. A link is a ``raw_material_gatein.POReceipt`` with its
``POItemReceipt`` lines on the FA entry: the same rows a finished-goods
receipt makes (``finished_goods_gatein/views.py``), told apart only by the
entry's ``entry_type``. The GRPO engine already posts those rows. Creating,
changing and deleting them is data, not a schema change.

THE ENTRY IS NEVER SAVED. A completed FA entry is locked, and
``GateEntryBase.save`` raises on a locked row. Nothing here writes the
``VehicleEntry``: linking works on any FA entry of the company, DRAFT through
COMPLETED, locked or not.

ONE PO PER ENTRY in this version. A second PO is refused with "change or
remove it first". The engine could merge several POs of one supplier into one
GRPO, but the screen and the lock rules are built around one.

LINES COME FROM SAP. The open lines are read through
``sap_purchasing.fa_po_reader`` (item lines of open POs whose warehouse is in
``FIXED_ASSET_WAREHOUSES``). Ordered and open quantity, price, tax code,
warehouse, G/L and variety are always SAP's; only the received quantity is the
user's, checked against 110% of the open quantity like every other receipt.
No QC on fixed assets: ``accepted_qty = received_qty``.

CHANGING A LINK DISCARDS UNSENT DRAFTS. A saved GRPO draft names the receipt
line ids it posts; an update can delete and recreate lines, so a draft saved
before it could post stale ids. Unsent drafts (DRAFT/FAILED, or QUEUED whose
SAP postings row was cancelled; no SAP document, nothing live in the queue)
are therefore deleted, with their files, before any
change or unlink. A GRPO on its way to SAP, or posted, locks the link instead.
"""

import logging
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

from django.db import IntegrityError, transaction
from django.db.models import ProtectedError
from rest_framework.exceptions import ValidationError

from gate_core.enums import GateEntryStatus
from grpo.models import GRPOLinePosting, GRPOStatus
from raw_material_gatein.models import POItemReceipt, POReceipt
from raw_material_gatein.services import (
    as_qty,
    booking_overlap_warning,
    is_over_receipt_exempt,
    po_line_bookings,
    validate_received_quantity,
)
from sap_client.drf import SAPBadGatewayAPIException, SAPUnavailableAPIException
from sap_client.exceptions import SAPConnectionError, SAPDataError

from ..constants import FA_ENTRY_TYPE
from ..permissions import can_link
from .state import (
    UNSENT_STATUSES,
    active_source_ids,
    any_on_its_way,
    closure_of,
    fa_detail_of,
    is_simulated,
    postings_of_receipt,
)

logger = logging.getLogger(__name__)


def can_link_po(user, vehicle_entry=None) -> bool:
    """Whether ``user`` may link, change or unlink the PO on an FA entry.

    The same answer for every FA entry, completed and locked ones included:
    the link happens after the gate, so it is a right of the GRPO side
    (``can_link_fixed_asset_po`` or ``can_post_fixed_asset_grpo``), not of
    the gate operator.
    """
    return can_link(user)


def entry_lock_reason(entry) -> Optional[str]:
    """Why no PO may be linked to ``entry`` at all (None when it may)."""
    if entry.status == GateEntryStatus.CANCELLED:
        return "This gate entry is cancelled."
    closure = closure_of(entry)
    if closure is not None:
        ref = f" as {closure.sap_doc_num}" if closure.sap_doc_num else ""
        return f"This entry was done in SAP by hand{ref}. Reopen it first."
    return None


def fa_po_receipt_lock_reason(po_receipt) -> Optional[str]:
    """Why this FA PO link can no longer be changed or removed (None if it can).

    First match wins:

    1. the entry is cancelled;
    2. the entry was closed as done in SAP by hand;
    3. a real (not simulated) GRPO of this PO is posted, named by its DocNum;
    4. a simulated GRPO is posted: discard it in history first;
    5. a GRPO of this PO is on its way to SAP (PENDING/QUEUED, or an unsent
       draft with a live queue row).
    """
    entry = po_receipt.vehicle_entry
    reason = entry_lock_reason(entry)
    if reason:
        return reason

    postings = list(postings_of_receipt(po_receipt).select_related("fa_record"))
    posted = [p for p in postings if p.status == GRPOStatus.POSTED]
    real = next((p for p in posted if not is_simulated(p)), None)
    if real is not None:
        return (
            f"This PO is already posted to SAP as GRPO {real.sap_doc_num}, "
            f"so it can no longer be changed."
        )
    if posted:
        return (
            "A simulated GRPO exists — discard it in Fixed Asset GRPO history first."
        )
    if any_on_its_way(postings) is not None:
        return "A GRPO for this PO is being sent to SAP."
    return None


def simulated_line_postings(sap_doc_entry, sap_line_num, *, exclude_po_receipt_id=None):
    """``[(entry_no, qty), ...]`` of simulated FA GRPOs on this PO line elsewhere.

    A simulated post is stored POSTED, so the gate's ``po_line_bookings``
    leaves it out (it takes a POSTED GRPO's share as already gone from SAP's
    ``OpenQty``) -- but nothing was sent, so SAP's open quantity still holds it.
    """
    if sap_doc_entry is None or sap_line_num is None:
        return []
    lines = GRPOLinePosting.objects.filter(
        base_entry=sap_doc_entry,
        base_line=sap_line_num,
        grpo_posting__status=GRPOStatus.POSTED,
        grpo_posting__fa_record__simulated=True,
    ).select_related("grpo_posting__vehicle_entry")
    if exclude_po_receipt_id is not None:
        lines = lines.exclude(po_item_receipt__po_receipt_id=exclude_po_receipt_id)
    return [
        (line.grpo_posting.vehicle_entry.entry_no, as_qty(line.quantity_posted))
        for line in lines
    ]


def simulated_overlap_warning(sap_doc_entry, sap_line_num, *, item_label, received_qty,
                              remaining_qty, uom="", exclude_po_receipt_id=None) -> Optional[str]:
    """The over-booking warning the gate's would give if simulated posts counted.

    Asked only when ``booking_overlap_warning`` said nothing. Advisory, like
    that one: never raises, never blocks a link.
    """
    try:
        simulated = simulated_line_postings(
            sap_doc_entry, sap_line_num, exclude_po_receipt_id=exclude_po_receipt_id
        )
        if not simulated:
            return None
        bookings = po_line_bookings(
            sap_doc_entry, sap_line_num, exclude_po_receipt_id=exclude_po_receipt_id
        )
    except Exception as exc:
        logger.warning(
            "Could not read simulated FA GRPOs for PO line %s/%s: %s",
            sap_doc_entry, sap_line_num, exc,
        )
        return None
    held = sum((qty for _, qty in simulated), Decimal("0"))
    booked = sum((qty for _, qty in bookings), Decimal("0"))
    remaining = as_qty(remaining_qty)
    if booked + held + as_qty(received_qty) <= remaining:
        return None
    unit = f" {uom}" if uom else ""
    entries = ", ".join(f"{entry_no} ({qty.normalize():f}{unit})" for entry_no, qty in simulated)
    return (
        f"{item_label}: only {remaining.normalize():f}{unit} is open on this PO line, and "
        f"{held.normalize():f}{unit} of it is held by a simulated GRPO of {entries} "
        f"(nothing was sent to SAP, so SAP still shows it open). Adding "
        f"{as_qty(received_qty).normalize():f}{unit} over-promises the line once that "
        f"entry is posted for real."
    )


def discard_unsent_fa_drafts(po_receipt) -> int:
    """Delete this receipt's unsent GRPO drafts and their files. Returns how many.

    Unsent: DRAFT, FAILED or QUEUED (``UNSENT_STATUSES``), no ``sap_doc_entry``,
    and no live row in the SAP posting queue (a QUEUED draft whose row was
    cancelled is stranded and goes too). Drafts carry no line postings, so deleting the row (and its
    attachments, by cascade) leaves nothing behind but the files, which are
    deleted first.
    """
    drafts = list(
        postings_of_receipt(po_receipt)
        .filter(status__in=UNSENT_STATUSES, sap_doc_entry__isnull=True)
        .prefetch_related("attachments")
    )
    active = active_source_ids(d.id for d in drafts)
    count = 0
    for draft in drafts:
        if draft.id in active:
            continue
        for att in draft.attachments.all():
            if att.file:
                att.file.delete(save=False)
        draft.delete()
        count += 1
    return count


def get_open_fa_po(company_code: str, supplier_code: str, po_number: str):
    """The open fixed-asset PO ``po_number`` of ``supplier_code``, as a ``PODTO``.

    Raises DRF errors the views pass straight on: 400 when the PO has no open
    item line in a fixed-asset warehouse, 503 when SAP cannot be reached, 502
    when SAP refuses the read.
    """
    from sap_purchasing.fa_po_reader import get_open_fixed_asset_pos

    try:
        pos = get_open_fixed_asset_pos(company_code, supplier_code)
    except SAPConnectionError as exc:
        logger.error("SAP connection error reading open FA POs: %s", exc)
        raise SAPUnavailableAPIException(
            detail="SAP system is currently unavailable. Please try again later."
        )
    except SAPDataError as exc:
        logger.error("SAP data error reading open FA POs: %s", exc)
        raise SAPBadGatewayAPIException(detail="Failed to retrieve PO data from SAP.")
    except ValueError as exc:
        raise ValidationError({"detail": str(exc)})

    for po in pos:
        if str(po.po_number) == str(po_number):
            return po
    raise ValidationError({
        "detail": (
            f"PO {po_number} has no open item lines in a fixed-asset warehouse "
            f"for {supplier_code}."
        )
    })


def _sap_items_map(po) -> Dict[int, dict]:
    return {
        item.line_num: {
            "po_item_code": item.po_item_code,
            # Both quantities come from SAP so the over-receipt ceiling can never
            # be widened by what the client posted.
            "ordered_qty": item.ordered_qty,
            "remaining_qty": item.remaining_qty,
            "rate": item.rate,
            "tax_code": item.tax_code or "",
            "warehouse_code": item.warehouse_code or "",
            "account_code": item.account_code or "",
            "variety": item.variety or "",
        }
        for item in po.items
    }


def _save_fa_po_items(po_receipt, items_data, sap_items_map, user, company_code) -> List[str]:
    """Upsert the receipt's lines from the request, checked against SAP's PO.

    A copy of ``finished_goods_gatein.views._save_po_items`` (which may not be
    imported: it is private and may not change), plus the gate's
    booking-overlap warnings. Upserts by ``sap_line_num`` so line ids stay
    stable; lines missing from the request are deleted. Returns the warnings.
    """
    existing_by_line = {
        item.sap_line_num: item
        for item in po_receipt.items.all()
        if item.sap_line_num is not None
    }
    seen_line_nums = set()
    warnings: List[str] = []

    # Resolved once per request, not per line: it costs an SAP read.
    exempt = is_over_receipt_exempt(company_code, po_receipt.supplier_code)

    for item_data in items_data:
        line_num = item_data["line_num"]
        po_item_code = item_data["po_item_code"]
        received_qty = item_data["received_qty"]

        if line_num in seen_line_nums:
            raise ValidationError({"detail": f"Duplicate PO line {line_num} in request."})
        seen_line_nums.add(line_num)

        sap_item_info = sap_items_map.get(line_num)
        if sap_item_info is None:
            raise ValidationError(
                {"detail": f"Invalid PO line {line_num} for item {po_item_code}"}
            )
        if sap_item_info["po_item_code"] != po_item_code:
            raise ValidationError(
                {"detail": f"Item code {po_item_code} does not match PO line {line_num}"}
            )

        label = f"{po_item_code} (line {line_num})"
        try:
            validate_received_quantity(
                sap_item_info["ordered_qty"],
                sap_item_info["remaining_qty"],
                received_qty,
                item_label=label,
                uom=item_data["uom"],
                exempt=exempt,
            )
        except ValueError as exc:
            raise ValidationError({"detail": str(exc)})

        warning = booking_overlap_warning(
            po_receipt.sap_doc_entry,
            line_num,
            item_label=label,
            received_qty=received_qty,
            remaining_qty=sap_item_info["remaining_qty"],
            uom=item_data["uom"],
            exclude_po_receipt_id=po_receipt.id,
        )
        if warning is None:
            warning = simulated_overlap_warning(
                po_receipt.sap_doc_entry,
                line_num,
                item_label=label,
                received_qty=received_qty,
                remaining_qty=sap_item_info["remaining_qty"],
                uom=item_data["uom"],
                exclude_po_receipt_id=po_receipt.id,
            )
        if warning:
            warnings.append(warning)

        defaults = {
            "po_item_code": po_item_code,
            "item_name": item_data["item_name"],
            # SAP's ordered quantity, not the client's.
            "ordered_qty": as_qty(sap_item_info["ordered_qty"]),
            "received_qty": received_qty,
            "uom": item_data["uom"],
            "sap_line_num": line_num,
            "unit_price": sap_item_info["rate"] or None,
            "tax_code": sap_item_info["tax_code"],
            "warehouse_code": sap_item_info["warehouse_code"],
            "gl_account": sap_item_info["account_code"],
            "variety": sap_item_info.get("variety", ""),
            # No QC for fixed assets: everything received is accepted.
            "accepted_qty": received_qty,
            "rejected_qty": 0,
        }

        item = existing_by_line.get(line_num)
        if item is None:
            POItemReceipt.objects.create(po_receipt=po_receipt, **defaults, created_by=user)
            continue
        for field, value in defaults.items():
            setattr(item, field, value)
        item.updated_by = user
        item.save()

    try:
        po_receipt.items.exclude(sap_line_num__in=seen_line_nums).delete()
    except ProtectedError as exc:
        raise ValidationError({
            "detail": f"A posted GRPO still points at a line being removed: {exc.protected_objects}"
        })
    return warnings


def _fa_invoice_no(entry, data) -> str:
    """The supplier's bill number: the gate's, else the one typed on the link."""
    detail = fa_detail_of(entry)
    invoice = (getattr(detail, "invoice_number", "") or "") if detail else ""
    return (invoice or data.get("invoice_no") or "")[:50]


def _require_fa(entry):
    if (entry.entry_type or "") != FA_ENTRY_TYPE:
        raise ValidationError({"detail": "This is not a fixed-asset gate entry."})


@transaction.atomic
def receive_fa_po(*, vehicle_entry, data, user, company_code) -> Tuple[POReceipt, List[str]]:
    """Link an open SAP PO's lines to a fixed-asset gate entry.

    ``data`` is ``POReceiveRequestSerializer``'s validated data (``po_number``,
    ``supplier_code``, ``supplier_name``, ``items``) plus an optional
    ``invoice_no``. Returns ``(receipt, warnings)``; refusals are DRF
    ``ValidationError`` (400) or the SAP API errors of ``get_open_fa_po``.
    """
    _require_fa(vehicle_entry)
    reason = entry_lock_reason(vehicle_entry)
    if reason:
        raise ValidationError({"detail": reason})

    po_number = data["po_number"]
    existing = vehicle_entry.po_receipts.order_by("id").first()
    if existing is not None:
        if existing.po_number == po_number:
            raise ValidationError({"detail": f"PO {po_number} is already linked to this entry."})
        raise ValidationError({
            "detail": (
                f"This entry already has PO {existing.po_number} — change or remove it first."
            )
        })

    po = get_open_fa_po(company_code, data["supplier_code"], po_number)

    try:
        with transaction.atomic():
            po_receipt = POReceipt.objects.create(
                vehicle_entry=vehicle_entry,
                po_number=str(po.po_number)[:30],
                supplier_code=(po.supplier_code or data["supplier_code"])[:30],
                supplier_name=(po.supplier_name or data.get("supplier_name") or "")[:150],
                sap_doc_entry=po.doc_entry,
                branch_id=po.branch_id,
                vendor_ref=(po.vendor_ref or "")[:100],
                po_date=po.doc_date,
                invoice_no=_fa_invoice_no(vehicle_entry, data),
                created_by=user,
            )
    except IntegrityError:
        raise ValidationError({"detail": f"PO {po_number} is already linked to this entry."})

    warnings = _save_fa_po_items(
        po_receipt, data["items"], _sap_items_map(po), user, company_code
    )
    return po_receipt, warnings


@transaction.atomic
def update_fa_po_receipt(*, po_receipt, data, user, company_code) -> Tuple[POReceipt, List[str]]:
    """Change a linked PO (another PO number, other lines or quantities).

    Refused while the lock reason is set. Unsent drafts are discarded first,
    because their saved payload names line ids this update may replace.
    """
    entry = po_receipt.vehicle_entry
    _require_fa(entry)
    reason = fa_po_receipt_lock_reason(po_receipt)
    if reason:
        raise ValidationError({"detail": reason})

    po_number = data["po_number"]
    if POReceipt.objects.filter(vehicle_entry=entry, po_number=po_number).exclude(
        id=po_receipt.id
    ).exists():
        raise ValidationError({"detail": f"PO {po_number} is already linked to this entry."})

    po = get_open_fa_po(company_code, data["supplier_code"], po_number)
    discard_unsent_fa_drafts(po_receipt)

    if po_receipt.po_number != str(po.po_number):
        # Another PO: its line numbers mean other lines, so start the lines afresh.
        try:
            po_receipt.items.all().delete()
        except ProtectedError as exc:
            raise ValidationError({
                "detail": f"A posted GRPO still points at this PO's lines: {exc.protected_objects}"
            })

    po_receipt.po_number = str(po.po_number)[:30]
    po_receipt.supplier_code = (po.supplier_code or data["supplier_code"])[:30]
    po_receipt.supplier_name = (po.supplier_name or data.get("supplier_name") or "")[:150]
    po_receipt.sap_doc_entry = po.doc_entry
    po_receipt.branch_id = po.branch_id
    po_receipt.vendor_ref = (po.vendor_ref or "")[:100]
    po_receipt.po_date = po.doc_date
    if data.get("invoice_no"):
        po_receipt.invoice_no = str(data["invoice_no"])[:50]
    po_receipt.updated_by = user
    po_receipt.save()

    warnings = _save_fa_po_items(
        po_receipt, data["items"], _sap_items_map(po), user, company_code
    )
    return po_receipt, warnings


@transaction.atomic
def remove_fa_po_receipt(*, po_receipt, user) -> None:
    """Unlink the PO: discard unsent drafts, then delete the receipt and its lines."""
    _require_fa(po_receipt.vehicle_entry)
    reason = fa_po_receipt_lock_reason(po_receipt)
    if reason:
        raise ValidationError({"detail": reason})
    discard_unsent_fa_drafts(po_receipt)
    try:
        po_receipt.delete()
    except ProtectedError as exc:
        raise ValidationError({
            "detail": f"Something still points at this PO receipt: {exc.protected_objects}"
        })


def serialize_fa_po_receipt(po_receipt) -> dict:
    """The link as the FA screens show it, with whether it may still change."""
    lock_reason = fa_po_receipt_lock_reason(po_receipt)
    return {
        "id": po_receipt.id,
        "po_number": po_receipt.po_number,
        "supplier_code": po_receipt.supplier_code,
        "supplier_name": po_receipt.supplier_name,
        "sap_doc_entry": po_receipt.sap_doc_entry,
        "branch_id": po_receipt.branch_id,
        "vendor_ref": po_receipt.vendor_ref,
        "po_date": po_receipt.po_date,
        "invoice_no": po_receipt.invoice_no,
        "created_at": po_receipt.created_at,
        "updated_at": po_receipt.updated_at,
        "is_editable": lock_reason is None,
        "lock_reason": lock_reason,
        "items": [
            {
                "id": item.id,
                "sap_line_num": item.sap_line_num,
                "po_item_code": item.po_item_code,
                "item_name": item.item_name,
                "ordered_qty": item.ordered_qty,
                "received_qty": item.received_qty,
                "accepted_qty": item.accepted_qty,
                "short_qty": item.short_qty,
                "uom": item.uom,
                "unit_price": item.unit_price,
                "tax_code": item.tax_code,
                "warehouse_code": item.warehouse_code,
                "gl_account": item.gl_account,
            }
            for item in po_receipt.items.all().order_by("sap_line_num", "id")
        ],
    }
