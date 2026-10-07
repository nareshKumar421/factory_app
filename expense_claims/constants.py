"""
The fixed facts behind expense claims.

WHO DOES WHAT
-------------
Two groups, created by ``setup_expense_claim_groups``:

* **Expense Submitter** -- everybody in the factory. Puts in the expense: the
  branch, the budget, the G/L account (or, when they do not know it, what it
  is for), a comment and the amount.
* **Expense Approver** -- approves or rejects any expense but their own, on
  the approval page. Filled from Admin.

WHAT THE SCREENS CALL THINGS
----------------------------
* **Branch** is the company -- Oil, Mart or Beverages -- and so whose SAP the
  budget and the account are read from.
* **Budget** is SAP's dimension 3 (Factory, Back Office, Sales...). It starts
  on Factory.
"""

SUBMIT_PERMISSION = "expense_claims.can_submit_expense_claim"
APPROVE_PERMISSION = "expense_claims.can_approve_expense_claims"

SUBMITTER_GROUP = "Expense Submitter"
APPROVER_GROUP = "Expense Approver"

#: The companies an expense can be put in for, as the page names them.
COMPANY_LABELS = {
    "JIVO_OIL": "Oil",
    "JIVO_MART": "Mart",
    "JIVO_BEVERAGES": "Beverages",
}

#: The budget a new expense starts on. Oil and Beverages spell its code
#: "Factory", Mart "FACTORY", so it is matched without case.
DEFAULT_BUDGET_CODE = "FACTORY"

#: Rows one list answers with. A claim is a single line, so a screen of them
#: is cheap -- but the list is a work queue, not an archive to scroll.
MAX_LIST_ROWS = 500

#: Rows returned by one G/L account search.
GL_ACCOUNT_SEARCH_LIMIT = 50
