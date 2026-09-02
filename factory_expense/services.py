"""
factory_expense/services.py

Builds the wall board from FactoryFlow's own registers. No SAP.

Where each number comes from:

* **Labour**      ``labour_gate.LabourGateEntry.count_in`` — how many labourers a
  contractor actually walked through the gate — priced at the ``factory-labour``
  rate from the **Cost Master** (Admin › Cost Master).
* **Salary**      the ``factory-salary`` PER_MONTH rates from the Cost Master,
  department by department, spread evenly across the month's days so a
  part-month view is an accrual, not the whole bill on the 1st.
* **Electricity** ``maintenance.DailyElectricityReading`` — units and cost the
  operator already enters on the Daily Electricity page.
* **Maintenance** spares issued or consumed at their unit cost, plus material
  indents once a company has been selected.

Every bucket also reports *why* it is empty when it is. A wall board that shows
₹0 with no explanation gets ignored within a week; one that says "no labour
rate set from 01 Sep" gets fixed.
"""

import calendar
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import F

from labour_gate.models import LabourGateEntry
from maintenance.models import (
    DailyElectricityReading,
    MaterialIndent,
    SpareMovement,
)

from .constants import (
    LABOUR_COST_TYPE_CODE,
    MAX_TREND_DAYS,
    MAINTENANCE_COMMITTED_INDENT_STATUSES,
    MAINTENANCE_SPEND_MOVEMENTS,
    SALARY_COST_TYPE_CODE,
    TREND_DAYS,
    ExpenseBucket,
)
from .models import FactoryExpenseSettings, MonthlyBudget, month_start
from .rates import load_rates_by_company, monthly_amounts_by_department, resolve

ZERO = Decimal("0.00")


def _money(value) -> Decimal:
    return (Decimal(value or 0)).quantize(Decimal("0.01"))


def get_settings(company) -> FactoryExpenseSettings:
    """The company's board settings, created with defaults on first read.

    Settings stay per-company even on the all-companies board: they are about
    presentation — which panels show, how fast the wall polls — so the settings
    of the company the viewer is signed into are the right ones to obey.
    """
    settings_row, _ = FactoryExpenseSettings.objects.get_or_create(company=company)
    return settings_row


# ---------------------------------------------------------------------------
# Labour
# ---------------------------------------------------------------------------

def labour_costs(companies, dates, focus=None):
    """Gate headcount priced from the Cost Master.

    Returns ``(per_date, departments, contractors, unpriced_headcount)`` where
    ``per_date`` maps a date to ``{"cost", "headcount"}``. The two breakdowns
    cover ``focus`` — the days the user actually selected — which is the last
    date when the board is showing a single day and the whole span when a range
    is picked.

    An entry whose department has no rate is still *counted*; its cost stays
    zero and its people land in ``unpriced_headcount`` so the board can say the
    rate is missing instead of implying the labour was free.
    """
    entries = list(
        LabourGateEntry.objects.filter(
            company__in=companies, work_date__in=dates, is_active=True
        ).select_related("department", "contractor")
    )
    # One Cost Master read for the whole window and every company, ranked per
    # entry in Python. Each entry is priced against its OWN company's rates —
    # a shared pile would let one company's rate price another's labourers.
    rates_by_company = load_rates_by_company(LABOUR_COST_TYPE_CODE, companies, max(dates))

    per_date = {day: {"cost": ZERO, "headcount": 0} for day in dates}
    departments = defaultdict(lambda: {"headcount": 0, "cost": ZERO})
    contractors = defaultdict(lambda: {"headcount": 0, "cost": ZERO})
    unpriced = 0
    focus = set(focus or [max(dates)])

    for entry in entries:
        rate = resolve(
            rates_by_company.get(entry.company_id, []),
            entry.department_id,
            entry.work_date,
        )
        headcount = entry.count_in or 0
        cost = _money(headcount * Decimal(rate.rate)) if rate else ZERO
        if rate is None and headcount:
            unpriced += headcount

        bucket = per_date[entry.work_date]
        bucket["cost"] += cost
        bucket["headcount"] += headcount

        if entry.work_date in focus:
            dept_name = entry.department.name if entry.department else "Unallocated"
            departments[dept_name]["headcount"] += headcount
            departments[dept_name]["cost"] += cost
            name = str(entry.contractor)
            contractors[name]["headcount"] += headcount
            contractors[name]["cost"] += cost

    return per_date, departments, contractors, unpriced


# ---------------------------------------------------------------------------
# Salary
# ---------------------------------------------------------------------------

