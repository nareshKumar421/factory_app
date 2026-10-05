"""
dispatch_plans/freight_benchmark_import.py

Reads the dispatch desk's "UPDATED TRANSPORT FARE" workbook into the freight
benchmark table, keeping the benchmark and leaving the transporters out.

How the workbook is laid out, as of the 2026-09 copy:

  - Each sheet holds one or more BLOCKS: an optional heading row (a single
    filled cell, e.g. "UP" or "BHARGAVE"), then a header row that has a
    "Destination" column, then one row per destination.
  - A header's benchmark columns are the bare capacities -- "5 MT", "10 MT" ...
    "24 MT", or on DELHI NCR the kg bands "UP TO 2000 kg", "2100 -2500 kg" ...
    Any other column after them is a transporter's own rate ("DELHI PUNJAB
    10MT", "MAHAVIR 10MT", "ABHIMAN 5MT", "AIR TRANS 16 MT") and is ignored, as
    is PUNJAB's "PER MT RATE", which is the 15 MT rate divided by 15.
  - DELHI NCR follows its benchmark block with one block per transporter
    (BHARGAVE, MAHAVEER, ARNAV). Those are told apart by having neither a STATE
    nor a DISTR column; the first block of a sheet is always the benchmark.
  - STATE and DISTR are merged down over the rows they cover, so a blank cell
    inside a merged range takes the range's value. A block without a STATE
    column is in the sheet's own region ("PUNJAB", "DELHI NCR").

A slab's band is its upper limit and the one below it in the same family:
"10 MT" is over 5 MT up to 10 MT, "2,501-3,500 kg" over 2,500 up to 3,500. The
workbook's "2100 -2500 kg" band is read as starting at 2,001, since nothing else
covers the 100 kg in between. A slab that already exists (matched on label)
keeps the band it has, so a band corrected on the page survives a re-import.
"""

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Dict, List, Optional, Tuple

from django.db import transaction

from .freight_benchmark_service import normalise_text
from .models_freight_benchmark import (
    FreightBenchmark,
    FreightDestination,
    FreightRateBasis,
    FreightSlab,
)

# The workbook's spellings of state names, and what the table stores.
STATE_SPELLINGS = {
    "UTTAR PARDESH": "UTTAR PRADESH",
    "HIMCHAL PARDESH": "HIMACHAL PRADESH",
    "MAHARASTRA": "MAHARASHTRA",
    "GUJRAT": "GUJARAT",
    "TELENGANA": "TELANGANA",
    "UTTRAKHAND": "UTTARAKHAND",
    "ROI": "REST OF INDIA",
}

# Column order on the page: vehicle sizes first (5 MT sorts at 5), then
# Delhi's kg bands (the 2,500 kg band sorts at 1025).
KG_SORT_BASE = 1000

MT = "MT"
KG = "KG"

MT_HEADER = re.compile(r"^(\d+)\s*MT$")
KG_UP_TO_HEADER = re.compile(r"^UP\s*TO\s*(\d+)\s*KG$")
KG_RANGE_HEADER = re.compile(r"^(\d+)\s*-\s*(\d+)\s*KG$")
PER_KG_VALUE = re.compile(r"^(\d+(?:\.\d+)?)\s*/\s*(?:PER\s*)?KG$")


@dataclass(frozen=True)
class SlabSpec:
    label: str
    above_kg: int
    up_to_kg: int
    sort_order: int


@dataclass
class ParsedRate:
    slab: Optional[SlabSpec]
    basis: str
    amount: Decimal
    # Filled while reading; `slab` is set once every header has been seen.
    family: str = ""
    up_to_kg: int = 0


@dataclass
class ParsedDestination:
    state: str
    district: str
    name: str
    pin_code: str
    distance_km: Optional[int]
    rates: List[ParsedRate]
    where: str  # "PUNJAB!B12", for messages


@dataclass
class ParsedWorkbook:
    destinations: List[ParsedDestination] = field(default_factory=list)
    skipped_blocks: List[str] = field(default_factory=list)
    problems: List[str] = field(default_factory=list)


def _kg_label(above_kg: int, up_to_kg: int) -> str:
    if above_kg == 0:
        return f"Up to {up_to_kg:,} kg"
    return f"{above_kg + 1:,}-{up_to_kg:,} kg"


def _state(value) -> str:
    text = normalise_text(value)
    return STATE_SPELLINGS.get(text, text)


def _place(value) -> str:
    text = normalise_text(value)
    # "RUPNAGAR (ROPER", "SAS NAGAR (MOHALI" -- the cell was cut short.
    if text.count("(") > text.count(")"):
        text += ")"
    return text


