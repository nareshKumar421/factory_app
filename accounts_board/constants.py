"""
The fixed facts behind the accounts control board.

Everything this board shows is read out of ``cash_book`` -- the custodian's own
register of the cash box -- and out of nothing else. No SAP call is made
anywhere in this app. That is a deliberate choice and it has a consequence the
board has to state rather than hide, which is what most of this file is about.

WHAT "VENDOR A/P" CAN AND CANNOT MEAN HERE
-------------------------------------------
The cash book has no vendor. It has no card code, no supplier master, and no
open-item ledger; a payment names its G/L head and a free-text narrative and
that is all. So there is no accounts-payable BALANCE to be had from it -- what
a vendor is owed lives in SAP's ``OPCH``, which this board does not read.

What the register can answer is the neighbouring question: *how much cash went
out of the box to outside suppliers, and on how many vouchers*. That is what
:data:`DETAIL_BUCKETS` computes under the ``vendor_ap`` key, and the board
labels it "paid to suppliers" with the period it covers, never "outstanding".
A tile that said "Vendor A/P 48,20,110" off this data would be inventing a
liability out of a spend, and the one thing a finance board must not do is
invent a liability.

WHY THE HEADS ARE LISTED RATHER THAN PATTERN-MATCHED
-----------------------------------------------------
SAP's chart has 1,318 postable accounts on Oil alone. Deciding a bucket by
matching words in the account name -- "anything with FREIGHT in it" -- is the
same mistake ``sales`` made before correction C-0003: the name is not the
classification. So each bucket names its accounts by CODE, the codes are the
ones ``cash_book.sheet_gl_map`` already resolved against the live chart, and a
head that belongs to no bucket falls into ``other`` where it is visible rather
than silently dropped.

The list is code rather than a database table because it is short, it is an
accounting policy rather than operational data, and getting it wrong should
show up in review. If it starts changing monthly it has earned a table.

A BUCKET WITH NO HEADS IS NOT ZERO
-----------------------------------
A bucket whose code list is empty and which is not computed some other way
reports ``has_source: False``, and the board prints "not kept in the cash book"
instead of a confident 0.00. This follows the admin board's ``has_source`` rule,
which draws the same line between "genuinely nil" and "nobody configured a
source". They look identical on a screen and mean opposite things.

Nothing is in that state today -- the fourth line was rescued from it by being
asked differently. See :data:`PAYMENT_PENDING_CODES`.
"""

#: How many people one people-table sends down. The advance list is a dozen
#: names on the live book; a board that needed to scroll past this many would
#: be the wrong screen for the question.
MAX_PEOPLE_ROWS = 25

#: How many G/L heads the expense breakdown names before the rest collapse into
#: a remainder row. 22 heads carry the whole live register, so this shows all of
#: them today and still cannot grow a wall of text later.
MAX_HEAD_ROWS = 12

#: How many individual vouchers the "pending from head office" list names. It is
#: a list of things to chase, so it is bounded and reports ``truncated`` rather
#: than sending a thousand rows nobody will read to a panel four rows tall.
MAX_PENDING_ROWS = 50


# ---------------------------------------------------------------------------
# The four detail lines
# ---------------------------------------------------------------------------

#: Cash paid to outside suppliers for goods and services. Carriage both ways,
#: the handling either end of it, consumables bought in, the trades who repair
#: plant and premises, and small equipment bought outright.
#:
#: NOT an accounts-payable balance -- see the module docstring.
VENDOR_AP_CODES = (
    "5670001",  # FREIGHT AND CARTAGE OUTWARD-INDIRECT EXP
    "5680028",  # FREIGHT INWARD-INDIRECT
    "5670002",  # UNLOADING/LOADING CHARGES-INDIRECT EXPENSE
    "5100015",  # CONSUMABLE/DIRECT EXPENSE
    "5650016",  # REPAIR AND MAINTENANCE PLANT & MACHINERY
    "5650001",  # REPAIR & MAINTENANCE OFFICE & BUILDING
    "5650002",  # REPAIR & MAINTENANCE VEHICLE
    "1205001",  # ELECTRICAL APPLIANCES
)

