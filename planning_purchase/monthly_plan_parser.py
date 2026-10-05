"""Parse the planning team's monthly plan workbook into MonthlyPlanRow fields.

EXIM's parser (planning.services.parser), carried across unchanged but for
the model it fills.

The sheet is laid out as:

    row 0   banner  - "PRODUCTION PLANING MONTH OF SEP 2026", plus the group
                      labels COMMODITY and PREMIUM sitting over their column blocks
    row 1   totals  - pre-computed grand totals (ignored; we roll our own up)
    row 2   header  - CODE | BRAND | HEAD | ... | SEP MONTHLY PLANNING | SEP 1ST WEEK | ...
    row 3+  data    - one row per SKU

Columns are located by header text and by the COMMODITY/PREMIUM banner rather than
by fixed index, so the sheet can gain a column or change month without breaking.
Monthly and total figures are recomputed from the weekly inputs; anything the
workbook disagrees with beyond a tolerance is reported, never silently imported.
"""

import re
from datetime import date
from decimal import Decimal, InvalidOperation

MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}

# header text -> model field, for the plain descriptive columns
BASE_COLUMNS = {
    "CODE": "code",
    "BRAND": "brand",
    "HEAD": "head",
    "CATEGORY": "category",
    "SUB-CATEGORY": "sub_category",
    "SKU": "sku",
    "PER LTRS": "per_ltrs",
    "LTRS/BOX": "ltrs_per_box",
    "CASE PACK": "case_pack",
}
TEXT_FIELDS = {"code", "brand", "head", "category", "sub_category", "sku"}

# within a COMMODITY/PREMIUM block, header fragment -> field suffix
WEEK_MARKERS = [("1ST", "w1"), ("2ND", "w2"), ("3RD", "w3"), ("4TH", "w4")]

BLOCKS = {"COMMODITY": "commodity", "PREMIUM": "premium"}


class PlanningParseError(Exception):
    """The workbook is not laid out the way this parser expects."""


def norm(value):
    """Collapse whitespace and upper-case a header cell for matching."""
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip().upper()


def clean_text(value):
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def to_decimal(value, field, source_row, warnings):
    if value is None or value == "":
        return Decimal("0")
    if isinstance(value, (int, float, Decimal)):
        return Decimal(str(value))
    text = str(value).strip().replace(",", "")
    if not text:
        return Decimal("0")
    try:
        return Decimal(text)
    except InvalidOperation:
        warnings.append(f"row {source_row}: {field} is not a number ({value!r}) - treated as 0")
        return Decimal("0")


def find_header_row(rows, limit=15):
    """The header row is the one whose first cell is CODE."""
    for index, row in enumerate(rows[:limit]):
        if row and norm(row[0]) == "CODE":
            return index
    raise PlanningParseError(
        "Could not find the header row - no row in the first "
        f"{limit} starts with a CODE column."
    )


def find_block_columns(rows, header_index, headers):
    """Map COMMODITY/PREMIUM banner labels onto their five columns each.

    Scans the rows above the header for the group labels, then reads the header
    text underneath to tell the monthly column from the four weekly ones.
    """
    found = {}
    for row in rows[:header_index]:
        if not row:
            continue
        spans = {}
        for col, cell in enumerate(row):
            label = norm(cell)
            if label in BLOCKS:
                spans.setdefault(label, []).append(col)
        for label, cols in spans.items():
            found.setdefault(label, cols)

    missing = [label for label in BLOCKS if label not in found]
    if missing:
        raise PlanningParseError(
            "Missing the {} banner above the planning columns. "
            "Each block of five planning columns must be labelled.".format(", ".join(missing))
        )

    mapping = {}
    for label, prefix in BLOCKS.items():
        for col in found[label]:
            head = norm(headers[col]) if col < len(headers) else ""
            suffix = next((s for marker, s in WEEK_MARKERS if marker in head), None)
            if suffix is None and "MONTHLY" in head:
                suffix = "monthly"
            if suffix:
                mapping.setdefault(prefix + "_" + suffix, col)

    for prefix in BLOCKS.values():
        for suffix in ("monthly", "w1", "w2", "w3", "w4"):
            if prefix + "_" + suffix not in mapping:
                raise PlanningParseError(
                    "The {} block is missing its {} column.".format(prefix.upper(), suffix)
                )
    return mapping


def find_named_column(headers, *fragments):
    for col, cell in enumerate(headers):
        head = norm(cell)
        if head and all(fragment in head for fragment in fragments):
            return col
    return None


