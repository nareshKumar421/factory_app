"""Inward returns — finalise a scanned return by issuing its Return Note.

The Return Note is the return's internal document of record: a sequential number
(``RTN-YYYYMMDD-NNNNN``) recorded on the return and rendered as a printable note
by the frontend. This flow is **internal only** — it posts nothing to SAP and
moves no stock; restocking returned goods is handled outside this flow.
"""
from django.db import transaction
from django.utils import timezone

from ..models import MarketplaceReturn, MarketplaceReturnStatus

RETURN_NOTE_PREFIX = "RTN"


@transaction.atomic
def submit_return(mp_return, *, user=None):
    """Assign the Return Note number and mark the return SUBMITTED.

    Idempotent: an already-submitted return keeps its note number unchanged.
    """
    if mp_return.status == MarketplaceReturnStatus.SUBMITTED:
        return mp_return

    today = timezone.localdate()
    prefix = f"{RETURN_NOTE_PREFIX}-{today:%Y%m%d}-"
    seq = MarketplaceReturn.objects.filter(
        company=mp_return.company, internal_credit_doc_num__startswith=prefix
    ).count() + 1
    mp_return.internal_credit_doc_num = f"{prefix}{seq:05d}"
    mp_return.status = MarketplaceReturnStatus.SUBMITTED
    mp_return.submitted_by = user
    mp_return.submitted_at = timezone.now()
    mp_return.updated_by = user
    mp_return.save()
    return mp_return
