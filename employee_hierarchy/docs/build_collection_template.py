"""
Build the Excel workbook the company fills in to hand over its employee data.

    python employee_hierarchy/docs/build_collection_template.py

This is a **documentation tool**, not part of the application: nothing imports
it and nothing at runtime depends on it. It exists so the allowed values in the
workbook's dropdowns can be regenerated from
:mod:`employee_hierarchy.constants` the day one of those lists changes —
a template whose "Employment status" list has drifted from the code is worse
than no template, because the data comes back looking valid and imports wrong.

The workbook it writes is deliberately opinionated about three things.

**Codes, not names, are the join.** Every cross-reference between sheets is a
short code (``ENG``, ``SR_DEV``, ``EMP001``) chosen from a dropdown, because
names get typed four different ways ("HR", "H.R.", "Human Resources", "Hr
Dept") and each spelling becomes a separate department on import.

**Every code column is also shown as a name.** Beside each dropdown sits a grey
``(auto)`` column that looks the name up. Somebody picking ``EMP004`` needs to
see "Kavya Iyer" appear next to it — that is what catches the wrong code while
the person who knows the answer is still looking at the row.

**Nothing that cannot be checked is left unchecked.** Dropdowns stop unknown
values, conditional formatting turns duplicates and self-reporting red as they
are typed, and the *Checks* sheet counts every remaining problem live, so the
person filling it in knows whether it is finished without sending it anywhere.
"""

from __future__ import annotations

import math
import os
import sys

from openpyxl import Workbook
from openpyxl.comments import Comment
from openpyxl.formatting.rule import FormulaRule
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.workbook.defined_name import DefinedName
from openpyxl.worksheet.datavalidation import DataValidation

# Allow running this file directly from the repo root without DJANGO_SETTINGS.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from employee_hierarchy.constants import (  # noqa: E402
    MAX_HIERARCHY_DEPTH,
    EmploymentStatus,
    RevisionType,
)

OUTPUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Employee_Data_Collection_Template.xlsx")

# --- how many rows each sheet is prepared for -------------------------------
ROWS = {
    "departments": 300,
    "designations": 120,
    "employees": 2000,
    "salary": 6000,
    "access": 500,
}

# --- sheet names, numbered so the fill order is obvious ---------------------
S_START = "Start here"
S_DEPT = "1. Departments"
S_DESIG = "2. Designations"
S_EMP = "3. Employees"
S_SAL = "4. Salary history"
S_ACCESS = "5. Access and roles"
S_EXAMPLE = "Example (filled in)"
S_VALUES = "Allowed values"
S_CHECKS = "Checks"

# --- palette ----------------------------------------------------------------
INK = "FF1E293B"
HEAD_REQUIRED = "FF1E293B"
HEAD_OPTIONAL = "FF475569"
HEAD_AUTO = "FF94A3B8"
AUTO_FILL = "FFF1F5F9"
BAND = "FFF8FAFC"
TITLE = "FF0F172A"
WARN_FILL = "FFFEE2E2"
WARN_TEXT = "FF991B1B"
OK_FILL = "FFDCFCE7"
NOTE_FILL = "FFFFFBEB"

WHITE = Font(color="FFFFFFFF", bold=True, size=10)
DARK_BOLD = Font(color=INK, bold=True, size=10)
BODY = Font(color=INK, size=10)
SMALL = Font(color="FF475569", size=9)
H1 = Font(color=TITLE, bold=True, size=16)
H2 = Font(color=TITLE, bold=True, size=12)

WRAP_CENTER = Alignment(horizontal="center", vertical="center", wrap_text=True)
WRAP_LEFT = Alignment(horizontal="left", vertical="top", wrap_text=True)
THIN = Side(style="thin", color="FFCBD5E1")
BOX = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)

DATE_FORMAT = "DD-MMM-YYYY"
MONEY_FORMAT = "#,##,##0"

#: A column of the template: header, width, whether it must be filled, the
#: rule that goes in its header comment, and how it is validated.
class Column:
    def __init__(self, key, header, width, kind="optional", note="", validation=None, formula=None, number_format=None):
        self.key = key
        self.header = header
        self.width = width
        #: "required" | "optional" | "auto"
        self.kind = kind
        self.note = note
        #: A callable taking (worksheet, column_letter, last_row).
        self.validation = validation
        #: Excel formula for an ``(auto)`` column, with ``{row}`` placeholders.
        self.formula = formula
        self.number_format = number_format


#: Roughly how many 10pt characters fit in one unit of Excel column width.
#: Used to work out how tall a wrapped paragraph needs to be — Excel does not
#: reliably auto-fit a row written by a script, and a clipped instruction is an
#: instruction nobody reads.
CHARS_PER_WIDTH = 1.05
LINE_HEIGHT = 13.5


def wrapped_height(text, width_chars, *, minimum=15.0):
    """How tall a row must be for ``text`` to be fully visible when wrapped."""
    if not text:
        return minimum
    usable = max(10, int(width_chars * CHARS_PER_WIDTH))
    lines = 0
    for line in str(text).split("\n"):
        lines += max(1, math.ceil(len(line) / usable))
    return max(minimum, lines * LINE_HEIGHT + 4)


def fit_row(worksheet, row, pairs):
    """Set a row's height to fit the tallest of several (text, column width) cells."""
    worksheet.row_dimensions[row].height = max(
        wrapped_height(text, width) for text, width in pairs
    )


def sheet_ref(sheet, column, last_row, first_row=2):
    """``'3. Employees'!$A$2:$A$2001`` — quoted, because the names have spaces."""
    return f"'{sheet}'!${column}${first_row}:${column}${last_row + 1}"


def add_list_validation(worksheet, letter, last_row, source, *, title, message, allow_blank=True):
    """A dropdown fed by a named range, with a message explaining the rule.

    The formula is stored WITHOUT a leading ``=``. That is not cosmetic: a
    stored validation formula that begins with an equals sign makes Excel
    declare the workbook damaged and offer to repair it, and the repair drops
    every dropdown in the file. ``showDropDown=False`` reads backwards for the
    same reason — in the file format the flag means *suppress* the arrow.
    """
    validation = DataValidation(
        type="list", formula1=source.lstrip("="), allow_blank=allow_blank, showDropDown=False
    )
    validation.errorTitle = title
    validation.error = message
    validation.promptTitle = title
    validation.prompt = message
    validation.showErrorMessage = True
    validation.showInputMessage = True
    worksheet.add_data_validation(validation)
    validation.add(f"{letter}2:{letter}{last_row + 1}")


def add_date_validation(worksheet, letter, last_row, *, title, message):
    validation = DataValidation(
        type="date", operator="between", formula1="DATE(1940,1,1)", formula2="DATE(2040,12,31)",
        allow_blank=True,
    )
    validation.errorTitle = title
    validation.error = message
    validation.promptTitle = "Date"
    validation.prompt = message
    validation.showErrorMessage = True
    validation.showInputMessage = True
    worksheet.add_data_validation(validation)
    validation.add(f"{letter}2:{letter}{last_row + 1}")


def add_number_validation(worksheet, letter, last_row, *, minimum=0, maximum=99999999, whole=False, title="Number", message=""):
    validation = DataValidation(
        type="whole" if whole else "decimal",
        operator="between",
        formula1=str(minimum),
        formula2=str(maximum),
        allow_blank=True,
    )
    validation.errorTitle = title
    validation.error = message
    validation.showErrorMessage = True
    worksheet.add_data_validation(validation)
    validation.add(f"{letter}2:{letter}{last_row + 1}")


def add_formula_validation(worksheet, letter, last_row, formula, *, title, message):
    # Stored without the leading "=" — see add_list_validation.
    validation = DataValidation(type="custom", formula1=formula.lstrip("="), allow_blank=True)
    validation.errorTitle = title
    validation.error = message
    validation.showErrorMessage = True
    worksheet.add_data_validation(validation)
    validation.add(f"{letter}2:{letter}{last_row + 1}")


def write_grid(worksheet, columns, last_row):
    """Write one data sheet's header, formats, comments, and auto formulas."""
    for index, column in enumerate(columns, start=1):
        letter = get_column_letter(index)
        header = worksheet.cell(row=1, column=index)
        header.value = column.header + (" *" if column.kind == "required" else "")
        header.font = WHITE if column.kind != "auto" else DARK_BOLD
        header.fill = PatternFill(
            "solid",
            fgColor={
                "required": HEAD_REQUIRED,
                "optional": HEAD_OPTIONAL,
                "auto": HEAD_AUTO,
            }[column.kind],
        )
        header.alignment = WRAP_CENTER
        header.border = BOX
        if column.note:
            header.comment = Comment(column.note, "Jivo data template", width=340, height=190)
        worksheet.column_dimensions[letter].width = column.width

        for row in range(2, last_row + 2):
            cell = worksheet.cell(row=row, column=index)
            cell.font = BODY
            cell.border = BOX
            if column.number_format:
                cell.number_format = column.number_format
            if column.kind == "auto":
                cell.fill = PatternFill("solid", fgColor=AUTO_FILL)
                if column.formula:
                    cell.value = column.formula.format(row=row)

        if column.validation:
            column.validation(worksheet, letter, last_row)

    worksheet.row_dimensions[1].height = 40
    worksheet.freeze_panes = "C2"
    worksheet.auto_filter.ref = f"A1:{get_column_letter(len(columns))}{last_row + 1}"
    worksheet.sheet_view.showGridLines = False


