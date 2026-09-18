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

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Max, Q, Sum
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from .constants import MAX_BUNCH_ENTRIES
from .models import (
    ZERO,
    AdvanceDirection,
    AdvanceEntry,
    AtmAccount,
    AtmReceipt,
    CashBunch,
    CashDirection,
    CashEntry,
    EntryApprovalStatus,
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


def _clean_direction_links(*, direction, atm_account, advance_holder):
    """Each link belongs to one direction, and only one.

    A card is where a *receipt* came from; an advance holder is who a *payment*
    clears. Crossing them over would mean drawing cash off a card by spending
    it, or clearing somebody's float by putting money in the box -- neither is
    a thing that happens, and either would quietly corrupt a balance.
    """
    if direction == CashDirection.IN:
        if advance_holder is not None:
            raise ValidationError(
                {"advance_holder": "A receipt does not clear anybody's advance."}
            )
        return {"atm_account": atm_account, "advance_holder": None}

    if atm_account is not None:
        raise ValidationError(
            {"atm_account": "A payment is not drawn off a card; a receipt is."}
        )
    return {"atm_account": None, "advance_holder": advance_holder}


def _clean_payment_fields(
    *, company, direction, branch, gl_account_code, gl_account_name
):
    """A payment has to say where it went; a receipt has nothing to say.

    Enforced here rather than in the serializer so the rule holds for a
    management command or the admin too. A receipt comes back with the three
    fields blank whatever was passed in, so switching an entry from a payment
    to a receipt cannot leave a stale G/L head behind to be read as its own.
    """
    code = (gl_account_code or "").strip()
    name = (gl_account_name or "").strip()

    if direction == CashDirection.IN:
        return {"branch": None, "gl_account_code": "", "gl_account_name": ""}

    if branch is None:
        raise ValidationError(
            {"branch": "Say which branch the money was spent for."}
        )
    # Branches are per company, and so is the book. Filing a payment against
    # another company's branch would put it in a list this book never offers
    # and a report it never appears in, so it is refused here rather than in
    # the serializer -- the rule then holds for the importer and the admin too.
    if branch.company_id != company.id:
        raise ValidationError(
            {"branch": f"{branch.name} is not a branch of this company."}
        )
    if not branch.is_active:
        raise ValidationError(
            {"branch": f"{branch.name} has been retired and cannot be used."}
        )
    if not code:
        raise ValidationError(
            {"gl_account_code": "Pick the G/L head this payment belongs to."}
        )
    return {
        "branch": branch,
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
    branch=None,
    gl_account_code="",
    gl_account_name="",
    item="",
    atm_account=None,
    advance_holder=None,
) -> CashEntry:
    """Write one line into the book.

    Always an append, even when ``entry_date`` is in the past: the book is kept
    in the order vouchers reach it, and the balance column follows that order.
    """
    fields = _clean_payment_fields(
        company=company,
        direction=direction,
        branch=branch,
        gl_account_code=gl_account_code,
        gl_account_name=gl_account_name,
    )
    fields.update(
        _clean_direction_links(
            direction=direction,
            atm_account=_own_atm_account(company, atm_account),
            advance_holder=advance_holder,
        )
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
    # A payment joins the approval queue the moment it is written down; a
    # receipt never joins it at all. There is no third possibility, so there
    # is no button for one.
    if direction == CashDirection.OUT:
        entry.approval_state = EntryApprovalStatus.PENDING
        entry.approval_sent_at = timezone.now()
    else:
        entry.approval_state = EntryApprovalStatus.NOT_REQUIRED

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
        "branch",
        "gl_account_code",
        "gl_account_name",
        "item",
        "detail",
        "atm_account",
        "advance_holder",
    ):
        if field in changes:
            setattr(entry, field, changes[field])

    for field, value in _clean_payment_fields(
        company=entry.company,
        direction=entry.direction,
        branch=entry.branch,
        gl_account_code=entry.gl_account_code,
        gl_account_name=entry.gl_account_name,
    ).items():
        setattr(entry, field, value)

    for field, value in _clean_direction_links(
        direction=entry.direction,
        atm_account=_own_atm_account(entry.company, entry.atm_account),
        advance_holder=entry.advance_holder,
    ).items():
        setattr(entry, field, value)

    # Correcting a rejected payment is the answer to the rejection, so it goes
    # back into the queue rather than waiting for somebody to resend it. The
    # note that sent it back no longer describes it.
    if entry.approval_state == EntryApprovalStatus.REJECTED:
        entry.approval_state = EntryApprovalStatus.PENDING
        entry.approval_sent_at = timezone.now()
        entry.approval_decided_at = None
        entry.approval_decided_by = None
        entry.approval_note = ""
    # A direction change decides which queue, if any, it belongs in at all.
    if entry.direction == CashDirection.IN:
        entry.approval_state = EntryApprovalStatus.NOT_REQUIRED
    elif entry.approval_state == EntryApprovalStatus.NOT_REQUIRED:
        entry.approval_state = EntryApprovalStatus.PENDING
        entry.approval_sent_at = timezone.now()

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
            if entry.approval_state == EntryApprovalStatus.PENDING
            else "has been approved"
        )
        raise ValidationError(f"This entry {state}, so it cannot be {verb}.")


