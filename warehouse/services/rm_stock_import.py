"""Reading the warehouse's own issue sheet into the raw-material register.

The store already keeps a shift-wise sheet — date, shift, RM code, SKU,
a requirement column and two "Issued to Production" columns filled by two
different people — and retyping seven oils through a dialog every morning is
the kind of chore that stops getting done. This reads that file instead.

**What becomes the registered quantity.** The *issued* figure, summed across
every shift for the item. Worth being clear-eyed about, because the register's
own label says something else: it holds "the quantity your store is holding",
and issued-to-production is what *left* the store. That mapping was chosen
deliberately, so every row imported here carries a remark saying where its
figure came from — a register entry nobody can trace back is a number nobody
can defend later.

**The two issuer columns are a double-entry check, not two issues.** Where both
are filled they agree (16,000 / 16,000; 8,500 / 8,500), so they are two people
counting the same thing. They are never added — that would double every
quantity. Both are kept in the audit remark, and where they disagree the row is
reported rather than quietly resolved; the caller decides whether to go ahead.

**A file or a paste, read the same way.** Selecting a block in Excel and
copying it puts the same grid on the clipboard as tab-separated text, and for
the four or five lines of one shift that is faster than saving a file and
finding it again. Both routes end up in :func:`_parse_rows`, so the two cannot
drift into reading a sheet differently.

**Nothing is written during a preview.** The page parses first, shows what it
found, and only writes when the keeper confirms — so a wrong file is a wrong
screen rather than a wrong register.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

ZERO = Decimal("0")

# Canonical field -> the header text the sheet uses. Matched loosely (case,
# spacing and punctuation ignored, substring allowed) because these headers
# carry people's names — "Issued to Production ( Vicky Veer ji )" — and those
# change when the people do.
HEADER_PATTERNS = {
    "date": ["date"],
    "shift": ["shift"],
    "item_code": ["rm sap code", "sap code", "item code", "rm code"],
    "item_name": ["sku", "item name", "material"],
    "requirement": ["requirement"],
    "issued": ["issued to production", "issued"],
}

# Fallback when no header row is recognisable: the layout as it stands today.
POSITIONAL = {
    0: "date", 1: "shift", 2: "item_code", 3: "item_name",
    4: "requirement", 5: "issued", 6: "issued",
}

MAX_ROWS = 20000


class SheetError(Exception):
    """The file cannot be read at all — not a row-level problem."""


def _norm(value) -> str:
    text = "" if value is None else str(value)
    return re.sub(r"[^a-z0-9 ]+", " ", text.strip().lower())
    # (collapsing runs of spaces is done by the caller's split/join)


def _squash(value) -> str:
    return " ".join(_norm(value).split())


def _to_decimal(value) -> Optional[Decimal]:
    """A quantity cell, whether Excel stored it as a number or as text.

    Returns None for an empty cell, which is different from zero: a blank
    issued column means that person did not fill it in, and a zero means they
    said none went out.
    """
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


def _to_date(value) -> Optional[date]:
    """`08.09.2026` is day-first, and Excel may hand it over as a real date."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%d.%m.%Y", "%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d.%m.%y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _load_rows(file_obj) -> List[List[Any]]:
    try:
        from openpyxl import load_workbook
    except ImportError as exc:  # pragma: no cover — openpyxl ships with the app
        raise SheetError("Reading .xlsx files needs openpyxl on the server.") from exc

    try:
        workbook = load_workbook(file_obj, data_only=True, read_only=True)
    except Exception as exc:  # noqa: BLE001 — any parse failure is the same to the caller
        raise SheetError(f"That file could not be opened as a spreadsheet: {exc}") from exc

    sheet = workbook.active
    rows = []
    for index, row in enumerate(sheet.iter_rows(values_only=True)):
        if index >= MAX_ROWS:
            break
        rows.append(list(row))
    workbook.close()
    return rows