def _amount(value) -> Optional[Tuple[str, Decimal]]:
    """(basis, amount) for a benchmark cell, None for an empty one."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        amount = Decimal(str(value))
        return (FreightRateBasis.PER_TRIP, amount) if amount > 0 else None
    text = normalise_text(value)
    match = PER_KG_VALUE.match(text)
    if match:
        return FreightRateBasis.PER_KG, Decimal(match.group(1))
    try:
        amount = Decimal(text.replace(",", ""))
    except InvalidOperation:
        raise ValueError(f"cannot read {value!r} as a freight")
    return (FreightRateBasis.PER_TRIP, amount) if amount > 0 else None


def _slab_columns(header: List[str]) -> Dict[int, Tuple[str, int]]:
    """Benchmark columns of one header row, as {column index: (family, up to kg)}."""
    columns = {}
    for index, text in enumerate(header):
        match = MT_HEADER.match(text)
        if match:
            columns[index] = (MT, int(match.group(1)) * 1000)
            continue
        match = KG_UP_TO_HEADER.match(text) or KG_RANGE_HEADER.match(text)
        if match:
            columns[index] = (KG, int(match.groups()[-1]))
    return columns


def _slab_ladder(tops: Dict[str, set]) -> Dict[Tuple[str, int], SlabSpec]:
    """Each band opens where the next smaller one in its family closes."""
    specs = {}
    for family, sizes in tops.items():
        previous = 0
        for up_to in sorted(sizes):
            if family == MT:
                label, sort_order = f"{up_to // 1000} MT", up_to // 1000
            else:
                label = _kg_label(previous, up_to)
                sort_order = KG_SORT_BASE + up_to // 100
            specs[(family, up_to)] = SlabSpec(
                label=label,
                above_kg=previous,
                up_to_kg=up_to,
                sort_order=sort_order,
            )
            previous = up_to
    return specs


def parse_workbook(path) -> ParsedWorkbook:
    import openpyxl

    workbook = openpyxl.load_workbook(path, data_only=True)
    parsed = ParsedWorkbook()
    seen: Dict[Tuple[str, str], str] = {}
    tops: Dict[str, set] = {}

    for sheet in workbook.worksheets:
        merged = {}
        for cell_range in sheet.merged_cells.ranges:
            top_left = sheet.cell(cell_range.min_row, cell_range.min_col).value
            for row in range(cell_range.min_row, cell_range.max_row + 1):
                for col in range(cell_range.min_col, cell_range.max_col + 1):
                    merged[(row, col)] = top_left

        def value(row_number, col_index, raw):
            if raw is None:
                return merged.get((row_number, col_index + 1))
            return raw

        sheet_region = _state(sheet.title)
        heading = ""
        block = None  # (roles, slab columns, header texts)
        blocks_seen = 0

        for row_number, row in enumerate(sheet.iter_rows(values_only=True), start=1):
            texts = [normalise_text(cell) for cell in row]
            filled = [i for i, text in enumerate(texts) if text]
            if not filled:
                continue

            if "DESTINATION" in texts:
                blocks_seen += 1
                roles = {}
                for index, text in enumerate(texts):
                    if text == "STATE":
                        roles["state"] = index
                    elif text in ("DISTR", "DISTRICT"):
                        roles["district"] = index
                    elif text == "DESTINATION":
                        roles["name"] = index
                    elif text == "PIN CODE":
                        roles["pin_code"] = index
                    elif text == "KM":
                        roles["distance_km"] = index
                # HARYANA's UP and RAJASTHAN headers leave the PIN column's
                # title blank; the codes are still there beside Destination.
                after_name = roles.get("name", -2) + 1
                if (
                    "pin_code" not in roles
                    and 0 < after_name < len(texts)
                    and not texts[after_name]
                ):
                    roles["pin_code"] = after_name
                is_benchmark = blocks_seen == 1 or "state" in roles or "district" in roles
                if is_benchmark:
                    block = (roles, _slab_columns(texts), texts)
                    for family, up_to in block[1].values():
                        tops.setdefault(family, set()).add(up_to)
                else:
                    block = None
                    parsed.skipped_blocks.append(
                        f"{sheet.title}: {heading or 'untitled'} block (row {row_number})"
                        " -- a transporter's rates, not the benchmark"
                    )
                continue

            # A heading row: one filled cell, nothing to either side of it.
            # (Merged across A:E on HARYANA, so the other cells read empty.)
            if len(filled) == 1 and filled[0] == 0:
                heading = texts[0]
                block = None
                continue

            if block is None:
                continue
            roles, slab_columns, header = block
            where = f"{sheet.title}!row {row_number}"

            name = _place(row[roles["name"]])
            if not name:
                continue
            if "state" in roles:
                state = _state(value(row_number, roles["state"], row[roles["state"]]))
            else:
                state = sheet_region
            district = ""
            if "district" in roles:
                district = _place(
                    value(row_number, roles["district"], row[roles["district"]])
                )
            pin_code = ""
            if "pin_code" in roles and row[roles["pin_code"]] is not None:
                pin_code = str(row[roles["pin_code"]]).strip().split(".")[0]
                if not re.fullmatch(r"\d{6}", pin_code):
                    parsed.problems.append(
                        f"{where}, {name}: PIN {pin_code!r} is not six digits; left blank"
                    )
                    pin_code = ""
            distance_km = None
            if "distance_km" in roles and isinstance(
                row[roles["distance_km"]], (int, float)
            ):
                distance_km = int(row[roles["distance_km"]])

            rates = []
            for index, (family, up_to) in slab_columns.items():
                try:
                    read = _amount(row[index] if index < len(row) else None)
                except ValueError as error:
                    parsed.problems.append(f"{where}, {name} {header[index]}: {error}")
                    continue
                if read:
                    rates.append(
                        ParsedRate(
                            slab=None,
                            basis=read[0],
                            amount=read[1],
                            family=family,
                            up_to_kg=up_to,
                        )
                    )

            key = (state, name)
            if key in seen:
                parsed.problems.append(
                    f"{where}: {name} ({state}) is listed again; kept the one at {seen[key]}"
                )
                continue
            seen[key] = where
            parsed.destinations.append(
                ParsedDestination(
                    state=state,
                    district=district,
                    name=name,
                    pin_code=pin_code,
                    distance_km=distance_km,
                    rates=rates,
                    where=where,
                )
            )

    ladder = _slab_ladder(tops)
    for destination in parsed.destinations:
        for rate in destination.rates:
            rate.slab = ladder[(rate.family, rate.up_to_kg)]
    return parsed


@dataclass
class ImportResult:
    slabs_created: List[str] = field(default_factory=list)
    destinations_created: int = 0
    destinations_updated: int = 0
    destinations_unchanged: int = 0
    rates_created: int = 0
    rates_changed: int = 0
    rates_removed: int = 0
    not_in_workbook: List[str] = field(default_factory=list)


def apply_workbook(parsed: ParsedWorkbook, *, user=None) -> ImportResult:
    """
    Write the parsed workbook into the table, in one transaction.

    The workbook is the whole truth for each destination it lists: its rates are
    replaced by the workbook's, so a slab the workbook leaves blank loses its
    rate. Destinations the workbook does not list are left alone and reported.
    """
    result = ImportResult()
    with transaction.atomic():
        slabs: Dict[str, FreightSlab] = {s.label: s for s in FreightSlab.objects.all()}
        for destination in parsed.destinations:
            for rate in destination.rates:
                spec = rate.slab
                if spec.label not in slabs:
                    slabs[spec.label] = FreightSlab.objects.create(
                        label=spec.label,
                        above_kg=spec.above_kg,
                        up_to_kg=spec.up_to_kg,
                        sort_order=spec.sort_order,
                    )
                    result.slabs_created.append(spec.label)

        listed = set()
        for parsed_row in parsed.destinations:
            row, created = FreightDestination.objects.get_or_create(
                state=parsed_row.state,
                name=parsed_row.name,
                defaults={"updated_by": user},
            )
            listed.add(row.pk)
            fields = {
                "district": parsed_row.district,
                "pin_code": parsed_row.pin_code,
                "distance_km": parsed_row.distance_km,
            }
            fields_changed = any(getattr(row, k) != v for k, v in fields.items())
            if fields_changed:
                for k, v in fields.items():
                    setattr(row, k, v)
                row.updated_by = user
                row.save()

            existing = {b.slab.label: b for b in row.benchmarks.select_related("slab")}
            rates_touched = False
            for rate in parsed_row.rates:
                current = existing.pop(rate.slab.label, None)
                if current is None:
                    FreightBenchmark.objects.create(
                        destination=row,
                        slab=slabs[rate.slab.label],
                        basis=rate.basis,
                        amount=rate.amount,
                        updated_by=user,
                    )
                    result.rates_created += 1
                    rates_touched = True
                elif current.amount != rate.amount or current.basis != rate.basis:
                    current.amount = rate.amount
                    current.basis = rate.basis
                    current.updated_by = user
                    current.save()
                    result.rates_changed += 1
                    rates_touched = True
            if existing:
                FreightBenchmark.objects.filter(
                    pk__in=[b.pk for b in existing.values()]
                ).delete()
                result.rates_removed += len(existing)
                rates_touched = True

            if created:
                result.destinations_created += 1
            elif fields_changed or rates_touched:
                result.destinations_updated += 1
            else:
                result.destinations_unchanged += 1

        result.not_in_workbook = sorted(
            f"{d.name} ({d.state})"
            for d in FreightDestination.objects.exclude(pk__in=listed)
        )
    return result
