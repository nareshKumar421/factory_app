"""Small shared reads about a fixed-asset entry's GRPOs.

Kept apart from the link, entry and GRPO services so each of those can use
them without importing the others.
"""

from typing import Iterable, Optional, Set

from django.db.models import Q

from grpo.models import GRPOPosting, GRPOStatus

from ..constants import KIND, MATERIAL_KIND
from ..models import FixedAssetGRPORecord

#: The queue kinds that send a GRPOPosting of a fixed-asset entry: ours, and the
#: material kind (grpo's generic draft endpoint, the residual in the README).
SENDING_KINDS = (KIND, MATERIAL_KIND)

#: Statuses of a GRPO that is not in SAP and that the user may still edit,
#: re-send or discard -- **unless the SAP posting queue holds a live row for it**
#: (``active_source_ids``), which every caller checks as well.
#:
#: QUEUED is here on purpose. The queue handler marks a draft QUEUED while its
#: row waits (SAP down, or the switch off), but ``sap_postings.services.cancel``
#: only cancels the row and leaves the draft QUEUED. A QUEUED draft with no live
#: row is therefore stranded, not on its way: treating it as "being sent" would
#: lock the PO link, refuse "done in SAP by hand" and refuse a discard or a
#: re-send for good (grpo's own ``_grpo_on_its_way`` reads it the same way).
UNSENT_STATUSES = (GRPOStatus.DRAFT, GRPOStatus.FAILED, GRPOStatus.QUEUED)

#: Said on a stranded QUEUED draft when it is opened again for a re-send.
STRANDED_MESSAGE = (
    "Its SAP postings row was cancelled, so the app stopped sending it; "
    "nothing was posted by the app."
)


def closure_of(entry):
    """The entry's "done in SAP by hand" row, or None."""
    from ..models import FixedAssetGRPOClosure

    try:
        return entry.fa_grpo_closure
    except FixedAssetGRPOClosure.DoesNotExist:
        return None


def fa_detail_of(entry):
    """The entry's fixed-asset gate detail (``FixedAssetGateEntry``), or None."""
    from fixed_asset_gatein.models import FixedAssetGateEntry

    try:
        return entry.fixed_asset_entry
    except FixedAssetGateEntry.DoesNotExist:
        return None


def is_simulated(posting: GRPOPosting) -> bool:
    """True when ``posting`` is a simulated post (no SAP document exists)."""
    try:
        return bool(posting.fa_record.simulated)
    except FixedAssetGRPORecord.DoesNotExist:
        return False


def active_source_ids(posting_ids: Iterable[int]) -> Set[int]:
    """Which of these GRPOPostings the SAP posting queue is sending or waiting to send."""
    from sap_postings.models import ACTIVE_STATUSES, SapPosting

    ids = [pid for pid in posting_ids if pid is not None]
    if not ids:
        return set()
    return set(
        SapPosting.objects.filter(
            kind__in=SENDING_KINDS,
            source_id__in=ids,
            status__in=ACTIVE_STATUSES,
        ).values_list("source_id", flat=True)
    )


def postings_of_receipt(po_receipt):
    """Every GRPOPosting naming this receipt, through the legacy FK or the M2M."""
    return GRPOPosting.objects.filter(
        Q(po_receipt=po_receipt) | Q(po_receipts=po_receipt)
    ).distinct()


def is_on_its_way(posting: GRPOPosting, active: Optional[Set[int]] = None) -> bool:
    """A GRPO being sent: PENDING, or a DRAFT/FAILED/QUEUED with a live queue row.

    PENDING only exists inside the engine's own post transaction, so it is
    always in flight. QUEUED is *not* on its own: once its queue row is
    cancelled nothing will send it (see ``UNSENT_STATUSES``).
    """
    if posting.status == GRPOStatus.PENDING:
        return True
    if posting.status in UNSENT_STATUSES:
        active = active_source_ids([posting.id]) if active is None else active
        return posting.id in active
    return False


def is_stranded(posting: GRPOPosting, active: Optional[Set[int]] = None) -> bool:
    """A QUEUED GRPO whose queue row is gone (cancelled): nothing will send it."""
    if posting.status != GRPOStatus.QUEUED:
        return False
    active = active_source_ids([posting.id]) if active is None else active
    return posting.id not in active


def reopen_stranded(posting: GRPOPosting) -> bool:
    """Turn a stranded QUEUED draft back into a FAILED one. Returns True if it did.

    grpo's ``save_grpo_draft`` re-uses only DRAFT/FAILED drafts, so a re-send of
    a stranded draft goes through FAILED first. An ``update()``, not a save,
    for the same reason ``mark_grpo_waiting_for_sap`` uses one: a save would
    tell the GRPO group it failed. Re-checks the queue, so a row opened
    meanwhile wins.
    """
    if not is_stranded(posting):
        return False
    changed = GRPOPosting.objects.filter(id=posting.id, status=GRPOStatus.QUEUED).update(
        status=GRPOStatus.FAILED, error_message=STRANDED_MESSAGE
    )
    if changed:
        posting.status = GRPOStatus.FAILED
        posting.error_message = STRANDED_MESSAGE
    return bool(changed)


def any_on_its_way(postings) -> Optional[GRPOPosting]:
    """The first of ``postings`` that is on its way to SAP, or None."""
    postings = list(postings)
    active = active_source_ids(p.id for p in postings)
    return next((p for p in postings if is_on_its_way(p, active)), None)


def grpo_on_its_way(vehicle_entry_id: int, po_receipt_ids) -> Optional[GRPOPosting]:
    """A GRPO of these POs that the queue is waiting to send, or sending.

    The FA twin of ``grpo.views._grpo_on_its_way``, which looks only at the
    material kind: a second press of Post while the first waits for SAP would
    otherwise queue a second document, and both would post once SAP is back.
    """
    candidates = list(
        GRPOPosting.objects.filter(
            vehicle_entry_id=vehicle_entry_id,
            # FAILED too: a Retry from the SAP postings screen sends a FAILED
            # draft while it stays FAILED.
            status__in=[*UNSENT_STATUSES, GRPOStatus.PENDING],
            po_receipts__id__in=list(po_receipt_ids or []),
        ).distinct()
    )
    if not candidates:
        return None
    active = active_source_ids(p.id for p in candidates)
    return next(
        (
            p for p in candidates
            if p.id in active or p.status == GRPOStatus.PENDING
        ),
        None,
    )


def record_fa_post(posting: GRPOPosting, *, user, simulated: bool, payload=None) -> FixedAssetGRPORecord:
    """Write (or refresh) the app's record of a POSTED fixed-asset GRPO."""
    record, _ = FixedAssetGRPORecord.objects.update_or_create(
        grpo_posting=posting,
        defaults={
            "simulated": simulated,
            "sap_payload": payload if simulated else None,
            "posted_by": user if getattr(user, "pk", None) else None,
        },
    )
    return record
