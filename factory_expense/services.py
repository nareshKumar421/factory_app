"""
factory_expense/services.py

Builds the wall board from FactoryFlow's own registers. No SAP.

Where each number comes from:

* **Labour**      ``labour_gate.LabourGateEntry.count_in`` — how many labourers a
  contractor actually walked through the gate — priced at the configured
  per-person-per-day rate.
* **Salary**      ``DepartmentSalaryConfig``, spread evenly across the month's
  days so a part-month view is an accrual, not the whole bill on the 1st.
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
    MAINTENANCE_COMMITTED_INDENT_STATUSES,
    MAINTENANCE_SPEND_MOVEMENTS,
    TREND_DAYS,
    ExpenseBucket,
    RateShift,
)
from .models import (
    DepartmentSalaryConfig,
    FactoryExpenseSettings,
    LabourRateConfig,
    MonthlyBudget,
    month_start,
)

ZERO = Decimal("0.00")


def _money(value) -> Decimal:
    return (Decimal(value or 0)).quantize(Decimal("0.01"))


def get_settings(company) -> FactoryExpenseSettings:
    """The company's board settings, created with defaults on first read."""
    settings_row, _ = FactoryExpenseSettings.objects.get_or_create(company=company)
    return settings_row


# ---------------------------------------------------------------------------
# Labour
# ---------------------------------------------------------------------------

def resolve_rate(rates, department_id, shift, work_date) -> LabourRateConfig | None:
    """The rate row that governs one gate entry.

    Most specific wins: department + shift, then shift alone, then the
    company-wide ``ANY`` row. Ties break on the latest ``effective_from`` on or
    before ``work_date``. Returns None when nothing has been configured yet,
    which the caller reports rather than silently pricing at zero.
    """
    best = None
    for rate in rates:
        if rate.effective_from > work_date:
            continue
        if rate.department_id is not None and rate.department_id != department_id:
            continue
        if rate.shift != RateShift.ANY and rate.shift != shift:
            continue
        if best is None:
            best = rate
            continue
        if (rate.specificity, rate.effective_from) > (best.specificity, best.effective_from):
            best = rate
    return best


def labour_costs(company, dates):
    """Gate headcount priced per day.

    Returns ``(per_date, departments, contractors, unpriced_headcount)`` where
    ``per_date`` maps a date to ``{"cost", "headcount"}`` and the two breakdowns
    cover the last date only — the wall shows today's split, not a fortnight's.
    """
    entries = list(
        LabourGateEntry.objects.filter(
            company=company, work_date__in=dates, is_active=True
        ).select_related("department", "contractor")
    )
    rates = list(LabourRateConfig.objects.filter(company=company, is_active=True))

    per_date = {day: {"cost": ZERO, "headcount": 0} for day in dates}
    departments = defaultdict(lambda: {"headcount": 0, "cost": ZERO})
    contractors = defaultdict(lambda: {"headcount": 0, "cost": ZERO})
    unpriced = 0
    today = max(dates)

    for entry in entries:
        rate = resolve_rate(rates, entry.department_id, entry.shift, entry.work_date)
        headcount = entry.count_in or 0
        cost = _money(headcount * rate.rate_per_person_per_day) if rate else ZERO
        if rate is None and headcount:
            unpriced += headcount

        bucket = per_date[entry.work_date]
        bucket["cost"] += cost
        bucket["headcount"] += headcount

        if entry.work_date == today:
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

def salary_costs(company, on_date):
    """The month's department-wise salary, and what one day of it accrues to."""
    month = month_start(on_date)
    days_in_month = calendar.monthrange(on_date.year, on_date.month)[1]

    rows = list(
        DepartmentSalaryConfig.objects.filter(
            company=company, month=month, is_active=True
        ).select_related("department")
    )

    departments = []
    monthly_total = ZERO
    employee_total = 0
    for row in rows:
        daily = _money(row.monthly_amount / days_in_month)
        monthly_total += row.monthly_amount
        employee_total += row.employee_count
        departments.append(
            {
                "department": row.department.name,
                "department_id": row.department_id,
                "employees": row.employee_count,
                "monthly": _money(row.monthly_amount),
                "daily": daily,
                "per_employee": (
                    _money(row.monthly_amount / row.employee_count)
                    if row.employee_count
                    else None
                ),
            }
        )

    departments.sort(key=lambda item: item["monthly"], reverse=True)
    daily_total = _money(monthly_total / days_in_month)
    return {
        "departments": departments,
        "monthly": _money(monthly_total),
        "daily": daily_total,
        "mtd": _money(daily_total * on_date.day),
        "employees": employee_total,
        "days_in_month": days_in_month,
        "configured": bool(rows),
    }


# ---------------------------------------------------------------------------
# Electricity
# ---------------------------------------------------------------------------

def electricity_costs(company, dates, settings_row):
    """Units and cost from the Daily Electricity register."""
    readings = DailyElectricityReading.objects.filter(date__in=dates, is_active=True)
    if settings_row.electricity_only_company_meters:
        readings = readings.filter(meter__companies=company)
    readings = readings.select_related("meter").distinct()

    per_date = {day: {"cost": ZERO, "units": ZERO} for day in dates}
    meters = defaultdict(lambda: {"units": ZERO, "cost": ZERO, "rate": ZERO})
    today = max(dates)

    for reading in readings:
        bucket = per_date[reading.date]
        bucket["cost"] += reading.total_cost or ZERO
        bucket["units"] += reading.units_consumed or ZERO
        if reading.date == today:
            name = reading.meter.name
            meters[name]["units"] += reading.units_consumed or ZERO
            meters[name]["cost"] += reading.total_cost or ZERO
            meters[name]["rate"] = reading.rate_per_unit or ZERO

    return per_date, meters