def flag(worksheet, cell_range, formula):
    """Turn a cell red as soon as its value breaks a rule."""
    worksheet.conditional_formatting.add(
        cell_range,
        FormulaRule(
            formula=[formula],
            fill=PatternFill("solid", bgColor=WARN_FILL),
            font=Font(color=WARN_TEXT, bold=True),
            stopIfTrue=False,
        ),
    )


#: The instruction sheet is laid out on four equal columns rather than one wide
#: one. Prose is merged across all four, which reads the same as a single wide
#: column — but it also leaves a sane grid for the intake form at the top,
#: where a label and its input box have to sit side by side. One 118-wide
#: column cannot do both.
PROSE_COLUMNS = 4
PROSE_COLUMN_WIDTH = 30


def title_block(worksheet, lines, start_row=2):
    """A stack of headed paragraphs, merged across the prose columns."""
    worksheet.sheet_view.showGridLines = False
    worksheet.column_dimensions["A"].width = 4
    for offset in range(PROSE_COLUMNS):
        worksheet.column_dimensions[get_column_letter(2 + offset)].width = PROSE_COLUMN_WIDTH
    width = PROSE_COLUMNS * PROSE_COLUMN_WIDTH
    row = start_row
    for kind, text in lines:
        cell = worksheet.cell(row=row, column=2, value=text)
        if text:
            worksheet.merge_cells(
                start_row=row, start_column=2, end_row=row, end_column=1 + PROSE_COLUMNS
            )
        if kind == "h1":
            cell.font = H1
            worksheet.row_dimensions[row].height = 26
        elif kind == "h2":
            cell.font = H2
            worksheet.row_dimensions[row].height = 22
        elif kind == "note":
            cell.font = BODY
            cell.fill = PatternFill("solid", fgColor=NOTE_FILL)
            cell.alignment = WRAP_LEFT
            worksheet.row_dimensions[row].height = wrapped_height(text, width)
        else:
            cell.font = BODY
            cell.alignment = WRAP_LEFT
            worksheet.row_dimensions[row].height = wrapped_height(text, width)
        row += 1
    return row


#: What we need to know about the file itself, not about the people in it.
INTAKE_FIELDS = [
    ("Company unit this file covers", "e.g. Jivo Oil / Jivo Mart / Jivo Beverages"),
    ("Prepared by (name)", ""),
    ("Prepared by (email or phone)", ""),
    ("Date prepared", "e.g. 09-Sep-2026"),
    ("How many employees should this file contain?", "So we can tell if a page went missing"),
]


def intake_block(worksheet, row):
    """The short form at the top: which company, who filled it in, how many people.

    Worth its own block because everything else in the workbook is *inside* one
    company. Departments, designation codes and employee codes are only unique
    within a company unit, so one file covers one unit — and a file that does
    not say which unit it is cannot be loaded at all without asking.
    """
    heading = worksheet.cell(row=row, column=2, value="About this file")
    heading.font = H2
    worksheet.row_dimensions[row].height = 22
    row += 1

    for label, hint in INTAKE_FIELDS:
        label_cell = worksheet.cell(row=row, column=2, value=label)
        label_cell.font = DARK_BOLD
        label_cell.alignment = WRAP_LEFT
        label_cell.border = BOX

        entry = worksheet.cell(row=row, column=3)
        entry.fill = PatternFill("solid", fgColor=NOTE_FILL)
        entry.border = BOX
        worksheet.merge_cells(start_row=row, start_column=3, end_row=row, end_column=4)

        if hint:
            hint_cell = worksheet.cell(row=row, column=5, value=hint)
            hint_cell.font = SMALL
            hint_cell.alignment = WRAP_LEFT
        worksheet.row_dimensions[row].height = max(
            wrapped_height(label, PROSE_COLUMN_WIDTH),
            wrapped_height(hint, PROSE_COLUMN_WIDTH),
        )
        row += 1
    return row + 1


# ---------------------------------------------------------------------------
# The reference lists
# ---------------------------------------------------------------------------

#: Employment statuses, with the one consequence that matters for the tree:
#: whether somebody in that state may still hold a team. Taken from
#: :data:`employee_hierarchy.constants.MANAGER_ELIGIBLE_STATUSES` in spirit and
#: spelled out here so the person filling the sheet can see it.
STATUS_MEANING = {
    "ACTIVE": ("Working normally.", "Yes"),
    "PROBATION": ("New joiner still under review. Counted in headcount.", "Yes"),
    "ON_LEAVE": ("Away for a while — still employed and still the manager.", "Yes"),
    "SUSPENDED": ("Suspended pending an inquiry. Still on the payroll.", "No — their team must be given to somebody else"),
    "INACTIVE": ("On the books but not working, and not expected back.", "No"),
    "RESIGNED": ("Left of their own accord.", "No"),
    "TERMINATED": ("Employment ended by the company.", "No"),
    "RETIRED": ("Retired.", "No"),
}

REVISION_MEANING = {
    "ANNUAL_INCREMENT": "The yearly raise.",
    "PROMOTION": "Went up a rung, and the money followed.",
    "PERFORMANCE": "Out-of-cycle raise for performance.",
    "ROLE_CHANGE": "Same rung, different job, different money.",
    "DEPARTMENT_TRANSFER": "Moved department and the package changed with it.",
    "MARKET_ADJUSTMENT": "Corrected to what the market pays.",
    "BONUS": "A bonus written into the package.",
    "INITIAL": "The salary they joined on. Use this for the oldest row of each person.",
    "OTHER": "Anything else — say what in the Reason column.",
}

ROLES = [
    ("Employee (Self Service)", "The directory and the org chart, plus their OWN salary. Nobody else's — not even their manager's."),
    ("Manager", "Their own salary and the salary of anyone below them in the tree. May propose a revision, not approve one. Cannot see their peers' pay."),
    ("Department Head", "Their department and its sub-departments, the history behind each figure, and the headcount reports."),
    ("HR", "Everybody's salary, and the right to add and edit employees and enter a revision — but NOT to approve one."),
    ("Finance (Payroll)", "Everybody's salary and the right to approve a revision. Cannot edit employees or restructure the org."),
    ("HR Administrator", "Everything. Give this to as few people as possible."),
]

CURRENCIES = ["INR", "USD", "AED", "EUR", "GBP", "SGD"]
YES_NO = ["Yes", "No"]
RECORD_STATUS = ["Active", "Inactive"]


def build_values_sheet(workbook):
    """The Allowed values sheet, and the named ranges the dropdowns read from."""
    worksheet = workbook.create_sheet(S_VALUES)
    worksheet.sheet_view.showGridLines = False
    worksheet.column_dimensions["A"].width = 4
    worksheet.column_dimensions["B"].width = 26
    worksheet.column_dimensions["C"].width = 62
    worksheet.column_dimensions["D"].width = 44

    row = 2
    worksheet.cell(row=row, column=2, value="Allowed values").font = H1
    row += 1
    intro = worksheet.cell(
        row=row,
        column=2,
        value="These are the only values the system accepts. The dropdowns on the other sheets read from "
              "here, so editing this sheet breaks them — please leave it alone.",
    )
    intro.font = SMALL
    intro.alignment = WRAP_LEFT
    worksheet.merge_cells(start_row=row, start_column=2, end_row=row, end_column=4)
    worksheet.row_dimensions[row].height = wrapped_height(intro.value, 132)
    row += 2

    ranges = {}

    def block(name, heading, entries, extra_header=None):
        nonlocal row
        worksheet.cell(row=row, column=2, value=heading).font = H2
        row += 1
        headers = ["Value", "What it means"] + ([extra_header] if extra_header else [])
        for offset, header in enumerate(headers):
            cell = worksheet.cell(row=row, column=2 + offset, value=header)
            cell.font = WHITE
            cell.fill = PatternFill("solid", fgColor=HEAD_OPTIONAL)
            cell.alignment = WRAP_CENTER
            cell.border = BOX
        row += 1
        start = row
        for entry in entries:
            value, meaning = entry[0], entry[1]
            worksheet.cell(row=row, column=2, value=value).font = DARK_BOLD
            worksheet.cell(row=row, column=2).border = BOX
            cell = worksheet.cell(row=row, column=3, value=meaning)
            cell.font = BODY
            cell.alignment = WRAP_LEFT
            cell.border = BOX
            measured = [(meaning, 62)]
            if extra_header:
                extra = worksheet.cell(row=row, column=4, value=entry[2])
                extra.font = BODY
                extra.alignment = WRAP_LEFT
                extra.border = BOX
                measured.append((entry[2], 44))
            fit_row(worksheet, row, measured)
            row += 1
        ranges[name] = (start, row - 1)
        row += 1

    block(
        "List_EmploymentStatus",
        "Employment status",
        [(value, STATUS_MEANING[value][0], STATUS_MEANING[value][1]) for value in EmploymentStatus.values],
        extra_header="Can they still hold a team?",
    )
    block(
        "List_RevisionType",
        "Salary revision type — why the money changed",
        [(value, REVISION_MEANING[value]) for value in RevisionType.values],
    )
    block("List_Role", "Access role — what each one can see", ROLES)
    block("List_Currency", "Currency", [(code, "Amounts in this currency." if code != "INR" else "Indian rupees. The default — use this unless the package really is in another currency.") for code in CURRENCIES])
    block("List_YesNo", "Yes / No", [("Yes", ""), ("No", "")])
    block("List_RecordStatus", "Department and designation status", [
        ("ACTIVE", "In use."),
        ("INACTIVE", "Kept so history still reads correctly, but not offered for new employees."),
    ])

    for name, (start, end) in ranges.items():
        workbook.defined_names.add(
            DefinedName(name, attr_text=f"'{S_VALUES}'!$B${start}:$B${end}")
        )
    return worksheet