def salary_costs(companies, on_date):
    """The month's department-wise salary bill, and what one day of it accrues to.

    The figures are ``factory-salary`` PER_MONTH rates from the Cost Master.
    Because a monthly rate is a rate like any other, back-dating the board to
    last month prices it at last month's rate without anything extra here.

    Across several companies each is resolved on its own and the results are
    merged by department, because a department is an org-wide thing rather than
    a per-company one: Packing in Oil and Packing in Beverages are the same
    Packing, and a wall showing it twice would invite the reader to add it up.
    """
    days_in_month = calendar.monthrange(on_date.year, on_date.month)[1]
    rates_by_company = load_rates_by_company(SALARY_COST_TYPE_CODE, companies, on_date)

    merged = {}
    for company in companies:
        for department_id, department_name, amount in monthly_amounts_by_department(
            rates_by_company.get(company.id, []), on_date
        ):
            key = department_id if department_id is not None else f"blanket:{company.id}"
            label = department_name
            if department_id is None and len(companies) > 1:
                # Several unallocated blankets on one board have to be told
                # apart, or the panel shows "All departments" three times.
                label = f"All departments ({company.code})"
            current = merged.get(key)
            merged[key] = (department_id, label, (current[2] if current else ZERO) + amount)
    rows = sorted(merged.values(), key=lambda row: row[1])

    departments = []
    monthly_total = ZERO
    for department_id, department_name, amount in rows:
        monthly_total += amount
        departments.append(
            {
                "department": department_name,
                "department_id": department_id,
                "monthly": _money(amount),
                "daily": _money(amount / days_in_month),
            }
        )

    departments.sort(key=lambda item: item["monthly"], reverse=True)
    daily_total = _money(monthly_total / days_in_month)
    return {
        "departments": departments,
        "monthly": _money(monthly_total),
        "daily": daily_total,
        "mtd": _money(daily_total * on_date.day),
        "days_in_month": days_in_month,
        "configured": bool(rows),
    }


# ---------------------------------------------------------------------------
# Electricity
# ---------------------------------------------------------------------------

def electricity_costs(companies, dates, settings_row, focus=None):
    """Units and cost from the Daily Electricity register.

    ``focus`` is the span the per-meter breakdown covers; the per-date series
    always spans ``dates`` so the trend can be drawn either way.

    **Each reading is counted once, however many of the selected companies its
    meter serves.** Four of the campus meters — HTP, KVAH, KWH and LP-196 —
    feed both Oil and Beverages, so adding up per-company boards would report
    twice the electricity the factory actually used. The ``__in`` + ``distinct``
    pair is what keeps a shared meter from being billed to the board twice.
    """
    readings = DailyElectricityReading.objects.filter(date__in=dates, is_active=True)
    if settings_row.electricity_only_company_meters:
        readings = readings.filter(meter__companies__in=companies)
    readings = readings.select_related("meter").distinct()

    per_date = {day: {"cost": ZERO, "units": ZERO} for day in dates}
    meters = defaultdict(lambda: {"units": ZERO, "cost": ZERO, "rate": ZERO})
    focus = set(focus or [max(dates)])

    for reading in readings:
        bucket = per_date[reading.date]
        bucket["cost"] += reading.total_cost or ZERO
        bucket["units"] += reading.units_consumed or ZERO
        if reading.date in focus:
            name = reading.meter.name
            meters[name]["units"] += reading.units_consumed or ZERO
            meters[name]["cost"] += reading.total_cost or ZERO
            meters[name]["rate"] = reading.rate_per_unit or ZERO

    return per_date, meters


# ---------------------------------------------------------------------------
# Maintenance
# ---------------------------------------------------------------------------

def maintenance_costs(companies, dates, settings_row, focus=None):
    """Spares consumed and indents committed, per day, plus the span's line items."""
    per_date = {day: {"cost": ZERO} for day in dates}
    items = []
    focus = set(focus or [max(dates)])
    window_start, window_end = min(dates), max(dates)

    if settings_row.maintenance_include_spares:
        movements = (
            SpareMovement.objects.filter(
                company__in=companies,
                movement_type__in=MAINTENANCE_SPEND_MOVEMENTS,
                created_at__date__gte=window_start,
                created_at__date__lte=window_end,
                is_active=True,
            )
            .select_related("spare")
            .annotate(line_value=F("quantity") * F("unit_cost"))
        )
        for movement in movements:
            moved_on = movement.created_at.date()
            value = _money(movement.line_value)
            per_date[moved_on]["cost"] += value
            if moved_on in focus and value:
                items.append(
                    {
                        "label": f"{movement.spare.part_number} × {movement.quantity:g}",
                        "kind": "Spare",
                        "amount": value,
                    }
                )

    if settings_row.maintenance_include_indents:
        indents = (
            MaterialIndent.objects.filter(
                company__in=companies,
                indent_date__gte=window_start,
                indent_date__lte=window_end,
                status__in=MAINTENANCE_COMMITTED_INDENT_STATUSES,
                selected_quotation__isnull=False,
                is_active=True,
            )
            .select_related("selected_quotation", "department")
            .prefetch_related("selected_quotation__lines")
        )
        for indent in indents:
            value = _money(indent.selected_quotation.total_amount)
            per_date[indent.indent_date]["cost"] += value
            if indent.indent_date in focus and value:
                items.append(
                    {
                        "label": indent.indent_no or f"Indent #{indent.pk}",
                        "kind": "Indent",
                        "amount": value,
                    }
                )

    items.sort(key=lambda item: item["amount"], reverse=True)
    return per_date, items


