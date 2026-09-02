"""
factory_expense/constants.py

The fixed vocabularies of the expense board. Everything else a user can
maintain is a master row, not a constant.
"""

from django.db import models

#: How many days of history the trend strip carries behind a single-day view.
TREND_DAYS = 14

#: Ceiling on the trend strip when a long range is picked. Beyond this the bars
#: are too thin to read on a wall, so the strip shows the most recent slice and
#: the headline figures still cover the whole range.
MAX_TREND_DAYS = 92


class ExpenseBucket(models.TextChoices):
    """The four cost lines the board shows, and the only keys it will accept.

    Each one is fed by a different part of FactoryFlow, which is why they are a
    closed list rather than a configurable one:

    * ``LABOUR``      — the gate's own headcount, priced at a configured rate;
    * ``SALARY``      — a department-wise monthly figure the admin types in;
    * ``ELECTRICITY`` — the Daily Electricity register in the maintenance module;
    * ``MAINTENANCE`` — spares consumed and material indents raised.
    """

    LABOUR = "LABOUR", "Labour"
    SALARY = "SALARY", "Salary"
    ELECTRICITY = "ELECTRICITY", "Electricity"
    MAINTENANCE = "MAINTENANCE", "Maintenance"


#: The Cost Master types this board prices itself from. Both are resolved
#: through ``cost_master.CostRate`` — this app stores no rates of its own.
#:
#: Deliberately separate from ``prod-*`` and ``blowing-*``: those price a
#: production run, and retuning a run's costing must not silently move the
#: number on the admin's wall.
LABOUR_COST_TYPE_CODE = "factory-labour"
SALARY_COST_TYPE_CODE = "factory-salary"

#: Scope precedence when several Cost Master rows could price the same thing.
#: Mirrors ``cost_master.services._SPECIFICITY``; a company-specific row beats
#: the company-agnostic variant at the same scope, and the latest
#: ``effective_from`` on or before the date breaks the final tie.
SCOPE_SPECIFICITY = {"FACTORY": 0, "COMPANY": 1, "DEPARTMENT": 2, "VALUE": 3}


#: Spare movements that represent money leaving the store. RECEIPT adds stock
#: and RETURN gives it back, so neither is spend; ADJUSTMENT is a stock
#: correction and is deliberately excluded from a cost board.
MAINTENANCE_SPEND_MOVEMENTS = ("ISSUE", "CONSUME")

#: Indent statuses that represent committed money — the approver has said yes
#: and a company has been picked or the goods are already moving. Anything
#: earlier is still a request, and REJECTED / CANCELLED never became spend.
MAINTENANCE_COMMITTED_INDENT_STATUSES = (
    "QUOTATION_SELECTED",
    "PURCHASED",
    "GATE_IN",
    "RECEIVED",
)