# ---------------------------------------------------------------------------
# The data sheets
# ---------------------------------------------------------------------------

REQUIRED_NOTE = "Required — a row without this cannot be imported.\n\n"


def build_departments(workbook):
    last = ROWS["departments"]
    worksheet = workbook.create_sheet(S_DEPT)
    codes = f"$A$2:$A${last + 1}"

    columns = [
        Column("code", "Department Code", 18, "required",
               REQUIRED_NOTE
               + "A short handle for the department, in CAPITALS, no spaces: ENG, QA, HR, FIN.\n\n"
                 "This is what every other sheet points at, so once data has been collected against "
                 "a code it should not be renamed. Must be unique. Up to 30 characters.",
               lambda ws, letter, n: add_formula_validation(
                   ws, letter, n, f"=COUNTIF({codes},{letter}2)<=1",
                   title="Duplicate department code",
                   message="This code is already used further up the sheet. Every department needs its own code.")),
        Column("name", "Department Name", 26, "required",
               REQUIRED_NOTE + "The name people use: Engineering, Human Resources, Finance. Must be unique."),
        Column("parent", "Sits Inside (Parent Code)", 22, "optional",
               "The department this one is part of — a code from column A of this sheet.\n\n"
               "Leave BLANK for a top-level department. Example:\n"
               "  CORP   (blank)\n"
               "  TECH   CORP\n"
               "  ENG    TECH\n\n"
               "A department cannot sit inside itself, and two departments cannot sit inside each other.",
               lambda ws, letter, n: add_list_validation(
                   ws, letter, n, "Dept_Codes",
                   title="Parent department",
                   message="Pick a department code from this sheet, or leave blank if it is top-level.")),
        Column("parent_name", "Parent Name (auto)", 22, "auto",
               "Filled in for you from the parent code. If this stays empty after you pick a parent, the code does not exist yet.",
               formula='=IF($C{row}="","",IFERROR(INDEX(Dept_Names,MATCH($C{row},Dept_Codes,0)),"NOT FOUND"))'),
        Column("head", "Department Head (Employee Code)", 26, "optional",
               "Who runs the department — an Employee Code from sheet 3.\n\n"
               "Leave blank if nobody is formally named. It can be filled in later.",
               lambda ws, letter, n: add_list_validation(
                   ws, letter, n, "Emp_Codes",
                   title="Department head",
                   message="Pick an Employee Code from sheet '3. Employees'. Add the person there first if they are missing.")),
        Column("head_name", "Head Name (auto)", 22, "auto",
               "Filled in from the employee code beside it. Check it says the person you meant.",
               formula='=IF($E{row}="","",IFERROR(INDEX(Emp_Names,MATCH($E{row},Emp_Codes,0)),"NOT FOUND"))'),
        Column("description", "Description", 40, "optional", "What this department does. One line is plenty."),
        Column("status", "Status", 14, "required",
               REQUIRED_NOTE + "ACTIVE for a department in use. INACTIVE only for one being kept for history.",
               lambda ws, letter, n: add_list_validation(
                   ws, letter, n, "List_RecordStatus", allow_blank=False,
                   title="Status", message="ACTIVE or INACTIVE.")),
        Column("sort", "Display Order", 14, "optional",
               "Where it sits among its brothers and sisters on the chart — 1, 2, 3…\n"
               "Leave blank and they come out alphabetically.",
               lambda ws, letter, n: add_number_validation(
                   ws, letter, n, minimum=0, maximum=999, whole=True,
                   title="Display order", message="A whole number between 0 and 999.")),
    ]
    write_grid(worksheet, columns, last)

    flag(worksheet, f"A2:A{last + 1}", f'AND($A2<>"",COUNTIF({codes},$A2)>1)')
    flag(worksheet, f"B2:B{last + 1}", f'AND($B2<>"",COUNTIF($B$2:$B${last + 1},$B2)>1)')
    flag(worksheet, f"C2:C{last + 1}", f'AND($C2<>"",OR($C2=$A2,COUNTIF({codes},$C2)=0))')
    return worksheet


def build_designations(workbook):
    last = ROWS["designations"]
    worksheet = workbook.create_sheet(S_DESIG)
    codes = f"$A$2:$A${last + 1}"

    columns = [
        Column("code", "Designation Code", 18, "required",
               REQUIRED_NOTE + "A short handle in CAPITALS: CEO, CTO, MGR, TL, SR_DEV, DEV, INTERN. Must be unique."),
        Column("name", "Designation Name", 26, "required",
               REQUIRED_NOTE + "The title as it is written: Chief Technology Officer, Manager, Senior Developer. Must be unique."),
        Column("level", "Organisational Level", 20, "required",
               REQUIRED_NOTE
               + "How senior the rung is: 1 at the very top (CEO), rising as you go down.\n\n"
                 "  1 CEO · 2 CTO · 3 Director · 4 General Manager · 5 Manager\n"
                 "  6 Team Lead · 7 Senior Developer · 8 Developer · 9 Junior · 10 Intern\n\n"
                 "Two designations may share a level (a CTO and a CFO are both level 2). This is the "
                 "GRADE — it is not the same thing as how many managers somebody has above them, which "
                 f"the system works out from the reporting lines. Between 1 and {MAX_HIERARCHY_DEPTH}.",
               lambda ws, letter, n: add_number_validation(
                   ws, letter, n, minimum=1, maximum=MAX_HIERARCHY_DEPTH, whole=True,
                   title="Organisational level",
                   message=f"A whole number between 1 (top) and {MAX_HIERARCHY_DEPTH}.")),
        Column("managerial", "Manages People?", 18, "required",
               REQUIRED_NOTE
               + "Yes if somebody on this rung is expected to have a team (CEO, CTO, Manager, Team Lead).\n"
                 "No for an individual contributor (Developer, QA Engineer, Intern).",
               lambda ws, letter, n: add_list_validation(
                   ws, letter, n, "List_YesNo", allow_blank=False,
                   title="Manages people?", message="Yes or No.")),
        Column("description", "Description", 42, "optional", "What the rung means, if it needs saying."),
        Column("status", "Status", 14, "required",
               REQUIRED_NOTE + "ACTIVE for a rung in use; INACTIVE for one kept only for history.",
               lambda ws, letter, n: add_list_validation(
                   ws, letter, n, "List_RecordStatus", allow_blank=False,
                   title="Status", message="ACTIVE or INACTIVE.")),
    ]
    write_grid(worksheet, columns, last)
    flag(worksheet, f"A2:A{last + 1}", f'AND($A2<>"",COUNTIF({codes},$A2)>1)')
    flag(worksheet, f"B2:B{last + 1}", f'AND($B2<>"",COUNTIF($B$2:$B${last + 1},$B2)>1)')
    return worksheet

