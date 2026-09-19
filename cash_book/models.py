"""
The cash book: money in and out of the factory's cash box.

It replaces one spreadsheet. A custodian records cash coming in (drawn on the
ATM card, handed over by accounts), then every payment made out of it. Each row
carries the date, the department it was spent for, the SAP G/L head it belongs
to, what was bought and the narrative -- and the running balance, which is the
column the sheet exists for.

TWO THINGS THE SHEET DOES THAT THIS MODEL KEEPS
-----------------------------------------------
**The balance follows the entry order, not the date.** In the sheet the dates
run 06/04, 06/05, 06/03, 06/03, 06/05 while the balance falls steadily. A
voucher is written into the book when it reaches the custodian, whatever day
the spend happened on. So :attr:`CashEntry.balance_after` is computed over
entries in the order they were *recorded* (``id``), and a back-dated entry is
appended to the end rather than inserted into the middle.

**Vouchers travel in bunches.** The sheet's "Bunch" column is a number shared
by a dozen rows, and its two date columns are that bunch's -- when it was sent
and when it came back signed. Here a bunch is :class:`CashBunch`: a set of
entries sent for approval together, approved or rejected as one. The sheet's
"Sign Date" is this module's ``decided_at``.

NOTHING IS POSTED TO SAP. SAP supplies the chart of accounts and nothing else:
this is the custodian's own record of a cash box, and the journal entry behind
it is made in SAP by accounts, separately.
"""

from decimal import Decimal

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models

from company.models import Company
from gate_core.models.base import BaseModel

ZERO = Decimal("0.00")


class CashDirection(models.TextChoices):
    """Which way the money moved. The sheet's In and Out columns."""

    IN = "IN", "Cash in"
    OUT = "OUT", "Cash out"



class EntryApprovalStatus(models.TextChoices):
    """Where one entry has got to with its approver.

    An entry's own, not its bunch's. A bunch is the batch of paper vouchers
    walked over together; approval is whether this particular spend has been
    agreed. They used to be the same thing, which meant nothing could be
    approved without first being bundled -- so a payment recorded on Tuesday
    waited on a batch that went on Friday.

    There is deliberately no "not sent yet". A payment goes into the queue the
    moment it is written down: a spend nobody has been told about is not a
    state the factory wants a register to be able to hold.
    """

    #: A receipt. Nobody approves money arriving, so it never enters a queue.
    NOT_REQUIRED = "NOT_REQUIRED", "No approval needed"
    PENDING = "PENDING", "Awaiting approval"
    APPROVED = "APPROVED", "Approved"
    REJECTED = "REJECTED", "Rejected"


#: The only state that puts an entry beyond the custodian's reach. Everything
#: else stays editable, including a payment that is waiting: the point of
#: sending it is to have it agreed, not to stop its author fixing a typo.
LOCKING_APPROVALS = frozenset({EntryApprovalStatus.APPROVED})


#: The branches a cash box spends against, as the factory is organised. Seeded
#: by migration 0002 for every company and editable from the settings page --
#: the list is short and stable, but it is data, not code.
DEFAULT_BRANCHES = ("Oil", "Beverage", "Water", "Common")


class CashBranch(BaseModel):
    """One branch of the business a payment can be spent for.

    This replaces the free-for-all of ``accounts.Department``, which is the
    whole company's list (IT, Ecom, Store, Mess...) and far wider than a cash
    box ever spends against. The four that matter here -- Oil, Beverage, Water
    and Common -- are the plant lines, and "Common" is what a drill bit bought
    for the whole site belongs to.

    Company-scoped like everything else in the module, so each company's book
    picks from its own list.
    """

    company = models.ForeignKey(
        Company, on_delete=models.CASCADE, related_name="cash_branches"
    )
    name = models.CharField(max_length=60)
    sort_order = models.PositiveSmallIntegerField(
        default=0, help_text="Position in the picker. Ties fall back to name."
    )

    class Meta:
        ordering = ["sort_order", "name"]
        verbose_name_plural = "Cash branches"
        constraints = [
            models.UniqueConstraint(
                fields=["company", "name"], name="uq_cash_branch_company_name"
            )
        ]
        permissions = [
            ("can_manage_cash_branches", "Can add, rename and retire cash book branches"),
        ]

    def __str__(self):
        return self.name



