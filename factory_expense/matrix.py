"""
factory_expense/matrix.py

The expense board as a **matrix**: one row per company, one column per cost
bucket, and a total row that ties exactly to the all-companies wall board.

Why this is not just ``build_board`` called once per company
------------------------------------------------------------

Two of the four buckets are not company-attributable, and calling the existing
board three times would silently invent the split:

* **Electricity.** Worse than unattributable — the register double-counts.
  ``KWH`` is the site's main incomer, ``KVAH`` is that same supply measured as
  apparent energy (their ratio is the 0.96 power factor), and every other meter
  is a sub-meter *of* that incomer: in September the sub-meters summed to
  1.035 × KWH and tracked it day by day. Summing the register therefore reports
  about three times the electricity the factory used — ₹19.87 L against a real
  ₹6.63 L. The board reads the sub-meters only, attributed by
  ``constants.ELECTRICITY_METER_COMPANY`` rather than by the meter's
  many-to-many company tagging, which leaves 69% of the bill on meters that
  "feed both companies" and so attributes nothing. The incomer is still read and
  served back as a reconciliation figure.

* **Salary.** A Cost Master row with no company set applies to every company
  (see ``rates.load_rates_by_company``), so a factory-wide ₹30 L blanket
  resolves once per company. Three boards added together bill it three times.

So anything that belongs to a single company is attributed to that company, and
anything that genuinely belongs to the whole campus lands in one explicit
**Shared** row instead of being spread on a guess. The user's rule, chosen when
this board was specified: the meter's own company tagging on the Daily
Electricity page is the source of truth, and a meter tagged with two companies
is a shared meter — not half of each.

* **Labour.** ``LabourGateEntry`` *has* a company foreign key, and it is not
  usable: every department row on the live register is tagged ``JIVO_OIL``,
  ``Warehouse Gupta`` (Mart) and ``Warehouse Beverage`` (Beverages) included.
  A labourer is owned by their **department**, through
  ``constants.LABOUR_DEPARTMENT_COMPANY``. The register also holds the gate's
  head count and an HOD's department split of those same people under one shape,
  so summing it reports roughly half as many people again as turned up — see
  :func:`labour_by_company`.

Only maintenance needs none of this: spares and indents carry a company foreign
key that means what it says, so each row already belongs to exactly one company.
"""

import calendar
from collections import defaultdict
from datetime import date
from decimal import Decimal

from labour_gate.models import LabourGateEntry
from maintenance.models import DailyElectricityReading, ElectricityMeter

from .constants import (
    ELECTRICITY_EXPECTED_METERS,
    MATRIX_ROW_LABELS,
    ELECTRICITY_MAIN_METERS,
    ELECTRICITY_MAINS_IN_SHARED,
    ELECTRICITY_METER_COMPANY,
    ELECTRICITY_PRIMARY_INCOMER,
    ELECTRICITY_SHARED_METERS,
    LABOUR_COST_TYPE_CODE,
    LABOUR_DEPARTMENT_COMPANY,
    LABOUR_SHARED_DEPARTMENTS,
    LABOUR_UNALLOCATED_LABEL,
    SALARY_COST_TYPE_CODE,
    ExpenseBucket,
    normalise_department,
    normalise_meter,
)
from .rates import load_rates_by_company, monthly_amounts_by_department, resolve
from .services import (
    ZERO,
    _days_between,
    _money,
    _price_labour,
    get_settings,
    maintenance_costs,
)

#: The row that carries what no single company owns: meters feeding more than
#: one company, Cost Master rates with no company set, and labour whose
#: department serves several companies or which no department has claimed.
SHARED_ROW_KEY = "__shared__"
SHARED_ROW_LABEL = "Shared (whole factory)"

#: Column order, left to right. Matches the board's own reading order —
#: what people cost, what power cost, what breakage cost, what the gate cost.
MATRIX_COLUMNS = (
    ExpenseBucket.SALARY,
    ExpenseBucket.ELECTRICITY,
    ExpenseBucket.MAINTENANCE,
    ExpenseBucket.LABOUR,
)


def _cell(amount, *, unit=None, unit_label=None, warning=None, note=None):
    """One square of the matrix.

    ``warning`` means *this figure cannot be read* — the tile draws a rule
    instead of a number. ``note`` is context on a figure that is real, such as
    the head count behind a labour cost.
    """
    return {
        "amount": _money(amount),
        "unit": unit,
        "unit_label": unit_label,
        "warning": warning,
        "note": note,
    }