def build_employees(workbook):
    last = ROWS["employees"]
    worksheet = workbook.create_sheet(S_EMP)
    codes = f"$A$2:$A${last + 1}"
    exit_statuses = '"RESIGNED","TERMINATED","RETIRED","INACTIVE"'

    columns = [
        Column("code", "Employee Code", 16, "required",
               REQUIRED_NOTE
               + "The code the company already uses for this person — EMP001, 1042, whatever it is.\n\n"
                 "It has to be unique, and every other sheet points at it, so please use the code from "
                 "your existing records rather than inventing a new one.",
               lambda ws, letter, n: add_formula_validation(
                   ws, letter, n, f"=COUNTIF({codes},{letter}2)<=1",
                   title="Duplicate employee code",
                   message="This code is already used further up the sheet. Two people cannot share a code.")),
        Column("first", "First Name", 18, "required", REQUIRED_NOTE + "Given name."),
        Column("last", "Last Name", 18, "optional", "Surname. Leave blank if they only go by one name."),
        Column("full", "Full Name (auto)", 24, "auto",
               "Made from the two name columns. Nothing to fill in.",
               formula='=IF($A{row}="","",TRIM($B{row}&" "&$C{row}))'),
        Column("email", "Work Email", 30, "optional",
               "Their work email, if they have one. Must be unique.\n\n"
               "This is NOT the same thing as the login in column U — plenty of people have an email "
               "and no account in this system.",
               lambda ws, letter, n: add_formula_validation(
                   ws, letter, n, f'=ISNUMBER(SEARCH("@",{letter}2))',
                   title="Email address",
                   message="That does not look like an email address. Leave it blank if they do not have one.")),
        Column("phone", "Phone", 18, "optional", "Mobile number. Digits, spaces and + only, please — no notes in this cell."),
        Column("dob", "Date of Birth", 16, "optional",
               "Type it as 01-Apr-1990. Leave blank if you do not hold it.",
               lambda ws, letter, n: add_date_validation(
                   ws, letter, n, title="Date of birth",
                   message="A real date, typed like 01-Apr-1990."),
               number_format=DATE_FORMAT),
        Column("joining", "Joining Date", 16, "required",
               REQUIRED_NOTE + "The day they started. Type it as 01-Apr-2020.\n\n"
               "A future date is allowed for somebody who has been hired but has not started yet.",
               lambda ws, letter, n: add_date_validation(
                   ws, letter, n, title="Joining date",
                   message="A real date, typed like 01-Apr-2020."),
               number_format=DATE_FORMAT),
        Column("status", "Employment Status", 20, "required",
               REQUIRED_NOTE
               + "Pick from the list. See the 'Allowed values' sheet for what each one means and, more "
                 "importantly, whether somebody in that state may still hold a team.\n\n"
                 "The four that mean somebody has gone — RESIGNED, TERMINATED, RETIRED, INACTIVE — also "
                 "need a Last Working Day in the next column.",
               lambda ws, letter, n: add_list_validation(
                   ws, letter, n, "List_EmploymentStatus", allow_blank=False,
                   title="Employment status",
                   message="Pick one from the list. 'Allowed values' explains each.")),
        Column("exit", "Last Working Day", 18, "optional",
               "Only for somebody who has left: RESIGNED, TERMINATED, RETIRED or INACTIVE.\n\n"
               "Leave BLANK for anybody still employed — including people on leave or suspended.\n\n"
               "This is what turnover reporting counts, so it is worth getting right.",
               lambda ws, letter, n: add_date_validation(
                   ws, letter, n, title="Last working day",
                   message="A real date, typed like 31-Mar-2026. Blank for anybody still employed."),
               number_format=DATE_FORMAT),
        Column("dept", "Department Code", 18, "required",
               REQUIRED_NOTE + "A code from sheet '1. Departments'. If the department is missing, add it there first.",
               lambda ws, letter, n: add_list_validation(
                   ws, letter, n, "Dept_Codes", allow_blank=False,
                   title="Department",
                   message="Pick a code from sheet '1. Departments'. Add the department there first if it is missing.")),
        Column("dept_name", "Department (auto)", 22, "auto",
               "Looked up from the code. If it says NOT FOUND, the code is not on sheet 1.",
               formula='=IF($K{row}="","",IFERROR(INDEX(Dept_Names,MATCH($K{row},Dept_Codes,0)),"NOT FOUND"))'),
        Column("desig", "Designation Code", 18, "required",
               REQUIRED_NOTE + "A code from sheet '2. Designations' — the rung they sit on.",
               lambda ws, letter, n: add_list_validation(
                   ws, letter, n, "Desig_Codes", allow_blank=False,
                   title="Designation",
                   message="Pick a code from sheet '2. Designations'. Add the rung there first if it is missing.")),
        Column("desig_name", "Designation (auto)", 22, "auto",
               "Looked up from the code beside it.",
               formula='=IF($M{row}="","",IFERROR(INDEX(Desig_Names,MATCH($M{row},Desig_Codes,0)),"NOT FOUND"))'),
        Column("level", "Level (auto)", 12, "auto",
               "The rung's organisational level, from sheet 2. Nothing to fill in.",
               formula='=IF($M{row}="","",IFERROR(INDEX(Desig_Levels,MATCH($M{row},Desig_Codes,0)),""))'),
        Column("job_title", "Job Title", 26, "optional",
               "What is on their business card — 'Engineering Manager', 'Plant Head'.\n\n"
               "It is fine for this to be the same as the designation. Where they differ, the "
               "designation is the grade and this is the job."),
        Column("location", "Location", 20, "optional", "Where they sit: Head Office, Unit 2, Delhi Warehouse."),
        Column("manager", "Reports To (Employee Code)", 26, "optional",
               "The ONE person they report to — an Employee Code from column A of this sheet.\n\n"
               "Leave BLANK only for the very top of the company. If the company has more than one "
               "top-level person (say a separate plant head), leave each of theirs blank too.\n\n"
               "Rules the system will enforce:\n"
               "  • nobody may report to themselves\n"
               "  • no loops — if A reports to B, B cannot report to A, or to anyone under A\n"
               "  • the manager must be somebody who can hold a team (see Employment status)\n"
               f"  • the chain may be at most {MAX_HIERARCHY_DEPTH} deep",
               lambda ws, letter, n: add_list_validation(
                   ws, letter, n, "Emp_Codes",
                   title="Reports to",
                   message="Pick an Employee Code from this sheet, or leave blank for the top of the company.")),
        Column("manager_name", "Reports To — Name (auto)", 26, "auto",
               "Looked up from the code beside it. THIS is the column to read back: it should say the "
               "person you meant. NOT FOUND means the code is not on this sheet.",
               formula='=IF($R{row}="","",IFERROR(INDEX(Emp_Names,MATCH($R{row},Emp_Codes,0)),"NOT FOUND"))'),
        Column("is_manager", "Is a Manager?", 16, "optional",
               "Yes if this person is meant to manage people.\n\n"
               "You can leave it blank: anybody who has somebody reporting to them is treated as a "
               "manager automatically. Fill it in for a manager who has been hired but has no team yet.",
               lambda ws, letter, n: add_list_validation(
                   ws, letter, n, "List_YesNo", title="Is a manager?", message="Yes, No, or leave blank.")),
        Column("login", "App Login Email", 30, "optional",
               "The account this person signs into the system with, if they have one.\n\n"
               "This is the link that makes 'my own salary' work: without it, an employee cannot see "
               "their own pay however many permissions they are given. One account belongs to one "
               "employee. List the accounts themselves on sheet '5. Access and roles'.",
               lambda ws, letter, n: add_formula_validation(
                   ws, letter, n, f'=ISNUMBER(SEARCH("@",{letter}2))',
                   title="Login email",
                   message="That does not look like an email address. Leave blank if they have no account.")),
        Column("photo", "Photo File Name", 22, "optional",
               "If you are sending photos, put the file name here — EMP001.jpg — and send the images in "
               "one folder alongside this workbook. Do not paste pictures into the sheet."),
    ]
    write_grid(worksheet, columns, last)

    flag(worksheet, f"A2:A{last + 1}", f'AND($A2<>"",COUNTIF({codes},$A2)>1)')
    flag(worksheet, f"E2:E{last + 1}", f'AND($E2<>"",COUNTIF($E$2:$E${last + 1},$E2)>1)')
    # Reporting: pointing at yourself, at a code that does not exist, or at
    # somebody who points straight back at you.
    flag(
        worksheet,
        f"R2:R{last + 1}",
        f'AND($R2<>"",OR($R2=$A2,COUNTIF({codes},$R2)=0,'
        f'IFERROR(INDEX($R$2:$R${last + 1},MATCH($R2,{codes},0)),"")=$A2))',
    )
    # An exit date on somebody who has not left, and a leaver with no exit date.
    flag(worksheet, f"J2:J{last + 1}", f'AND($J2<>"",$A2<>"",ISNA(MATCH($I2,{{{exit_statuses}}},0)))')
    flag(worksheet, f"I2:I{last + 1}", f'AND($A2<>"",$J2="",NOT(ISNA(MATCH($I2,{{{exit_statuses}}},0))))')
    flag(worksheet, f"U2:U{last + 1}", f'AND($U2<>"",COUNTIF($U$2:$U${last + 1},$U2)>1)')
    return worksheet


