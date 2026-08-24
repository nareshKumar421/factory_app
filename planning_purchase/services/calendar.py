"""Turning a monthly plan into day, week and month buckets.

SAP stores the plan as one lump per item, dated the period start — the August
2026 plan is 98 lines all dated 2026-08-01. It carries no daily or weekly detail,
so any daily number this module shows is **derived**, and every bucket says so.
That flag is not decoration: a spread figure is a suggestion, and a planner who
mistakes it for a commitment ends up defending a number no human ever set.

Two policies:

``PERIOD_START``
    The whole quantity stays on the date SAP gave it. Invents nothing, and is the
    honest answer when nobody has agreed a daily target.

``EVEN_WORKING_DAYS`` (default)
    Spread across the working days of the plan period. This is the split the
    factory actually asked for, and the default because a monthly lump cannot be
    compared against day-wise production at all.

Whichever runs, the sum of the DAY buckets equals the sum of the WEEK buckets
equals the sum of the MONTH buckets equals the planned quantity, exactly. That is
enforced by distributing the rounding remainder rather than rounding each bucket
independently, and it is the property that stops a five-week month quietly losing
three days of plan.
"""

from __future__ import annotations

from calendar import monthrange
from datetime import date, timedelta
from decimal import Decimal
from typing import Dict, List, Sequence

from django.conf import settings

DAY = "DAY"
WEEK = "WEEK"
MONTH = "MONTH"
BUCKET_TYPES = (DAY, WEEK, MONTH)

POLICY_PERIOD_START = "PERIOD_START"
POLICY_EVEN_WORKING_DAYS = "EVEN_WORKING_DAYS"
SPREAD_POLICIES = (POLICY_PERIOD_START, POLICY_EVEN_WORKING_DAYS)

# Weekday numbers Python's date.weekday() uses: Monday 0 … Sunday 6.
# Sunday off by default, which is how this factory runs; override in settings
# rather than in code so a change of shift pattern is a config change.
DEFAULT_NON_WORKING_WEEKDAYS = (6,)
DEFAULT_WEEK_START = 0  # Monday


def _non_working_weekdays() -> frozenset:
    configured = getattr(
        settings, "PLANNING_NON_WORKING_WEEKDAYS", DEFAULT_NON_WORKING_WEEKDAYS
    )
    return frozenset(int(d) for d in configured)


def _week_start_day() -> int:
    return int(getattr(settings, "PLANNING_WEEK_START_DAY", DEFAULT_WEEK_START))


def working_days(period_start: date, period_end: date) -> List[date]:
    """Every working day in the inclusive range.

    Falls back to *all* days when the range contains no working day at all — a
    single-Sunday period would otherwise divide by zero and lose the quantity
    entirely, and losing plan is worse than putting it on a day off.
    """
    if period_end < period_start:
        period_start, period_end = period_end, period_start

    off = _non_working_weekdays()
    days = []
    cursor = period_start
    while cursor <= period_end:
        if cursor.weekday() not in off:
            days.append(cursor)
        cursor += timedelta(days=1)

    if days:
        return days

    cursor, days = period_start, []
    while cursor <= period_end:
        days.append(cursor)
        cursor += timedelta(days=1)
    return days


def week_start(day: date) -> date:
    """First day of the week `day` falls in, honouring the configured week start."""
    shift = (day.weekday() - _week_start_day()) % 7
    return day - timedelta(days=shift)


def month_start(day: date) -> date:
    return day.replace(day=1)


def month_end(day: date) -> date:
    return day.replace(day=monthrange(day.year, day.month)[1])


def spread_even(total: Decimal, days: Sequence[date]) -> Dict[date, Decimal]:
    """Split `total` across `days` so the parts sum back to exactly `total`.

    Each day gets the quantised share; the rounding remainder goes to the
    earliest days one unit at a time. Rounding each day independently and hoping
    would leave the month total adrift from the plan, which is the one error a
    planner will always spot and never forgive.
    """
    if not days:
        return {}

    quantum = Decimal("0.001")
    count = len(days)
    base = (total / count).quantize(quantum)
    allocation = {day: base for day in days}

    drift = total - (base * count)
    if drift:
        step = quantum if drift > 0 else -quantum
        remaining = int(abs(drift / quantum).to_integral_value())
        for index in range(remaining):
            allocation[days[index % count]] += step

    return allocation


def build_buckets(
    planned_qty: Decimal,
    bucket_date: date,
    period_start: date,
    period_end: date,
    policy: str = POLICY_EVEN_WORKING_DAYS,
) -> Dict[str, List[dict]]:
    """Day, week and month buckets for one plan line.

    `bucket_date` is the date SAP put on the line. Under ``PERIOD_START`` that
    single date holds the whole quantity and nothing is derived; under
    ``EVEN_WORKING_DAYS`` the quantity is spread across the period's working days
    and every bucket is marked derived.

    Week and month buckets are always summed from the day buckets rather than
    computed separately, which is what guarantees the three grains agree.
    """
    planned_qty = Decimal(planned_qty or 0)

    if policy == POLICY_PERIOD_START or period_end <= period_start:
        daily = {bucket_date: planned_qty}
        derived = False
    else:
        daily = spread_even(planned_qty, working_days(period_start, period_end))
        derived = True

    day_rows = [
        {
            "bucket_type": DAY,
            "bucket_start": day,
            "planned_qty": qty,
            "derived": derived,
            "spread_policy": policy,
        }
        for day, qty in sorted(daily.items())
    ]

    return {
        DAY: day_rows,
        WEEK: _roll_up(daily, week_start, WEEK, derived, policy),
        MONTH: _roll_up(daily, month_start, MONTH, derived, policy),
    }


def _roll_up(daily, key_fn, bucket_type, derived, policy) -> List[dict]:
    totals: Dict[date, Decimal] = {}
    for day, qty in daily.items():
        key = key_fn(day)
        totals[key] = totals.get(key, Decimal(0)) + qty

    return [
        {
            "bucket_type": bucket_type,
            "bucket_start": start,
            "planned_qty": qty,
            # A month bucket built from a single stated date is not derived; one
            # built from a spread is, because its shape came from the policy.
            "derived": derived and bucket_type != MONTH,
            "spread_policy": policy,
        }
        for start, qty in sorted(totals.items())
    ]


def bucket_label(bucket_type: str, start: date) -> str:
    if bucket_type == DAY:
        return start.strftime("%d %b %Y")
    if bucket_type == WEEK:
        return f"{start.strftime('%d %b')} - {(start + timedelta(days=6)).strftime('%d %b %Y')}"
    return start.strftime("%b %Y")
