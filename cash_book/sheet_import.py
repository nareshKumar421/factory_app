"""
Reads the cash sheet workbook the module replaces.

Parsing only: nothing here touches the database, so the whole file can be
checked before a single row is written (``import_cash_sheet --dry-run``).

THE DATE TRAP
-------------
Every date in this sheet was typed ``dd/mm/yyyy``, but the workbook was not
always edited on a machine that reads dates that way. While the input locale
was ``mm/dd/yyyy``, Excel mis-read every date whose day was 12 or lower --
storing the day as the month and the month as the day -- and refused the rest,
leaving them as *text* like ``30/05/2026``. Later the file was edited on a
machine reading ``dd/mm/yyyy``, and from that point dates were stored correctly.
The column displays ``dd/mm/yyyy`` throughout, so none of this shows on screen.

That gives three kinds of cell:

* **text** (``30/05/2026``) -- Excel never parsed it, so it is literal
  ``dd/mm/yyyy``. Always read as typed.
* **datetime, before the switch** -- transposed. ``2026-12-06`` was typed as
  12 June, not 6 December, and has to be swapped back.
* **datetime, after the switch** -- literal. ``2026-07-18`` is 18 July.

WHERE THE SWITCH IS, AND WHY IT IS NOT HARDCODED
------------------------------------------------
Each cell type is decisive about the locale in force when it was written:

* a text date proves the locale was ``mm/dd`` (only that refuses ``30/05``);
* a datetime whose **day is above 12** proves the locale was ``dd/mm`` (only
  that produces ``2026-07-18``).

:func:`detect_transposition_boundary` collects those, checks the two groups do
not interleave, and returns the row where one era ends and the other begins.
Ambiguous cells -- a datetime whose day and month are both 12 or under -- take
the rule of whichever era they fall in. If the groups ever *do* interleave the
function raises, so a workbook this reasoning does not fit fails loudly instead
of being silently mis-dated.

The Date and Sign Date columns are calibrated separately and genuinely switch
at different rows (236/237 and 215/216 in the September 2026 file). That is not
a bug: an entry's date is typed when the money moves, and its bunch's sign date
weeks later when the vouchers come back -- different sittings, either side of
the switch.

THE READING IS CHECKED, NOT ASSUMED
-----------------------------------
:func:`check_bunch_dating` looks for entries dated after the day their own
bunch was signed, which is impossible. Read literally throughout, the workbook
is full of them -- a porter charge dated ``2026-12-06`` in a bunch signed
``16/06/2026``, signed six months before it was spent. Read as above, there are
none.
"""

from datetime import date, datetime

#: Column positions in the sheet, left to right.
COL_SERIAL = 0
COL_BUNCH = 1
COL_DATE = 2
COL_DEPARTMENT = 3
COL_GL = 4
COL_ITEM = 5
COL_DETAIL = 6
COL_OUT = 7
COL_IN = 8
COL_BALANCE = 9
COL_SIGN_DATE = 10
COL_SEND_DATE = 11

#: Columns past the register are a separate informal IOU list somebody keeps in
#: the same tab ("Jameet ji ko deye", 1638). Not part of the cash book.
LAST_REGISTER_COLUMN = 12

#: The sheet's Department column, mapped onto the four branches the business
#: actually runs on. The sheet spells things several ways ("Wg", "wg", "WG")
#: and names plant lines ("Canola") rather than branches, so this is a
#: translation, not a tidy-up. Anything unrecognised falls to Common, which is
#: what a spend nobody assigned belongs to anyway.
BRANCH_ALIASES = {
    "canola": "Oil",
    "oil": "Oil",
    "wg": "Beverage",
    "beverage": "Beverage",
    "beverages": "Beverage",
    "water": "Water",
    "mart": "Common",
    "common": "Common",
}

#: Where an unrecognised or blank department lands.
FALLBACK_BRANCH = "Common"


def to_branch(department: str) -> str:
    """The branch a sheet department belongs to."""
    return BRANCH_ALIASES.get((department or "").strip().lower(), FALLBACK_BRANCH)


class SheetError(ValueError):
    """Something the sheet cannot be read from, named so it can be reported."""


# ----------------------------------------------------------------------
# Dates
# ----------------------------------------------------------------------


def _as_date(value):
    if isinstance(value, datetime):
        return value.date()
    return value if isinstance(value, date) else None


def _is_blank(value):
    return value is None or str(value).strip() == ""


def detect_transposition_boundary(cells):
    """Find the row where this column stops being transposed.

    ``cells`` is an iterable of ``(row_index, value)``. Returns the first row
    index that should be read literally; every row before it is transposed. A
    column with no ``dd/mm``-era evidence returns ``None``, meaning "transposed
    all the way down".

    Raises :class:`SheetError` if the two eras interleave, because then no
    single boundary explains the column and every ambiguous cell in it would be
    a coin toss.
    """
    mm_dd_rows, dd_mm_rows = [], []
    for row_index, value in cells:
        if _is_blank(value):
            continue
        parsed = _as_date(value)
        if parsed is None:
            mm_dd_rows.append(row_index)  # text: Excel refused it
        elif parsed.day > 12:
            dd_mm_rows.append(row_index)  # only dd/mm input produces this

    if not dd_mm_rows:
        return None
    if not mm_dd_rows:
        return min(dd_mm_rows)

    boundary = min(dd_mm_rows)
    stragglers = [row for row in mm_dd_rows if row > boundary]
    if stragglers:
        raise SheetError(
            f"The date column does not switch locale cleanly: rows "
            f"{stragglers[:5]} were typed mm/dd but row {boundary} was typed "
            f"dd/mm. No single boundary explains this column, so its ambiguous "
            f"dates cannot be read safely."
        )
    return boundary


