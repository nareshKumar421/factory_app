"""Inward returns — finalise a scanned return by issuing its Return Note.

The Return Note is the return's internal document of record: a sequential number
(``RTN-YYYYMMDD-NNNNN``) recorded on the return and rendered as a printable note
by the frontend. This flow is **internal only** — it posts nothing to SAP and
moves no stock; restocking returned goods is handled outside this flow.
"""
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from ..models import MarketplaceOrderStatus, MarketplaceReturn, MarketplaceReturnStatus

RETURN_NOTE_PREFIX = "RTN"


def _next_return_note(company):
    today = timezone.localdate()
    prefix = f"{RETURN_NOTE_PREFIX}-{today:%Y%m%d}-"
    seq = MarketplaceReturn.objects.filter(
        company=company, internal_credit_doc_num__startswith=prefix
    ).count() + 1
    return f"{prefix}{seq:05d}"


def _apply_returned_status(mp_return, user):
    """Reflect the return on the ORDER: RETURNED when everything shipped is back,
    PARTIAL when only some is. Sums returned units across all of the order's
    (non-cancelled) returns vs the ordered finished-goods quantity."""
    from .resolve_service import fg_lines, resolve_order

    order = mp_return.order
    ordered = sum(
        (Decimal(l["required_quantity"]) for l in fg_lines(resolve_order(order)["resolved_lines"])),
        Decimal("0"),
    )
    returned = Decimal("0")
    for r in order.returns.exclude(status=MarketplaceReturnStatus.CANCELLED):
        for q in r.scans.filter(is_active=True).values_list("quantity", flat=True):
            returned += Decimal(q)
    if ordered > 0 and returned >= ordered:
        order.status = MarketplaceOrderStatus.RETURNED
    elif returned > 0:
        order.status = MarketplaceOrderStatus.PARTIAL
    else:
        return
    order.updated_by = user
    order.save(update_fields=["status", "updated_by", "updated_at"])


@transaction.atomic
def submit_return(mp_return, *, user=None):
    """Assign the Return Note number, mark the return SUBMITTED, and reflect it on
    the order (RETURNED / PARTIAL).

    Idempotent: an already-submitted return keeps its note number unchanged.
    """
    if mp_return.status == MarketplaceReturnStatus.SUBMITTED:
        return mp_return

    mp_return.internal_credit_doc_num = _next_return_note(mp_return.company)
    mp_return.status = MarketplaceReturnStatus.SUBMITTED
    mp_return.submitted_by = user
    mp_return.submitted_at = timezone.now()
    mp_return.updated_by = user
    mp_return.save()
    _apply_returned_status(mp_return, user)
    return mp_return