#: Running the site: what is consumed keeping people fed, moved, clean and in
#: touch. The heads a petty cash box exists for.
EXPENSE_CODES = (
    "5630004",  # REFRESHMENT
    "5630003",  # STAFF WELFARE
    "5690002",  # CONVEYANCE
    "5680015",  # HOUSE KEEPING
    "5680023",  # POSTAGE & COURIER
    "5680012",  # PRINTING AND STATIONERY
    "5680013",  # LAB AND TESTING
    "5650015",  # FUEL - VEHICLES
    "5680003",  # TELEPHONE MOBILE AND INTERNET
    "5680025",  # LEGAL AND PROFESSIONAL
    "5660002",  # RENT
    "5610003",  # BANK CHARGES
)

#: Money that moves a person's pay rather than buying anything: an advance
#: against salary, which lands on the staff debtor account, and an increment or
#: arrear handed over in cash.
#:
#: 1101015 is a BALANCE SHEET account and the other is a P&L one, which is
#: exactly why they are one line here: from the cash box both are "we paid a
#: person something that is not an expense of running the factory today", and
#: the board's reader is asking about the person, not the ledger.
SALARY_ADJUSTMENT_CODES = (
    "1101015",  # SUNDRY DEBTORS STAFF -- advance against salary
    "5630001",  # SALARY EXPENSE -- increments and arrears paid in cash
)

#: The fourth line is NOT a G/L bucket, and that is why there are no codes here.
#:
#: The whiteboard called it "Payment penalty" and the register has no penalty
#: head to read -- the nearest candidate, 5610003 BANK CHARGES, is a bank's fee
#: for running an account rather than a penalty for paying somebody late, and
#: mapping it there would put a number under a heading it does not answer.
#:
#: The UI mock renames it "Payment pending", which is a different question and a
#: far better one: money already out of the drawer that nobody has agreed yet.
#: That has a real and exact source -- the approval state -- so the line is
#: computed from ``EntryApprovalStatus`` in the service rather than from a code
#: list here. See ``AccountsBoardService._detail``.
PAYMENT_PENDING_CODES = ()


#: The four lines of the detail box, in the order the whiteboard draws them.
#:
#: ``codes`` empty means the register has no source for this line, and the
#: service reports ``has_source: False`` rather than a zero.
DETAIL_BUCKETS = (
    {
        "key": "vendor_ap",
        "label": "Vendor A/P",
        # The tile's own subtitle, so the caveat travels with the number
        # instead of living in a wiki nobody opens.
        "note": "Cash paid to suppliers out of the box. The register keeps no "
        "vendor ledger, so this is a spend, not an outstanding balance.",
        "codes": VENDOR_AP_CODES,
    },
    {
        "key": "expenses",
        "label": "Expenses",
        "note": "Running the site: refreshment, welfare, conveyance, "
        "housekeeping, stationery and the rest.",
        "codes": EXPENSE_CODES,
    },
    {
        "key": "salary_adjustment",
        "label": "Salary adjustment",
        "note": "Advances against salary, and increments paid in cash.",
        "codes": SALARY_ADJUSTMENT_CODES,
    },
    {
        "key": "payment_pending",
        "label": "Payment pending",
        "note": "Out of the drawer, not yet agreed. Read off the approval "
        "state, not a G/L head.",
        "codes": PAYMENT_PENDING_CODES,
        # Says "compute me from the approval state instead of summing codes".
        # A flag rather than a fourth empty list, so the service cannot
        # accidentally report it as a bucket with no source.
        "from_approval_state": True,
    },
)

#: Every code that belongs to a named bucket. Anything else a payment is booked
#: to lands in the ``other`` remainder, which the board shows rather than drops
#: -- a head nobody classified is a question for accounts, not a rounding error.
CLASSIFIED_CODES = frozenset(
    code for bucket in DETAIL_BUCKETS for code in bucket["codes"]
)


# ---------------------------------------------------------------------------
# The salary section
# ---------------------------------------------------------------------------

#: The heads that make a payment a salary matter, for the per-person salary
#: table. The same pair the ``salary_adjustment`` bucket totals -- named again
#: rather than aliased, because the two answer different questions (one is a
#: total, one is a list of people) and one could later want a narrower list
#: than the other without silently moving the other.
SALARY_PERSON_CODES = SALARY_ADJUSTMENT_CODES