def parse_sheet_date(value, *, transposed: bool) -> date:
    """One date cell, as the person typing it meant it.

    ``transposed`` says which era the row falls in; it is ignored for a text
    cell, which Excel never parsed and which is therefore literal either way.
    """
    parsed = _as_date(value)
    if parsed is not None:
        if not transposed:
            return parsed
        if parsed.day > 12:
            raise SheetError(
                f"{parsed.isoformat()} sits in the transposed part of the sheet "
                f"but its day is above 12, so it cannot be swapped back."
            )
        return date(parsed.year, parsed.day, parsed.month)

    text = str(value).strip()
    if not text:
        raise SheetError("blank date")
    for separator in ("/", "-", "."):
        if separator in text:
            parts = text.split(separator)
            break
    else:
        raise SheetError(f"unreadable date {text!r}")
    if len(parts) != 3:
        raise SheetError(f"unreadable date {text!r}")
    try:
        day, month, year = (int(part) for part in parts)
    except ValueError as exc:
        raise SheetError(f"unreadable date {text!r}") from exc
    if year < 100:
        year += 2000
    try:
        return date(year, month, day)
    except ValueError as exc:
        raise SheetError(f"impossible date {text!r}") from exc


# ----------------------------------------------------------------------
# Rows
# ----------------------------------------------------------------------


def _text(value) -> str:
    return "" if value is None else str(value).strip()


def _amount(value):
    """An Out / In / Balance cell. Blank and zero both mean "nothing here"."""
    if _is_blank(value):
        return None
    try:
        amount = round(float(value), 2)
    except (TypeError, ValueError) as exc:
        raise SheetError(f"unreadable amount {value!r}") from exc
    return amount or None


def _padded(raw):
    raw = tuple(raw)[:LAST_REGISTER_COLUMN]
    return raw + (None,) * (LAST_REGISTER_COLUMN - len(raw))


def read_rows(worksheet):
    """Every real entry in the sheet, in its own order.

    Rows carrying only a serial number are the blank remainder of the template
    (514 of the 999 in the September 2026 file) and are skipped: a row with no
    date is not an entry.
    """
    raw_rows = [
        (index, _padded(raw))
        for index, raw in enumerate(worksheet.iter_rows(values_only=True), start=1)
        if index > 1
    ]
    dated = [
        (index, raw) for index, raw in raw_rows if not _is_blank(raw[COL_DATE])
    ]

    # Each date column is calibrated on its own -- see the module docstring.
    entry_boundary = detect_transposition_boundary(
        (index, raw[COL_DATE]) for index, raw in dated
    )
    sign_boundary = detect_transposition_boundary(
        (index, raw[COL_SIGN_DATE]) for index, raw in dated
    )
    send_boundary = detect_transposition_boundary(
        (index, raw[COL_SEND_DATE]) for index, raw in dated
    )

    def transposed(index, boundary):
        return boundary is None or index < boundary

    rows = []
    for index, raw in dated:
        cash_out = _amount(raw[COL_OUT])
        cash_in = _amount(raw[COL_IN])
        if cash_out is None and cash_in is None:
            raise SheetError(f"row {index}: neither an Out nor an In amount")
        if cash_out is not None and cash_in is not None:
            raise SheetError(f"row {index}: both an Out and an In amount")

        def optional(column, boundary):
            if _is_blank(raw[column]):
                return None
            return parse_sheet_date(
                raw[column], transposed=transposed(index, boundary)
            )

        bunch = raw[COL_BUNCH]
        try:
            rows.append(
                {
                    "excel_row": index,
                    "serial": raw[COL_SERIAL],
                    "bunch": int(bunch) if not _is_blank(bunch) else None,
                    "date": parse_sheet_date(
                        raw[COL_DATE],
                        transposed=transposed(index, entry_boundary),
                    ),
                    "department": _text(raw[COL_DEPARTMENT]),
                    "branch": to_branch(_text(raw[COL_DEPARTMENT])),
                    "gl": _text(raw[COL_GL]),
                    "item": _text(raw[COL_ITEM]),
                    "detail": _text(raw[COL_DETAIL]),
                    "out": cash_out,
                    "in": cash_in,
                    "balance": _amount(raw[COL_BALANCE]),
                    "sign_date": optional(COL_SIGN_DATE, sign_boundary),
                    "send_date": optional(COL_SEND_DATE, send_boundary),
                }
            )
        except SheetError as exc:
            raise SheetError(f"row {index}: {exc}") from exc
    return rows


# ----------------------------------------------------------------------
# Checks
# ----------------------------------------------------------------------


def running_balances(rows):
    """Rebuild the Balance column from the amounts, in the sheet's own order."""
    balance = 0.0
    rebuilt = []
    for row in rows:
        balance += row["in"] if row["in"] is not None else -row["out"]
        rebuilt.append(round(balance, 2))
    return rebuilt


def check_balances(rows, tolerance=0.01):
    """Rows whose own Balance cell disagrees with the arithmetic before it."""
    problems = []
    for row, rebuilt in zip(rows, running_balances(rows)):
        stated = row["balance"]
        if stated is not None and abs(stated - rebuilt) > tolerance:
            problems.append((row, stated, rebuilt))
    return problems


def check_bunch_dating(rows):
    """Entries dated after the day their own bunch was signed -- impossible.

    This is the check that settles the date reading; see the module docstring.
    """
    return [
        row
        for row in rows
        if row["bunch"] is not None
        and row["sign_date"] is not None
        and row["date"] > row["sign_date"]
    ]
