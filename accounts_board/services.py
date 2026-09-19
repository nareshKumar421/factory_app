"""
accounts_board/services.py

The accounts dashboard, composed server-side in one read.

WHAT THIS IS
------------
The cash box, summarised: what came in, what went out, what is still in the
drawer, what is out with people, and what is waiting to go to head office. Every
figure is read off ``cash_book`` and nothing else -- no SAP call is made
anywhere in this app, so no tile can go stale behind a HANA outage and the
section machinery's outage latch never fires.

THE FOUR HEADLINE FIGURES
-------------------------
``imprest_issued``  paid ONTO the imprest card(s), over N top-ups.
``cash_issued``     drawn OFF them at a machine, over N withdrawals.
``pending_ho``      vouchers not yet sent to head office.
``cash_in_hand``    what should physically be in the drawer.

The first two are one float seen from each end, which is what makes them a
pair worth standing side by side: money goes onto the card, and money comes
off it. They ARE comparable -- and what separates them over the card's life is
what is still on it, reported as ``imprest.card_balance`` and computed
properly rather than by subtracting one tile from the other.

**Spending is deliberately not one of the four.** What was actually paid out
of the box is a third population again, and it is the one the breakdown below
totals; it travels as ``paid_out`` and is stated as a companion line rather
than given a card of its own. Four similar-looking lakh figures in one column
is how somebody subtracts two that were never related.

A PERIOD, AND ONE FIGURE THAT REFUSES TO HAVE ONE
--------------------------------------------------
The screen carries a month selector, and the movement tiles follow it: what was
received, what was paid out, what each expense head came to. Those are flows and
a flow without a period is meaningless.

**Cash in hand is not a flow and is not filtered.** It is a closing balance --
what should physically be in the drawer right now -- and a "September cash in
hand" is not a smaller truth than the real one, it is a fabricated one. The
drawer does not reset on the first of the month. So the balance is always read
whole, from the register's own reconciliation, and the tile is labelled
"closing balance" to say which of the two kinds of number it is.

That distinction is the single most important thing in this file. Three tiles
answer "during September" and one answers "as of now", they sit in the same
column, and only the labels stop somebody subtracting one from another.

CASH IN HAND IS NOT ``imprest issued - cash issued``
----------------------------------------------------
Tempting, and wrong twice over: those two are the CARD's two ends and say
nothing about the drawer, and even the drawer's own in-and-out ignores the
money sitting out with people.
``cash_book.services.reconciliation`` already computes the real one and proves
it --

    cash in - approved out - awaiting - advances + owed = cash in hand

-- so this module does not reimplement it. It calls that function and passes
the whole identity down, ``difference`` included, so the screen can show that
the book balances rather than asserting it.

**The balance can be negative, and on the live book it has been** -- it read
-22,555 on Jivo Oil during this screen's development and +50,414 a day later,
once the receipts behind the spend were written up. That is the normal life of
the figure, not a fault: a negative means the register says more has left the
box than ever entered it, which is real, reportable, and almost always a receipt
nobody has typed yet.

So it is never clamped at zero, and ``in_hand_negative`` flags it rather than
leaving every consumer to re-derive the sign. A finance screen that cannot show
a negative hides the one number somebody needs to act on.

WHAT "PENDING FROM HEAD OFFICE" COUNTS
---------------------------------------
Vouchers that have not gone to head office yet: the entries inside bunches not
marked sent, plus approved entries sitting in no bunch at all. They are listed
as items because that is the question being asked of them -- *what* is waiting,
not merely how much.

It is honest about its blind spot. A bunch that HAS gone but has not been
reimbursed is indistinguishable here, because the register records no
reimbursement anywhere. So the tile is titled by what it measures and carries
the count of already-sent bunches beside it, rather than implying that
everything sent has been funded.

On the live book this reads zero: all 47 bunches are marked sent and no approved
voucher is sitting outside one. That is a true answer rather than an empty one,
which is exactly why ``sent_bunches`` travels beside it -- "nothing waiting, 47
already gone" is a different statement from "no data", and on a dashboard the
two zeroes look identical without it.
"""

from __future__ import annotations

import calendar
import logging
from datetime import date
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from django.contrib.auth import get_user_model
from django.db.models import Count, Max, Q, Sum
from django.utils import timezone

