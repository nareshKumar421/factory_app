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