def parse_month(rows, header_index, headers):
    """Read the planned month from the banner, falling back to the column headers."""
    candidates = []
    for row in rows[:header_index]:
        candidates.extend(str(cell) for cell in (row or []) if cell)
    candidates.extend(str(cell) for cell in headers if cell)

    pattern = r"\b(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)[A-Z]*\b\s*[-,]?\s*(\d{4})\b"
    for text in candidates:
        match = re.search(pattern, norm(text))
        if match:
            return date(int(match.group(2)), MONTHS[match.group(1)], 1)
    return None


def parse_planning_workbook(file_or_path, sheet_name=None, tolerance=Decimal("0.5")):
    """Parse a planning workbook.

    Returns (month, title, rows, warnings, mismatches) where `rows` is a list of
    kwargs dicts ready for MonthlyPlanRow(**kwargs).
    """
    import openpyxl

    workbook = openpyxl.load_workbook(file_or_path, data_only=True, read_only=True)
    sheet = workbook[sheet_name] if sheet_name else workbook.worksheets[0]
    rows = list(sheet.iter_rows(values_only=True))
    if not rows:
        raise PlanningParseError("The workbook is empty.")

    header_index = find_header_row(rows)
    headers = rows[header_index]

    base = {}
    for label, field in BASE_COLUMNS.items():
        col = next((i for i, cell in enumerate(headers) if norm(cell) == label), None)
        if col is None:
            raise PlanningParseError("Missing the {} column.".format(label))
        base[field] = col

    blocks = find_block_columns(rows, header_index, headers)
    ecom_col = find_named_column(headers, "ECOM")
    total_col = find_named_column(headers, "TOTAL")
    if ecom_col is None:
        raise PlanningParseError("Missing the Ecom Planning column.")

    title = ""
    for row in rows[:header_index]:
        for cell in row or []:
            text = clean_text(cell)
            if "PLAN" in norm(text) and len(text) > len(title):
                title = text
    month = parse_month(rows, header_index, headers)

    parsed, warnings, mismatches = [], [], []
    seen = {}

    for offset, row in enumerate(rows[header_index + 1:], start=header_index + 2):
        if not row or not any(cell not in (None, "") for cell in row):
            continue
        code = clean_text(row[base["code"]] if base["code"] < len(row) else None)
        if not code:
            continue  # totals strip or spacer, not a SKU line

        if code in seen:
            warnings.append(
                "row {}: code {} repeats row {} - both kept".format(offset, code, seen[code])
            )
        seen.setdefault(code, offset)

        def cell(col):
            return row[col] if col is not None and col < len(row) else None

        fields = {"source_row": offset}
        for field, col in base.items():
            raw = cell(col)
            if field in TEXT_FIELDS:
                fields[field] = clean_text(raw)
            else:
                fields[field] = to_decimal(raw, field, offset, warnings)

        for field, col in blocks.items():
            fields[field] = to_decimal(cell(col), field, offset, warnings)
        fields["ecom_planning"] = to_decimal(cell(ecom_col), "ecom_planning", offset, warnings)

        sheet_monthly = {
            "commodity": fields["commodity_monthly"],
            "premium": fields["premium_monthly"],
        }
        sheet_total = None
        if total_col is not None:
            sheet_total = to_decimal(cell(total_col), "total_planning", offset, warnings)

        # recompute each monthly from its four weeks, and the total from the three parts
        for prefix in BLOCKS.values():
            computed = sum(
                (fields[prefix + "_" + s] for s in ("w1", "w2", "w3", "w4")), Decimal("0")
            )
            if abs(computed - sheet_monthly[prefix]) > tolerance:
                mismatches.append(
                    "row {} ({}) {} monthly: sheet {:,} vs weeks {:,}".format(
                        offset, code, prefix, sheet_monthly[prefix], computed
                    )
                )
            fields[prefix + "_monthly"] = computed

        computed_total = (
            fields["commodity_monthly"] + fields["premium_monthly"] + fields["ecom_planning"]
        )
        if sheet_total is not None and abs(computed_total - sheet_total) > tolerance:
            mismatches.append(
                "row {} ({}) total: sheet {:,} vs computed {:,}".format(
                    offset, code, sheet_total, computed_total
                )
            )
        fields["total_planning"] = computed_total

        if fields["commodity_monthly"] and fields["premium_monthly"]:
            warnings.append(
                "row {} ({}): planned in both COMMODITY and PREMIUM blocks".format(offset, code)
            )

        parsed.append(fields)

    if not parsed:
        raise PlanningParseError("No SKU rows found below the header.")

    return month, title, parsed, warnings, mismatches