def build_salary(workbook):
    last = ROWS["salary"]
    worksheet = workbook.create_sheet(S_SAL)

    columns = [
        Column("code", "Employee Code", 16, "required",
               REQUIRED_NOTE
               + "Whose salary this is — a code from sheet '3. Employees'.\n\n"
                 "ONE ROW PER SALARY, not one row per person. If somebody's pay has changed three "
                 "times, they get three rows here. Nothing is overwritten: the whole history is what "
                 "makes it possible to say what somebody was paid in 2024.",
               lambda ws, letter, n: add_list_validation(
                   ws, letter, n, "Emp_Codes", allow_blank=False,
                   title="Employee",
                   message="Pick an Employee Code from sheet '3. Employees'.")),
        Column("name", "Employee Name (auto)", 24, "auto",
               "Looked up from the code. Check it is the person you meant before typing their pay.",
               formula='=IF($A{row}="","",IFERROR(INDEX(Emp_Names,MATCH($A{row},Emp_Codes,0)),"NOT FOUND"))'),
        Column("effective", "Effective From", 16, "required",
               REQUIRED_NOTE
               + "The day this salary STARTED being paid — 01-Apr-2025.\n\n"
                 "Not the day it was decided (that is column L). One person cannot have two rows "
                 "starting on the same day.\n\n"
                 "A future date is fine and is the right way to record a raise that has been agreed "
                 "but has not started yet.",
               lambda ws, letter, n: add_date_validation(
                   ws, letter, n, title="Effective from",
                   message="A real date, typed like 01-Apr-2025."),
               number_format=DATE_FORMAT),
        Column("basic", "Basic — per year", 18, "required",
               REQUIRED_NOTE
               + "ANNUAL basic salary, in whole rupees. 600000, not 6,00,000 and not 50000/month.\n\n"
                 "If your records are monthly, multiply by 12 before typing. No ₹ sign, no commas, no text.",
               lambda ws, letter, n: add_number_validation(
                   ws, letter, n, minimum=0, maximum=99999999,
                   title="Basic salary",
                   message="A number — annual rupees, digits only (600000)."),
               number_format=MONEY_FORMAT),
        Column("allowances", "Allowances — per year", 20, "optional",
               "Annual total of every allowance: HRA, conveyance, medical, special. 0 or blank if none.",
               lambda ws, letter, n: add_number_validation(
                   ws, letter, n, minimum=0, maximum=99999999,
                   title="Allowances", message="A number — annual rupees, digits only."),
               number_format=MONEY_FORMAT),
        Column("bonuses", "Bonuses — per year", 18, "optional",
               "Annual bonus that is part of the package. Leave blank for a one-off bonus that is not.",
               lambda ws, letter, n: add_number_validation(
                   ws, letter, n, minimum=0, maximum=99999999,
                   title="Bonuses", message="A number — annual rupees, digits only."),
               number_format=MONEY_FORMAT),
        Column("deductions", "Deductions — per year", 20, "optional",
               "Anything subtracted from the package annually. 0 or blank if none.\n\n"
               "Do not put PF or tax here unless it genuinely reduces what the company pays out.",
               lambda ws, letter, n: add_number_validation(
                   ws, letter, n, minimum=0, maximum=99999999,
                   title="Deductions", message="A number — annual rupees, digits only."),
               number_format=MONEY_FORMAT),
        Column("total", "Total — per year (auto)", 20, "auto",
               "Basic + allowances + bonuses − deductions. Calculated — do not type over it.\n\n"
               "This is the figure the system treats as the salary, so read it back: if it does not "
               "match the CTC on the letter, one of the four columns to the left is wrong.",
               formula='=IF($A{row}="","",$D{row}+IF($E{row}="",0,$E{row})+IF($F{row}="",0,$F{row})-IF($G{row}="",0,$G{row}))',
               number_format=MONEY_FORMAT),
        Column("currency", "Currency", 12, "required",
               REQUIRED_NOTE + "INR unless the package really is in another currency.",
               lambda ws, letter, n: add_list_validation(
                   ws, letter, n, "List_Currency", allow_blank=False,
                   title="Currency", message="Pick from the list. INR for rupees.")),
        Column("type", "Revision Type", 22, "required",
               REQUIRED_NOTE
               + "Why the money changed. Pick from the list — 'Allowed values' explains each.\n\n"
                 "For the OLDEST row of each person, use INITIAL (the salary they joined on).",
               lambda ws, letter, n: add_list_validation(
                   ws, letter, n, "List_RevisionType", allow_blank=False,
                   title="Revision type",
                   message="Pick one from the list. Use INITIAL for the salary somebody joined on.")),
        Column("reason", "Reason", 34, "optional",
               "One line, in the words you would use to explain it: 'Annual review 2025', "
               "'Promoted to Team Lead', 'Corrected to market'. It is shown next to the figure for years."),
        Column("decision", "Decision Date", 16, "optional",
               "When the raise was actually decided, if you know it — often a month or two before it "
               "started. Leave blank and the effective date is used.",
               lambda ws, letter, n: add_date_validation(
                   ws, letter, n, title="Decision date",
                   message="A real date, typed like 15-Mar-2025."),
               number_format=DATE_FORMAT),
        Column("approved", "Approved By", 24, "optional",
               "Who signed it off — a name or their login email. For historical rows, whoever you "
               "have on record; blank is fine."),
        Column("in_force", "Was It Actually Paid?", 20, "required",
               REQUIRED_NOTE
               + "Yes for a salary that was really paid (or is agreed and will be).\n"
                 "No for a proposal that was never approved.\n\n"
                 "Anything marked No is loaded as a proposal awaiting approval and is NOT counted as "
                 "anybody's salary. If in doubt, Yes.",
               lambda ws, letter, n: add_list_validation(
                   ws, letter, n, "List_YesNo", allow_blank=False,
                   title="Was it paid?", message="Yes or No.")),
        Column("notes", "Notes", 30, "optional", "Anything payroll should know about this package."),
    ]
    write_grid(worksheet, columns, last)

    flag(worksheet, f"A2:A{last + 1}", 'AND($A2<>"",COUNTIF(Emp_Codes,$A2)=0)')
    # Two salaries starting the same day for one person: almost always a
    # duplicated row rather than two real decisions.
    flag(
        worksheet,
        f"C2:C{last + 1}",
        f'AND($A2<>"",$C2<>"",COUNTIFS($A$2:$A${last + 1},$A2,$C$2:$C${last + 1},$C2)>1)',
    )
    flag(worksheet, f"H2:H{last + 1}", 'AND($A2<>"",$H2<=0)')
    return worksheet


def build_access(workbook):
    last = ROWS["access"]
    worksheet = workbook.create_sheet(S_ACCESS)

    columns = [
        Column("login", "App Login Email", 30, "required",
               REQUIRED_NOTE
               + "The email address this person signs in with. One row per person who needs access.\n\n"
                 "If they already use the system for anything else, use that same email — do not create "
                 "a second account.",
               lambda ws, letter, n: add_formula_validation(
                   ws, letter, n, f'=AND(ISNUMBER(SEARCH("@",{letter}2)),COUNTIF($A$2:$A${last + 1},{letter}2)<=1)',
                   title="Login email",
                   message="An email address, and only once in this sheet.")),
        Column("name", "Full Name", 24, "required", REQUIRED_NOTE + "Who the account belongs to."),
        Column("code", "Employee Code", 18, "optional",
               "Which employee this account IS — a code from sheet 3.\n\n"
               "Fill this in wherever you can. It is what lets somebody see their own salary, and what "
               "makes 'my team' mean their actual team. Leave blank only for an account that is not a "
               "person in the directory (a shared HR login, say).",
               lambda ws, letter, n: add_list_validation(
                   ws, letter, n, "Emp_Codes",
                   title="Employee",
                   message="Pick an Employee Code from sheet '3. Employees'.")),
        Column("emp_name", "Employee Name (auto)", 24, "auto",
               "Looked up from the code beside it.",
               formula='=IF($C{row}="","",IFERROR(INDEX(Emp_Names,MATCH($C{row},Emp_Codes,0)),"NOT FOUND"))'),
        Column("role", "Role", 26, "required",
               REQUIRED_NOTE
               + "What this person is allowed to see. The 'Allowed values' sheet spells out each one — "
                 "please read it before filling this in, because the difference between Manager and HR "
                 "is the difference between seeing your team's pay and seeing everybody's.\n\n"
                 "When in doubt pick the SMALLER one. It is easy to add access later and impossible to "
                 "un-see a salary.",
               lambda ws, letter, n: add_list_validation(
                   ws, letter, n, "List_Role", allow_blank=False,
                   title="Role", message="Pick one from the list. 'Allowed values' explains each.")),
        Column("approve", "May Approve Salary Revisions?", 30, "optional",
               "Yes only for the people who actually sign off a raise — usually Finance or the owner.\n\n"
               "Entering a revision and approving it are separate rights on purpose, so that the person "
               "who prepares a raise is not the person who approves it. Leave blank for No.",
               lambda ws, letter, n: add_list_validation(
                   ws, letter, n, "List_YesNo",
                   title="May approve?", message="Yes, No, or leave blank.")),
        Column("notes", "Notes", 30, "optional", "Anything we should know — 'read-only until March', and so on."),
    ]
    write_grid(worksheet, columns, last)
    flag(worksheet, f"C2:C{last + 1}", 'AND($C2<>"",COUNTIF(Emp_Codes,$C2)=0)')
    return worksheet