# ---------------------------------------------------------------------------
# Maintenance
# ---------------------------------------------------------------------------

def maintenance_costs(company, dates, settings_row):
    """Spares consumed and indents committed, per day, plus today's line items."""
    per_date = {day: {"cost": ZERO} for day in dates}
    items = []
    today = max(dates)
    window_start, window_end = min(dates), max(dates)

    if settings_row.maintenance_include_spares:
        movements = (
            SpareMovement.objects.filter(
                company=company,
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
            if moved_on == today and value:
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
                company=company,
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
            if indent.indent_date == today and value:
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

def budgets_for_month(company, on_date):
    """Configured targets keyed by bucket, for the month ``on_date`` falls in."""
    rows = MonthlyBudget.objects.filter(
        company=company, month=month_start(on_date), is_active=True
    )
    return {row.bucket: _money(row.amount) for row in rows}


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


def build_board(company, on_date: date) -> dict:
    """Everything the wall shows for one day, in one payload.

    The trend window and the month-to-date window overlap but neither contains
    the other — on the 3rd the fortnight reaches back into last month, and on
    the 28th the month reaches back past the fortnight. Both are read in a
    single pass over their union, then sliced, so a wide month costs the same
    number of queries as a narrow one.
    """
    settings_row = get_settings(company)
    dates = [on_date - timedelta(days=offset) for offset in range(TREND_DAYS - 1, -1, -1)]
    month_first = month_start(on_date)
    mtd_dates = _month_dates(month_first, on_date)
    all_dates = sorted(set(dates) | set(mtd_dates))

    budgets = budgets_for_month(company, on_date)
    warnings = []

    # --- labour ----------------------------------------------------------
    labour_per_date, labour_departments, labour_contractors, unpriced = labour_costs(
        company, all_dates
    )
    labour_mtd = sum((labour_per_date[day]["cost"] for day in mtd_dates), ZERO)
    labour_warning = None
    if unpriced:
        labour_warning = f"{unpriced} labourers have no rate configured — set one in Configuration."
        warnings.append(labour_warning)

    # --- salary ----------------------------------------------------------
    salary = salary_costs(company, on_date)
    salary_warning = None
    if not salary["configured"]:
        salary_warning = f"No department salary set for {on_date:%B %Y}."
        warnings.append(salary_warning)

    # --- electricity -----------------------------------------------------
    electricity_per_date, meters = electricity_costs(company, all_dates, settings_row)
    electricity_mtd = sum((electricity_per_date[day]["cost"] for day in mtd_dates), ZERO)
    electricity_warning = None
    if not electricity_per_date[on_date]["units"]:
        electricity_warning = "No meter reading entered today — Maintenance › Daily Electricity."
        warnings.append(electricity_warning)

    # --- maintenance -----------------------------------------------------
    maintenance_per_date, maintenance_items = maintenance_costs(
        company, all_dates, settings_row
    )
    maintenance_mtd = sum((maintenance_per_date[day]["cost"] for day in mtd_dates), ZERO)

    trend = [
        {
            "date": day.isoformat(),
            "is_today": day == on_date,
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
        for day in dates
    ]

    today_total = trend[-1]["total"]
    mtd_total = labour_mtd + salary["mtd"] + electricity_mtd + maintenance_mtd
    budget_total = sum(budgets.values(), ZERO) or None

    return {
        "date": on_date.isoformat(),
        "month": month_first.isoformat(),
        "company_code": company.code,
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
                labour_per_date[on_date]["cost"],
                _money(labour_mtd),
                budgets.get(ExpenseBucket.LABOUR),
                unit=labour_per_date[on_date]["headcount"],
                unit_label="labourers in",
                warning=labour_warning,
            ),
            ExpenseBucket.SALARY: _bucket(
                salary["daily"],
                salary["mtd"],
                budgets.get(ExpenseBucket.SALARY),
                unit=salary["employees"] or None,
                unit_label="employees",
                warning=salary_warning,
            ),
            ExpenseBucket.ELECTRICITY: _bucket(
                electricity_per_date[on_date]["cost"],
                _money(electricity_mtd),
                budgets.get(ExpenseBucket.ELECTRICITY),
                unit=electricity_per_date[on_date]["units"],
                unit_label="units",
                warning=electricity_warning,
            ),
            ExpenseBucket.MAINTENANCE: _bucket(
                maintenance_per_date[on_date]["cost"],
                _money(maintenance_mtd),
                budgets.get(ExpenseBucket.MAINTENANCE),
                unit=len(maintenance_items) or None,
                unit_label="entries today",
            ),
        },
        "total": {
            "today": _money(today_total),
            "mtd": _money(mtd_total),
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


def _month_dates(month_first: date, on_date: date):
    span = (on_date - month_first).days
    return [month_first + timedelta(days=offset) for offset in range(span + 1)]


def _as_rows(mapping, key_name):
    rows = [{key_name: name, **values} for name, values in mapping.items()] if mapping else []
    rows.sort(key=lambda row: row.get("cost", ZERO), reverse=True)
    return rows