# ---------------------------------------------------------------------------
# The board
# ---------------------------------------------------------------------------

def budgets_for_month(companies, on_date):
    """Configured targets keyed by bucket, for the month ``on_date`` falls in.

    Across companies the targets add up: two companies each budgeting a lakh
    for power means the board is measuring against two lakh.
    """
    rows = MonthlyBudget.objects.filter(
        company__in=companies, month=month_start(on_date), is_active=True
    )
    totals = {}
    for row in rows:
        totals[row.bucket] = totals.get(row.bucket, ZERO) + _money(row.amount)
    return totals


def _bucket(today_value, mtd_value, budget, *, unit=None, unit_label=None, warning=None):
    """One tile's worth of numbers, in the shape the board renders."""
    pace = None
    if budget:
        pace = round(float(mtd_value) / float(budget) * 100, 1)
    return {
        "today": today_value,
        "mtd": mtd_value,
        "budget": budget,
        "budget_used_pct": pace,
        "unit": unit,
        "unit_label": unit_label,
        "warning": warning,
    }


def build_board(
    companies,
    date_from: date,
    date_to: date | None = None,
    settings_company=None,
) -> dict:
    """Everything the wall shows for a span of days, in one payload.

    ``companies`` is a list, because the board's normal state is the whole
    factory rather than one legal entity: the plant shares a campus, a gate and
    four electricity meters, so "what did we spend" is a question about all of
    them at once. Pass a single-item list for one company.

    ``settings_company`` decides whose panel layout and refresh interval the
    wall obeys — the company the viewer is signed into. It defaults to the first
    of ``companies``.

    ``date_to`` defaults to ``date_from``, which is the board's normal state: a
    single day, today. Widening the range changes what the headline figure
    means, so the payload says which mode it is in via ``is_single_day`` rather
    than leaving the client to compare two date strings.

    Three windows are in play and none contains the others — the selected span,
    the fortnight of trend context, and the month behind the budget comparison.
    On the 3rd the fortnight reaches back into last month; on the 28th the month
    reaches back past the fortnight. All three are read in a single pass over
    their union, then sliced, so a wide span costs the same number of queries as
    a narrow one.
    """
    date_to = date_to or date_from
    if date_to < date_from:
        date_from, date_to = date_to, date_from

    companies = list(companies)
    if not companies:
        raise ValueError("build_board needs at least one company.")
    settings_row = get_settings(settings_company or companies[0])

    span = _days_between(date_from, date_to)
    # A single-day pick still wants a fortnight of context behind it; a longer
    # pick draws itself, capped so a year-long range is not 365 unreadable bars.
    trend_dates = (
        span
        if len(span) >= TREND_DAYS
        else [date_to - timedelta(days=offset) for offset in range(TREND_DAYS - 1, -1, -1)]
    )
    if len(trend_dates) > MAX_TREND_DAYS:
        trend_dates = trend_dates[-MAX_TREND_DAYS:]

    month_first = month_start(date_to)
    mtd_dates = _month_dates(month_first, date_to)
    all_dates = sorted(set(span) | set(trend_dates) | set(mtd_dates))
    focus = set(span)

    budgets = budgets_for_month(companies, date_to)
    warnings = []

    # --- labour ----------------------------------------------------------
    labour_per_date, labour_departments, labour_contractors, unpriced = labour_costs(
        companies, all_dates, focus
    )
    labour_range = sum((labour_per_date[day]["cost"] for day in span), ZERO)
    labour_mtd = sum((labour_per_date[day]["cost"] for day in mtd_dates), ZERO)
    labour_heads = sum(labour_per_date[day]["headcount"] for day in span)
    labour_warning = None
    if unpriced:
        labour_warning = (
            f"{unpriced} labourers have no rate — set '{LABOUR_COST_TYPE_CODE}' "
            f"in Admin › Cost Master."
        )
        warnings.append(labour_warning)

    # --- salary ----------------------------------------------------------
    # Priced from the span's end, so a range crossing a rate change is valued at
    # the rate in force when it closed.
    salary = salary_costs(companies, date_to)
    salary_range = _money(salary["daily"] * len(span))
    salary_warning = None
    if not salary["configured"]:
        salary_warning = (
            f"No '{SALARY_COST_TYPE_CODE}' rate in force for {date_to:%B %Y} "
            f"— set one in Admin › Cost Master."
        )
        warnings.append(salary_warning)

    # --- electricity -----------------------------------------------------
    electricity_per_date, meters = electricity_costs(
        companies, all_dates, settings_row, focus
    )
    electricity_range = sum((electricity_per_date[day]["cost"] for day in span), ZERO)
    electricity_mtd = sum((electricity_per_date[day]["cost"] for day in mtd_dates), ZERO)
    electricity_units = sum((electricity_per_date[day]["units"] for day in span), ZERO)
    electricity_warning = None
    if not electricity_units:
        electricity_warning = (
            "No meter reading entered "
            + ("today" if len(span) == 1 else "in this range")
            + " — Maintenance › Daily Electricity."
        )
        warnings.append(electricity_warning)

    # --- maintenance -----------------------------------------------------
    maintenance_per_date, maintenance_items = maintenance_costs(
        companies, all_dates, settings_row, focus
    )
    maintenance_range = sum((maintenance_per_date[day]["cost"] for day in span), ZERO)
    maintenance_mtd = sum((maintenance_per_date[day]["cost"] for day in mtd_dates), ZERO)

    trend = [
        {
            "date": day.isoformat(),
            "is_today": day == date_to,
            "in_range": date_from <= day <= date_to,
            "labour": labour_per_date[day]["cost"],
            "salary": salary["daily"],
            "electricity": electricity_per_date[day]["cost"],
            "maintenance": maintenance_per_date[day]["cost"],
            "total": (
                labour_per_date[day]["cost"]
                + salary["daily"]
                + electricity_per_date[day]["cost"]
                + maintenance_per_date[day]["cost"]
            ),
            "headcount": labour_per_date[day]["headcount"],
            "units": electricity_per_date[day]["units"],
        }
        for day in trend_dates
    ]

    range_total = labour_range + salary_range + electricity_range + maintenance_range
    mtd_total = labour_mtd + salary["mtd"] + electricity_mtd + maintenance_mtd
    budget_total = sum(budgets.values(), ZERO) or None

    return {
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "days": len(span),
        "is_single_day": len(span) == 1,
        "month": month_first.isoformat(),
        "company_codes": [item.code for item in companies],
        "company_count": len(companies),
        "company_code": companies[0].code if len(companies) == 1 else "All companies",
        "settings": {
            "show_labour": settings_row.show_labour,
            "show_salary": settings_row.show_salary,
            "show_electricity": settings_row.show_electricity,
            "show_maintenance": settings_row.show_maintenance,
            "refresh_seconds": settings_row.refresh_seconds,
            "rotate_seconds": settings_row.rotate_seconds,
        },
        "buckets": {
            ExpenseBucket.LABOUR: _bucket(
                _money(labour_range),
                _money(labour_mtd),
                budgets.get(ExpenseBucket.LABOUR),
                unit=labour_heads,
                unit_label="labourers in",
                warning=labour_warning,
            ),
            ExpenseBucket.SALARY: _bucket(
                salary_range,
                salary["mtd"],
                budgets.get(ExpenseBucket.SALARY),
                unit=len(salary["departments"]) or None,
                unit_label="departments",
                warning=salary_warning,
            ),
            ExpenseBucket.ELECTRICITY: _bucket(
                _money(electricity_range),
                _money(electricity_mtd),
                budgets.get(ExpenseBucket.ELECTRICITY),
                unit=electricity_units,
                unit_label="units",
                warning=electricity_warning,
            ),
            ExpenseBucket.MAINTENANCE: _bucket(
                _money(maintenance_range),
                _money(maintenance_mtd),
                budgets.get(ExpenseBucket.MAINTENANCE),
                unit=len(maintenance_items) or None,
                unit_label="entries",
            ),
        },
        "total": {
            "today": _money(range_total),
            "mtd": _money(mtd_total),
            "per_day": _money(range_total / len(span)),
            "budget": budget_total,
            "budget_used_pct": (
                round(float(mtd_total) / float(budget_total) * 100, 1)
                if budget_total
                else None
            ),
        },
        "trend": trend,
        "labour_departments": _as_rows(labour_departments, "department"),
        "labour_contractors": _as_rows(labour_contractors, "contractor"),
        "salary_departments": salary["departments"],
        "meters": _as_rows(meters, "meter"),
        "maintenance_items": maintenance_items,
        "warnings": warnings,
    }


def _days_between(start: date, end: date):
    """Every day from ``start`` to ``end`` inclusive."""
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]


def _month_dates(month_first: date, on_date: date):
    span = (on_date - month_first).days
    return [month_first + timedelta(days=offset) for offset in range(span + 1)]


def _as_rows(mapping, key_name):
    rows = [{key_name: name, **values} for name, values in mapping.items()] if mapping else []
    rows.sort(key=lambda row: row.get("cost", ZERO), reverse=True)
    return rows
