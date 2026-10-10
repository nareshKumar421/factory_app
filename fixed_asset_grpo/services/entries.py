"""The fixed-asset gate entries as the Fixed Asset GRPO screen sees them.

SCOPE. Every list here, and the post, applies ``fa_scope``: an entry that was
closed as "done in SAP by hand" is gone from the work lists, and an entry that
came in before ``FIXED_ASSET_GRPO_CUTOVER`` is gone everywhere, because
accounts keyed those in SAP before this screen existed. Posting either would
receive the same asset twice.

CLOSING. "Done in SAP by hand" is for an entry the app will never post: the
backlog, a PO in another warehouse, a capital purchase booked as a service
invoice. It needs a SAP document number or a note, so the next reader knows
where the receipt went, and it is refused once the app has posted the entry
for real or has a GRPO of it on its way to SAP.

GATE PHOTOS. A fixed-asset GRPO needs an attachment (SAP rule 200019, unless
the vendor's BP group is 101), and the gate has already photographed the
invoice. When the poster uploads nothing, the gate entry's active attachments
are copied onto the GRPO instead (``gate_attachment_files``).
"""

import os
from typing import List, Optional

from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone

from driver_management.models import VehicleEntry
from gate_core.enums import GateEntryStatus
from gate_core.models import GateAttachment
from grpo.models import GRPOPosting, GRPOStatus

from .. import flags
from ..constants import FA_ENTRY_TYPE
from ..models import FixedAssetGRPOClosure
from .state import (
    active_source_ids,
    any_on_its_way,
    closure_of,
    fa_detail_of,
    is_on_its_way,
    is_simulated,
)


def fa_scope(queryset, *, include_closed: bool = False):
    """Narrow a ``VehicleEntry`` queryset to what Fixed Asset GRPO works on.

    Drops entries closed as done in SAP by hand (unless ``include_closed``)
    and, when a cut-over is set, entries that came in before it.
    """
    if not include_closed:
        queryset = queryset.filter(fa_grpo_closure__isnull=True)
    cut = flags.cutover()
    if cut:
        queryset = queryset.filter(entry_time__date__gte=cut)
    return queryset


def is_before_cutover(entry) -> bool:
    cut = flags.cutover()
    if not cut or entry.entry_time is None:
        return False
    return timezone.localtime(entry.entry_time).date() < cut


def fa_entries(company_code: str):
    """Every live FA gate entry of the company (no scope applied)."""
    return VehicleEntry.objects.filter(
        company__code=company_code,
        entry_type=FA_ENTRY_TYPE,
        is_active=True,
    )


def needs_po_entries(company_code: str, *, year: Optional[int] = None,
                     month: Optional[int] = None):
    """Completed FA entries with no PO linked yet, newest first ("Needs PO")."""
    queryset = fa_scope(
        fa_entries(company_code).filter(
            status=GateEntryStatus.COMPLETED,
            po_receipts__isnull=True,
        )
    )
    if year and month:
        queryset = queryset.filter(entry_time__year=year, entry_time__month=month)
    return (
        queryset.select_related("fixed_asset_entry")
        .prefetch_related("fixed_asset_entry__items")
        .distinct()
        .order_by("-entry_time")
    )


def needs_po_row(entry) -> dict:
    detail = fa_detail_of(entry)
    return {
        "vehicle_entry_id": entry.id,
        "entry_no": entry.entry_no,
        "entry_time": entry.entry_time,
        "status": entry.status,
        "work_order_number": getattr(detail, "work_order_number", "") or "",
        "supplier_name": getattr(detail, "supplier_name", "") or "",
        "invoice_number": getattr(detail, "invoice_number", "") or "",
        "item_count": detail.items.count() if detail else 0,
    }