# ----------------------------------------------------------------------
# Bunches
# ----------------------------------------------------------------------


def _next_bunch_number(company) -> int:
    highest = CashBunch.objects.filter(company=company).aggregate(Max("number"))
    return (highest["number__max"] or 0) + 1


def _lock_entries(company, ids):
    """The entries about to be bundled, locked against a concurrent batch.

    Deliberately WITHOUT ``select_related("bunch")``. ``bunch`` is nullable, so
    selecting it joins ``cash_book_cashbunch`` as a LEFT OUTER JOIN, and
    PostgreSQL refuses ``SELECT ... FOR UPDATE`` across the nullable side of an
    outer join::

        FeatureNotSupported: FOR UPDATE cannot be applied to the nullable side
        of an outer join

    SQLite ignores row locking entirely and raises nothing, so this only ever
    showed up against a real database. Nothing here needs the bunch *object* --
    ``bunch_id`` is a column on the entry row itself, which is all the
    already-bundled check reads.
    """
    return CashEntry.objects.select_for_update().filter(
        company=company, id__in=ids
    )


@transaction.atomic
def create_bunch(*, user, company, entry_ids, remarks="") -> CashBunch:
    """Bundle approved payments into a batch to send to head office.

    Approved, because a bunch is the paperwork that follows the decision -- a
    batch carrying something nobody has agreed to would be asking Delhi to file
    a spend this factory has not finished arguing about.

    Partial success is refused: a batch the sender did not choose is worse than
    no batch, because they will mail it believing it is the one they picked.
    """
    ids = list(dict.fromkeys(entry_ids or []))
    if not ids:
        raise ValidationError({"entry_ids": "Pick at least one entry to bundle."})
    if len(ids) > MAX_BUNCH_ENTRIES:
        raise ValidationError(
            {
                "entry_ids": f"A bunch carries at most {MAX_BUNCH_ENTRIES} entries; "
                f"{len(ids)} were picked."
            }
        )

    entries = list(_lock_entries(company, ids))
    found = {entry.id for entry in entries}
    missing = [entry_id for entry_id in ids if entry_id not in found]
    if missing:
        raise ValidationError(
            {"entry_ids": f"Not entries of this cash book: {_join(missing)}."}
        )

    cancelled = [entry.id for entry in entries if not entry.is_active]
    if cancelled:
        raise ValidationError(
            {"entry_ids": f"Cancelled entries cannot be bundled: {_join(cancelled)}."}
        )

    already = [entry.id for entry in entries if entry.bunch_id is not None]
    if already:
        raise ValidationError(
            {"entry_ids": f"Already in a bunch: {_join(already)}."}
        )

    unapproved = [
        entry.id
        for entry in entries
        if entry.approval_state != EntryApprovalStatus.APPROVED
    ]
    if unapproved:
        raise ValidationError(
            {
                "entry_ids": f"Only approved payments can be bundled; these are "
                f"not: {_join(unapproved)}."
            }
        )

    bunch = CashBunch.objects.create(
        company=company,
        number=_next_bunch_number(company),
        remarks=(remarks or "").strip(),
        created_by=user,
        updated_by=user,
    )
    CashEntry.objects.filter(id__in=found).update(bunch=bunch, updated_by=user)
    return bunch


@transaction.atomic
def mark_bunch_sent(*, user, bunch: CashBunch, sent=True) -> CashBunch:
    """Record that the batch went to head office, or that it did not after all.

    The app does not send the mail, so it cannot know on its own; somebody says
    so. Reversible, because "I ticked the wrong row" is a likelier event than
    an envelope coming back.
    """
    bunch.sent_at = timezone.now() if sent else None
    bunch.sent_by = user if sent else None
    bunch.updated_by = user
    bunch.save(update_fields=["sent_at", "sent_by", "updated_by", "updated_at"])
    return bunch