def _find_header(rows: List[List[Any]]) -> tuple:
    """Locate the header row and map each column to a canonical field.

    Searched rather than assumed to be row 1: these sheets grow a title row, a
    frozen filter row or a blank line above the headers over time, and a parser
    that insists on row 1 breaks the first time someone tidies the file.
    """
    for index, row in enumerate(rows[:20]):
        mapping: Dict[int, str] = {}
        for position, cell in enumerate(row):
            label = _squash(cell)
            if not label:
                continue
            for field, patterns in HEADER_PATTERNS.items():
                if any(pattern in label for pattern in patterns):
                    # "Issued to Production" appears twice, by design — the two
                    # people. Both are kept, in the order they appear.
                    mapping[position] = field
                    break
        if "item_code" in mapping.values() and "issued" in mapping.values():
            return index, mapping
    return -1, dict(POSITIONAL)


def _rows_from_text(text: str) -> List[List[Any]]:
    """A block copied out of Excel, which arrives as tab-separated lines.

    Only tabs are treated as column breaks. Splitting on runs of spaces as well
    would look more forgiving and would quietly cut "CANOLA COLD PRESS LOOSE
    OIL OLD" into five columns, so a paste with no tabs in it is refused with an
    explanation instead.
    """
    lines = [line for line in (text or "").replace("\r\n", "\n").split("\n")]
    if not any("\t" in line for line in lines):
        raise SheetError(
            "That paste has no columns in it. Select the cells across all the "
            "columns in Excel and copy, rather than copying one column at a time."
        )
    return [line.split("\t") for line in lines]


def parse_pasted(text: str) -> Dict[str, Any]:
    """Read rows copied straight out of Excel. Writes nothing."""
    rows = _rows_from_text(text)
    if not rows:
        raise SheetError("Nothing was pasted.")
    return _parse_rows(rows)


def parse_sheet(file_obj) -> Dict[str, Any]:
    """Read an uploaded .xlsx into per-item totals. Writes nothing."""
    rows = _load_rows(file_obj)
    if not rows:
        raise SheetError("That spreadsheet has no rows in it.")
    return _parse_rows(rows)


def _parse_rows(rows: List[List[Any]]) -> Dict[str, Any]:
    """The shared reading, whichever way the grid arrived.

    Every row that carries an item code and an issued figure is kept, and the
    totals are summed per item across shifts. Rows the parser could not use are
    returned as `skipped` with the reason, rather than dropped — a file that
    quietly imports four of its seven lines is worse than one that refuses.
    """

    header_index, mapping = _find_header(rows)
    issued_columns = [pos for pos, field in mapping.items() if field == "issued"]
    if not issued_columns:
        raise SheetError(
            "No 'Issued to Production' column was found. The sheet needs the "
            "RM SAP Code and at least one issued column."
        )

    def cell(row, field):
        for position, name in mapping.items():
            if name == field and position < len(row):
                return row[position]
        return None

    items: Dict[str, Dict[str, Any]] = {}
    skipped: List[Dict[str, Any]] = []
    mismatches: List[Dict[str, Any]] = []
    parsed_rows: List[Dict[str, Any]] = []

    for index, row in enumerate(rows):
        if index <= header_index:
            continue
        if not any(c is not None and str(c).strip() for c in row):
            continue

        excel_row = index + 1
        code = (str(cell(row, "item_code") or "")).strip().upper()
        if not code:
            continue  # a spacer or a running-total line, not a failure

        issued_values = [
            _to_decimal(row[pos]) if pos < len(row) else None for pos in issued_columns
        ]
        present = [v for v in issued_values if v is not None]

        if not present:
            skipped.append({
                "row": excel_row, "item_code": code,
                "reason": "No issued quantity on this row.",
            })
            continue
        if any(v < ZERO for v in present):
            skipped.append({
                "row": excel_row, "item_code": code,
                "reason": "A negative issued quantity cannot be registered.",
            })
            continue

        # Two people recording the same issue — never added together.
        agreed = present[-1]
        disagreed = len(present) > 1 and len(set(present)) > 1
        if disagreed:
            mismatches.append({
                "row": excel_row,
                "item_code": code,
                "values": [str(v) for v in present],
                "using": str(agreed),
            })

        row_date = _to_date(cell(row, "date"))
        shift = (str(cell(row, "shift") or "")).strip()
        name = (str(cell(row, "item_name") or "")).strip()
        requirement = _to_decimal(cell(row, "requirement"))

        entry = items.setdefault(code, {
            "item_code": code, "item_name": name, "qty": ZERO,
            "requirement": ZERO, "as_of_date": row_date, "shifts": [],
            "rows": [], "has_mismatch": False,
        })
        # Summed across shifts: the day's issue for an item is all of its lines.
        entry["qty"] += agreed
        if requirement is not None:
            entry["requirement"] += requirement
        if name and not entry["item_name"]:
            entry["item_name"] = name
        if shift and shift not in entry["shifts"]:
            entry["shifts"].append(shift)
        # The figure is only as current as its most recent line.
        if row_date and (entry["as_of_date"] is None or row_date > entry["as_of_date"]):
            entry["as_of_date"] = row_date
        entry["rows"].append(excel_row)
        entry["has_mismatch"] = entry["has_mismatch"] or disagreed

        parsed_rows.append({
            "row": excel_row, "date": row_date.isoformat() if row_date else None,
            "shift": shift, "item_code": code, "item_name": name,
            "requirement": str(requirement) if requirement is not None else None,
            "issued": [str(v) if v is not None else None for v in issued_values],
            "using": str(agreed), "mismatch": disagreed,
        })

    if not items:
        raise SheetError(
            "No usable rows were found. Each row needs an RM SAP Code and an "
            "issued quantity."
        )

    return {
        "header_row": header_index + 1 if header_index >= 0 else None,
        "issuer_columns": len(issued_columns),
        "rows": parsed_rows,
        "items": [
            {
                "item_code": e["item_code"],
                "item_name": e["item_name"],
                "qty": str(e["qty"]),
                "requirement": str(e["requirement"]),
                "as_of_date": e["as_of_date"].isoformat() if e["as_of_date"] else None,
                "shifts": e["shifts"],
                "row_count": len(e["rows"]),
                "rows": e["rows"],
                "has_mismatch": e["has_mismatch"],
            }
            for e in sorted(items.values(), key=lambda x: x["item_code"])
        ],
        "skipped": skipped,
        "mismatches": mismatches,
    }