def gate_attachment_files(vehicle_entry) -> List[ContentFile]:
    """The gate entry's active attachments, read into memory as new files.

    Copies, not references: the GRPO attachment row stores its own file under
    ``grpo_attachments/``, and the gate's soft-delete must never reach it.
    """
    files = []
    for attachment in GateAttachment.objects.filter(
        gate_entry=vehicle_entry, is_active=True
    ).order_by("uploaded_at", "id"):
        if not attachment.file:
            continue
        attachment.file.open("rb")
        try:
            data = attachment.file.read()
        finally:
            attachment.file.close()
        files.append(ContentFile(data, name=os.path.basename(attachment.file.name)))
    return files


def open_draft_of(vehicle_entry) -> Optional[GRPOPosting]:
    """The entry's latest unposted GRPO (DRAFT, FAILED or QUEUED), or None."""
    return (
        GRPOPosting.objects.filter(
            vehicle_entry=vehicle_entry,
            status__in=[GRPOStatus.DRAFT, GRPOStatus.FAILED, GRPOStatus.QUEUED],
        )
        .prefetch_related("attachments")
        .order_by("-created_at", "-id")
        .first()
    )


def _closure_dict(closure) -> Optional[dict]:
    if closure is None:
        return None
    by = closure.closed_by
    return {
        "sap_doc_num": closure.sap_doc_num,
        "note": closure.note,
        "closed_at": closure.closed_at,
        "closed_by": (by.get_full_name() if hasattr(by, "get_full_name") else str(by)) if by else "",
    }


def fa_entry_detail(vehicle_entry, company_code: str) -> dict:
    """Everything the FA preview screen shows about one entry."""
    from .po_link import entry_lock_reason, serialize_fa_po_receipt

    detail = fa_detail_of(vehicle_entry)
    items = []
    if detail is not None:
        for item in detail.items.select_related("asset_category", "unit").order_by("line_no"):
            items.append({
                "line_no": item.line_no,
                "asset_category": getattr(item.asset_category, "category_name", "") or "",
                "asset_name": item.asset_name,
                "serial_number": item.serial_number or "",
                "quantity": item.quantity,
                "unit": getattr(item.unit, "name", "") or "",
            })

    gate_attachments = []
    for att in GateAttachment.objects.filter(gate_entry=vehicle_entry, is_active=True).order_by(
        "uploaded_at", "id"
    ):
        try:
            url = att.file.url if att.file else ""
        except ValueError:
            url = ""
        gate_attachments.append({
            "id": att.id,
            "name": os.path.basename(att.file.name) if att.file else "",
            "url": url,
        })

    draft = open_draft_of(vehicle_entry)
    open_draft = None
    if draft is not None:
        open_draft = {
            "id": draft.id,
            "status": draft.status,
            "error_message": draft.error_message,
            "request_payload": draft.request_payload,
            "on_its_way": is_on_its_way(draft),
            "attachments": [
                {"id": att.id, "name": att.original_filename}
                for att in draft.attachments.all()
            ],
        }

    posted = [
        {
            "id": posting.id,
            "sap_doc_num": posting.sap_doc_num,
            "sap_doc_entry": posting.sap_doc_entry,
            "posted_at": posting.posted_at,
            "simulated": is_simulated(posting),
        }
        for posting in GRPOPosting.objects.filter(
            vehicle_entry=vehicle_entry, status=GRPOStatus.POSTED
        ).select_related("fa_record").order_by("-posted_at", "-id")
    ]

    receipts = list(vehicle_entry.po_receipts.prefetch_related("items").order_by("id"))
    return {
        "vehicle_entry_id": vehicle_entry.id,
        "entry_no": vehicle_entry.entry_no,
        "status": vehicle_entry.status,
        "entry_time": vehicle_entry.entry_time,
        "remarks": vehicle_entry.remarks or "",
        "is_before_cutover": is_before_cutover(vehicle_entry),
        "work_order_number": getattr(detail, "work_order_number", "") or "",
        "supplier_name": getattr(detail, "supplier_name", "") or "",
        "invoice_number": getattr(detail, "invoice_number", "") or "",
        "fa_remarks": getattr(detail, "remarks", "") or "",
        "items": items,
        "gate_attachments": gate_attachments,
        "po_receipt_ids": [r.id for r in receipts],
        "po_receipts": [serialize_fa_po_receipt(r) for r in receipts],
        "link_lock_reason": entry_lock_reason(vehicle_entry),
        "open_draft": open_draft,
        "posted_grpos": posted,
        "closure": _closure_dict(closure_of(vehicle_entry)),
        "sap": flags.flags_for(company_code),
    }


