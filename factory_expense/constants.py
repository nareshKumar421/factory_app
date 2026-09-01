"""
factory_expense/constants.py

The fixed vocabularies of the expense board. Everything else a user can
maintain is a master row, not a constant.
"""

from django.db import models

#: How many days of history the wall board's trend strip carries.
TREND_DAYS = 14


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


class RateShift(models.TextChoices):
    """Which shift a labour rate applies to.

    ``ANY`` is the fallback a company starts with; a DAY or NIGHT row beats it
    for that shift only, so a night premium is one extra row rather than a
    second rate table.
    """

    ANY = "ANY", "Any shift"
    DAY = "DAY", "Day"
    NIGHT = "NIGHT", "Night"


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
