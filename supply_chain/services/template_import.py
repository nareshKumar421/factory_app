"""Read the JIVO Supply Chain Reference Template workbook.

The template has a README plus three data sheets, each owned by a different
department:

    1. Lead Times            Procurement (Packaging) + Procurement (Oils / RM)
    2. Machine Capacities    Production / Infrastructure
    3. Material-Machine Map  Production

Two rules from the template's own README drive this parser:

  * only BLUE cells are input, and
  * **grey italic rows are EXAMPLES showing the expected format** — they carry
    real-looking codes (PM-CAP-26, M-01, FG0000030) and must never be ingested as
    data. They are the single most likely way this import corrupts the reference
    set, so they are detected and counted rather than quietly loaded.

Excel parsing prefers ``openpyxl`` and falls back to a standard-library reader
(an ``.xlsx`` is a zip of XML), mirroring ``marketplace.services.amazon_sheet``,
so the import works on a server without the extra dependency. That reader only
handles the first sheet, hence this one.
"""
import io
import re
import zipfile
from decimal import Decimal, InvalidOperation
from xml.etree import ElementTree as ET

from .errors import SupplyChainError

# Sheet name -> canonical key. Matched case/space-insensitively and with the
# template's leading "1. " numbering stripped, so a renamed tab still lands.
SHEET_KEYS = {
    "lead times": "lead_times",
    "machine capacities": "machines",
    "material-machine map": "mappings",
    "material machine map": "mappings",
}

# Canonical column -> the template's header label, matched case/space-insensitively.
LEAD_TIME_COLUMNS = {
    "material_code": "Material Code",
    "material_name": "Material Name",
    "material_type": "Material Type",
    "category": "Category / Spec",
    "supplier_name": "Supplier Name",
    "lead_time_days": "Lead Time (Days)",
    "moq": "MOQ",
    "unit": "Unit",
    "remarks": "Remarks",
}

MACHINE_COLUMNS = {
    "machine_id": "Machine ID",
    "name": "Machine / Line Name",
    "location": "Location",
    "pack_type": "Pack Type Handled",
    "pack_size_range": "Pack Size Range",
    "output_per_hour": "Output (Units/Hour)",
    "shift_hours": "Shift Hours",
    "shifts_per_day": "Shifts/Day",
    "working_days_per_month": "Working Days/Month",
    "changeover_minutes": "Changeover (Min)",
}

MAPPING_COLUMNS = {
    "sku_code": "SKU Code",
    "sku_name": "Finished Good / SKU Name",
    "brand": "Brand",
    "pack_type": "Pack Type",
    "pack_size": "Pack Size",
    "primary_machine_id": "Primary Machine ID",
    "alternate_machine_ids": "Alternate Machine ID(s)",
    "output_on_primary": "Output on Primary (Units/Hr)",
}

# A cell starting "e.g." is the template's own example marker, used in the
# Supplier Name column of every example row.
_EXAMPLE_RE = re.compile(r"^\s*e\.?g\.?\b", re.IGNORECASE)


def _norm(value):
    return (value or "").strip().lower().replace("_", " ").replace("  ", " ")


def _sheet_key(name):
    """Canonical key for a sheet tab, ignoring the template's "1. " numbering."""
    stripped = re.sub(r"^\s*\d+\s*[.)]\s*", "", name or "")
    return SHEET_KEYS.get(_norm(stripped))


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _col_index(ref):
    """``"BC12"`` -> 54 (0-based column). Cells without a ref keep document order."""
    letters = "".join(c for c in ref if c.isalpha())
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch.upper()) - 64)
    return n - 1