def _real_posted(vehicle_entry) -> Optional[GRPOPosting]:
    for posting in GRPOPosting.objects.filter(
        vehicle_entry=vehicle_entry, status=GRPOStatus.POSTED
    ).select_related("fa_record"):
        if not is_simulated(posting):
            return posting
    return None


def entry_in_flight(vehicle_entry) -> Optional[GRPOPosting]:
    """A GRPO of this entry on its way to SAP, or None."""
    return any_on_its_way(
        GRPOPosting.objects.filter(
            vehicle_entry=vehicle_entry,
            status__in=[
                GRPOStatus.DRAFT, GRPOStatus.FAILED, GRPOStatus.QUEUED, GRPOStatus.PENDING,
            ],
        )
    )


@transaction.atomic
def close_fa_entry(*, vehicle_entry, user, sap_doc_num: str = "", note: str = "") -> FixedAssetGRPOClosure:
    """Mark an FA entry as done in SAP by hand. Raises ``ValueError`` when refused."""
    if (vehicle_entry.entry_type or "") != FA_ENTRY_TYPE:
        raise ValueError("This is not a fixed-asset gate entry.")
    # Serialise against a concurrent post or close of the same entry. The row is
    # only locked, never saved: a completed gate entry refuses a save.
    list(
        VehicleEntry.objects.select_for_update()
        .filter(pk=vehicle_entry.pk)
        .values_list("pk", flat=True)
    )

    sap_doc_num = (sap_doc_num or "").strip()[:30]
    note = (note or "").strip()
    if not sap_doc_num and not note:
        raise ValueError("Give the SAP document number or a note saying where this went.")
    if closure_of(vehicle_entry) is not None:
        raise ValueError("This entry is already marked as done in SAP by hand.")
    posted = _real_posted(vehicle_entry)
    if posted is not None:
        raise ValueError(
            f"The app already posted this entry as SAP GRPO {posted.sap_doc_num}; "
            f"there is nothing left to do by hand."
        )
    if entry_in_flight(vehicle_entry) is not None:
        raise ValueError(
            "A GRPO of this entry is on its way to SAP. Wait for it, or cancel it "
            "in SAP postings, before closing the entry by hand."
        )
    return FixedAssetGRPOClosure.objects.create(
        vehicle_entry=vehicle_entry,
        sap_doc_num=sap_doc_num,
        note=note,
        closed_by=user,
    )


@transaction.atomic
def reopen_fa_entry(*, vehicle_entry, user) -> None:
    """Undo "done in SAP by hand": the entry is back in the lists."""
    deleted, _ = FixedAssetGRPOClosure.objects.filter(vehicle_entry=vehicle_entry).delete()
    if not deleted:
        raise ValueError("This entry is not marked as done in SAP by hand.")


def entry_state(entry, *, active_ids=None) -> str:
    """One word for the all-entries list: where this FA entry stands."""
    if entry.status == GateEntryStatus.CANCELLED:
        return "CANCELLED"
    if closure_of(entry) is not None:
        return "CLOSED"
    postings = list(entry.grpo_postings.all())
    posted = [p for p in postings if p.status == GRPOStatus.POSTED]
    if any(not is_simulated(p) for p in posted):
        return "POSTED"
    if posted:
        return "SIMULATED"
    if active_ids is None:
        active_ids = active_source_ids(p.id for p in postings)
    if any(is_on_its_way(p, active_ids) for p in postings):
        return "SENDING"
    if entry.status not in (GateEntryStatus.COMPLETED, GateEntryStatus.QC_COMPLETED):
        return "AT_GATE"
    if not entry.po_receipts.exists():
        return "NEEDS_PO"
    return "READY"
