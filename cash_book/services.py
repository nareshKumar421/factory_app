"""
Everything that changes the cash book goes through here.

Two invariants are this module's whole job:

1. **The running balance is always right.** ``CashEntry.balance_after`` is a
   stored figure, so any write that could move it has to fix the rows after it.
   Recording is the common case and costs nothing extra -- a new entry is
   appended to the end of the book, so its balance is the previous balance plus
   or minus the amount. Corrections and cancellations are rare and rewrite the
   tail (:func:`recompute_balances`).

2. **An entry sitting with an approver cannot move.** Once a bunch is sent, its
   entries are frozen until the approver decides. Rejection unfreezes them,
   which is the point of rejecting rather than deleting.
"""

from decimal import Decimal

from django.db import transaction
from django.db.models import Max, Q, Sum
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from .constants import MAX_BUNCH_ENTRIES
from .models import (
    ZERO,
    BunchStatus,
    CashBunch,
    CashDirection,
    CashEntry,
)


# ----------------------------------------------------------------------
# The balance
# ----------------------------------------------------------------------


def current_balance(company) -> Decimal:
    """Cash in hand right now: the last live entry's balance.

    Read off the tail rather than summed, so it agrees with the column on the
    screen by construction. An empty book is zero.
    """
    last = (
        CashEntry.objects.filter(company=company, is_active=True)
        .order_by("-id")
        .values_list("balance_after", flat=True)
        .first()
    )
    return last if last is not None else ZERO


def recompute_balances(company, *, from_entry_id=None) -> int:
    """Rewrite ``balance_after`` from one entry onwards. Returns rows touched.

    ``from_entry_id`` is where the book stopped being true -- the corrected or
    cancelled entry. Everything before it is untouched, so a correction to
    yesterday's row does not rewrite three years of book.

    Cancelled (``is_active=False``) entries are skipped: they contribute
    nothing to the balance, which is what cancelling one means.
    """
    opening = ZERO
    if from_entry_id is not None:
        previous = (
            CashEntry.objects.filter(
                company=company, is_active=True, id__lt=from_entry_id
            )
            .order_by("-id")
            .values_list("balance_after", flat=True)
            .first()
        )
        opening = previous if previous is not None else ZERO

    tail = CashEntry.objects.filter(company=company, is_active=True)
    if from_entry_id is not None:
        tail = tail.filter(id__gte=from_entry_id)

    running = opening
    changed = []
    for entry in tail.order_by("id").only("id", "direction", "amount", "balance_after"):
        running += entry.signed_amount
        if entry.balance_after != running:
            entry.balance_after = running
            changed.append(entry)

    if changed:
        CashEntry.objects.bulk_update(changed, ["balance_after"], batch_size=500)
    return len(changed)


# ----------------------------------------------------------------------
# Entries
# ----------------------------------------------------------------------


def _clean_payment_fields(*, direction, department, gl_account_code, gl_account_name):
    """A payment has to say where it went; a receipt has nothing to say.

    Enforced here rather than in the serializer so the rule holds for a
    management command or the admin too. A receipt comes back with the three
    fields blank whatever was passed in, so switching an entry from a payment
    to a receipt cannot leave a stale G/L head behind to be read as its own.
    """
    code = (gl_account_code or "").strip()
    name = (gl_account_name or "").strip()

    if direction == CashDirection.IN:
        return {"department": None, "gl_account_code": "", "gl_account_name": ""}

    if department is None:
        raise ValidationError(
            {"department": "Say which department the money was spent for."}
        )
    if not code:
        raise ValidationError(
            {"gl_account_code": "Pick the G/L head this payment belongs to."}
        )
    return {
        "department": department,
        "gl_account_code": code,
        "gl_account_name": name,
    }


@transaction.atomic
def record_entry(
    *,
    user,
    company,
    entry_date,
    direction,
    amount,
    detail,
    department=None,
    gl_account_code="",
    gl_account_name="",
    item="",
) -> CashEntry:
    """Write one line into the book.

    Always an append, even when ``entry_date`` is in the past: the book is kept
    in the order vouchers reach it, and the balance column follows that order.
    """
    fields = _clean_payment_fields(
        direction=direction,
        department=department,
        gl_account_code=gl_account_code,
        gl_account_name=gl_account_name,
    )

    entry = CashEntry(
        company=company,
        entry_date=entry_date,
        direction=direction,
        amount=amount,
        detail=(detail or "").strip(),
        item=(item or "").strip(),
        created_by=user,
        updated_by=user,
        **fields,
    )
    entry.balance_after = current_balance(company) + entry.signed_amount
    entry.save()
    return entry