def _sheets_stdlib(content):
    """``{sheet name: [[cell, ...], ...]}`` using only the standard library."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile:
        raise SupplyChainError(
            "That file could not be read as an Excel workbook.", code="BAD_WORKBOOK"
        )

    shared = []
    if "xl/sharedStrings.xml" in zf.namelist():
        for si in ET.fromstring(zf.read("xl/sharedStrings.xml")):
            if _local(si.tag) == "si":
                shared.append("".join(t.text or "" for t in si.iter() if _local(t.tag) == "t"))

    # workbook.xml gives name + rId; the rels file maps rId -> the sheet part.
    wb = ET.fromstring(zf.read("xl/workbook.xml"))
    rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
    target_by_id = {
        r.get("Id"): r.get("Target") for r in rels if _local(r.tag) == "Relationship"
    }

    out = {}
    for sheet in wb.iter():
        if _local(sheet.tag) != "sheet":
            continue
        name = sheet.get("name") or ""
        rid = next((v for k, v in sheet.attrib.items() if _local(k) == "id"), None)
        target = target_by_id.get(rid) or ""
        path = target if target.startswith("xl/") else f"xl/{target.lstrip('/')}"
        if path not in zf.namelist():
            continue
        out[name] = _rows_from_sheet_xml(zf.read(path), shared)
    return out


def _rows_from_sheet_xml(blob, shared):
    rows = []
    for row in ET.fromstring(blob).iter():
        if _local(row.tag) != "row":
            continue
        cells = {}
        width = 0
        for idx, cell in enumerate(c for c in row if _local(c.tag) == "c"):
            ref = cell.get("r") or ""
            pos = _col_index(ref) if ref else idx
            text = ""
            for child in cell:
                if _local(child.tag) == "v":
                    text = child.text or ""
                elif _local(child.tag) == "is":
                    text = "".join(t.text or "" for t in child.iter() if _local(t.tag) == "t")
            if cell.get("t") == "s" and text.isdigit():
                i = int(text)
                text = shared[i] if i < len(shared) else ""
            cells[pos] = text.strip()
            width = max(width, pos + 1)
        rows.append([cells.get(i, "") for i in range(width)])
    return rows


def _sheets(content):
    """``{sheet name: rows}``, preferring openpyxl when it is installed."""
    try:
        import openpyxl
    except Exception:  # not installed on this server — stdlib reader
        return _sheets_stdlib(content)

    wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    return {
        ws.title: [["" if c is None else str(c).strip() for c in row]
                   for row in ws.iter_rows(values_only=True)]
        for ws in wb.worksheets
    }


def _header_row(rows, columns):
    """Index of the row that carries the column headers.

    The data sheets open with a title and a how-to line, so the header is not row
    0 and its position differs per sheet. Find it by content instead of guessing.
    """
    wanted = {_norm(label) for label in columns.values()}
    for i, row in enumerate(rows[:15]):
        present = {_norm(c) for c in row if c}
        if len(wanted & present) >= max(2, len(wanted) // 2):
            return i
    return None


def _dec(value, field, code, warnings):
    """Parse a number, warning rather than failing the whole upload."""
    text = (value or "").strip().replace(",", "")
    if not text:
        return Decimal("0")
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        warnings.append(f"{code}: could not read {field} value {value!r} — treated as 0.")
        return Decimal("0")


def _is_example(row_map):
    """True for the template's grey italic example rows.

    Detected by the "e.g." marker the template puts in its example supplier and
    machine cells. Loading these would seed the reference set with fictional
    suppliers and machines that look entirely real.
    """
    return any(_EXAMPLE_RE.match(v or "") for v in row_map.values())


def _rows_as_dicts(rows, columns, key_field, warnings, sheet_label):
    """Yield ``(row_map, is_example)`` for each real data row.

    A row counts as data only when its IDENTITY column is filled. Blank-but-
    formatted rows are not merely cosmetic here: the Machine Capacities sheet
    fills "Effective Monthly Capacity" down as a formula, so twenty empty rows
    carry a cached zero and look populated to any "does this row have any cell?"
    test. Keying off the code column reads the sheet the way a person does.
    """
    header_idx = _header_row(rows, columns)
    if header_idx is None:
        warnings.append(f"{sheet_label}: no recognisable header row — sheet skipped.")
        return
    index = {_norm(h): i for i, h in enumerate(rows[header_idx]) if h}
    col = {key: index.get(_norm(label)) for key, label in columns.items()}
    for raw in rows[header_idx + 1:]:
        row_map = {
            key: (raw[i].strip() if i is not None and i < len(raw) and raw[i] else "")
            for key, i in col.items()
        }
        if not row_map.get(key_field):
            continue
        yield row_map, _is_example(row_map)


def parse_workbook(content):
    """Parse the template into ``{lead_times, machines, mappings, warnings, examples_skipped}``.

    Rows are returned, not written — writing is :func:`import_reference_workbook`
    so the parse can be unit-tested and previewed without touching the database.
    """
    sheets = _sheets(content)
    by_key = {}
    for name, rows in sheets.items():
        key = _sheet_key(name)
        if key:
            by_key[key] = rows

    missing = [k for k in ("lead_times", "machines", "mappings") if k not in by_key]
    if len(missing) == 3:
        raise SupplyChainError(
            "This workbook has none of the three reference sheets "
            "(Lead Times, Machine Capacities, Material-Machine Map).",
            code="NOT_THE_TEMPLATE",
        )

    warnings = [f"Sheet '{k}' is missing from the workbook." for k in missing]
    out = {"lead_times": [], "machines": [], "mappings": [], "examples_skipped": 0}

    for key, columns, key_field, label in (
        ("lead_times", LEAD_TIME_COLUMNS, "material_code", "Lead Times"),
        ("machines", MACHINE_COLUMNS, "machine_id", "Machine Capacities"),
        ("mappings", MAPPING_COLUMNS, "sku_code", "Material-Machine Map"),
    ):
        for row_map, is_example in _rows_as_dicts(
            by_key.get(key, []), columns, key_field, warnings, label
        ):
            if is_example:
                out["examples_skipped"] += 1
                continue
            out[key].append(row_map)

    out["warnings"] = warnings
    return out


def _material_type(text):
    return "RAW" if "raw" in (text or "").lower() or "oil" in (text or "").lower() else "PACKAGING"


def import_reference_workbook(company_code, content, *, filename="", user=None):
    """Parse and upsert the template. Returns the :class:`ReferenceImport` audit row.

    Upsert, not replace: the three sheets are owned by different departments and
    arrive at different times, so loading one must never wipe another's data.
    """
    from django.db import transaction

    from ..models import (
        MachineCapacity,
        MaterialLeadTime,
        MaterialMachineMap,
        ReferenceImport,
    )

    parsed = parse_workbook(content)
    warnings = list(parsed["warnings"])
    counts = {"lead_times": 0, "machines": 0, "mappings": 0}

    with transaction.atomic():
        for row in parsed["lead_times"]:
            code = row.get("material_code", "")
            if not code:
                warnings.append("Lead Times: a row has no Material Code — skipped.")
                continue
            MaterialLeadTime.objects.update_or_create(
                company_code=company_code, material_code=code,
                defaults={
                    "material_name": row.get("material_name", "")[:255],
                    "material_type": _material_type(row.get("material_type")),
                    "category": row.get("category", "")[:120],
                    "supplier_name": row.get("supplier_name", "")[:200],
                    "lead_time_days": int(_dec(row.get("lead_time_days"), "lead time", code, warnings)),
                    "moq": _dec(row.get("moq"), "MOQ", code, warnings),
                    "unit": row.get("unit", "")[:30],
                    "remarks": row.get("remarks", ""),
                    "is_active": True,
                },
            )
            counts["lead_times"] += 1

        for row in parsed["machines"]:
            code = row.get("machine_id", "")
            if not code:
                warnings.append("Machine Capacities: a row has no Machine ID — skipped.")
                continue
            MachineCapacity.objects.update_or_create(
                company_code=company_code, machine_id=code,
                defaults={
                    "name": row.get("name", "")[:150],
                    "location": row.get("location", "")[:120],
                    "pack_type": row.get("pack_type", "")[:80],
                    "pack_size_range": row.get("pack_size_range", "")[:80],
                    "output_per_hour": _dec(row.get("output_per_hour"), "output/hour", code, warnings),
                    "shift_hours": _dec(row.get("shift_hours"), "shift hours", code, warnings),
                    "shifts_per_day": _dec(row.get("shifts_per_day"), "shifts/day", code, warnings),
                    "working_days_per_month": _dec(
                        row.get("working_days_per_month"), "working days", code, warnings),
                    "changeover_minutes": _dec(
                        row.get("changeover_minutes"), "changeover", code, warnings),
                    "is_active": True,
                },
            )
            counts["machines"] += 1

        known_machines = set(
            MachineCapacity.objects.filter(company_code=company_code)
            .values_list("machine_id", flat=True)
        )
        for row in parsed["mappings"]:
            code = row.get("sku_code", "")
            if not code:
                warnings.append("Material-Machine Map: a row has no SKU Code — skipped.")
                continue
            primary = row.get("primary_machine_id", "")
            # The template says machine IDs must match sheet 2. Warn rather than
            # reject: the sheets are owned by different people and may land apart.
            if primary and primary not in known_machines:
                warnings.append(
                    f"{code}: primary machine {primary!r} is not in Machine Capacities."
                )
            MaterialMachineMap.objects.update_or_create(
                company_code=company_code, sku_code=code,
                defaults={
                    "sku_name": row.get("sku_name", "")[:255],
                    "brand": row.get("brand", "")[:80],
                    "pack_type": row.get("pack_type", "")[:80],
                    "pack_size": row.get("pack_size", "")[:80],
                    "primary_machine_id": primary[:50],
                    "alternate_machine_ids": row.get("alternate_machine_ids", "")[:200],
                    "output_on_primary": _dec(
                        row.get("output_on_primary"), "output on primary", code, warnings),
                    "is_active": True,
                },
            )
            counts["mappings"] += 1

        return ReferenceImport.objects.create(
            company_code=company_code,
            filename=filename[:255],
            lead_times_loaded=counts["lead_times"],
            machines_loaded=counts["machines"],
            mappings_loaded=counts["mappings"],
            examples_skipped=parsed["examples_skipped"],
            warnings=warnings,
            imported_by=getattr(user, "email", "") or "",
        )
