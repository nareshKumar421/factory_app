"""Turning a block pasted out of SAP or Excel into movement lines.

The keeper already has the list on screen — an SAP stock or delivery grid, or
the sheet he keeps in Excel — and retyping twenty item codes through a picker to
say what is going out is the kind of chore that stops getting done. This reads
that block instead.

Nothing here writes. It parses, resolves each code against SAP, and answers with
what it found; the keeper reviews it on screen and the normal
``create_movement`` path does the saving. That is deliberate: a paste that wrote
straight through would be a second way into the register, with its own copy of
the manager check and the line validation to drift out of step.

How the block is read, and why each choice:

* **Tabs are the column break.** Copying a selection out of Excel or an SAP grid
  puts tab-separated lines on the clipboard, so that is the format this expects.
  Runs of spaces are deliberately NOT treated as breaks — item names are full of
  spaces ("COLD PRESS 5 LTR + COLD PRESS 1 LTR 4 PCS") and splitting on them
  would quietly cut one name into eight columns. Commas are accepted only when
  the block contains no tabs at all, which covers a paste out of a .csv.
* **The header is searched for, not assumed.** These grids grow a title row or a
  filter row above the headers, and a parser that insists on row 1 breaks the
  first time somebody tidies the sheet. Aliases are matched loosely because SAP
  says "Item No." where Excel says "Item Code" and the floor says "SAP Code".
* **No recognisable header falls back to position**, taking the first cell that
  looks like an item code and the last numeric cell as the quantity. That is
  what makes an arbitrary SAP grid — item, description, batch, warehouse,
  quantity — readable without configuring anything.
* **Duplicate codes within one paste are summed**, and the fact is reported. Two
  rows for one item in a copied document normally means two batches of the same
  thing, unlike the manual form where a duplicate is a double-entry mistake and
  is refused. Summing silently would be the wrong half of that trade, so the
  caller is told which codes were combined.
* **A row that cannot be resolved is reported, not dropped and not guessed.**
  Every rejected row comes back with its line number and a reason, so the keeper
  can see what to fix rather than wondering why 18 lines became 15.

The quantity's unit is the caller's to state. Pasting a box count into a
register that stores pieces is the one mistake here that would be invisible
afterwards — a ~20x error that still looks like a plausible number — so the
screen asks, and ``unit="BOX"`` multiplies by each item's own ``SalFactor2``
rather than by a guess.
"""

from __future__ import annotations

import logging
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple

from rest_framework.exceptions import ValidationError

from .pf_movement_service import FG_ITEM_GROUP_CODE, default_source_warehouse
from .wms_hana_reader import WMSHanaReader

logger = logging.getLogger(__name__)

# How many rows one paste may carry. Generous — a month's sheet is a few
# hundred — but bounded, so a mis-copied whole workbook is refused rather than
# turned into a HANA statement with fifty thousand parameters.
MAX_ROWS = 2000

# Canonical field -> header text to look for. Matched on a squashed,
# punctuation-stripped label, substring allowed, because the same column is
# "Item No." in SAP, "Item Code" in one sheet and "RM SAP Code" in another.
HEADER_PATTERNS: Dict[str, List[str]] = {
    "item_code": [
        "item no", "item code", "itemcode", "sap code", "item number",
        "material code", "material no", "product code", "code",
    ],
    "item_name": [
        "item description", "item name", "description", "material description",
        "product name", "sku",
    ],
    # Deliberately excludes "in stock" / "on hand": a stock column is what SAP
    # *has*, not what the keeper is sending, and reading it as the quantity
    # would file the entire floor balance as an outward movement. When a grid
    # offers only a stock column, the positional fallback picks the last numeric
    # cell and the keeper sees the figure on the preview before it is used.
    "qty": [
        "quantity", "qty", "pieces", "pcs", "nos", "no of pieces", "piece",
        "boxes", "box", "cases", "case", "cartons", "carton",
    ],
}

# An item code as SAP writes it: two or more letters then digits — FG0000032,
# PM0000121, RM0000002. Used only by the positional fallback, to find which
# cell is the code when no header said so.
ITEM_CODE_SHAPE = re.compile(r"^[A-Z]{2,}[0-9]{3,}$")