def _attachment_path(instance, filename):
    """Where a bill lands on disk.

    Foldered by company and entry so a directory listing means something and
    one entry's papers can be found without the database.
    """
    return (
        f"cash_book/{instance.entry.company_id}/{instance.entry_id}/{filename}"
    )


class CashEntryAttachment(BaseModel):
    """The bill behind a line of the book.

    The sheet's own column carried a bill number and nothing else, so proving
    a payment meant finding the paper. A voucher photographed at the moment it
    is written down is the difference between a register somebody can audit
    and one they have to take on trust.

    Several per entry, because a bill is often more than one sheet of paper
    and a photograph of a long one is several pictures.
    """

    entry = models.ForeignKey(
        "CashEntry",
        on_delete=models.CASCADE,
        related_name="attachments",
    )
    file = models.FileField(upload_to=_attachment_path)
    #: What it was called on the way in. The stored name is sanitised by
    #: Django and a reader should still see the name they recognise.
    original_filename = models.CharField(max_length=255)
    #: Snapshotted, so a listing does not have to touch the disk for every row
    #: -- and still says something if the file itself goes missing.
    size_bytes = models.PositiveIntegerField(default=0)
    uploaded_at = models.DateTimeField(auto_now_add=True)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="cash_entry_attachments",
    )

    class Meta:
        ordering = ["id"]
        indexes = [models.Index(fields=["entry"])]

    def __str__(self):
        return f"{self.original_filename} on entry {self.entry_id}"

class CashBunch(BaseModel):
    """A batch of approved vouchers, bundled to be sent to head office.

    The sheet's "Bunch" column. It is paperwork, not a decision: the vouchers
    in it have already been approved one by one, and bundling them only decides
    which go in the same envelope. That separation is the whole point -- a
    payment recorded on Tuesday is agreed on Tuesday, whatever day its batch
    eventually goes.

    So a bunch is a record of an act that happens outside this system: somebody
    filtered the register, picked a set of approved entries, downloaded the
    spreadsheet and mailed it to Delhi. What is kept here is which entries went
    together, what they came to, who made the batch, and when it was sent.

    Its number is allocated per company from 1. The numbers in the old sheet
    (17570, 36972) were never identifiers -- each was the batch's own total,
    which is derived here instead and so can never disagree with its contents.
    """

    company = models.ForeignKey(
        Company,
        on_delete=models.CASCADE,
        related_name="cash_bunches",
        help_text="The company whose cash box this is.",
    )
    number = models.PositiveIntegerField(
        help_text="Allocated per company when the batch is made, starting at 1."
    )
    remarks = models.TextField(
        blank=True,
        default="",
        help_text="Anything the sender wants recorded against the batch.",
    )

    sent_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the batch went to head office. Null until somebody "
        "says it has gone -- the app does not send the mail, so it cannot know.",
    )
    sent_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sent_cash_bunches",
    )

    class Meta:
        ordering = ["-number"]
        verbose_name_plural = "Cash bunches"
        constraints = [
            models.UniqueConstraint(
                fields=["company", "number"], name="uq_cash_bunch_company_number"
            )
        ]
        indexes = [models.Index(fields=["company", "-number"])]

    def __str__(self):
        return f"Bunch {self.number}"

    @property
    def is_sent(self) -> bool:
        return self.sent_at is not None


class AtmAccount(BaseModel):
    """A debit card the factory draws its cash from.

    The sheet calls it an imprest card and names the holder -- "Ginni Vg
    Imprest Debit Card (Vishal)". Money is paid onto it (:class:`AtmReceipt`)
    and drawn off it at the machine, and what is drawn becomes a cash receipt
    in the book.

    Withdrawals are deliberately NOT a model of their own. A withdrawal *is*
    the cash-in entry it produces -- the same event seen from two sides -- so
    it is recorded once, on :attr:`CashEntry.atm_account`, and the card's
    balance is read through that. Two rows for one movement is how a card
    balance and a cash balance start disagreeing.
    """

    company = models.ForeignKey(
        Company, on_delete=models.CASCADE, related_name="atm_accounts"
    )
    name = models.CharField(
        max_length=120,
        help_text="As the sheet names it, holder and all: "
        "'Ginni Vg Imprest Debit Card (Vishal)'.",
    )
    opening_balance = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=ZERO,
        help_text="What was on the card when this register started. The sheet "
        "opens at 19,538.",
    )

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["company", "name"], name="uq_atm_account_company_name"
            )
        ]

    def __str__(self):
        return self.name