# ---------------------------------------------------------------------------
# Electricity, split the way the meters are tagged
# ---------------------------------------------------------------------------

def mapped_meter_counts(companies):
    """How many existing meters the mapping gives each company.

    Read from the meter master rather than inferred from the readings, because
    the two failures look identical in a reading-derived count and need opposite
    fixes: a company with no meter mapped to it can never show a power figure,
    while a company with meters but no reading in the span is simply waiting for
    the operator to type one in.

    Only meters that actually exist are counted. The mapping deliberately names
    four that do not yet (``Basement``, ``LB``, ``Admin``, ``TR 60``), and
    counting those would promise a figure the register cannot produce.
    """
    by_code = {company.code: company for company in companies}
    counts = {company.id: 0 for company in companies}
    for meter in ElectricityMeter.objects.all():
        key = normalise_meter(meter.name)
        if key in ELECTRICITY_MAIN_METERS:
            continue
        code = ELECTRICITY_METER_COMPANY.get(key)
        company = by_code.get(code) if code else None
        if company is not None:
            counts[company.id] += 1
    return counts


def _power_warning(power, sole_meters):
    """Why a company's electricity square cannot be read, or None.

    Two different failures, two different desks: no meter mapped is fixed in
    ``constants.ELECTRICITY_METER_COMPANY``, an unread meter by the operator who
    enters the day's figures. ``₹0`` would be a lie for both — the factory does
    not stop drawing power because nobody wrote the reading down.
    """
    if power:
        return None
    if not sole_meters:
        return "No meter is mapped to this company"
    return (
        f"No reading entered for its {sole_meters} meter"
        f"{'s' if sole_meters != 1 else ''}"
    )


def electricity_by_company(companies, dates, settings_row):
    """Sub-meter cost split by the mapping, with the incomer kept aside.

    Returns ``(per_company, shared, incomer, notes)``.

    ``ELECTRICITY_MAIN_METERS`` measure the incoming supply that every other
    meter is a part of. They are always summed separately into ``incomer`` so the
    sub-meters can be checked against the meter the bill is struck on, and
    ``ELECTRICITY_MAINS_IN_SHARED`` decides whether they ALSO land in the shared
    row and so in the column total. They do today, by the user's choice, which
    means the column counts the same electricity about three times; the returned
    notes say so.

    ``sub_meter_cost`` is the columns without the mains — the figure the
    reconciliation is meaningful against, whichever way that flag is set.

    ``settings_row.electricity_only_company_meters`` is deliberately ignored
    here. It filters on ``meter__companies``, which this board no longer uses;
    honouring it would silently drop meters the mapping has already placed.
    """
    readings = (
        DailyElectricityReading.objects.filter(date__in=dates, is_active=True)
        .select_related("meter")
    )

    def blank():
        return {"cost": ZERO, "units": ZERO, "meters": set()}

    by_code = {company.code: company for company in companies}
    per_company = defaultdict(blank)
    shared = blank()
    incomer = defaultdict(blank)
    unmapped = set()
    notes = []
    sub_meter_cost = ZERO

    for reading in readings:
        name = reading.meter.name
        key = normalise_meter(name)
        cost = reading.total_cost or ZERO
        units = reading.units_consumed or ZERO

        if key in ELECTRICITY_MAIN_METERS:
            # Always tracked on its own, so the reconciliation survives whichever
            # way the flag is set; ALSO added to the shared row when it is on.
            main = incomer[key]
            main["cost"] += cost
            main["units"] += units
            main["meters"].add(name)
            if not ELECTRICITY_MAINS_IN_SHARED:
                continue
            bucket = shared
        else:
            sub_meter_cost += cost
            code = ELECTRICITY_METER_COMPANY.get(key)
            company = by_code.get(code) if code else None
            if company is not None:
                bucket = per_company[company.id]
            else:
                bucket = shared
                if code is None and key not in ELECTRICITY_SHARED_METERS:
                    unmapped.add(name)

        bucket["cost"] += cost
        bucket["units"] += units
        bucket["meters"].add(name)

    if unmapped:
        notes.append(
            "Not in the meter mapping, counted as shared: " + ", ".join(sorted(unmapped)) + "."
        )

    # Meters the mapping is waiting on. Named so a reader knows the column is
    # incomplete by configuration rather than wrong by accident.
    existing = {normalise_meter(name) for name in ElectricityMeter.objects.values_list("name", flat=True)}
    awaited = sorted(
        label for key, label in ELECTRICITY_EXPECTED_METERS.items() if key not in existing
    )
    if awaited:
        notes.append(
            "The mapping names "
            + ", ".join(awaited)
            + ", which "
            + ("do" if len(awaited) > 1 else "does")
            + " not exist on the Daily Electricity page yet."
        )

    if ELECTRICITY_MAINS_IN_SHARED and incomer:
        notes.append(
            "Electricity counts the mains ("
            + ", ".join(sorted(name for b in incomer.values() for name in b["meters"]))
            + ") alongside the sub-meters that measure the same supply, so the "
            "column is roughly three times the metered bill."
        )

    return per_company, shared, dict(incomer), sub_meter_cost, notes