@transaction.atomic
def remove_from_bunch(*, user, entry: CashEntry) -> CashEntry:
    """Take one voucher back out of a batch that has not gone yet."""
    if entry.bunch_id is None:
        return entry
    if entry.bunch.is_sent:
        raise ValidationError(
            f"Bunch {entry.bunch.number} has already been sent, so its "
            f"contents cannot be changed."
        )
    entry.bunch = None
    entry.updated_by = user
    entry.save(update_fields=["bunch", "updated_by", "updated_at"])
    return entry


def bunch_total(bunch: CashBunch) -> Decimal:
    """What the batch comes to -- the figure the old sheet used as its number."""
    total = bunch.entries.filter(is_active=True).aggregate(
        total=Sum("amount")
    )["total"]
    return total or ZERO


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


# ----------------------------------------------------------------------
# The card
# ----------------------------------------------------------------------


def _own_atm_account(company, account):
    """Refuse another company's card, the way a branch is refused."""
    if account is None:
        return None
    if account.company_id != company.id:
        raise ValidationError(
            {"atm_account": f"{account.name} is not a card of this company."}
        )
    if not account.is_active:
        raise ValidationError(
            {"atm_account": f"{account.name} is closed and cannot be drawn on."}
        )
    return account


def atm_balance(account) -> Decimal:
    """What is left on the card: opening, plus what was paid on, less what was
    drawn off.

    Withdrawals are counted off the cash receipts that name this card, because
    that is the only place they are recorded -- see :class:`AtmAccount`.
    Cancelled receipts do not count: the cash never reached the box, so it
    never left the card either.
    """
    paid_on = AtmReceipt.objects.filter(account=account, is_active=True).aggregate(
        total=Sum("amount")
    )["total"] or ZERO
    drawn_off = CashEntry.objects.filter(
        atm_account=account, is_active=True, direction=CashDirection.IN
    ).aggregate(total=Sum("amount"))["total"] or ZERO
    return (account.opening_balance or ZERO) + paid_on - drawn_off


def atm_statement(account):
    """The card's ledger: every movement in date order, with a running balance.

    Merged from two tables because a withdrawal is a cash receipt, not a row of
    its own. Sorted by date and then by when it was recorded, so two movements
    on one day read in the order they happened.
    """
    movements = [
        {
            "kind": "RECEIPT",
            "id": receipt.id,
            "date": receipt.received_on,
            "recorded": receipt.id,
            "amount": receipt.amount,
            "signed": receipt.amount,
            "detail": receipt.detail,
            "cash_entry_id": None,
        }
        for receipt in AtmReceipt.objects.filter(account=account, is_active=True)
    ] + [
        {
            "kind": "WITHDRAWAL",
            "id": entry.id,
            "date": entry.entry_date,
            "recorded": entry.id,
            "amount": entry.amount,
            "signed": -entry.amount,
            "detail": entry.detail,
            "cash_entry_id": entry.id,
        }
        for entry in CashEntry.objects.filter(
            atm_account=account, is_active=True, direction=CashDirection.IN
        )
    ]
    movements.sort(key=lambda row: (row["date"], row["recorded"]))

    running = account.opening_balance or ZERO
    for row in movements:
        running += row["signed"]
        row["balance_after"] = running
    return movements


@transaction.atomic
def record_atm_receipt(*, user, account, received_on, amount, detail="") -> AtmReceipt:
    """Money paid onto the card."""
    if not account.is_active:
        raise ValidationError(
            {"account": f"{account.name} is closed and cannot be paid onto."}
        )
    return AtmReceipt.objects.create(
        account=account,
        received_on=received_on,
        amount=amount,
        detail=(detail or "").strip(),
        created_by=user,
        updated_by=user,
    )


@transaction.atomic
def cancel_atm_receipt(*, user, receipt: AtmReceipt) -> AtmReceipt:
    """Take a receipt off the card without losing the row."""
    if not receipt.is_active:
        return receipt
    receipt.is_active = False
    receipt.updated_by = user
    receipt.save(update_fields=["is_active", "updated_by", "updated_at"])
    return receipt


# ----------------------------------------------------------------------
# Advances
# ----------------------------------------------------------------------


def advance_balance(company, person) -> Decimal:
    """What this person is still holding and has not explained.

    Cash handed over raises it. Everything else lowers it: cash handed back,
    an expense they account for, or money they laid out themselves. The
    expense is the whole point of the register -- an advance is not settled by
    being forgotten, it is settled by somebody saying where the money went.

    The lowering half is deliberately "anything that is not a handout" rather
    than a list of the kinds that lower it. A list has to be revisited every
    time a kind is added, and the one time it was not, a whole direction would
    have gone missing from every balance silently.
    """
    handed = AdvanceEntry.objects.filter(
        company=company, person=person, is_active=True
    ).aggregate(
        given=Sum("amount", filter=Q(direction=AdvanceDirection.GIVEN)),
        returned=Sum("amount", filter=~Q(direction=AdvanceDirection.GIVEN)),
    )
    explained = CashEntry.objects.filter(
        company=company,
        advance_holder=person,
        is_active=True,
        direction=CashDirection.OUT,
    ).aggregate(total=Sum("amount"))["total"] or ZERO

    return (
        (handed["given"] or ZERO) - (handed["returned"] or ZERO) - explained
    )