from cash_book.models import (
    AdvanceDirection,
    AdvanceEntry,
    AtmAccount,
    AtmReceipt,
    CashBunch,
    CashDirection,
    CashEntry,
    EntryApprovalStatus,
)
from cash_book.services import advance_holders, atm_balance, reconciliation
from control_boards.sections import SectionBuilder

from .constants import (
    CLASSIFIED_CODES,
    DETAIL_BUCKETS,
    MAX_HEAD_ROWS,
    MAX_PEOPLE_ROWS,
    MAX_PENDING_ROWS,
    SALARY_PERSON_CODES,
)
from .permissions import may_name_people

logger = logging.getLogger(__name__)

ZERO = Decimal("0.00")

#: How often the screen re-reads itself. The cash book is typed up by one person
#: a few times a day, so a faster poll would only add load to show the same
#: figure.
ACCOUNTS_BOARD_REFRESH_SECONDS = 300

#: The two approval states that mean "out of the drawer, nobody has agreed it".
#: Rejected belongs with pending: the money is just as gone, and the voucher is
#: now somebody's to fix, which is more urgent rather than less.
UNAGREED_STATES = (EntryApprovalStatus.PENDING, EntryApprovalStatus.REJECTED)

#: What a masked person is called for a reader who may not see names. Numbered,
#: so thirteen holders still read as thirteen people rather than collapsing into
#: one anonymous row.
MASKED_LABEL = "Person {n}"

#: The same for a salary voucher, whose label is free text that names somebody
#: mid-sentence. Masked whole -- there is no safe way to show part of a
#: narrative.
MASKED_VOUCHER_LABEL = "Voucher {n}"

#: Item values that carry no information: the custodian's generic words, where
#: the name lives in the narrative instead. Lower-cased, and the misspelling is
#: the register's own -- "Advacne" appears on 20 of the 26 live salary rows.
GENERIC_ITEM_WORDS = frozenset(
    {"advacne", "advance", "salary", "increment", "advances"}
)


def _money(value) -> float:
    """A Decimal as JSON.

    Float rather than string because every consumer is a chart or a formatted
    tile, and the alternative -- strings the client must parse before it can
    compare them -- is how a sort ends up alphabetical. Precision is not at risk
    on a five-figure rupees-and-paise book.
    """
    return float(value or ZERO)


def month_bounds(year: int, month: int) -> Tuple[date, date]:
    """First and last day of a month, inclusive both ends.

    Inclusive because ``entry_date`` is a DateField: a half-open range would
    need the first of the next month and invites the off-by-one where every
    month silently borrows the next one's first day.
    """
    last = calendar.monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last)