def apply_sheet(
    *,
    user,
    company,
    parsed: Dict[str, Any],
    source_name: str = "",
) -> Dict[str, Any]:
    """Write the parsed totals into the register, one `set_quantity` each.

    Goes through the service rather than the model so the per-warehouse manager
    check and the history entry happen exactly as they do for a typed figure —
    an imported quantity is not a privileged one.
    """
    from . import rm_stock_service

    written, failed = [], []
    for item in parsed["items"]:
        remark = _remark(item, parsed, source_name)
        # The parsed payload carries dates as ISO strings so it can cross the
        # wire for the preview; the service wants a real date to compare.
        as_of = _to_date(item["as_of_date"])
        try:
            row = rm_stock_service.set_quantity(
                user=user,
                company=company,
                warehouse_code=rm_stock_service.register_warehouse(),
                item_code=item["item_code"],
                item_name=item["item_name"],
                qty=Decimal(item["qty"]),
                as_of_date=as_of,
                remarks=remark,
            )
            written.append({
                "item_code": row.item_code,
                "qty": str(row.qty),
                "as_of_date": row.as_of_date.isoformat() if row.as_of_date else None,
            })
        except Exception as exc:  # noqa: BLE001 — one bad line must not lose the rest
            logger.warning("RM sheet import: %s failed — %s", item["item_code"], exc)
            failed.append({"item_code": item["item_code"], "reason": str(exc)})

    return {"written": written, "failed": failed}


def _remark(item: Dict[str, Any], parsed: Dict[str, Any], source_name: str) -> str:
    """Where this figure came from, in words, on the permanent record."""
    parts = [
        "Imported from the production issue sheet"
        + (f" ({source_name})" if source_name else "")
        + "."
    ]
    if item["shifts"]:
        parts.append("Shifts: " + ", ".join(item["shifts"]) + ".")
    parts.append(
        f"Issued to production, summed over {item['row_count']} line"
        f"{'s' if item['row_count'] != 1 else ''}."
    )
    detail = [
        r for r in parsed["rows"]
        if r["item_code"] == item["item_code"]
    ]
    readings = [
        f"{r['shift'] or 'row ' + str(r['row'])}: "
        + " / ".join(v for v in r["issued"] if v is not None)
        for r in detail
    ]
    if readings:
        parts.append("Both issuers — " + "; ".join(readings) + ".")
    if item["has_mismatch"]:
        parts.append("The two issuers disagreed on at least one line.")
    return " ".join(parts)