class PasteError(Exception):
    """The block cannot be read at all — not a row-level problem."""


# ---------------------------------------------------------------------------
# Cell helpers
# ---------------------------------------------------------------------------

def _squash(value: Any) -> str:
    text = "" if value is None else str(value)
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", text.strip().lower()).split())


# A cell that IS a quantity: an optional sign, digits with optional thousands
# separators and decimals, and at most a short trailing unit — "240", "1,200",
# "240 PCS", "12.5", "(5)".
#
# Matched against the WHOLE cell on purpose. The forgiving version of this
# function stripped every non-digit and read whatever was left, which turned
# "COLD PRESS 1 LTR 20 PCS" into 1204 and "L3 004192" into 3004192 — so an item
# name or a batch number in the last column could be picked as the quantity, and
# the result would still look like a plausible figure.
QUANTITY_SHAPE = re.compile(
    r"""^\(?\s*                # Excel writes a negative as (5)
        (?P<sign>[+-])?\s*
        (?P<number>\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?|\.\d+)
        \s*(?:[A-Za-z%]{1,6}\.?)?   # a trailing unit: PCS, KG, LTR, NOS
        \s*\)?$""",
    re.VERBOSE,
)


def _to_decimal(value: Any) -> Optional[Decimal]:
    """A quantity cell, however the source wrote it, or None if it is not one.

    Handles the thousands separators and trailing units SAP grids carry
    ("1,200", "240 PCS") and the parenthesised negatives Excel produces. None
    for anything that is not simply a number — see ``QUANTITY_SHAPE``.
    """
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return Decimal(str(value))

    text = str(value).strip()
    if not text:
        return None
    match = QUANTITY_SHAPE.match(text)
    if not match:
        return None
    try:
        number = Decimal(match.group("number").replace(",", ""))
    except (InvalidOperation, ValueError):
        return None
    negative = match.group("sign") == "-" or (
        text.startswith("(") and text.endswith(")")
    )
    return -number if negative else number


def _rows_from_text(text: str) -> List[List[str]]:
    """Split the pasted block into cells. See the module docstring for the rules."""
    raw = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [line for line in raw.split("\n")]
    if not any(line.strip() for line in lines):
        raise PasteError("Nothing was pasted.")

    if any("\t" in line for line in lines):
        return [line.split("\t") for line in lines]
    if any("," in line for line in lines):
        # A .csv paste. Split naively rather than with the csv module: a quoted
        # item name containing a comma is rare, and a wrong split shows up on
        # the preview as an unresolved row rather than as a wrong quantity.
        return [line.split(",") for line in lines]
    raise PasteError(
        "That paste has no columns in it — every line needs at least an item "
        "code and a quantity. Select the cells across all the columns in SAP or "
        "Excel and copy, rather than copying one column at a time."
    )


# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------

def _find_header(rows: List[List[str]]) -> Tuple[int, Dict[int, str]]:
    """Locate the header row and map columns to fields, or return (-1, {}).

    Only accepted when the row names both an item code and a quantity: a row
    that names one of them is as likely to be a data row that happens to say
    "code" somewhere.
    """
    for index, row in enumerate(rows[:20]):
        mapping: Dict[int, str] = {}
        for position, cell in enumerate(row):
            label = _squash(cell)
            if not label:
                continue
            for field, patterns in HEADER_PATTERNS.items():
                if field in mapping.values():
                    # First match wins for each field. A grid with "Quantity"
                    # and "Open Quantity" must not silently switch columns.
                    continue
                if any(pattern in label for pattern in patterns):
                    mapping[position] = field
                    break
        values = set(mapping.values())
        if "item_code" in values and "qty" in values:
            return index, mapping
    return -1, {}


