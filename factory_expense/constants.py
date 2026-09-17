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


# ---------------------------------------------------------------------------
# Which company a labour department works for
# ---------------------------------------------------------------------------

def normalise_department(name) -> str:
    """A department name reduced to something safe to match on.

    The master spells the same idea three ways — ``Warehouse Basement``,
    ``production(oil)``, ``production(beverages)`` — so case and spacing carry
    no meaning here and both are dropped. Matching on the remainder means a
    rename that only adds a space or a capital letter cannot silently unmap a
    department and move its people into the shared row.
    """
    return "".join((name or "").split()).lower()


#: Department → company, for the Company Expense board's labour column.
#:
#: **This mapping, not ``LabourGateEntry.company``, is what owns a labourer.**
#: Every department row on the live register is tagged ``JIVO_OIL`` — including
#: ``Warehouse Gupta`` (Mart) and ``Warehouse Beverage`` (Beverages) — so the
#: company foreign key cannot attribute labour, and the department is the only
#: thing that can.
#:
#: A department that is not here, or that serves more than one company, goes to
#: the board's shared row rather than being guessed at. Only ``Mess`` is in that
#: position today — it feeds whoever is on site — but the shared row also carries
#: every unmapped department, and all the labour no department has claimed.
#:
#: Note what this mapping leaves: **Jivo Mart has no labour department at all.**
#: That is the user's own correction rather than an omission — ``Warehouse
#: Gupta`` reads as Mart's godown, but the labour working it is Oil's.
#:
#: Kept as a constant rather than a settings table because the department master
#: is short and stable, and the mapping states how the factory is organised
#: rather than a per-screen preference. Changing it is a code edit.
LABOUR_DEPARTMENT_COMPANY = {
    normalise_department("Warehouse Basement"): "JIVO_OIL",
    normalise_department("Boiling Floor 1"): "JIVO_OIL",
    normalise_department("production(oil)"): "JIVO_OIL",
    normalise_department("Scrap"): "JIVO_OIL",
    # Previously shared as "worked by Oil and Mart"; the user reassigned it
    # wholly to Oil (2026-09-14).
    normalise_department("Dock"): "JIVO_OIL",
    # Moved off Mart by the user (2026-09-14): the Gupta godown's labour is
    # Oil's, even though the stock sitting in it is Mart's.
    normalise_department("Warehouse Gupta"): "JIVO_OIL",
    normalise_department("Warehouse Beverage"): "JIVO_BEVERAGES",
    normalise_department("production(beverages)"): "JIVO_BEVERAGES",
    # The master spells this plural; the singular is accepted too, so a rename
    # in either direction keeps the department attributed.
    normalise_department("production(beverage)"): "JIVO_BEVERAGES",
}

#: Departments known to serve more than one company, or none.
#:
#: Functionally identical to being absent from the mapping — both land in the
#: shared row — but naming them says the placement was decided rather than
#: overlooked, which is the difference between a board that is trusted and one
#: that gets checked. ``Mess`` feeds whoever is on site.
#:
#: ``Dock`` was here too until the user reassigned it wholly to Oil on
#: 2026-09-14; no labour department is multi-company any more.
LABOUR_SHARED_DEPARTMENTS = {
    normalise_department("Mess"): "serves the whole campus",
}

#: How the shared row's labour is described when no department claimed it.
LABOUR_UNALLOCATED_LABEL = "unallocated"


# ---------------------------------------------------------------------------
# Which company an electricity meter belongs to
# ---------------------------------------------------------------------------

def normalise_meter(name) -> str:
    """A meter name reduced to something safe to match on.

    The register spells meters inconsistently — ``HP-196``, ``TR 125``,
    ``Pouch  packing meter Fg warehouse`` with a double space — so punctuation,
    spacing and case are all dropped. What is left still distinguishes every
    meter on the campus from every other.
    """
    return "".join(character for character in (name or "").lower() if character.isalnum())


#: Meters that measure the **incoming supply**, not a part of the factory.
#:
#: These are excluded from the board's electricity column, and this is the
#: single most important thing in this file. ``KWH`` is the main incomer and
#: ``KVAH`` is that same electricity measured as apparent energy — their ratio
#: is 0.96, which is the site's power factor, not two different supplies. Every
#: other meter is a *sub-meter* of that incomer: in September their readings
#: summed to 1.035 × KWH, tracking it day by day between 0.85 and 1.15.
#:
#: So adding the mains to the sub-meters counts the same electricity about three
#: times over. The board was reporting ₹19.87 L against a real bill near
#: ₹6.63 L. The sub-meters alone are the whole supply, broken down.
#:
#: The incomer is still read — it is served back as a reconciliation figure so
#: the sum of the parts can be checked against the meter the bill comes from.
#:
#: This set identifies WHICH meters are mains, which is a fact about the site and
#: does not change. Whether they are counted in the column is a separate choice:
#: see :data:`ELECTRICITY_MAINS_IN_SHARED`.
ELECTRICITY_MAIN_METERS = {
    normalise_meter("KWH"),
    normalise_meter("KVAH"),
    normalise_meter("LP-196"),
    normalise_meter("LP"),
}