# ---------------------------------------------------------------------------
# Named ranges — what the dropdowns and the checks read
# ---------------------------------------------------------------------------

#: name -> (sheet, column letter, row budget key)
NAMED_RANGES = {
    "Dept_Codes": (S_DEPT, "A", "departments"),
    "Dept_Names": (S_DEPT, "B", "departments"),
    "Dept_Parents": (S_DEPT, "C", "departments"),
    "Dept_Heads": (S_DEPT, "E", "departments"),
    "Desig_Codes": (S_DESIG, "A", "designations"),
    "Desig_Names": (S_DESIG, "B", "designations"),
    "Desig_Levels": (S_DESIG, "C", "designations"),
    "Emp_Codes": (S_EMP, "A", "employees"),
    "Emp_First": (S_EMP, "B", "employees"),
    "Emp_Names": (S_EMP, "D", "employees"),
    "Emp_Email": (S_EMP, "E", "employees"),
    "Emp_Status": (S_EMP, "I", "employees"),
    "Emp_Exit": (S_EMP, "J", "employees"),
    "Emp_Dept": (S_EMP, "K", "employees"),
    "Emp_Desig": (S_EMP, "M", "employees"),
    "Emp_Manager": (S_EMP, "R", "employees"),
    "Emp_Login": (S_EMP, "U", "employees"),
    "Sal_Emp": (S_SAL, "A", "salary"),
    "Sal_Date": (S_SAL, "C", "salary"),
    "Sal_Total": (S_SAL, "H", "salary"),
    "Acc_Login": (S_ACCESS, "A", "access"),
    "Acc_Emp": (S_ACCESS, "C", "access"),
    "Acc_Role": (S_ACCESS, "E", "access"),
}


def register_names(workbook):
    for name, (sheet, column, budget) in NAMED_RANGES.items():
        workbook.defined_names.add(
            DefinedName(name, attr_text=sheet_ref(sheet, column, ROWS[budget]))
        )


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

#: (check, what it should be, formula, kind)
#: ``kind`` is "must" for something that has to be zero before the file is sent,
#: or "info" for a number that is worth reading but is not a mistake.
CHECKS = [
    ("Departments", None, None, "heading"),
    ("Rows with a name but no code", "0", '=SUMPRODUCT((Dept_Names<>"")*(Dept_Codes=""))', "must"),
    ("Department codes used twice", "0", '=SUMPRODUCT((Dept_Codes<>"")*(COUNTIF(Dept_Codes,Dept_Codes)>1))', "must"),
    ("Department names used twice", "0", '=SUMPRODUCT((Dept_Names<>"")*(COUNTIF(Dept_Names,Dept_Names)>1))', "must"),
    ("'Sits inside' pointing at a department that does not exist", "0",
     '=SUMPRODUCT((Dept_Parents<>"")*(COUNTIF(Dept_Codes,Dept_Parents)=0))', "must"),
    ("Department heads who are not on sheet 3", "0",
     '=SUMPRODUCT((Dept_Heads<>"")*(COUNTIF(Emp_Codes,Dept_Heads)=0))', "must"),
    ("Departments filled in", "—", '=SUMPRODUCT((Dept_Codes<>"")*1)', "info"),

    ("Designations", None, None, "heading"),
    ("Designation codes used twice", "0",
     '=SUMPRODUCT((Desig_Codes<>"")*(COUNTIF(Desig_Codes,Desig_Codes)>1))', "must"),
    ("Designation names used twice", "0",
     '=SUMPRODUCT((Desig_Names<>"")*(COUNTIF(Desig_Names,Desig_Names)>1))', "must"),
    ("Designations with no level", "0", '=SUMPRODUCT((Desig_Codes<>"")*(Desig_Levels=""))', "must"),
    ("Designations filled in", "—", '=SUMPRODUCT((Desig_Codes<>"")*1)', "info"),

    ("Employees", None, None, "heading"),
    ("Rows with a name but no employee code", "0", '=SUMPRODUCT((Emp_First<>"")*(Emp_Codes=""))', "must"),
    ("Employee codes used twice", "0", '=SUMPRODUCT((Emp_Codes<>"")*(COUNTIF(Emp_Codes,Emp_Codes)>1))', "must"),
    ("Employees with no department", "0", '=SUMPRODUCT((Emp_Codes<>"")*(Emp_Dept=""))', "must"),
    ("Department codes that are not on sheet 1", "0",
     '=SUMPRODUCT((Emp_Dept<>"")*(COUNTIF(Dept_Codes,Emp_Dept)=0))', "must"),
    ("Employees with no designation", "0", '=SUMPRODUCT((Emp_Codes<>"")*(Emp_Desig=""))', "must"),
    ("Designation codes that are not on sheet 2", "0",
     '=SUMPRODUCT((Emp_Desig<>"")*(COUNTIF(Desig_Codes,Emp_Desig)=0))', "must"),
    ("'Reports to' pointing at somebody who is not on this sheet", "0",
     '=SUMPRODUCT((Emp_Manager<>"")*(COUNTIF(Emp_Codes,Emp_Manager)=0))', "must"),
    ("People reporting to themselves", "0",
     '=SUMPRODUCT((Emp_Manager<>"")*(Emp_Manager=Emp_Codes))', "must"),
    ("Work emails used twice", "0", '=SUMPRODUCT((Emp_Email<>"")*(COUNTIF(Emp_Email,Emp_Email)>1))', "must"),
    ("Login emails used twice on sheet 3", "0",
     '=SUMPRODUCT((Emp_Login<>"")*(COUNTIF(Emp_Login,Emp_Login)>1))', "must"),
    ("People who have left with no last working day", "0",
     '=SUMPRODUCT((Emp_Codes<>"")*(Emp_Exit="")*((Emp_Status="RESIGNED")+(Emp_Status="TERMINATED")'
     '+(Emp_Status="RETIRED")+(Emp_Status="INACTIVE")))', "must"),
    ("A last working day on somebody still employed", "0",
     '=SUMPRODUCT((Emp_Exit<>"")*((Emp_Status="ACTIVE")+(Emp_Status="PROBATION")'
     '+(Emp_Status="ON_LEAVE")+(Emp_Status="SUSPENDED")))', "must"),
    ("Top-level people (blank 'reports to')", "1 — or one per separate unit",
     '=SUMPRODUCT((Emp_Codes<>"")*(Emp_Manager=""))', "info"),
    ("Employees filled in", "—", '=SUMPRODUCT((Emp_Codes<>"")*1)', "info"),
    ("Managers (somebody reports to them)", "—",
     '=SUMPRODUCT((Emp_Codes<>"")*(COUNTIF(Emp_Manager,Emp_Codes)>0))', "info"),

    ("Salary history", None, None, "heading"),
    ("Salary rows for somebody who is not on sheet 3", "0",
     '=SUMPRODUCT((Sal_Emp<>"")*(COUNTIF(Emp_Codes,Sal_Emp)=0))', "must"),
    ("Salary rows with no 'effective from' date", "0", '=SUMPRODUCT((Sal_Emp<>"")*(Sal_Date=""))', "must"),
    ("One person with two salaries starting the same day", "0",
     '=SUMPRODUCT((Sal_Emp<>"")*(Sal_Date<>"")*(COUNTIFS(Sal_Emp,Sal_Emp,Sal_Date,Sal_Date)>1))', "must"),
    ("Salary rows totalling zero or less", "0", '=SUMPRODUCT((Sal_Emp<>"")*(Sal_Total<=0))', "must"),
    ("Employees with no salary row at all", "0",
     '=SUMPRODUCT((Emp_Codes<>"")*(COUNTIF(Sal_Emp,Emp_Codes)=0))', "must"),
    ("Salary rows filled in", "—", '=SUMPRODUCT((Sal_Emp<>"")*1)', "info"),

    ("Access and roles", None, None, "heading"),
    ("Access rows with no role", "0", '=SUMPRODUCT((Acc_Login<>"")*(Acc_Role=""))', "must"),
    ("Employee codes on sheet 5 that are not on sheet 3", "0",
     '=SUMPRODUCT((Acc_Emp<>"")*(COUNTIF(Emp_Codes,Acc_Emp)=0))', "must"),
    ("Logins used on sheet 3 that are missing from sheet 5", "0",
     '=SUMPRODUCT((Emp_Login<>"")*(COUNTIF(Acc_Login,Emp_Login)=0))', "must"),
    ("People given HR Administrator", "as few as possible", '=COUNTIF(Acc_Role,"HR Administrator")', "info"),
    ("People who may approve salary revisions", "at least 1",
     f"=COUNTIF('{S_ACCESS}'!$F$2:$F${ROWS['access'] + 1},\"Yes\")", "info"),
]