def advance_statement(company, person, *, include_cancelled=False):
    """One person's ledger: handouts, returns and everything they explained.

    ``include_cancelled`` brings back the rows somebody has taken out. They are
    shown, and they are worth showing -- a row that was removed is part of the
    story of an account, and without it a ledger that stopped adding up has no
    explanation on its face. But a cancelled row contributes NOTHING to the
    running balance, which is the same rule the register follows: a line that
    is out of the book must not move the figure beside it, or every balance
    below it becomes a number nobody can reproduce.
    """
    entries = AdvanceEntry.objects.filter(company=company, person=person)
    if not include_cancelled:
        entries = entries.filter(is_active=True)

    movements = [
        {
            "kind": entry.direction,
            "id": entry.id,
            "date": entry.entry_date,
            "recorded": entry.id,
            "amount": entry.amount,
            "signed": entry.signed_amount if entry.is_active else ZERO,
            "detail": entry.detail,
            "cash_entry_id": None,
            "is_active": entry.is_active,
        }
        for entry in entries
    ] + [
        {
            "kind": "EXPLAINED",
            "id": entry.id,
            "date": entry.entry_date,
            "recorded": entry.id,
            "amount": entry.amount,
            "signed": -entry.amount,
            "detail": entry.detail,
            "cash_entry_id": entry.id,
            "is_active": True,
        }
        for entry in CashEntry.objects.filter(
            company=company,
            advance_holder=person,
            is_active=True,
            direction=CashDirection.OUT,
        )
    ]
    movements.sort(key=lambda row: (row["date"], row["recorded"]))

    running = ZERO
    for row in movements:
        running += row["signed"]
        row["balance_after"] = running
    return movements


def advance_holders(company):
    """Everyone who has ever held a float here, with what they hold now.

    Includes people settled back to zero: an empty ledger is worth seeing, and
    hiding it would make a person look new the next time they take cash.
    """
    User = get_user_model()
    ids = set(
        AdvanceEntry.objects.filter(company=company, is_active=True).values_list(
            "person_id", flat=True
        )
    ) | set(
        CashEntry.objects.filter(
            company=company, is_active=True, advance_holder__isnull=False
        ).values_list("advance_holder_id", flat=True)
    )
    people = User.objects.filter(id__in=ids)
    rows = [
        {"person": person, "balance": advance_balance(company, person)}
        for person in people
    ]
    rows.sort(key=lambda row: (-row["balance"], str(row["person"])))
    return rows


@transaction.atomic
def record_advance(
    *, user, company, person, entry_date, direction, amount, detail=""
) -> AdvanceEntry:
    """Hand cash to somebody, or take it back off them.

    Neither touches the cash book. The custodian is accountable for the same
    total either way -- the money has only moved between two pockets, and it
    reaches the book as the expenses the holder eventually explains.
    """
    return AdvanceEntry.objects.create(
        company=company,
        person=person,
        entry_date=entry_date,
        direction=direction,
        amount=amount,
        detail=(detail or "").strip(),
        created_by=user,
        updated_by=user,
    )


@transaction.atomic
def cancel_advance(*, user, entry: AdvanceEntry) -> AdvanceEntry:
    """Take a handout or a return back out of the ledger, keeping the row."""
    if not entry.is_active:
        return entry
    entry.is_active = False
    entry.updated_by = user
    entry.save(update_fields=["is_active", "updated_by", "updated_at"])
    return entry


# ----------------------------------------------------------------------
# Approval, which belongs to the entry
# ----------------------------------------------------------------------