#: Whether the mains are counted in the board's electricity column.
#:
#: **User decision, 2026-09-14, reversing the exclusion made the same day:** put
#: KWH, KVAH and LP-196 in the shared row rather than leaving them out.
#:
#: The consequence was put to them and accepted. Because the sub-meters are a
#: breakdown OF these meters, and KVAH is KWH measured a second way, the column
#: now counts the same electricity roughly three times — about ₹19.87 L against a
#: metered bill near ₹6.63 L. The board says so in a warning rather than letting
#: the figure pass as a bill, and the reconciliation below still compares the
#: SUB-METERS alone to the incomer, so the overlap stays measurable.
#:
#: Set to False to go back to reading sub-meters only; nothing else changes.
ELECTRICITY_MAINS_IN_SHARED = True

#: Which of the mains is *the* incomer to reconcile against.
#:
#: ``KWH`` rather than ``KVAH``: real energy is what the sub-meters measure and
#: what the tariff is struck on, so it is the one the breakdown should tie to.
ELECTRICITY_PRIMARY_INCOMER = normalise_meter("KWH")

#: Meter → company, for the Company Expense board's electricity column.
#:
#: Supersedes ``ElectricityMeter.companies`` for this board. That many-to-many
#: says six meters feed two companies at once, which is true of the *supply* but
#: useless for costing: it leaves 69% of the bill unattributable. This mapping
#: is the factory's own answer to which plant each sub-meter actually serves.
#:
#: A meter that is not here goes to the shared row and is named in a warning,
#: so a meter added on the Daily Electricity page cannot silently disappear.
#:
#: Keys are canonical register spellings; a few aliases follow for the way the
#: same meter gets written by hand.
ELECTRICITY_METER_COMPANY = {
    # --- Jivo Oil ---
    normalise_meter("Production Floor OIL"): "JIVO_OIL",
    normalise_meter("Oil storage meter"): "JIVO_OIL",
    normalise_meter("Pouch  packing meter Fg warehouse"): "JIVO_OIL",
    normalise_meter("1/4 Sidel"): "JIVO_OIL",
    normalise_meter("HP-196"): "JIVO_OIL",
    normalise_meter("Basement"): "JIVO_OIL",
    normalise_meter("TR 40"): "JIVO_OIL",
    # --- Jivo Beverages ---
    # Given as "mart" and corrected by the user: every meter here is beverage
    # plant equipment, and Mart is a trading company with no plant of its own.
    normalise_meter("Boiler"): "JIVO_BEVERAGES",
    normalise_meter("ETP"): "JIVO_BEVERAGES",
    normalise_meter("HP-512"): "JIVO_BEVERAGES",
    normalise_meter("Production Floor Beverage"): "JIVO_BEVERAGES",
    normalise_meter("Terrace"): "JIVO_BEVERAGES",
    normalise_meter("TR 125"): "JIVO_BEVERAGES",
    normalise_meter("Ro meter"): "JIVO_BEVERAGES",
    # A separate meter from "Ro meter", despite the register briefly carrying
    # one and not the other. Mapped ahead of being created.
    normalise_meter("TR 60"): "JIVO_BEVERAGES",
    # --- hand-written variants of the above ---
    normalise_meter("Sidel 1/4"): "JIVO_OIL",
    normalise_meter("Pouch packing FG warehouse"): "JIVO_OIL",
    normalise_meter("HP196"): "JIVO_OIL",
    normalise_meter("HP512"): "JIVO_BEVERAGES",
    normalise_meter("RO"): "JIVO_BEVERAGES",
}

#: Meters serving the whole campus rather than one plant.
#:
#: Functionally the same as being unmapped — both land in the shared row — but
#: listing them says the placement was decided rather than overlooked.
ELECTRICITY_SHARED_METERS = {
    normalise_meter("STP"),
    normalise_meter("Lab"),
    normalise_meter("LB"),
    normalise_meter("Admin"),
}

#: Meters named in the mapping that the register has never heard of.
#:
#: Kept deliberately: ``Basement``, ``LB``, ``Admin`` and ``TR 60`` are real
#: meters the factory has not yet created on the Daily Electricity page. Mapping
#: them now means they attribute correctly the day they appear, and until then
#: the board says which names it is waiting for rather than staying silent.
ELECTRICITY_EXPECTED_METERS = {
    normalise_meter("Basement"): "Basement",
    normalise_meter("LB"): "LB",
    normalise_meter("Admin"): "Admin",
    normalise_meter("TR 60"): "TR 60",
}


# ---------------------------------------------------------------------------
# What a row is called on the Company Expense board
# ---------------------------------------------------------------------------

#: Display name per company code, for the matrix board only.
#:
#: **This renames a row, not a company.** ``company.Company`` is untouched, so
#: every other screen, export and permission still says "Jivo Mart"; only this
#: board's row label changes. Renaming the company record instead would move the
#: name across the whole product for the sake of one wall.
#:
#: ``JIVO_MART`` reads as **Water** because the factory's third line is the water
#: plant, and Mart — a trading company with no plant — was never a meaningful row
#: on a *factory* expense board. The row still keys on ``JIVO_MART``, so anything
#: mapped to that company lands here; nothing is mapped to it today, which is why
#: the row is empty. Water's own meters and departments come later.
MATRIX_ROW_LABELS = {
    "JIVO_MART": "Water",
}