def reconcile_against_incomer(sub_meter_cost, incomer):
    """The sum of the sub-meters against the meter the bill comes from.

    Takes the SUB-METER total, never the column total: with the mains counted in
    the column, comparing the column to the incomer would be comparing a number
    to a part of itself.

    A single number nobody can argue with: if the parts do not add up to the
    incomer, either a sub-meter is unread or one is being counted that should
    not be. Returned as ``None`` when the incomer was not read in the span,
    because "no drift" and "no reading" must not look the same.
    """
    primary = incomer.get(ELECTRICITY_PRIMARY_INCOMER)
    if not primary or not primary["cost"]:
        return None

    reference = primary["cost"]
    drift = (Decimal(sub_meter_cost) - reference) / reference * 100
    return {
        "meter": sorted(primary["meters"])[0] if primary["meters"] else None,
        "sub_meter_cost": _money(sub_meter_cost),
        "cost": _money(reference),
        "units": _money(primary["units"]),
        "drift_pct": round(float(drift), 1),
        "excluded_meters": sorted(
            name for bucket in incomer.values() for name in bucket["meters"]
        ),
    }


# ---------------------------------------------------------------------------
# Labour, split the way the departments are mapped
# ---------------------------------------------------------------------------

def labour_by_company(companies, dates, cost_type_code):
    """Gate labour attributed by **department**, priced per company.

    Returns ``(per_company, shared, notes)``. ``per_company`` maps a company id
    to ``{"cost", "headcount"}``; ``shared`` is the same shape for labour that
    no single company owns; ``notes`` carries the board-level warnings.

    Two rules drive everything here, and both are easy to get wrong silently.

    **1. The register holds two kinds of row under one shape.** A row with no
    department is what the gate recorded — a contractor turned up with N people,
    and that is the head count that walked through the barrier. A row *with* a
    department is an HOD afterwards splitting those same N people across
    departments: a view of the first kind, never extra people. Adding the two
    together double-counts every allocated labourer — on the live register in
    September that is 1,308 man-days reported against 827 real ones. This is
    the same rule `useGateBoard.splitLabourDay` applies on the gate wall.

    **2. ``LabourGateEntry.company`` cannot attribute labour.** Every department
    row on the live register is tagged ``JIVO_OIL``, ``Warehouse Gupta`` and
    ``Warehouse Beverage`` included. So a company owns a labourer only through
    ``LABOUR_DEPARTMENT_COMPANY``; anything unmapped, multi-company, or never
    allocated at all goes to the shared row.

    Allocation is reconciled per ``(day, contractor)`` against what that
    contractor actually brought in, so the column always ties to the gate count
    rather than to the sum of somebody's spreadsheet.
    """
    entries = list(
        LabourGateEntry.objects.filter(
            company__in=companies, work_date__in=dates, is_active=True
        ).select_related("department")
    )

    # --- reconcile each contractor's day against their own gate count --------
    gate_in = defaultdict(int)
    allocations = defaultdict(list)
    for entry in entries:
        key = (entry.work_date, entry.contractor_id)
        heads = entry.count_in or 0
        if entry.department_id is None:
            gate_in[key] += heads
        elif heads:
            allocations[key].append((entry.department, heads))

    notes = []
    over_allocated = []
    # target key -> {date -> headcount}. Dates are kept because a flat PER_DAY
    # rate is charged once per day, not once per department.
    heads_by_target = defaultdict(lambda: defaultdict(int))
    department_names = defaultdict(set)

    for key, rows in allocations.items():
        work_date, _ = key
        claimed = sum(heads for _, heads in rows)
        walked_in = gate_in.get(key, 0)

        # An HOD allocating more people than the contractor brought is a data
        # error, not more labour. Scale the split back onto the gate count so
        # the column still ties, and say so rather than quietly absorbing it.
        scale = 1.0
        if claimed > walked_in:
            over_allocated.append(work_date)
            scale = (walked_in / claimed) if claimed else 0.0

        for department, heads in rows:
            share = int(round(heads * scale))
            if not share:
                continue
            normalised = normalise_department(department.name)
            target = LABOUR_DEPARTMENT_COMPANY.get(normalised)
            if normalised in LABOUR_SHARED_DEPARTMENTS or target is None:
                target = SHARED_ROW_KEY
                department_names[SHARED_ROW_KEY].add(department.name)
            heads_by_target[target][work_date] += share

    # Whatever walked in and no department has claimed. Per day and contractor,
    # floored at zero — a scaled-back over-allocation must not create negatives.
    unallocated = defaultdict(int)
    for key, walked_in in gate_in.items():
        work_date, _ = key
        claimed = min(sum(heads for _, heads in allocations.get(key, [])), walked_in)
        remainder = walked_in - claimed
        if remainder > 0:
            unallocated[work_date] += remainder
    for work_date, heads in unallocated.items():
        heads_by_target[SHARED_ROW_KEY][work_date] += heads
    if unallocated:
        department_names[SHARED_ROW_KEY].add(LABOUR_UNALLOCATED_LABEL)

    if over_allocated:
        days = sorted(set(over_allocated))
        notes.append(
            f"{len(days)} day{'s' if len(days) != 1 else ''} allocate more labour to "
            "departments than walked through the gate; the split has been scaled "
            "back onto the gate count."
        )

    # --- price it -----------------------------------------------------------
    by_code = {company.code: company for company in companies}
    rates_by_company = load_rates_by_company(cost_type_code, companies, max(dates))
    # The shared row belongs to no company, so only a rate set for no particular
    # company can price it — the same rule the salary column follows.
    agnostic = [
        rate
        for rate in next(iter(rates_by_company.values()), [])
        if rate.company_id is None
    ]

    per_company = {}
    shared = {"cost": ZERO, "headcount": 0, "warning": None, "note": None}
    unpriced = 0

    for target, by_date in heads_by_target.items():
        headcount = sum(by_date.values())
        if target == SHARED_ROW_KEY:
            rates = agnostic
        else:
            company = by_code.get(target)
            if company is None:
                # A mapped company the viewer cannot see. Its labour is not
                # theirs to read, and must not fall into the shared row.
                continue
            rates = rates_by_company.get(company.id, [])

        cost = ZERO
        flat_charged = set()
        for work_date, heads in by_date.items():
            rate = resolve(rates, None, work_date)
            if rate is None:
                unpriced += heads
                continue
            if rate.basis == "PER_DAY":
                # A flat daily charge lands once for the day, however many
                # departments the day's people were split across.
                if work_date in flat_charged:
                    continue
                flat_charged.add(work_date)
            cost += _money(_price_labour(rate, heads))

        if target == SHARED_ROW_KEY:
            shared["cost"] = cost
            shared["headcount"] = headcount
            if headcount and not agnostic:
                shared["warning"] = "No factory-wide labour rate set"
        else:
            per_company[by_code[target].id] = {"cost": cost, "headcount": headcount}

    for company in companies:
        per_company.setdefault(company.id, {"cost": ZERO, "headcount": 0})

    names = sorted(department_names.get(SHARED_ROW_KEY, set()))
    shared["note"] = ", ".join(names) if names else None

    if unpriced:
        notes.append(
            f"{unpriced} man-days have no '{cost_type_code}' rate and are counted "
            "at zero cost."
        )

    return per_company, shared, notes