class AtmReceipt(BaseModel):
    """Money paid onto the card. The sheet's "Amount Received" column."""

    account = models.ForeignKey(
        AtmAccount, on_delete=models.PROTECT, related_name="receipts"
    )
    received_on = models.DateField()
    amount = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    detail = models.TextField(
        blank=True,
        default="",
        help_text="Where it came from -- 'Imprest received from Vicky Vg'.",
    )

    class Meta:
        ordering = ["received_on", "id"]
        indexes = [models.Index(fields=["account", "received_on"])]

    def __str__(self):
        return f"{self.received_on} +{self.amount}"


class AdvanceDirection(models.TextChoices):
    """What happened between the box and somebody holding a float.

    Named for what physically happens, because "advance" and "returned" were
    read as jargon: one is handing somebody cash, the other is taking cash
    back off them.

    The third is neither, and it exists because saying "cash taken back" when
    no cash came back is a lie the screen tells about a real person. It is the
    position the workbook's advance list carries for somebody who paid for
    something out of their own pocket: nothing moved between them and the box,
    and the factory owes them. The app cannot produce one -- when it happens
    from now on it is a payment on the cash book naming them, which shows as
    "Spent" -- so it only ever arrives with the sheet.
    """

    GIVEN = "GIVEN", "Cash given"
    RETURNED = "RETURNED", "Cash taken back"
    SPENT_OWN = "SPENT_OWN", "Paid it themselves"


class AdvanceEntry(BaseModel):
    """Cash handed to somebody who has not yet said what it went on.

    The sheet keeps one of these per person who holds a float for any length of
    time -- "bunty in out", "Jasmeet in out" -- with a running total of what
    they are still holding.

    NOT a cash book entry, and that is the point. Handing Bunty 15,000 does not
    change what the custodian is accountable for; it only moves it from the box
    to Bunty's pocket. The money reaches the cash book later, as the expenses he
    eventually explains (:attr:`CashEntry.advance_holder`), which is why none of
    the sheet's advance handouts appear in its cash register.
    """

    company = models.ForeignKey(
        Company, on_delete=models.CASCADE, related_name="cash_advances"
    )
    person = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="cash_advances",
        help_text="Who is holding the money.",
    )
    entry_date = models.DateField()
    direction = models.CharField(max_length=10, choices=AdvanceDirection.choices)
    amount = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
        help_text="Always positive; ``direction`` says which way it moved.",
    )
    detail = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-entry_date", "-id"]
        verbose_name_plural = "Advance entries"
        indexes = [models.Index(fields=["company", "person", "-entry_date"])]

    def __str__(self):
        return f"{self.person} {self.get_direction_display()} {self.amount}"

    @property
    def signed_amount(self) -> Decimal:
        """What this does to the holder's outstanding advance."""
        amount = self.amount or ZERO
        # Anything that is not a handout lowers what they hold, whether the
        # cash came back or they never had it in the first place.
        return amount if self.direction == AdvanceDirection.GIVEN else -amount