def build_checks(workbook):
    worksheet = workbook.create_sheet(S_CHECKS)
    worksheet.sheet_view.showGridLines = False
    worksheet.column_dimensions["A"].width = 4
    worksheet.column_dimensions["B"].width = 62
    worksheet.column_dimensions["C"].width = 26
    worksheet.column_dimensions["D"].width = 12
    worksheet.column_dimensions["E"].width = 14

    worksheet["B2"] = "Checks"
    worksheet["B2"].font = H1
    worksheet["B3"] = (
        "These count themselves as you type. Everything in the 'Must be fixed' rows has to read 0 "
        "before the workbook is sent back — each one is something that would either fail to import or "
        "import as the wrong thing. Nothing here needs to be filled in."
    )
    worksheet["B3"].font = SMALL
    worksheet["B3"].alignment = WRAP_LEFT
    worksheet.merge_cells("B3:E3")
    worksheet.row_dimensions[3].height = wrapped_height(worksheet["B3"].value, 112)

    row = 5
    for header, width in (("What is checked", "B"), ("Should be", "C"), ("Found", "D"), ("", "E")):
        cell = worksheet[f"{width}{row}"]
        cell.value = header
        cell.font = WHITE
        cell.fill = PatternFill("solid", fgColor=HEAD_REQUIRED)
        cell.alignment = WRAP_CENTER
        cell.border = BOX
    row += 1

    first_data_row = row
    for label, should_be, formula, kind in CHECKS:
        if kind == "heading":
            cell = worksheet[f"B{row}"]
            cell.value = label
            cell.font = H2
            worksheet.row_dimensions[row].height = 22
            row += 1
            continue
        label_cell = worksheet[f"B{row}"]
        label_cell.value = label
        label_cell.font = BODY
        label_cell.alignment = WRAP_LEFT
        label_cell.border = BOX
        fit_row(worksheet, row, [(label, 62), (should_be, 26)])

        should_cell = worksheet[f"C{row}"]
        should_cell.value = should_be
        should_cell.font = SMALL
        should_cell.alignment = WRAP_CENTER
        should_cell.border = BOX

        found = worksheet[f"D{row}"]
        found.value = formula
        found.font = DARK_BOLD
        found.alignment = WRAP_CENTER
        found.border = BOX

        verdict = worksheet[f"E{row}"]
        verdict.value = "—" if kind == "info" else f'=IF(D{row}=0,"OK","Fix")'
        verdict.font = DARK_BOLD
        verdict.alignment = WRAP_CENTER
        verdict.border = BOX
        row += 1

    last_data_row = row - 1
    worksheet.conditional_formatting.add(
        f"D{first_data_row}:E{last_data_row}",
        FormulaRule(formula=[f'$E{first_data_row}="Fix"'],
                    fill=PatternFill("solid", bgColor=WARN_FILL),
                    font=Font(color=WARN_TEXT, bold=True)),
    )
    worksheet.conditional_formatting.add(
        f"D{first_data_row}:E{last_data_row}",
        FormulaRule(formula=[f'$E{first_data_row}="OK"'],
                    fill=PatternFill("solid", bgColor=OK_FILL)),
    )

    row += 1
    note = worksheet[f"B{row}"]
    note.value = (
        "Not checked here, because a spreadsheet cannot see it: a reporting LOOP more than two people "
        "long (A → B → C → A). Sheet 3 turns the cell red for the two-person case; anything deeper is "
        "caught when the file is loaded, and we will come back to you with the names."
    )
    note.font = SMALL
    note.alignment = WRAP_LEFT
    note.fill = PatternFill("solid", fgColor=NOTE_FILL)
    worksheet.merge_cells(start_row=row, start_column=2, end_row=row, end_column=5)
    worksheet.row_dimensions[row].height = wrapped_height(note.value, 112)
    return worksheet


# ---------------------------------------------------------------------------
# Example and instructions
# ---------------------------------------------------------------------------

EXAMPLE_DEPARTMENTS = [
    ["CORP", "Company", "", "EMP001", "The organisation as a whole", "ACTIVE", 1],
    ["TECH", "Technology", "CORP", "EMP002", "Engineering, quality, infrastructure", "ACTIVE", 2],
    ["ENG", "Engineering", "TECH", "EMP003", "Product and platform development", "ACTIVE", 3],
    ["QA", "QA", "TECH", "", "Release testing", "ACTIVE", 4],
    ["HR", "Human Resources", "CORP", "EMP006", "People, hiring and payroll", "ACTIVE", 5],
]

EXAMPLE_DESIGNATIONS = [
    ["CEO", "CEO", 1, "Yes", "", "ACTIVE"],
    ["CTO", "CTO", 2, "Yes", "", "ACTIVE"],
    ["MGR", "Manager", 5, "Yes", "", "ACTIVE"],
    ["SR_DEV", "Senior Developer", 7, "No", "", "ACTIVE"],
    ["DEV", "Developer", 8, "No", "", "ACTIVE"],
]

EXAMPLE_EMPLOYEES = [
    ["EMP001", "Arun", "Mehta", "arun@example.com", "01-Apr-2016", "ACTIVE", "", "CORP", "CEO", "Chief Executive Officer", "", "Yes", "arun@example.com"],
    ["EMP002", "Priya", "Nair", "priya@example.com", "10-Jul-2017", "ACTIVE", "", "TECH", "CTO", "Chief Technology Officer", "EMP001", "Yes", "priya@example.com"],
    ["EMP003", "Sandeep", "Rao", "sandeep@example.com", "18-Feb-2019", "ACTIVE", "", "ENG", "MGR", "Engineering Manager", "EMP002", "Yes", "sandeep@example.com"],
    ["EMP004", "Kavya", "Iyer", "kavya@example.com", "01-Jun-2020", "ACTIVE", "", "ENG", "SR_DEV", "Senior Developer", "EMP003", "", "kavya@example.com"],
    ["EMP005", "Rohit", "Sharma", "rohit@example.com", "05-Sep-2022", "ACTIVE", "", "ENG", "DEV", "Developer", "EMP004", "", ""],
    ["EMP006", "Rajesh", "Kulkarni", "rajesh@example.com", "02-May-2018", "ACTIVE", "", "HR", "MGR", "HR Head", "EMP001", "Yes", "rajesh@example.com"],
    ["EMP007", "Anjali", "Verma", "anjali@example.com", "13-May-2024", "RESIGNED", "31-Aug-2026", "QA", "DEV", "QA Engineer", "EMP002", "", ""],
]

EXAMPLE_SALARY = [
    ["EMP004", "01-Jan-2024", 750000, 200000, 50000, 0, 1000000, "INR", "INITIAL", "Joining salary", "Yes"],
    ["EMP004", "01-Apr-2025", 900000, 240000, 90000, 0, 1230000, "INR", "ANNUAL_INCREMENT", "Annual review 2025", "Yes"],
    ["EMP004", "01-Apr-2026", 1080000, 280000, 140000, 0, 1500000, "INR", "PROMOTION", "Promoted to Senior Developer", "Yes"],
    ["EMP005", "05-Sep-2022", 480000, 120000, 0, 0, 600000, "INR", "INITIAL", "Joining salary", "Yes"],
]

EXAMPLE_ACCESS = [
    ["arun@example.com", "Arun Mehta", "EMP001", "HR Administrator", "Yes"],
    ["priya@example.com", "Priya Nair", "EMP002", "Department Head", ""],
    ["sandeep@example.com", "Sandeep Rao", "EMP003", "Manager", ""],
    ["kavya@example.com", "Kavya Iyer", "EMP004", "Employee (Self Service)", ""],
    ["rajesh@example.com", "Rajesh Kulkarni", "EMP006", "HR", ""],
]