# ---------------------------------------------------------------------------
# Salary, split by whether the rate names a company
# ---------------------------------------------------------------------------

def salary_by_company(companies, on_date, days_in_month, cost_type_code):
    """The month's salary bill split into per-company and factory-wide parts.

    Returns ``(per_company, shared)`` of daily accrual Decimals, where
    ``per_company`` maps a company id to its own rates' daily share and
    ``shared`` is the daily share of every rate that names no company.

    The split is on ``CostRate.company_id``, which is the only thing that makes
    a salary rate belong to a company. ``load_rates_by_company`` deliberately
    hands the company-agnostic rows to *every* company — right for resolving one
    labourer's rate, and exactly what would treble a factory-wide blanket here —
    so those rows are lifted out and resolved once, on their own.
    """
    rates_by_company = load_rates_by_company(cost_type_code, companies, on_date)

    per_company = {}
    for company in companies:
        own = [
            rate
            for rate in rates_by_company.get(company.id, [])
            if rate.company_id == company.id
        ]
        monthly = sum(
            (amount for _, _, amount, _ in monthly_amounts_by_department(own, on_date)),
            Decimal("0"),
        )
        per_company[company.id] = monthly / days_in_month if monthly else ZERO

    # Every company's list carries the same company-agnostic rows, so one list
    # is enough — resolving them per company is what would count them N times.
    agnostic = [
        rate
        for rate in next(iter(rates_by_company.values()), [])
        if rate.company_id is None
    ]
    shared_monthly = sum(
        (amount for _, _, amount, _ in monthly_amounts_by_department(agnostic, on_date)),
        Decimal("0"),
    )

    return per_company, (shared_monthly / days_in_month if shared_monthly else ZERO)