@transaction.atomic
def update_entry(*, user, entry: CashEntry, **changes) -> CashEntry:
    """Correct an entry that has not been handed to an approver.

    Only the fields present in ``changes`` are touched. If the amount or the
    direction moved, the rest of the book is rewritten from here.
    """
    _require_unlocked(entry, verb="corrected")

    before = (entry.direction, entry.amount)

    for field in (
        "entry_date",
        "direction",
        "amount",
        "department",
        "gl_account_code",
        "gl_account_name",
        "item",
        "detail",
    ):
        if field in changes:
            setattr(entry, field, changes[field])

    for field, value in _clean_payment_fields(
        direction=entry.direction,
        department=entry.department,
        gl_account_code=entry.gl_account_code,
        gl_account_name=entry.gl_account_name,
    ).items():
        setattr(entry, field, value)

    entry.updated_by = user
    entry.save()

    if before != (entry.direction, entry.amount):
        recompute_balances(entry.company, from_entry_id=entry.id)
        entry.refresh_from_db(fields=["balance_after"])
    return entry


@transaction.atomic
def cancel_entry(*, user, entry: CashEntry) -> CashEntry:
    """Take an entry out of the book without losing it.

    Cancelled rather than deleted: a cash book that can lose a line is not a
    cash book. The row stays readable and stays out of the balance.
    """
    _require_unlocked(entry, verb="cancelled")
    if not entry.is_active:
        return entry

    entry.is_active = False
    entry.updated_by = user
    entry.save(update_fields=["is_active", "updated_by", "updated_at"])
    recompute_balances(entry.company, from_entry_id=entry.id)
    return entry


def _require_unlocked(entry: CashEntry, *, verb: str) -> None:
    if entry.is_locked:
        state = (
            "is waiting for approval"
            if entry.bunch.status == BunchStatus.PENDING
            else "has been approved"
        )
        raise ValidationError(
            f"Entry is in bunch {entry.bunch.number}, which {state}, so it "
            f"cannot be {verb}."
        )


# ----------------------------------------------------------------------
# Bunches
# ----------------------------------------------------------------------


def _next_bunch_number(company) -> int:
    highest = CashBunch.objects.filter(company=company).aggregate(Max("number"))
    return (highest["number__max"] or 0) + 1


@transaction.atomic
def send_for_approval(*, user, company, entry_ids, remarks="") -> CashBunch:
    """Bundle loose entries into a bunch and hand it to an approver.

    Every id must be a live entry of this company that is not already in a
    bunch. Partial success is refused: a bunch the custodian did not choose is
    worse than no bunch.
    """
    ids = list(dict.fromkeys(entry_ids or []))
    if not ids:
        raise ValidationError({"entry_ids": "Pick at least one entry to send."})
    if len(ids) > MAX_BUNCH_ENTRIES:
        raise ValidationError(
            {
                "entry_ids": f"A bunch carries at most {MAX_BUNCH_ENTRIES} entries; "
                f"{len(ids)} were picked."
            }
        )

    entries = list(
        CashEntry.objects.select_for_update()
        .filter(company=company, id__in=ids)
        .select_related("bunch")
    )
    found = {entry.id for entry in entries}
    missing = [entry_id for entry_id in ids if entry_id not in found]
    if missing:
        raise ValidationError(
            {"entry_ids": f"Not entries of this cash book: {_join(missing)}."}
        )

    cancelled = [entry.id for entry in entries if not entry.is_active]
    if cancelled:
        raise ValidationError(
            {"entry_ids": f"Cancelled entries cannot be sent: {_join(cancelled)}."}
        )

    already = [entry.id for entry in entries if entry.bunch_id is not None]
    if already:
        raise ValidationError(
            {"entry_ids": f"Already in a bunch: {_join(already)}."}
        )

    bunch = CashBunch.objects.create(
        company=company,
        number=_next_bunch_number(company),
        status=BunchStatus.PENDING,
        remarks=(remarks or "").strip(),
        sent_at=timezone.now(),
        sent_by=user,
        created_by=user,
        updated_by=user,
    )
    CashEntry.objects.filter(id__in=found).update(bunch=bunch, updated_by=user)
    return bunch