class AccountsBoardService(SectionBuilder):
    """The whole accounts dashboard, for one company, in one read."""

    #: Asks for the newest month this book has entries in, whatever that is.
    #: The screen's default, so it opens on the month somebody is working in
    #: rather than on a five-month total.
    LATEST = "latest"

    def __init__(self, company, *, user=None, today=None, period=None):
        """``period`` is a ``(year, month)`` pair, :data:`LATEST`, or None.

        None means the whole book, and is a real choice on this screen rather
        than a missing default -- which is why :data:`LATEST` has to be asked
        for by name. Resolving "latest" HERE rather than on the client is what
        lets the page open on September in one round trip instead of fetching
        the whole book, reading the month list off it and fetching again.

        A book with no entries at all resolves ``latest`` to None: there is no
        newest month, and inventing one would open the screen on a month that
        never existed.
        """
        self.company = company
        self.user = user
        self.today = today or timezone.localdate()

        if period == self.LATEST:
            period = self._latest_period()
        self.period = period
        self.period_from, self.period_to = (
            month_bounds(*period) if period else (None, None)
        )
        # Resolved once: several sections ask the same question, and asking the
        # permission layer six times invites the six answers drifting apart.
        self.may_name = may_name_people(user) if user is not None else True
        self._init_sections()

    # ------------------------------------------------------------------ build

    def build(self) -> Dict[str, Any]:
        """Every band, each absent-able on its own.

        ``needs_sap=False`` throughout, so a section can only go missing by
        being withheld or by genuinely failing -- never by an outage.
        """
        feed = "cash_book"

        def band(name, fn):
            return self.section(name, fn, needs_sap=False, feed=feed)

        return {
            "headline": band("headline", self._headline),
            "imprest": band("imprest", self._imprest),
            "cash_issued": band("cash_issued", self._cash_issued),
            "pending_ho": band("pending_ho", self._pending_ho),
            "detail": band("detail", self._detail),
            "salary": band("salary", self._salary),
            "meta": {
                "company": getattr(self.company, "name", ""),
                "as_of": self.today.isoformat(),
                "generated_at": timezone.now().isoformat(),
                "refresh_seconds": ACCOUNTS_BOARD_REFRESH_SECONDS,
                "period": (
                    {
                        "year": self.period[0],
                        "month": self.period[1],
                        "from": self.period_from.isoformat(),
                        "to": self.period_to.isoformat(),
                    }
                    if self.period
                    else None
                ),
                "periods": self._available_periods(),
                # So the client never has to infer from the shape of the data
                # whether a name is missing because there is none or because
                # this reader may not have it.
                "names_visible": self.may_name,
                **self.section_meta(),
            },
        }

    # --------------------------------------------------------------- helpers

    @property
    def _all(self):
        """Every live entry, whatever the period. A cancelled voucher is kept
        by the register but is not money, so it is excluded everywhere."""
        return CashEntry.objects.filter(company=self.company, is_active=True)

    @property
    def _live(self):
        """Live entries inside the selected period."""
        queryset = self._all
        if self.period:
            queryset = queryset.filter(
                entry_date__gte=self.period_from, entry_date__lte=self.period_to
            )
        return queryset

    def _latest_period(self) -> Optional[Tuple[int, int]]:
        """The newest month with an entry in it, or None on an empty book."""
        newest = CashEntry.objects.filter(
            company=self.company, is_active=True
        ).dates("entry_date", "month", order="DESC").first()
        return (newest.year, newest.month) if newest else None

    def _available_periods(self) -> List[Dict[str, Any]]:
        """The months this book actually has entries in, newest first.

        Offered instead of an open date picker so the selector cannot land on a
        month that was never traded and show a screen of zeroes -- which reads
        as a broken dashboard rather than as an empty month.
        """
        seen = (
            self._all.dates("entry_date", "month", order="DESC")
        )
        return [
            {
                "year": when.year,
                "month": when.month,
                "label": f"{calendar.month_name[when.month]} {when.year}",
            }
            for when in seen
        ]

    def _person_label(self, person, index: int) -> str:
        """A person's name, or a stable stand-in for a reader who may not see it."""
        if not self.may_name:
            return MASKED_LABEL.format(n=index + 1)
        if person is None:
            return "Unattributed"
        return (
            getattr(person, "full_name", "")
            or getattr(person, "email", "")
            or f"User {getattr(person, 'id', '?')}"
        )

    @staticmethod
    def _span(queryset, field: str) -> Dict[str, Optional[str]]:
        """First and last date in a set.

        Every total that is not period-bound carries one. A lifetime total with
        no range on it is the most quietly misleading thing a finance screen can
        print, because it looks exactly like a month-to-date one.
        """
        ordered = queryset.order_by(field).values_list(field, flat=True)
        first = ordered.first()
        last = queryset.order_by(f"-{field}").values_list(field, flat=True).first()
        return {
            "from": first.isoformat() if first else None,
            "to": last.isoformat() if last else None,
        }

    # -------------------------------------------------------------- headline

    def _headline(self) -> Dict[str, Any]:
        """The four big figures.

        Three are flows and follow the period selector. The fourth is a balance
        and does not -- see the module docstring. ``reconciliation()`` is the
        register's own function and is not reimplemented, so the screen and the
        register can never disagree about what is in the drawer.
        """
        recon = reconciliation(self.company)
        in_hand = recon["cash_in_hand"]

        # "Imprest issued" is what was loaded ONTO the card -- see _imprest for
        # why that is a different population from the cash that reached the box,
        # and why the two are never added.
        loads = AtmReceipt.objects.filter(
            account__company=self.company, is_active=True
        )
        if self.period:
            loads = loads.filter(
                received_on__gte=self.period_from, received_on__lte=self.period_to
            )
        period_in = loads.aggregate(total=Sum("amount"), count=Count("id"))

        # The other side of the same float: cash taken OFF the card at a
        # machine. That is what "cash issued" means here -- see the note below.
        receipts = self._live.filter(direction=CashDirection.IN).aggregate(
            total=Sum("amount"),
            count=Count("id"),
            off_card=Sum("amount", filter=Q(atm_account__isnull=False)),
            off_card_count=Count("id", filter=Q(atm_account__isnull=False)),
        )
        spend = self._live.filter(direction=CashDirection.OUT).aggregate(
            total=Sum("amount"), count=Count("id")
        )
        pending = self._pending_ho_entries().aggregate(
            total=Sum("amount"), count=Count("id")
        )

        return {
            # --- flows, inside the selected period -------------------------
            # Loaded ONTO the imprest card(s), over N top-ups.
            "imprest_issued": _money(period_in["total"]),
            "imprest_count": period_in["count"] or 0,
            # Taken OFF it at a machine, over N withdrawals. The two are the
            # same float seen from each end, so they ARE comparable -- unlike
            # the card total and the spending total, which are not.
            "cash_issued": _money(receipts["off_card"]),
            "cash_issued_count": receipts["off_card_count"] or 0,
            # Every rupee that reached the drawer, however it got there: the
            # withdrawals above PLUS cash handed straight in by somebody. On
            # the live book those extras are 16,149 over the whole register,
            # which is why this is reported rather than assumed equal.
            "into_box": _money(receipts["total"]),
            "into_box_count": receipts["count"] or 0,
            # What was actually spent out of the box. It has no card of its own
            # any more, so it travels here for the tile that states it as a
            # companion figure -- and it is what the breakdown below totals.
            "paid_out": _money(spend["total"]),
            "paid_out_count": spend["count"] or 0,
            "pending_ho": _money(pending["total"]),
            "pending_ho_count": pending["count"] or 0,
            # --- a balance, always whole -----------------------------------
            "cash_in_hand": _money(in_hand),
            "in_hand_negative": in_hand < ZERO,
            # The rest of the register's identity, so the screen can show that
            # the book balances instead of asserting it.
            "reconciliation": {
                "cash_in": _money(recon["cash_in"]),
                "cash_out": _money(recon["cash_out"]),
                "awaiting_approval": _money(recon["awaiting_approval"]),
                "advance_given": _money(recon["advance_given"]),
                "owed_to_people": _money(recon["owed_to_people"]),
                "difference": _money(recon["difference"]),
                "balances": recon["difference"] == ZERO,
            },
        }

    # --------------------------------------------------------------- imprest

    def _imprest(self) -> Dict[str, Any]:
        """Money paid ONTO the imprest cards, top-up by top-up.

        WHAT "IMPREST ISSUED" COUNTS HERE
        ----------------------------------
        The card, not the drawer. This is the sum of :class:`AtmReceipt` --
        every time somebody loaded the imprest debit card -- because that is
        what "imprest issued" means to the person asking: how much has been put
        onto the card for the custodian to spend.

        It is deliberately NOT the cash that arrived in the box. Those are two
        different events and the register keeps them apart: money is paid onto
        the card, and later drawn off it at a machine, and that withdrawal is
        the cash receipt. Reporting both under one heading would double the
        money, so the drawer figure is returned separately as ``into_box`` and
        labelled, never added.

        **This breaks the tempting subtraction, and that is correct.** Imprest
        issued minus cash issued is NOT cash in hand: the card total and the
        drawer total are different populations, and the real balance comes from
        the register's own reconciliation. The screen labels each accordingly.

        ROWS ARE INDIVIDUAL TOP-UPS
        ----------------------------
        One row per loading, newest first -- not one row per card. There is a
        single card on the live book, so grouping by card produced a table with
        exactly one line in it, which answered nothing. The question the panel
        is really asked is "when was it topped up, and by how much", and that
        is a list of events.
        """
        loads = AtmReceipt.objects.filter(
            account__company=self.company, is_active=True
        )
        if self.period:
            loads = loads.filter(
                received_on__gte=self.period_from, received_on__lte=self.period_to
            )

        totals = loads.aggregate(total=Sum("amount"), count=Count("id"))

        rows = [
            {
                "id": row["id"],
                # A card's name is the card's, not private data -- it is printed
                # on the thing -- so it is shown to every reader, unmasked.
                "name": row["account__name"],
                "amount": _money(row["amount"]),
                "last_updated": row["received_on"].isoformat(),
                "detail": row["detail"] or "",
            }
            for row in loads.order_by("-received_on", "-id").values(
                "id", "account__name", "amount", "received_on", "detail"
            )[:MAX_PENDING_ROWS]
        ]

        # Per card, kept for "to how many holders" -- the headline count is
        # top-ups, which is a different number and answers a different question.
        cards = (
            loads.values("account__id", "account__name")
            .annotate(total=Sum("amount"), count=Count("id"), last=Max("received_on"))
            .order_by("-total")
        )

        # The drawer, reported beside the card and never folded into it.
        receipts = self._live.filter(direction=CashDirection.IN)
        drawer = receipts.aggregate(
            total=Sum("amount"),
            count=Count("id"),
            from_card=Sum("amount", filter=Q(atm_account__isnull=False)),
            card_count=Count("id", filter=Q(atm_account__isnull=False)),
        )
        into_box = drawer["total"] or ZERO
        from_card = drawer["from_card"] or ZERO

        return {
            # Loaded onto the card(s). The headline figure.
            "total": _money(totals["total"]),
            "count": totals["count"] or 0,
            "holders": len(cards),
            "rows": rows,
            "truncated": (totals["count"] or 0) > len(rows),
            "cards": [
                {
                    "id": card["account__id"],
                    "name": card["account__name"],
                    "amount": _money(card["total"]),
                    "count": card["count"] or 0,
                    "last_updated": (
                        card["last"].isoformat() if card["last"] else None
                    ),
                }
                for card in cards
            ],
            # What is left ON the card right now.
            #
            # A BALANCE, so like cash in hand it ignores the period entirely --
            # and it is NOT this period's top-ups less this period's
            # withdrawals. It is the card's opening figure plus everything ever
            # loaded less everything ever drawn, which is the register's own
            # `atm_balance`. A month's arithmetic would give a different number
            # and look just as plausible.
            "card_balance": _money(
                sum(
                    (
                        atm_balance(account)
                        for account in AtmAccount.objects.filter(
                            company=self.company, is_active=True
                        )
                    ),
                    ZERO,
                )
            ),
            # A DIFFERENT population: cash that reached the drawer. Sent so the
            # screen can state it rather than leaving a reader to assume the
            # card total and the box total are the same money.
            "into_box": {
                "total": _money(into_box),
                "count": drawer["count"] or 0,
                "drawn_off_card": _money(from_card),
                "drawn_off_card_count": drawer["card_count"] or 0,
                "handed_in": _money(into_box - from_card),
                "handed_in_count": (drawer["count"] or 0)
                - (drawer["card_count"] or 0),
            },
            "span": self._span(loads, "received_on"),
        }

    # ----------------------------------------------------------- cash issued

    def _cash_issued(self) -> Dict[str, Any]:
        """Money out of the box: how much, over how many vouchers, to whom.

        "How many issues, to how many persons" is answered off the ADVANCE
        register rather than the payment rows, and the difference matters: a
        payment's payee is free text in the narrative and cannot be counted,
        while an advance names a real person. So the payment side reports
        vouchers and the people side reports holders, each saying which it is.
        """
        payments = self._live.filter(direction=CashDirection.OUT)

        state = payments.aggregate(
            total=Sum("amount"),
            count=Count("id"),
            approved=Sum(
                "amount", filter=Q(approval_state=EntryApprovalStatus.APPROVED)
            ),
            approved_count=Count(
                "id", filter=Q(approval_state=EntryApprovalStatus.APPROVED)
            ),
            pending=Sum("amount", filter=Q(approval_state=EntryApprovalStatus.PENDING)),
            pending_count=Count(
                "id", filter=Q(approval_state=EntryApprovalStatus.PENDING)
            ),
            rejected=Sum(
                "amount", filter=Q(approval_state=EntryApprovalStatus.REJECTED)
            ),
            rejected_count=Count(
                "id", filter=Q(approval_state=EntryApprovalStatus.REJECTED)
            ),
        )

        handouts = AdvanceEntry.objects.filter(
            company=self.company, is_active=True, direction=AdvanceDirection.GIVEN
        )
        if self.period:
            handouts = handouts.filter(
                entry_date__gte=self.period_from, entry_date__lte=self.period_to
            )
        handout_totals = handouts.aggregate(
            total=Sum("amount"), count=Count("id"), people=Count("person", distinct=True)
        )

        return {
            "total": _money(state["total"]),
            # The whiteboard's "how many debits": one per payment voucher.
            "debits": state["count"] or 0,
            "states": [
                {
                    "key": "APPROVED",
                    "label": "Approved",
                    "amount": _money(state["approved"]),
                    "count": state["approved_count"] or 0,
                },
                {
                    "key": "PENDING",
                    "label": "Awaiting approval",
                    "amount": _money(state["pending"]),
                    "count": state["pending_count"] or 0,
                },
                {
                    "key": "REJECTED",
                    "label": "Sent back",
                    "amount": _money(state["rejected"]),
                    "count": state["rejected_count"] or 0,
                },
            ],
            "by_branch": [
                {
                    "label": row["branch__name"] or "Unassigned",
                    "amount": _money(row["amount"]),
                    "count": row["count"] or 0,
                }
                for row in payments.values("branch__name")
                .annotate(amount=Sum("amount"), count=Count("id"))
                .order_by("-amount")
            ],
            "handouts": {
                "total": _money(handout_totals["total"]),
                "count": handout_totals["count"] or 0,
                "people": handout_totals["people"] or 0,
            },
            "holders": self._holders(),
            "span": self._span(payments, "entry_date"),
        }

    def _holders(self) -> Dict[str, Any]:
        """Who is holding the factory's cash, and who the factory owes.

        The two are NOT netted, for the reason ``cash_book.services`` gives at
        length: a positive balance is our money in somebody's pocket, a negative
        one is their money in our till, and one figure covering both states
        neither.

        Balances are as-of-now and are deliberately NOT period-filtered -- what
        somebody is holding is a position, like cash in hand, not a flow.

        People settled back to zero are dropped, unlike in the register itself:
        a dashboard answers "what is outstanding now", and a wall of zeroes
        buries the four rows that are not.
        """
        rows = advance_holders(self.company)

        holding: List[Dict[str, Any]] = []
        owed: List[Dict[str, Any]] = []
        for index, row in enumerate(rows):
            balance = row["balance"]
            if balance == ZERO:
                continue
            record = {
                "name": self._person_label(row["person"], index),
                "amount": _money(abs(balance)),
                "last_updated": self._last_cleared(row["person"]),
            }
            (holding if balance > ZERO else owed).append(record)

        def summed(bucket):
            return {
                "rows": bucket[:MAX_PEOPLE_ROWS],
                "people": len(bucket),
                "total": _money(sum((Decimal(str(r["amount"])) for r in bucket), ZERO)),
            }

        return {"holding": summed(holding), "owed": summed(owed)}

    def _last_cleared(self, person) -> Optional[str]:
        """When this person last explained some of what they were holding.

        A payment recorded against them as ``advance_holder`` IS the act of
        clearing -- the moment cash they held stopped being an advance and
        became an expense -- so the most recent one is the answer. Somebody
        handed cash who has never explained any of it has none, which is exactly
        the row worth looking at.

        Read over the whole book, not the period: "last cleared" means last,
        and a date filtered to September would show blank for somebody who
        settled in August and read as though they never had.
        """
        last = (
            self._all.filter(direction=CashDirection.OUT, advance_holder=person)
            .order_by("-entry_date")
            .values_list("entry_date", flat=True)
            .first()
        )
        return last.isoformat() if last else None

    # ------------------------------------------------------------ pending HO

    def _pending_ho_entries(self):
        """Vouchers that have not gone to head office.

        Two populations, and both belong: entries in a bunch that has not been
        marked sent, and approved entries in no bunch at all. The second is a
        step further back -- not even bundled -- and leaving it out would report
        a reassuring zero on a book where nothing has been bundled yet.

        Not period-filtered: what is waiting is waiting, whenever it was
        recorded, and a September filter would hide the August voucher that has
        been stuck longest.
        """
        return self._all.filter(
            Q(bunch__isnull=False, bunch__sent_at__isnull=True)
            | Q(bunch__isnull=True, approval_state=EntryApprovalStatus.APPROVED),
            direction=CashDirection.OUT,
        )

    def _pending_ho(self) -> Dict[str, Any]:
        """What is still waiting to go to head office, item by item.

        See the module docstring for what this cannot know: a bunch that HAS
        gone but has not been reimbursed looks identical to one that was funded,
        because the register records no reimbursement. The count of already-sent
        bunches travels with the tile so a zero reads as "nothing waiting, 47
        already gone" rather than as a broken query.
        """
        entries = self._pending_ho_entries()

        rows = [
            {
                "id": entry.id,
                # The sheet's Item column: what was actually bought. Falls back
                # to the G/L head, then to the narrative, so a row is never
                # nameless on screen.
                "item": (
                    entry.item
                    or entry.gl_account_name
                    or (entry.detail or "").strip()[:60]
                    or "Unnamed voucher"
                ),
                "amount": _money(entry.amount),
                "entry_date": entry.entry_date.isoformat(),
                "gl_account_name": entry.gl_account_name,
                "branch": entry.branch.name if entry.branch_id else None,
                "approval_state": entry.approval_state,
                "bunch": entry.bunch.number if entry.bunch_id else None,
                "detail": entry.detail,
            }
            for entry in entries.select_related("branch", "bunch").order_by(
                "-entry_date", "-id"
            )[:MAX_PENDING_ROWS]
        ]

        totals = entries.aggregate(total=Sum("amount"), count=Count("id"))
        bunches = CashBunch.objects.filter(company=self.company, is_active=True)

        return {
            "total": _money(totals["total"]),
            "count": totals["count"] or 0,
            "rows": rows,
            "truncated": (totals["count"] or 0) > len(rows),
            "unsent_bunches": bunches.filter(sent_at__isnull=True).count(),
            "sent_bunches": bunches.filter(sent_at__isnull=False).count(),
        }

    # ------------------------------------------------------------ detail box

    def _detail(self) -> Dict[str, Any]:
        """The outstanding breakdown: four lines over the period's payments.

        Three are sums over configured G/L heads. The fourth, payment pending,
        is read off the approval state instead -- see
        :mod:`accounts_board.constants` for why that question replaced the
        whiteboard's "payment penalty", which had no source at all.
        """
        payments = self._live.filter(direction=CashDirection.OUT)

        by_code = {
            row["gl_account_code"]: row
            for row in payments.values("gl_account_code", "gl_account_name").annotate(
                amount=Sum("amount"), count=Count("id")
            )
        }

        def head_rows(rows):
            return sorted(
                (
                    {
                        "code": r["gl_account_code"],
                        "name": r["gl_account_name"],
                        "amount": _money(r["amount"]),
                        "count": r["count"],
                    }
                    for r in rows
                ),
                key=lambda head: -head["amount"],
            )

        buckets = []
        for spec in DETAIL_BUCKETS:
            if spec.get("from_approval_state"):
                unagreed = payments.filter(
                    approval_state__in=UNAGREED_STATES
                ).aggregate(amount=Sum("amount"), count=Count("id"))
                buckets.append(
                    {
                        "key": spec["key"],
                        "label": spec["label"],
                        "note": spec["note"],
                        "has_source": True,
                        "amount": _money(unagreed["amount"]),
                        "count": unagreed["count"] or 0,
                        "heads": [],
                    }
                )
                continue

            rows = [by_code[c] for c in spec["codes"] if c in by_code]
            buckets.append(
                {
                    "key": spec["key"],
                    "label": spec["label"],
                    "note": spec["note"],
                    "has_source": bool(spec["codes"]),
                    "amount": _money(sum((r["amount"] for r in rows), ZERO)),
                    "count": sum(r["count"] for r in rows),
                    "heads": head_rows(rows),
                }
            )

        # Heads no bucket claims. Shown rather than dropped: a head nobody
        # classified is a question for accounts, not a rounding error.
        unclassified = [
            row for code, row in by_code.items() if code not in CLASSIFIED_CODES
        ]
        if unclassified:
            self.warn(
                f"{len(unclassified)} G/L head(s) on this register belong to no "
                "named line and are grouped under Other."
            )

        return {
            "buckets": buckets,
            "other": {
                "amount": _money(sum((r["amount"] for r in unclassified), ZERO)),
                "count": sum(r["count"] for r in unclassified),
                "heads": head_rows(unclassified)[:MAX_HEAD_ROWS],
            },
            # The whole payment side, so the four lines read as shares of
            # something rather than as four unrelated numbers.
            "total": _money(payments.aggregate(t=Sum("amount"))["t"]),
            "debits": payments.count(),
        }

    # ---------------------------------------------------------------- salary

    def _salary(self) -> Dict[str, Any]:
        """Salary advances and cash increments, voucher by voucher.

        **No payroll is read.** Not a salary, not a revision, not a due amount.
        This is the cash book's own record of what was handed to somebody
        against their pay -- a petty-cash fact rather than an HR one -- which is
        what makes it safe to gate on a cash-book right instead of the narrow
        salary family in ``employee_hierarchy``.

        WHY THIS IS NOT GROUPED BY PERSON
        ----------------------------------
        It was, using ``CashEntry.advance_holder``, and that was wrong twice
        over. On the live register only 1 of 26 salary vouchers carries one at
        all -- so the panel showed a total with almost no rows under it, which
        reads as a broken query -- and the single row it did produce named the
        WRONG PERSON: the entry is booked to Jasmeet Singh and its narrative
        says "Advance to Hardeep Singh".

        That is not a data-entry slip. ``advance_holder`` means "whose float
        this payment clears", which is a different question from "who was this
        advance for", and the two only coincide when somebody spends their own
        float on themselves. Grouping salary by it will keep producing
        confident, wrong names.

        SO THE ROW IS THE VOUCHER, AND ITS LABEL IS THE REGISTER'S OWN WORDS
        --------------------------------------------------------------------
        The custodian does record who -- in the Item column ("Parveen khatun")
        or in the narrative ("Cash paid advance to Shyam shukla (Deduct of sep.
        month salary)"). Those words are shown verbatim.

        Quoting the register is not the same as attributing money to an
        employee. Nothing here is matched against the directory, no name is
        parsed out of prose, and no row claims to identify a person -- it shows
        what the voucher says, which is exactly what the Cash Book screen shows
        for the same row. The earlier refusal was about inventing an
        attribution; it was never a reason to hide the voucher's own text.
        """
        salary_rows = self._live.filter(
            direction=CashDirection.OUT, gl_account_code__in=SALARY_PERSON_CODES
        )
        totals = salary_rows.aggregate(total=Sum("amount"), count=Count("id"))

        rows = [
            {
                "id": entry.id,
                "description": self._voucher_label(entry, index),
                "amount": _money(entry.amount),
                "entry_date": entry.entry_date.isoformat(),
                "gl_account_name": entry.gl_account_name,
                # The full narrative for the detail panel. Masked with the
                # label, because a name in prose is still a name.
                "detail": entry.detail if self.may_name else "",
            }
            for index, entry in enumerate(
                salary_rows.order_by("-entry_date", "-id")[:MAX_PENDING_ROWS]
            )
        ]

        return {
            "total": _money(totals["total"]),
            "count": totals["count"] or 0,
            "rows": rows,
            "truncated": (totals["count"] or 0) > len(rows),
            "heads": [
                {
                    "code": row["gl_account_code"],
                    "name": row["gl_account_name"],
                    "amount": _money(row["amount"]),
                    "count": row["count"],
                }
                for row in salary_rows.values("gl_account_code", "gl_account_name")
                .annotate(amount=Sum("amount"), count=Count("id"))
                .order_by("-amount")
            ],
            "span": self._span(salary_rows, "entry_date"),
        }

    def _voucher_label(self, entry, index: int) -> str:
        """What a salary voucher is called on screen.

        The Item column when it says something -- it is usually the person's
        name -- and the narrative when Item is one of the custodian's generic
        words ("Advacne", "Salary"), which carry no information at all.

        Masked wholesale for a reader who may not see names. Not partially:
        the narrative names somebody in the middle of a sentence, so there is
        no safe way to show some of it.
        """
        if not self.may_name:
            return MASKED_VOUCHER_LABEL.format(n=index + 1)

        item = (entry.item or "").strip()
        if item and item.lower() not in GENERIC_ITEM_WORDS:
            return item

        detail = (entry.detail or "").strip()
        return detail or entry.gl_account_name or "Unnamed voucher"

