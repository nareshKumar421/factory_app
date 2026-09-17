"""
The advance summary block -- the list of who is holding the factory's cash.

It sits in columns M to P of the Cash details tab, immediately right of the
register and with no header of its own, which is exactly why it is easy to
miss: read the grid by its headers and the block is simply past the last one.
Four columns, in the custodian's shorthand::

    M  the date it was handed over
    N  who, and what for -- "Kabal singh ko deye advance gaddi k leye"
    O  how much
    P  an occasional note -- "500/- online by kamal ji"

WHAT IT IS, AND WHAT IT IS NOT
------------------------------
It is not a memo of rows already in the register. Its distinctive amounts --
1,638, 3,534, 4,790, 21,626, 83, 7,740, 1,815 -- appear nowhere in the book.
This is money that left the box without an expense to file it against, which
is precisely what an advance is.

It IS the complete list. The arithmetic says so::

    block net             30,347.00
    book closing balance  30,761.00
    notes left in the box    414.00

THE SIGN
--------
A positive row is cash out with somebody who has not yet said what it went on.
A negative row -- the ones written "ko dene hai" -- is the other direction:
they spent their own money and the factory owes them. Both belong on the same
list, because both are the same question, asked of the same person: what is
between us. So the sign is kept and the list nets.

THE TWO DETAILED TABS
---------------------
Two people have a tab of their own, because their balance never clears --
``bunty in out`` closes at exactly the 21,626.00 the block carries against
"Tiwari ji", the same pot written down twice. Where a tab's closing balance
matches a block row to the rupee, the tab is the detail behind that row and
the row is left alone; counting both would double it.
"""

from __future__ import annotations

import re

from .sheet_import import _amount, _is_blank, _text, detect_transposition_boundary, parse_sheet_date

# The block's four columns, 1-based, as openpyxl counts them.
COL_DATE = 13  # M
COL_DETAIL = 14  # N
COL_AMOUNT = 15  # O
COL_NOTE = 16  # P

# It starts at the very top of the sheet, beside the register's header row,
# and is short. Read generously and stop at the first long gap.
FIRST_ROW = 1
MAX_ROW = 60
GAP_THAT_ENDS_IT = 8

# "Kabal singh ko deye advance gaddi k leye" -> "Kabal singh". The shorthand
# always names the person first and then what it was for, in one of a handful
# of joining words.
NAME_END = re.compile(
    r"\s+(ko|ke|se|k|vg|ji\s+ko|jo\s+ko)\s+(deye|dene|lene|transfer|hai|leye)\b",
    re.IGNORECASE,
)
TRAILING_TITLE = re.compile(r"\s+(ji|sir|vg|jo)$", re.IGNORECASE)


def person_of(detail: str) -> str:
    """The person a block row is about, out of the custodian's shorthand.

    Only the name is wanted -- the rest of the line says what the money was
    for, which belongs in the detail, not in who is holding it.
    """
    text = _text(detail).strip()
    if not text:
        return ""
    match = NAME_END.search(text)
    name = text[: match.start()] if match else text
    # "Jasmeet ji" and "Jasmeet" are the same person; the title is dropped so
    # they do not become two holders.
    name = TRAILING_TITLE.sub("", name.strip())
    return " ".join(part.capitalize() for part in name.split()) or text


def read_advance_block(worksheet) -> list[dict]:
    """The block's rows, in sheet order.

    Rows with no amount are skipped: the block has blank spacer rows in it,
    and a line with a name but no figure is somebody the custodian started to
    write down and did not finish.
    """
    raw = []
    blanks = 0
    for index in range(FIRST_ROW, MAX_ROW + 1):
        detail = worksheet.cell(row=index, column=COL_DETAIL).value
        amount = worksheet.cell(row=index, column=COL_AMOUNT).value
        if _is_blank(detail) and _is_blank(amount):
            blanks += 1
            if blanks >= GAP_THAT_ENDS_IT and raw:
                break
            continue
        blanks = 0
        raw.append(
            (
                index,
                worksheet.cell(row=index, column=COL_DATE).value,
                detail,
                amount,
                worksheet.cell(row=index, column=COL_NOTE).value,
            )
        )

    # The block's dates carry the same dd/mm-vs-mm/dd transposition as the
    # register, and are calibrated the same way -- from the file, not a rule.
    boundary = detect_transposition_boundary(
        (index, date) for index, date, _, _, _ in raw if not _is_blank(date)
    )

    rows = []
    for index, date, detail, amount, note in raw:
        value = _amount(amount)
        if value is None:
            continue
        when = None
        if not _is_blank(date):
            when = parse_sheet_date(
                date, transposed=boundary is None or index < boundary
            )
        rows.append(
            {
                "excel_row": index,
                "date": when,
                "detail": _text(detail),
                "person": person_of(detail),
                "amount": value,
                # Negative means the factory owes them: they spent their own.
                "owed_to_them": value < 0,
                "note": _text(note),
            }
        )
    return rows


def block_total(rows) -> float:
    """What the list comes to, netting the two directions against each other."""
    return round(sum(row["amount"] for row in rows), 2)