# ---------------------------------------------------------------------------
# The matrix
# ---------------------------------------------------------------------------

def build_matrix(
    companies,
    date_from: date,
    date_to: date | None = None,
    settings_company=None,
) -> dict:
    """Company × bucket expense for a span of days.

    Every figure covers the selected span — there is no month-to-date second
    number here, because the matrix's job is comparison across companies rather
    than pace against a budget, and two numbers per square would make a wall
    board unreadable at ten feet.

    ``settings_company`` decides whose configuration the matrix obeys — which
    Cost Master types the salary and labour columns are priced from, and whether
    electricity is limited to tagged meters. It defaults to the first company,
    matching ``build_board``.
    """
    date_to = date_to or date_from
    if date_to < date_from:
        date_from, date_to = date_to, date_from

    companies = list(companies)
    if not companies:
        raise ValueError("build_matrix needs at least one company.")

    settings_row = get_settings(settings_company or companies[0])
    span = _days_between(date_from, date_to)
    labour_code = settings_row.labour_cost_type_code or LABOUR_COST_TYPE_CODE
    salary_code = settings_row.salary_cost_type_code or SALARY_COST_TYPE_CODE

    warnings = []

    # --- salary ----------------------------------------------------------
    # A monthly rate spread over the month's days, then multiplied by the days
    # on screen: a part-month view is an accrual, not the whole bill on the 1st.
    # `date_to`'s month sets the divisor, the same choice `build_board` makes.
    days_in_month = calendar.monthrange(date_to.year, date_to.month)[1]
    salary_daily, salary_shared_daily = salary_by_company(
        companies, date_to, days_in_month, salary_code
    )
    salary_configured = any(salary_daily.values()) or bool(salary_shared_daily)
    if not salary_configured:
        warnings.append(
            f"No '{salary_code}' rate is in force — the salary column reads nothing."
        )

    # --- electricity -----------------------------------------------------
    # Sub-meters only: the mains measure the supply every one of them is a part
    # of, so counting both reports the same electricity about three times over.
    (
        power_by_company,
        power_shared,
        incomer,
        sub_meter_cost,
        power_notes,
    ) = electricity_by_company(companies, span, settings_row)
    sole_meters = mapped_meter_counts(companies)
    warnings.extend(power_notes)

    # --- labour ----------------------------------------------------------
    # Attributed by DEPARTMENT, not by the entry's company: every department row
    # on the live register is tagged Oil, Warehouse Gupta included. Unmapped,
    # multi-company and never-allocated labour lands in the shared row.
    labour, labour_shared, labour_notes = labour_by_company(companies, span, labour_code)
    warnings.extend(labour_notes)

    # --- maintenance, one company at a time ------------------------------
    # Spares and indents carry a real company foreign key, so a per-company call
    # is the whole answer and none of it is shared.
    maintenance_by_company = {}
    for company in companies:
        maint_per_date, _ = maintenance_costs([company], span, settings_row, set(span))
        maintenance_by_company[company.id] = sum(
            (maint_per_date[day]["cost"] for day in span), ZERO
        )

    # --- rows ------------------------------------------------------------
    rows = []
    for company in companies:
        power = power_by_company.get(company.id)
        labour_row = labour[company.id]
        salary_amount = salary_daily.get(company.id, ZERO) * len(span)

        rows.append(
            {
                "key": company.code,
                # The board's own name for the row, falling back to the company's.
                # See MATRIX_ROW_LABELS: this renames a row, never a company.
                "label": MATRIX_ROW_LABELS.get(company.code, company.name),
                "kind": "COMPANY",
                "cells": {
                    ExpenseBucket.SALARY.value: _cell(
                        salary_amount,
                        warning=None if salary_configured else "No rate set",
                        note=(
                            f"{len(span)} of {days_in_month} days"
                            if salary_amount
                            else None
                        ),
                    ),
                    ExpenseBucket.ELECTRICITY.value: _cell(
                        power["cost"] if power else ZERO,
                        unit=str(_money(power["units"])) if power else None,
                        unit_label="units",
                        warning=_power_warning(power, sole_meters.get(company.id, 0)),
                        note=(
                            f"{len(power['meters'])} meter"
                            f"{'s' if len(power['meters']) != 1 else ''}"
                            if power
                            else None
                        ),
                    ),
                    ExpenseBucket.MAINTENANCE.value: _cell(
                        maintenance_by_company[company.id]
                    ),
                    ExpenseBucket.LABOUR.value: _cell(
                        labour_row["cost"],
                        unit=labour_row["headcount"],
                        unit_label="man-days",
                    ),
                },
            }
        )

    # The shared row is always present, so the reader learns that a shared
    # supply exists rather than only noticing on the months it is non-zero.
    shared_meters = sorted(power_shared["meters"])
    rows.append(
        {
            "key": SHARED_ROW_KEY,
            "label": SHARED_ROW_LABEL,
            "kind": "SHARED",
            "cells": {
                ExpenseBucket.SALARY.value: _cell(
                    salary_shared_daily * len(span),
                    note="Rates set for no particular company" if salary_shared_daily else None,
                ),
                ExpenseBucket.ELECTRICITY.value: _cell(
                    power_shared["cost"],
                    unit=str(_money(power_shared["units"])),
                    unit_label="units",
                    note=", ".join(shared_meters) if shared_meters else None,
                ),
                # Maintenance cannot produce a row without a company, so it is
                # structurally zero here rather than merely empty today.
                ExpenseBucket.MAINTENANCE.value: _cell(ZERO),
                ExpenseBucket.LABOUR.value: _cell(
                    labour_shared["cost"],
                    unit=labour_shared["headcount"],
                    unit_label="man-days",
                    warning=labour_shared["warning"],
                    note=labour_shared["note"],
                ),
            },
        }
    )

    for row in rows:
        row["total"] = _money(
            sum((Decimal(row["cells"][column.value]["amount"]) for column in MATRIX_COLUMNS), ZERO)
        )

    # --- the total row ---------------------------------------------------
    # Added down the columns from the rows above, so what the board displays is
    # provably what it adds up to: a separately-computed total that disagreed
    # with its own rows would be the one number nobody could reconcile.
    totals = {
        column.value: _cell(
            sum(
                (Decimal(row["cells"][column.value]["amount"]) for row in rows),
                ZERO,
            )
        )
        for column in MATRIX_COLUMNS
    }
    grand = sum((Decimal(cell["amount"]) for cell in totals.values()), ZERO)

    # The sum of the sub-meters against the meter the bill is struck on. The one
    # figure that says whether the electricity column is complete.
    reconciliation = reconcile_against_incomer(sub_meter_cost, incomer)

    return {
        "date_from": date_from,
        "date_to": date_to,
        "days": len(span),
        "is_single_day": date_from == date_to,
        "columns": [
            {"key": column.value, "label": column.label} for column in MATRIX_COLUMNS
        ],
        "rows": rows,
        "total": {
            "key": "__total__",
            "label": "Total",
            "kind": "TOTAL",
            "cells": totals,
            "total": _money(grand),
        },
        "company_codes": [company.code for company in companies],
        "electricity_reconciliation": reconciliation,
        "settings": {
            "labour_cost_type_code": labour_code,
            "salary_cost_type_code": salary_code,
            "refresh_seconds": settings_row.refresh_seconds,
        },
        "warnings": warnings,
    }