def build_example(workbook):
    """One small company, filled in properly, so the codes can be seen joining up."""
    worksheet = workbook.create_sheet(S_EXAMPLE)
    worksheet.sheet_view.showGridLines = False
    worksheet.column_dimensions["A"].width = 3
    for index in range(2, 18):
        worksheet.column_dimensions[get_column_letter(index)].width = 17

    row = 2
    worksheet[f"B{row}"] = "Example — a five-department, seven-person company"
    worksheet[f"B{row}"].font = H1
    row += 1
    worksheet[f"B{row}"] = (
        "Nothing here needs to be filled in or deleted. It is here to show how the sheets point at each "
        "other: notice that EMP004 appears on the employee sheet once and on the salary sheet three "
        "times, and that every department, designation and manager is written as a CODE."
    )
    worksheet[f"B{row}"].font = SMALL
    worksheet[f"B{row}"].alignment = WRAP_LEFT
    worksheet.merge_cells(start_row=row, start_column=2, end_row=row, end_column=12)
    worksheet.row_dimensions[row].height = wrapped_height(worksheet[f"B{row}"].value, 190)
    row += 2

    def table(heading, headers, rows_data, hint=""):
        nonlocal row
        worksheet[f"B{row}"] = heading
        worksheet[f"B{row}"].font = H2
        row += 1
        if hint:
            worksheet[f"B{row}"] = hint
            worksheet[f"B{row}"].font = SMALL
            worksheet[f"B{row}"].alignment = WRAP_LEFT
            worksheet.merge_cells(start_row=row, start_column=2, end_row=row, end_column=12)
            worksheet.row_dimensions[row].height = wrapped_height(hint, 190)
            row += 1
        for offset, header in enumerate(headers):
            cell = worksheet.cell(row=row, column=2 + offset, value=header)
            cell.font = WHITE
            cell.fill = PatternFill("solid", fgColor=HEAD_OPTIONAL)
            cell.alignment = WRAP_CENTER
            cell.border = BOX
        worksheet.row_dimensions[row].height = 30
        row += 1
        for entry in rows_data:
            for offset, value in enumerate(entry):
                cell = worksheet.cell(row=row, column=2 + offset, value=value)
                cell.font = BODY
                cell.border = BOX
                if isinstance(value, int) and offset >= 2 and heading.startswith("Salary"):
                    cell.number_format = MONEY_FORMAT
            row += 1
        row += 1

    table(
        "Sheet 1 — Departments",
        ["Code", "Name", "Sits inside", "Head", "Description", "Status", "Order"],
        EXAMPLE_DEPARTMENTS,
        "CORP has no parent — it is the top. TECH sits inside CORP, ENG and QA inside TECH.",
    )
    table(
        "Sheet 2 — Designations",
        ["Code", "Name", "Level", "Manages people?", "Description", "Status"],
        EXAMPLE_DESIGNATIONS,
        "Level 1 is the top of the ladder. Notice a Senior Developer is level 7 whoever they report to.",
    )
    table(
        "Sheet 3 — Employees (the auto columns are left out here)",
        ["Code", "First", "Last", "Email", "Joined", "Status", "Last working day",
         "Dept code", "Desig code", "Job title", "Reports to", "Manager?", "Login email"],
        EXAMPLE_EMPLOYEES,
        "EMP001 has a blank 'reports to' because he is the top. EMP007 has left, so she has a last "
        "working day — and she keeps her place in the chart and her history.",
    )
    table(
        "Sheet 4 — Salary history",
        ["Employee", "Effective from", "Basic", "Allowances", "Bonuses", "Deductions",
         "Total (auto)", "Currency", "Revision type", "Reason", "Paid?"],
        EXAMPLE_SALARY,
        "Three rows for EMP004, oldest first: what she joined on, her 2025 increment and her 2026 "
        "promotion. The old rows are NOT edited when a new one is added — that history is the point.",
    )
    table(
        "Sheet 5 — Access and roles",
        ["Login email", "Full name", "Employee code", "Role", "May approve salary?"],
        EXAMPLE_ACCESS,
        "Kavya gets 'Employee (Self Service)': she can see her own salary and nobody else's — not even "
        "Rohit's, who reports to her. Sandeep gets 'Manager', so he can see his team's.",
    )
    return worksheet


def build_start(workbook):
    worksheet = workbook.create_sheet(S_START, 0)
    lines = [
        ("h1", "Employee data collection"),
        ("body",
         "This workbook is how we take over your employee records. Fill in the five numbered sheets and "
         "send the file back — that is the whole job. Nothing needs to be formatted, sorted or "
         "prettified; the dropdowns and the checks do the fussy part.\n\n"
         "ONE FILE PER COMPANY UNIT. Department codes, designation codes and employee codes only have "
         "to be unique inside one unit, so if you run more than one (Oil, Mart, Beverages), please send "
         "a separate copy of this workbook for each — it is far safer than one file with everything "
         "mixed together."),
        ("body", ""),
        ("h2", "Fill the sheets in this order"),
        ("body",
         "1.  Departments — the list of departments, and which sits inside which.\n"
         "2.  Designations — the ladder of titles, each with how senior it is.\n"
         "3.  Employees — everybody, with their department code, designation code and who they report to.\n"
         "4.  Salary history — one row per salary anybody has ever been on. Not one row per person.\n"
         "5.  Access and roles — who logs in, and what they are allowed to see."),
        ("body",
         "The order matters because sheets 3, 4 and 5 choose from codes you create on sheets 1 and 2. "
         "If a department or a designation is missing from a dropdown, add it to sheet 1 or 2 and it "
         "appears in the list straight away."),
        ("body", ""),
        ("h2", "What the colours mean"),
        ("body",
         "Dark heading with a *   —  required. A row without it cannot be loaded.\n"
         "Grey heading             —  optional. Leave it blank if you do not hold it; blank is better than a guess.\n"
         "Pale grey heading (auto) —  calculated. Do not type in these columns; they look things up for you so you can check a code is the one you meant.\n"
         "A cell that turns red    —  something is wrong in that cell. Hover it, or look at the Checks sheet."),
        ("body", ""),
        ("h2", "If you run out of rows"),
        ("body",
         f"The sheets come ready for {ROWS['departments']} departments, {ROWS['designations']} "
         f"designations, {ROWS['employees']} employees, {ROWS['salary']} salary rows and "
         f"{ROWS['access']} logins. If you need more, click the row number of the LAST row, copy it, "
         "and paste it down as many times as you need — the dropdowns, the date rules and the grey "
         "calculated columns all come with it. Typing into a fresh row below the prepared ones works "
         "too, but you lose the dropdowns, so copying is safer."),
        ("body", ""),
        ("h2", "Hover any heading"),
        ("body",
         "Every column heading has a note attached explaining exactly what goes in it, with an example. "
         "Rest the mouse on the heading to read it. The 'Allowed values' sheet lists every value the "
         "dropdowns accept and what each one means."),
        ("body", ""),
        ("h2", "The five things that go wrong most often"),
        ("body",
         "•  Names instead of codes. Departments, designations and managers are always written as the "
         "short CODE from sheets 1–2 or the employee code from sheet 3. The grey (auto) column beside "
         "each one shows the name it found — read it back.\n\n"
         "•  Monthly salary in an annual column. Every money column on sheet 4 is PER YEAR. If your "
         "register is monthly, multiply by twelve first.\n\n"
         "•  One row per person on the salary sheet. It is one row per SALARY. Three raises means three "
         "rows, oldest first. Please do not overwrite the old figures — the history is the reason we are "
         "asking.\n\n"
         "•  A last working day on somebody who is still here. That column is only for people who have "
         "left. Somebody on long leave has NOT left.\n\n"
         "•  Everybody made an administrator on sheet 5. Salary is the sensitive part of this system. "
         "Give each person the smallest role that lets them do their job; it is easy to widen later."),
        ("body", ""),
        ("h2", "Salary is treated as confidential"),
        ("body",
         "Sheet 4 is the reason this file should be shared carefully: send it to us directly rather than "
         "over a group chat, and if your organisation would rather not put pay in a spreadsheet at all, "
         "fill in sheets 1, 2, 3 and 5 and tell us — the salary history can be loaded separately by "
         "whoever is allowed to hold it.\n\n"
         "Once it is loaded, nobody sees a salary unless they have been given salary access "
         "specifically: being somebody's manager is not enough on its own, and every time one person "
         "opens another person's pay, that is recorded."),
        ("body", ""),
        ("h2", "Before you send it back"),
        ("body",
         "Open the Checks sheet. Every row marked 'Fix' is something that would import as the wrong "
         "thing — most of them take a second to correct. When they all read OK, the file is done.\n\n"
         "Photos, if you have them: put the file name in the last column of sheet 3 (EMP001.jpg) and "
         "send the images in one folder with the workbook. Please do not paste pictures into the sheet."),
        ("body", ""),
        ("note",
         "Anything you cannot answer, leave blank and say so when you send the file. A blank we know "
         "about is easy; a guess that looks like data is not."),
    ]
    row = title_block(worksheet, lines[:3])
    row = intake_block(worksheet, row)
    return title_block(worksheet, lines[3:], start_row=row)


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def main():
    workbook = Workbook()
    workbook.remove(workbook.active)

    # Tab order is the fill order. "Start here" is inserted at position 0 by
    # its builder, so it is what opens.
    build_departments(workbook)
    build_designations(workbook)
    build_employees(workbook)
    build_salary(workbook)
    build_access(workbook)
    build_example(workbook)
    build_values_sheet(workbook)
    build_checks(workbook)
    build_start(workbook)
    register_names(workbook)

    workbook.properties.title = "Employee data collection template"
    workbook.properties.subject = "Employees, departments, designations, salary history and access"
    workbook.properties.creator = "Jivo — employee hierarchy module"
    workbook.properties.description = (
        "Fill in the five numbered sheets and send the file back. Hover a column heading for the rule "
        "that applies to it; the Checks sheet counts anything still wrong."
    )

    workbook.save(OUTPUT)
    print(f"Wrote {OUTPUT}")
    print(f"  sheets: {', '.join(workbook.sheetnames)}")
    print(f"  named ranges: {len(workbook.defined_names)}")


if __name__ == "__main__":
    main()