def _positional(row: List[str]) -> Tuple[Optional[str], Optional[Decimal], str]:
    """Item code, quantity and name from a row with no header to go by.

    The first cell shaped like a SAP item code is the code; the last cell that
    parses as a number is the quantity. That reads an arbitrary grid — item,
    description, batch, warehouse, quantity — without being told its layout.
    """
    code = None
    code_at = -1
    for position, cell in enumerate(row):
        candidate = (cell or "").strip().upper()
        if ITEM_CODE_SHAPE.match(candidate):
            code, code_at = candidate, position
            break

    qty = None
    for position in range(len(row) - 1, -1, -1):
        if position == code_at:
            continue
        number = _to_decimal(row[position])
        if number is not None:
            qty = number
            break

    # The longest remaining text cell is the best guess at the description, and
    # it is only ever a fallback label — the name is overwritten from SAP once
    # the code resolves.
    name = ""
    for position, cell in enumerate(row):
        if position == code_at:
            continue
        candidate = (cell or "").strip()
        if len(candidate) > len(name) and _to_decimal(candidate) is None:
            name = candidate
    return code, qty, name[:200]


# ---------------------------------------------------------------------------
# Parse
# ---------------------------------------------------------------------------

def parse_block(text: str) -> Dict[str, Any]:
    """Read the pasted block into per-item quantities. Touches nothing."""
    rows = _rows_from_text(text)
    if len(rows) > MAX_ROWS:
        raise PasteError(
            f"That paste has {len(rows):,} rows in it, more than the "
            f"{MAX_ROWS:,} this page will read at once. Paste it in parts."
        )

    header_at, mapping = _find_header(rows)
    body = rows[header_at + 1:] if header_at >= 0 else rows

    def cell(row: List[str], field: str) -> Optional[str]:
        for position, mapped in mapping.items():
            if mapped == field and position < len(row):
                return row[position]
        return None

    # Ordered so the preview lists items in the order they were pasted.
    found: Dict[str, Dict[str, Any]] = {}
    skipped: List[Dict[str, Any]] = []
    combined: List[str] = []

    for offset, row in enumerate(body):
        # +1 for the header itself, +1 to count from one like the source does.
        line_no = (header_at + 1 if header_at >= 0 else 0) + offset + 1

        if not any((c or "").strip() for c in row):
            continue  # a blank line between blocks, not a problem to report

        if mapping:
            code = ((cell(row, "item_code") or "").strip().upper()) or None
            qty = _to_decimal(cell(row, "qty"))
            name = (cell(row, "item_name") or "").strip()[:200]
        else:
            code, qty, name = _positional(row)

        # A row carrying neither a code nor a number is structure, not data —
        # the report's title, the header itself when it was not recognised, a
        # note the keeper typed between blocks. Reporting those would put two or
        # three "problems" on every SAP paste and teach him to skip the warnings
        # that matter. A row with one of the two IS reported: it might have been
        # a line.
        if not code and qty is None:
            continue

        if not code:
            skipped.append({
                "line": line_no,
                "text": _preview(row),
                "reason": f"A quantity of {qty} with no item code beside it.",
            })
            continue
        if qty is None:
            skipped.append({
                "line": line_no,
                "text": _preview(row),
                "reason": f"No quantity found for {code}.",
            })
            continue
        if qty <= 0:
            skipped.append({
                "line": line_no,
                "text": _preview(row),
                "reason": (
                    f"{code} has a quantity of {qty} — nothing to send."
                    if qty == 0
                    else f"{code} has a negative quantity ({qty})."
                ),
            })
            continue

        existing = found.get(code)
        if existing is None:
            found[code] = {
                "item_code": code,
                "pasted_name": name,
                "qty": qty,
                "lines": [line_no],
            }
        else:
            # Two rows for one item — normally two batches of the same thing.
            existing["qty"] += qty
            existing["lines"].append(line_no)
            if code not in combined:
                combined.append(code)

    return {
        "header_row": None if header_at < 0 else header_at + 1,
        "columns_read": (
            {str(pos): field for pos, field in sorted(mapping.items())}
            if mapping
            else {}
        ),
        "items": list(found.values()),
        "skipped": skipped,
        "combined_codes": combined,
    }


def _preview(row: List[str]) -> str:
    """The row as the keeper would recognise it, short enough for one line."""
    text = " | ".join((c or "").strip() for c in row if (c or "").strip())
    return text[:120]


