"""
The few fixed facts behind the cash book.

WHERE THE G/L HEADS COME FROM
-----------------------------
SAP's chart of accounts, ``OACT``, filtered to ``Postable = 'Y'`` -- the same
filter ``sap_client.hana.service_grpo_options_reader`` uses when it offers an
expense account on a service GRPO. Nothing is typed in: the sheet this module
replaces spells its heads as "Refreshment", "Staffwellfair", "R&M", and each of
those is a real SAP account (5630004 REFRESHMENT, 5630003 STAFF WELFARE,
5650016 REPAIR AND MAINTENANCE PLANT & MACHINERY). Picking the account rather
than typing the word is what lets this register be reconciled against SAP.

Postable accounts as at 15 September 2026: Oil 1,318, Mart 1,180,
Beverages 660. Far too many to send down in full, so the picker searches on the
server and the account's code *and* name are snapshotted onto the entry -- the
register then reads back without SAP.

The accounts are NOT narrowed to expense (``ActType = 'E'``). Petty cash pays
expenses, but it also pays advances against salary, which land on a debtor
account; a filter on expenses alone would make the sheet's own "Advance" rows
impossible to record.

WHERE THE DEPARTMENTS COME FROM
-------------------------------
``accounts.Department`` -- the list the rest of the app already uses, and the
only one with rows in it (19 on the live database; ``employee_hierarchy``'s
stricter, company-scoped Department table is still empty). Departments are
global rather than per-company there, so the cash book offers all of them.
"""

#: Rows returned by one G/L account search. The picker is a type-ahead, not a
#: list to scroll: a search that needs more than this needs better words.
GL_ACCOUNT_SEARCH_LIMIT = 50

#: Page size for the register when the client asks for a page at all.
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 500

#: How many entries one bunch may carry. A bunch is a day or two of vouchers
#: walked to the approver together; a four-figure selection is a mis-click.
MAX_BUNCH_ENTRIES = 200

#: Names returned by one employee search on the salary advance screen. A
#: type-ahead over a payroll of thousands, like the G/L picker: a search that
#: needs more than this needs better words.
SALARY_ADVANCE_PEOPLE_LIMIT = 50

#: The G/L heads that make a payment an advance against somebody's wages.
#:
#: The same pair the accounts board totals under "Salary advance"
#: (``accounts_board.constants.SALARY_ADJUSTMENT_CODES``), and named again here
#: rather than imported for the reason that module gives for its own copy: the
#: two answer different questions, and one could later want a narrower list
#: without silently moving the other. ``test_salary_advances`` asserts they
#: still agree, so a drift is a failing test rather than a screen that quietly
#: stops showing half the vouchers.
#:
#: 1101015 is a BALANCE SHEET account and the other is a P&L one, which is
#: exactly why they are one line: from the cash box both are "we paid a person
#: something that is not an expense of running the factory today".
SALARY_ADVANCE_GL_CODES = (
    "1101015",  # SUNDRY DEBTORS STAFF -- advance against salary
    "5630001",  # SALARY EXPENSE -- increments and arrears paid in cash
)

#: Words a custodian types in the Item column that name nobody. When Item is
#: one of these the narrative is shown instead, because "Advance" as a label on
#: a list of advances tells the reader nothing at all.
GENERIC_ITEM_WORDS = frozenset(
    {"advacne", "advance", "salary", "increment", "advances"}
)

#: What goes in the Sr.no. box to say a line has no voucher at all.
#:
#: Not everything the book records is a payment somebody wrote a voucher for.
#: A bank deduction on a withdrawal is charged by the bank, not paid out by the
#: custodian, so there is no paper to number -- but it still has to be in the
#: book or the cash in hand is wrong. On the sheet those lines are written with
#: a dash in the Sr. column, so a dash is what the form takes; the entry is
#: then kept with no number, and the voucher run carries on unbroken to the
#: next real one.
#:
#: Several dashes because a keyboard and a paste from the sheet do not agree on
#: which one they produce.
NO_VOUCHER_MARKS = frozenset({"-", "--", "–", "—"})