@transaction.atomic
def approve_bunch(*, user, bunch: CashBunch, note="") -> CashBunch:
    """Approve. The decision time is what the sheet's 'Sign Date' becomes."""
    _require_pending(bunch, verb="approved")
    bunch.status = BunchStatus.APPROVED
    bunch.decided_at = timezone.now()
    bunch.decided_by = user
    bunch.decision_note = (note or "").strip()
    bunch.updated_by = user
    bunch.save(
        update_fields=[
            "status",
            "decided_at",
            "decided_by",
            "decision_note",
            "updated_by",
            "updated_at",
        ]
    )
    return bunch


@transaction.atomic
def reject_bunch(*, user, bunch: CashBunch, note="") -> CashBunch:
    """Send it back. The entries unfreeze so they can be put right."""
    _require_pending(bunch, verb="rejected")
    reason = (note or "").strip()
    if not reason:
        raise ValidationError(
            {"note": "Say what is wrong with it -- the custodian has to know "
                     "what to fix."}
        )
    bunch.status = BunchStatus.REJECTED
    bunch.decided_at = timezone.now()
    bunch.decided_by = user
    bunch.decision_note = reason
    bunch.updated_by = user
    bunch.save(
        update_fields=[
            "status",
            "decided_at",
            "decided_by",
            "decision_note",
            "updated_by",
            "updated_at",
        ]
    )
    return bunch


@transaction.atomic
def resend_bunch(*, user, bunch: CashBunch, remarks=None) -> CashBunch:
    """Send a rejected bunch back up, once its entries have been corrected.

    The same bunch and the same number: this is the second walk of one set of
    vouchers, not a new set. The rejection note is cleared, because it no
    longer describes the bunch.
    """
    if bunch.status != BunchStatus.REJECTED:
        raise ValidationError(
            f"Bunch {bunch.number} is {bunch.get_status_display().lower()}; only "
            f"a rejected bunch can be sent again."
        )
    if not bunch.entries.filter(is_active=True).exists():
        raise ValidationError(
            f"Every entry in bunch {bunch.number} has been cancelled, so there "
            f"is nothing to send."
        )

    bunch.status = BunchStatus.PENDING
    bunch.sent_at = timezone.now()
    bunch.sent_by = user
    bunch.decided_at = None
    bunch.decided_by = None
    bunch.decision_note = ""
    if remarks is not None:
        bunch.remarks = remarks.strip()
    bunch.updated_by = user
    bunch.save()
    return bunch


def _require_pending(bunch: CashBunch, *, verb: str) -> None:
    if bunch.status != BunchStatus.PENDING:
        raise ValidationError(
            f"Bunch {bunch.number} is already "
            f"{bunch.get_status_display().lower()}, so it cannot be {verb}."
        )


def _join(ids) -> str:
    return ", ".join(str(entry_id) for entry_id in sorted(ids))


# ----------------------------------------------------------------------
# Reading
# ----------------------------------------------------------------------


def totals(queryset) -> dict:
    """In, out and the net of whatever set of entries is on screen.

    Deliberately not an opening/closing pair. The balance column follows
    recording order while the filters are mostly by date, so a "balance at the
    start of this filter" would be a number the book itself never held. What is
    true is the book's own balance, which the caller adds separately.
    """
    aggregate = queryset.aggregate(
        cash_in=Sum("amount", filter=Q(direction=CashDirection.IN)),
        cash_out=Sum("amount", filter=Q(direction=CashDirection.OUT)),
    )
    cash_in = aggregate["cash_in"] or ZERO
    cash_out = aggregate["cash_out"] or ZERO
    return {
        "cash_in": cash_in,
        "cash_out": cash_out,
        "net": cash_in - cash_out,
        "count": queryset.count(),
    }
