"""
Reads the workbook's other two registers: the card, and the people.

Both are looser than the cash register. The card sheet carries a second,
unrelated table in its right-hand columns, and the person tabs stack several
blocks one under another, each with its own header, and name other people all
through the detail text ("Sandhu ko deye", "Milkha ne deye").

None of which matters, because THE TAB IS THE PERSON. A tab's running Total
column nets every row in it regardless of whose name the row mentions, and ends
exactly where that person's balance ends -- 16,626 for "bunty in out", 265 for
"Jasmeet in out". The office opens a tab for somebody whose balance does not
clear quickly, and everything in it is their account with the custodian; the
other names are narrative, saying who they passed the cash to or who handed the
vouchers in for them.

So the person comes from the tab name and nothing is guessed from the text.
:func:`read_person_sheet` also returns the sheet's own closing Total, which the
importer checks its arithmetic against.

Dates carry the same transposition as the cash register -- see
:mod:`cash_book.sheet_import` -- and are read the same way.
"""

import re

from .sheet_import import (
    SheetError,
    _is_blank,
    _text,
    detect_transposition_boundary,
    parse_sheet_date,
)

# --- the card sheet ----------------------------------------------------
ATM_COL_DATE = 0
ATM_COL_OPENING = 1
ATM_COL_RECEIVED = 2
ATM_COL_WITHDRAWN = 3
ATM_COL_CLOSING = 4
#: Columns 5 and 6 are a separate voucher-submission log sharing the tab.
ATM_LAST_COLUMN = 5

#: The card's name sits above the table rather than in it.
ATM_NAME_ROW = 2
ATM_HEADER_ROW = 3

# --- the person tabs ---------------------------------------------------
LEDGER_COL_SERIAL = 0
LEDGER_COL_DATE = 1
LEDGER_COL_DETAIL = 2
LEDGER_COL_OUT = 3
LEDGER_COL_IN = 4
LEDGER_COL_TOTAL = 5
LEDGER_LAST_COLUMN = 6

#: A row whose detail says a batch of vouchers was handed in. The sheet spells
#: it four ways; all of them mean the same thing.
VOUCHER_PATTERN = re.compile(r"vou?ch?er|vocher|vouher", re.IGNORECASE)

#: How a tab is named after its person, where "Bunty" alone would read oddly.
TAB_NAMES = {
    "bunty in out": "Bunty Ji",
    "jasmeet in out": "Jasmeet Ji",
}


def person_of(sheet_name: str) -> str:
    """Whose account a tab is. "bunty in out" -> "Bunty Ji"."""
    key = sheet_name.strip().lower()
    if key in TAB_NAMES:
        return TAB_NAMES[key]
    return re.sub(r"\s+in\s+out$", "", sheet_name.strip(), flags=re.IGNORECASE).title()


def _amount(value):
    if _is_blank(value):
        return None
    try:
        amount = round(float(value), 2)
    except (TypeError, ValueError) as exc:
        raise SheetError(f"unreadable amount {value!r}") from exc
    return amount or None


def read_atm_sheet(worksheet):
    """The card: its name, what it opened at, and every movement on it.

    Withdrawals come back as rows of their own here even though the cash book
    is where they really live -- the importer uses them to find the matching
    cash receipts and point those at the card.
    """
    rows = [tuple(raw) for raw in worksheet.iter_rows(values_only=True)]
    if len(rows) <= ATM_HEADER_ROW:
        raise SheetError("the card sheet has no rows under its header")

    name = _text(rows[ATM_NAME_ROW - 1][0]) if len(rows) >= ATM_NAME_ROW else ""
    body = [
        (index, raw)
        for index, raw in enumerate(rows, start=1)
        if index > ATM_HEADER_ROW and not _is_blank(raw[ATM_COL_DATE])
    ]
    boundary = detect_transposition_boundary(
        (index, raw[ATM_COL_DATE]) for index, raw in body
    )

    opening = None
    movements = []
    for index, raw in body:
        if opening is None:
            opening = _amount(raw[ATM_COL_OPENING]) or 0.0
        received = _amount(raw[ATM_COL_RECEIVED])
        withdrawn = _amount(raw[ATM_COL_WITHDRAWN])
        if received is None and withdrawn is None:
            continue
        movements.append(
            {
                "excel_row": index,
                "date": parse_sheet_date(
                    raw[ATM_COL_DATE],
                    transposed=boundary is None or index < boundary,
                ),
                "kind": "RECEIPT" if received is not None else "WITHDRAWAL",
                "amount": received if received is not None else withdrawn,
                "stated_balance": _amount(raw[ATM_COL_CLOSING]),
            }
        )

    return {
        "name": name or "Imprest Debit Card",
        "opening_balance": opening or 0.0,
        "movements": movements,
    }


def read_person_sheet(worksheet, sheet_name):
    """One person's account: every row of the tab, plus its closing Total.

    Repeated header rows are skipped rather than treated as separators. They
    mark where the office started a fresh page, not a different person -- the
    Total column runs straight through them.
    """
    person = person_of(sheet_name)
    raw_rows = [
        (index, tuple(raw)[:LEDGER_LAST_COLUMN])
        for index, raw in enumerate(worksheet.iter_rows(values_only=True), start=1)
    ]

    dated = [
        (index, raw)
        for index, raw in raw_rows
        if len(raw) > LEDGER_COL_IN
        and not _is_blank(raw[LEDGER_COL_DETAIL])
        and _text(raw[LEDGER_COL_DETAIL]).lower() != "detail"
        and (
            not _is_blank(raw[LEDGER_COL_OUT]) or not _is_blank(raw[LEDGER_COL_IN])
        )
    ]
    boundary = detect_transposition_boundary(
        (index, raw[LEDGER_COL_DATE]) for index, raw in dated
    )

    rows = []
    for index, raw in dated:
        out = _amount(raw[LEDGER_COL_OUT])
        held = _amount(raw[LEDGER_COL_IN])
        if out is None and held is None:
            continue
        detail = _text(raw[LEDGER_COL_DETAIL])
        date = None
        if not _is_blank(raw[LEDGER_COL_DATE]):
            date = parse_sheet_date(
                raw[LEDGER_COL_DATE],
                transposed=boundary is None or index < boundary,
            )
        rows.append(
            {
                "excel_row": index,
                "sheet": sheet_name,
                "person": person,
                "date": date,
                "detail": detail,
                # Out of the box and into their pocket, or back the other way.
                "direction": "GIVEN" if out is not None else "CLEARED",
                "amount": out if out is not None else held,
                "is_voucher": bool(VOUCHER_PATTERN.search(detail)),
            }
        )

    # The sheet's own closing figure, for the importer to check against.
    stated = None
    for _, raw in reversed(raw_rows):
        if len(raw) > LEDGER_COL_TOTAL and not _is_blank(raw[LEDGER_COL_TOTAL]):
            value = _amount(raw[LEDGER_COL_TOTAL])
            if value is not None or _text(raw[LEDGER_COL_TOTAL]) == "0":
                stated = value or 0.0
                break

    return {"person": person, "rows": rows, "stated_balance": stated}


def person_sheet_names(workbook):
    """The tabs that are somebody's ledger: "<name> in out"."""
    return [
        name
        for name in workbook.sheetnames
        if name.strip().lower().endswith(" in out")
    ]