# ---------------------------------------------------------------------------
# Resolve against SAP
# ---------------------------------------------------------------------------

def resolve_block(
    *,
    company_code: str,
    text: str,
    unit: str = "PCS",
    warehouse_code: Optional[str] = None,
) -> Dict[str, Any]:
    """Parse the block and resolve every code against SAP's finished goods.

    Returns the lines that are ready to add, and — separately, never merged into
    them — the rows that need the keeper's attention. Writes nothing and reserves
    nothing.
    """
    unit = (unit or "PCS").strip().upper()
    if unit not in {"PCS", "BOX"}:
        raise ValidationError(
            {"unit": "The pasted quantity is either PCS or BOX."}
        )

    parsed = parse_block(text)
    warehouse_code = (warehouse_code or "").strip().upper() or default_source_warehouse()

    codes = [item["item_code"] for item in parsed["items"]]
    resolved: Dict[str, Dict] = {}
    lookup_error = ""
    if codes:
        try:
            resolved = WMSHanaReader(company_code=company_code).fetch_items_by_code(
                item_codes=codes,
                item_group_code=FG_ITEM_GROUP_CODE,
                warehouse_code=warehouse_code,
            )
        except Exception as exc:  # noqa: BLE001 — reported, not raised
            # A paste is worth salvaging when HANA is down: the codes and
            # quantities are the keeper's own and still usable. The rows come
            # back unresolved with the reason, rather than the whole paste
            # failing.
            logger.warning("Paste resolve failed for %s: %s", company_code, exc)
            lookup_error = str(exc)

    lines: List[Dict[str, Any]] = []
    unresolved: List[Dict[str, Any]] = []

    for item in parsed["items"]:
        code = item["item_code"]
        sap = resolved.get(code)
        if sap is None:
            unresolved.append({
                "item_code": code,
                "qty": str(item["qty"]),
                "lines": item["lines"],
                "reason": (
                    f"SAP could not be reached to check {code}."
                    if lookup_error
                    else f"{code} is not a finished-goods item in this company."
                ),
            })
            continue

        pieces_per_box = sap.get("pieces_per_box")
        if unit == "BOX":
            if not pieces_per_box:
                # Refused rather than treated as one piece per box: assuming a
                # pack size is how a box count becomes a piece count that is
                # wrong by a factor nobody can spot later.
                unresolved.append({
                    "item_code": code,
                    "qty": str(item["qty"]),
                    "lines": item["lines"],
                    "reason": (
                        f"{code} has no pack size in SAP, so a box count cannot "
                        "be turned into pieces. Paste this one in pieces."
                    ),
                })
                continue
            pieces = item["qty"] * pieces_per_box
        else:
            pieces = item["qty"]

        # Pieces are whole — SAP counts them in PCS and there is no half bottle.
        # A fractional paste is reported rather than rounded, because rounding
        # would silently disagree with the sheet it came from.
        if pieces != pieces.to_integral_value():
            unresolved.append({
                "item_code": code,
                "qty": str(item["qty"]),
                "lines": item["lines"],
                "reason": (
                    f"{code} works out to {pieces} pieces, which is not a whole "
                    "number."
                ),
            })
            continue

        lines.append({
            "item_code": code,
            "item_name": sap.get("item_name") or item["pasted_name"],
            "uom": sap.get("uom") or "",
            "pieces": int(pieces),
            "pieces_per_box": pieces_per_box,
            "litres_per_piece": sap.get("litres_per_piece"),
            "sap_on_hand": sap.get("sap_on_hand"),
            # Kept so the preview can say "rows 4 and 11 were added together".
            "source_lines": item["lines"],
            "pasted_qty": str(item["qty"]),
            "inactive_in_sap": not sap.get("is_active", True),
        })

    return {
        "unit": unit,
        "warehouse_code": warehouse_code,
        "header_row": parsed["header_row"],
        "columns_read": parsed["columns_read"],
        "lines": lines,
        "skipped": parsed["skipped"],
        "unresolved": unresolved,
        "combined_codes": parsed["combined_codes"],
        "total_pieces": sum(line["pieces"] for line in lines),
        "lookup_error": lookup_error,
    }