class CashEntry(BaseModel):
    """One line of the cash book: money in, or money out.

    ``balance_after`` is stored rather than summed on read. The book is what it
    is *because* of that column, and every screen shows it beside the row; a
    stored figure keeps it right under any filter the register is looked at
    through. It is maintained by :mod:`cash_book.services`, never by hand.
    """

    company = models.ForeignKey(
        Company, on_delete=models.CASCADE, related_name="cash_entries"
    )

    serial_number = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="The sheet's Sr.no. -- the voucher's own number, which is "
        "how a payment is referred to away from the screen. Filled in for you "
        "with the next one, and typeable when a voucher carries its own.",
    )
    entry_date = models.DateField(
        help_text="The date the money moved, which is often before the day the "
        "voucher reached the book."
    )
    direction = models.CharField(max_length=4, choices=CashDirection.choices)
    amount = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
        help_text="Always positive. Which way it moved is ``direction``.",
    )

    branch = models.ForeignKey(
        CashBranch,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="cash_entries",
        help_text="Which branch the money was spent for. Required on a "
        "payment; a cash receipt into the box belongs to no branch.",
    )

    atm_account = models.ForeignKey(
        AtmAccount,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="withdrawals",
        help_text="On a RECEIPT: the card this cash was drawn off, which is "
        "what takes it off that card's balance. Blank when the cash came from "
        "somewhere else -- handed over by a director, say.",
    )

    advance_holder = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="cleared_cash_entries",
        help_text="On a PAYMENT: the person who spent this out of an advance "
        "they were already holding, whose outstanding advance it therefore "
        "clears. Blank when the custodian paid it straight out of the box.",
    )

    # --- The G/L head, snapshotted from SAP --------------------------------
    gl_account_code = models.CharField(
        max_length=32,
        blank=True,
        default="",
        help_text="SAP OACT.AcctCode, picked from the chart of accounts.",
    )
    gl_account_name = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="SAP OACT.AcctName as it read when the entry was made. A "
        "snapshot, so the register still names the head when SAP is down or "
        "the account is later renamed.",
    )

    item = models.CharField(
        max_length=120,
        blank=True,
        default="",
        help_text="What was actually bought -- 'Vegetable', 'DP switch', "
        "'Drill bit'. The sheet's Item column: a note under the G/L head, not "
        "a stock item, so it is free text.",
    )
    detail = models.TextField(
        help_text="The narrative, as written in the book: who was paid, what "
        "for, and any bill or party reference."
    )

    bunch = models.ForeignKey(
        CashBunch,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="entries",
        help_text="The bunch this entry was sent for approval in. Null until "
        "it is sent.",
    )

    balance_after = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=ZERO,
        help_text="The cash in hand once this entry is applied. Maintained by "
        "cash_book.services -- never set directly.",
    )

    # --- Approval, which is this entry's own and not its bunch's -----------
    approval_state = models.CharField(
        max_length=16,
        choices=EntryApprovalStatus.choices,
        default=EntryApprovalStatus.PENDING,
        help_text="Whether this spend has been agreed. A payment starts "
        "awaiting approval the moment it is recorded; a receipt needs none.",
    )
    approver = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="cash_entries_to_approve",
        help_text="Who this payment was sent to. Only they can decide it. "
        "Null on receipts, and on payments recorded before approvals were "
        "addressed to a person -- those stay open to any approver.",
    )
    approval_sent_at = models.DateTimeField(null=True, blank=True)
    approval_decided_at = models.DateTimeField(null=True, blank=True)
    approval_decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="decided_cash_entries",
    )
    approval_note = models.TextField(
        blank=True,
        default="",
        help_text="Required when rejecting, so the custodian knows what to fix.",
    )

    class Meta:
        # Recording order, which is the order the balance is built in.
        ordering = ["id"]
        verbose_name_plural = "Cash entries"
        constraints = [
            # Two lines with one number is the end of a register: a voucher
            # number that does not name one entry names none.
            models.UniqueConstraint(
                fields=["company", "serial_number"],
                condition=models.Q(serial_number__isnull=False),
                name="cash_entry_serial_unique_per_company",
            ),
        ]
        indexes = [
            models.Index(fields=["company", "id"]),
            models.Index(fields=["company", "serial_number"]),
            models.Index(fields=["company", "approval_state"]),
            # The approver's queue: their own pending work, nothing else.
            models.Index(fields=["company", "approver", "approval_state"]),
            models.Index(fields=["company", "entry_date"]),
            models.Index(fields=["bunch"]),
        ]
        permissions = [
            ("can_view_cash_book", "Can view the cash book"),
            ("can_manage_cash_book", "Can record, correct and cancel cash entries"),
            # Approval belongs to the entry, so its permission does too.
            ("can_approve_cash_entries", "Can approve or reject cash entries"),
        ]

    def __str__(self):
        sign = "+" if self.direction == CashDirection.IN else "-"
        return f"{self.entry_date} {sign}{self.amount}"

    @property
    def signed_amount(self) -> Decimal:
        """What this entry does to the balance."""
        amount = self.amount or ZERO
        return amount if self.direction == CashDirection.IN else -amount

    @property
    def approval_status(self) -> str:
        """Kept as a name the API already sends. Now simply the entry's own."""
        return self.approval_state

    @property
    def is_locked(self) -> bool:
        """True only once the spend has been agreed.

        A payment waiting on somebody stays editable -- it is in the queue to
        be agreed, not to be put out of its author's reach, and a typo spotted
        while it waits should be fixable without a rejection first.
        """
        return self.approval_state in LOCKING_APPROVALS

    @property
    def counts_as_spent(self) -> bool:
        """Whether this payment is agreed money, for the reconciliation.

        Only an approved payment is spent as far as the top of the register is
        concerned; everything else is still owed an explanation.
        """
        return (
            self.direction == CashDirection.OUT
            and self.approval_state == EntryApprovalStatus.APPROVED
        )
