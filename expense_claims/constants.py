"""
The fixed facts behind expense claims.

WHO DOES WHAT
-------------
* **Expense Submitter** -- everybody in the factory (``setup_expense_claim_groups``
  creates the group). Fills in the whole expense on one page: the branch, the
  budget, the G/L account, a comment, the amount and who approves it.
* **The approver** can be any active user. Whoever an expense is sent to
  approves or rejects it; no right is needed beyond being that person.
* The **cash book's approvers** (``cash_book.can_approve_cash_entries``) may
  also see every expense, not only those sent to them.

WHAT THE SCREENS CALL THINGS
----------------------------
The page's words are not SAP's:

* **Branch** is the company -- Oil, Mart or Beverages -- and so whose SAP the
  rest is read from.
* **Budget** is SAP's branch (``OBPL``, a business place): DELHI, FACTORY,
  PUNJAB... It starts on FACTORY.
"""

from cash_book.permissions import APPROVE_PERMISSION  # noqa: F401 -- sees every expense

SUBMIT_PERMISSION = "expense_claims.can_submit_expense_claim"

SUBMITTER_GROUP = "Expense Submitter"

#: The companies an expense can be put in for, as the page names them.
COMPANY_LABELS = {
    "JIVO_OIL": "Oil",
    "JIVO_MART": "Mart",
    "JIVO_BEVERAGES": "Beverages",
}

#: The budget a new expense starts on, where the company's SAP has one.
DEFAULT_BUDGET_NAME = "FACTORY"

#: Rows one list answers with. A claim is a single line, so a screen of them
#: is cheap -- but the list is a work queue, not an archive to scroll.
MAX_LIST_ROWS = 500

#: Rows returned by one G/L account search -- the cash book's own limit, since
#: it is the same chart of accounts searched the same way.
GL_ACCOUNT_SEARCH_LIMIT = 50