def _entries_for_decision(company, entry_ids, *, expected, verb):
    """The entries named, checked to be this company's and in the right state."""
    ids = list(dict.fromkeys(entry_ids or []))
    if not ids:
        raise ValidationError({"entry_ids": f"Pick at least one entry to {verb}."})

    entries = list(CashEntry.objects.filter(company=company, id__in=ids))
    found = {entry.id for entry in entries}
    missing = [entry_id for entry_id in ids if entry_id not in found]
    if missing:
        raise ValidationError(
            {"entry_ids": f"Not entries of this cash book: {_join(missing)}."}
        )

    cancelled = [entry.id for entry in entries if not entry.is_active]
    if cancelled:
        raise ValidationError(
            {"entry_ids": f"Cancelled entries cannot be {verb}: {_join(cancelled)}."}
        )

    wrong = [entry.id for entry in entries if entry.approval_state not in expected]
    if wrong:
        raise ValidationError(
            {
                "entry_ids": f"Not in a state that can be {verb}: {_join(wrong)}."
            }
        )
    return entries


@transaction.atomic
def decide_entries(*, user, company, entry_ids, approve: bool, note="") -> list:
    """Approve or reject entries waiting on somebody.

    A rejection must say why: the custodian has to know what to fix, and a
    rejected entry unfreezes so they can fix it.
    """
    reason = (note or "").strip()
    if not approve and not reason:
        raise ValidationError(
            {"note": "Say what is wrong with it -- the custodian has to know "
                     "what to fix."}
        )

    entries = _entries_for_decision(
        company,
        entry_ids,
        expected={EntryApprovalStatus.PENDING},
        verb="approved" if approve else "rejected",
    )
    now = timezone.now()
    for entry in entries:
        entry.approval_state = (
            EntryApprovalStatus.APPROVED if approve else EntryApprovalStatus.REJECTED
        )
        entry.approval_decided_at = now
        entry.approval_decided_by = user
        entry.approval_note = reason
        entry.updated_by = user
    CashEntry.objects.bulk_update(
        entries,
        [
            "approval_state",
            "approval_decided_at",
            "approval_decided_by",
            "approval_note",
            "updated_by",
        ],
    )
    return entries


# ----------------------------------------------------------------------
# The reconciliation at the top of the register
# ----------------------------------------------------------------------


def reconciliation(company) -> dict:
    """The six figures the register heads itself with, and their check.

    Read down the list and every rupee that ever came in is accounted for:

        cash in                       all of it that arrived
      - cash out (approved)           the part somebody has agreed was spent
      - awaiting approval             spent, but not yet agreed
      - cash in hand                  the notes still in the box
      - advance given                 out with people, not yet explained
      + owed to people                they spent their own and explained it
      = difference                    nothing left over

    **Advances and reimbursements are not the same money and are not netted.**
    A positive balance is the factory's cash in somebody's pocket. A negative
    one is the reverse: they paid for something themselves and have explained
    what for, so the factory owes them. Folding the two into one "advance"
    figure states neither -- it reads as though less is out with people than
    really is, and says nothing at all about what is owed back.

    It comes to zero because every term is read off the same ledger -- which
    is the point: it is a proof that the book adds up, and it stops being zero
    the moment something in it does not. What it cannot do is catch a shortage
    in the physical drawer, because nothing here counts the actual notes.
    """
    live = CashEntry.objects.filter(company=company, is_active=True)

    totals = live.aggregate(
        cash_in=Sum("amount", filter=Q(direction=CashDirection.IN)),
        approved_out=Sum(
            "amount",
            filter=Q(direction=CashDirection.OUT)
            & Q(approval_state=EntryApprovalStatus.APPROVED),
        ),
        # Everything spent that nobody has agreed yet -- waiting, or sent
        # back to be put right. Both are money out of the box with no
        # explanation the factory has accepted.
        awaiting_out=Sum(
            "amount",
            filter=Q(direction=CashDirection.OUT)
            & Q(
                approval_state__in=[
                    EntryApprovalStatus.PENDING,
                    EntryApprovalStatus.REJECTED,
                ]
            ),
        ),
    )
    cash_in = totals["cash_in"] or ZERO
    approved_out = totals["approved_out"] or ZERO
    awaiting_out = totals["awaiting_out"] or ZERO

    # Split at the person, not the row: somebody can be handed cash twice and
    # pay for a third thing themselves, and what matters is where they end up.
    balances = [row["balance"] for row in advance_holders(company)]
    advances = sum((b for b in balances if b > ZERO), ZERO)
    owed = -sum((b for b in balances if b < ZERO), ZERO)

    # What the book says is still ours, less what is out with people, plus
    # what people have laid out for us, is what should be in the box.
    in_hand = (cash_in - approved_out - awaiting_out) - advances + owed

    return {
        "cash_in": cash_in,
        "cash_out": approved_out,
        "awaiting_approval": awaiting_out,
        "cash_in_hand": in_hand,
        "advance_given": advances,
        "owed_to_people": owed,
        "difference": (
            cash_in
            - approved_out
            - awaiting_out
            - in_hand
            - advances
            + owed
        ),
    }
