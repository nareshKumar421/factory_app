"""
board_builder/cards/gate.py

What the gate knows: how long a truck is inside, and which ones still are.

THE TIMEZONE TRAP, WHICH IS THE WHOLE REASON THIS MODULE HAS A DOCSTRING
-------------------------------------------------------------------------
``VehicleArrival`` records the same departure twice. ``departed_at`` is a
timezone-aware ``DateTimeField``; ``gate_out_date`` + ``out_time`` are a naive
local date and a naive local time. The gate-IN pair is naive too.

Subtract the naive gate-in from the aware ``departed_at`` -- the obvious way,
and the way anybody writing this query for the first time will write it -- and
the answer is shifted by the UTC offset. Measured on production: a median of
MINUS 3.2 hours across 1,573 departed arrivals. Negative, and therefore
noticed. A deployment closer to UTC would have produced a plausible wrong
number instead, which is the version of this bug that reaches a wall screen.

So every duration here is computed from ``gate_out_date`` + ``out_time`` less
``gate_in_date`` + ``in_time``: two naive local stamps, one subtraction, no
offset anywhere. The same rows then give a 2.34h median and no negatives.

WHY THESE CARDS ARE NOT COMPANY-FILTERED
-----------------------------------------
An arrival is one physical truck trip and is deliberately not company-scoped --
a single truck carries bills for several companies, and the per-company
dockings hang off it. "How long is a truck at our gate" is a fact about the
gate, not about a company, and filtering it by the viewer's company switcher
would report a fraction of the queue as the whole of it. Each card says so on
its face rather than quietly reporting less than it appears to.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from statistics import median

from django.utils import timezone

from ..catalogue import CardContext, CardOption, CardSpec, register
from .. import viz

#: How far back the turnaround cards look, in days. Offered rather than fixed
#: because the same card answers two questions: a week is "how are we doing
#: this week", a quarter is "what is normal".
WINDOW = CardOption(
    key="window_days",
    label="Window",
    kind="choice",
    default="30",
    choices=(("7", "Last 7 days"), ("30", "Last 30 days"), ("90", "Last 90 days")),
    help="How far back the figures are computed over.",
)

WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _hours(arrival) -> float | None:
    """One arrival's time on site, in hours, or ``None`` if it cannot be known.

    Both stamps naive and local; see the module docstring. A negative result is
    dropped rather than returned: it means the two pairs disagree about which
    day the truck left, which happens on a record edited by hand, and a
    negative turnaround in a median drags it somewhere no truck has ever been.
    """
    if not (arrival.gate_out_date and arrival.out_time):
        return None
    left = datetime.combine(arrival.gate_out_date, arrival.out_time)
    came = datetime.combine(arrival.gate_in_date, arrival.in_time)
    delta = (left - came).total_seconds() / 3600
    return delta if delta >= 0 else None


def _quantile(values: list[float], fraction: float) -> float:
    """The value at ``fraction`` through a sorted list. Nearest-rank.

    Not interpolated, and not ``statistics.quantiles``: this is regularly
    handed six values, and the interpolated p90 of six numbers is an invented
    figure sitting between two real ones. Nearest-rank always returns a
    turnaround some truck actually had.
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(fraction * len(ordered)) - 1))
    return ordered[index]


def _turnaround(context: CardContext) -> dict:
    from gate_core.models.vehicle_arrival import VehicleArrival, VehicleArrivalStatus

    days = int(context.option("window_days", "30"))
    since = timezone.localdate() - timedelta(days=days)

    arrivals = VehicleArrival.objects.filter(
        is_active=True,
        status=VehicleArrivalStatus.DEPARTED,
        gate_in_date__gte=since,
        gate_out_date__isnull=False,
        out_time__isnull=False,
    ).only("gate_in_date", "in_time", "gate_out_date", "out_time")

    measured: list[tuple[date, float]] = []
    for arrival in arrivals.iterator(chunk_size=2000):
        hours = _hours(arrival)
        if hours is not None:
            measured.append((arrival.gate_in_date, hours))

    if not measured:
        return viz.missing(
            f"No truck completed a trip in the last {days} days.",
            sub="Gate to gate, every company",
        )

    values = [hours for _, hours in measured]
    mid = median(values)
    p90 = _quantile(values, 0.90)

    # Median per weekday, on the same scale, so the shape answers "which day is
    # the dock queue". Mean was rejected: one 89-day arrival would make a
    # Tuesday look like a crisis that never happened.
    by_weekday: dict[int, list[float]] = {index: [] for index in range(7)}
    for day, hours in measured:
        by_weekday[day.weekday()].append(hours)

    today = timezone.localdate().weekday()
    columns = [
        (WEEKDAYS[index], median(by_weekday[index]) if by_weekday[index] else 0.0, index == today)
        for index in range(7)
    ]

    return viz.figure(
        value=f"{mid:.2f}",
        unit="h median",
        sub=f"{len(values):,} trips · last {days} days · every company",
        tag=viz.tag(f"p90 {p90:.2f} h", "warn" if p90 >= 6 else "neut"),
        viz=viz.bars(columns),
        note="Median hours on site by weekday. Today is picked out.",
    )


def _on_site(context: CardContext) -> dict:
    from gate_core.models.vehicle_arrival import VehicleArrival, VehicleArrivalStatus

    open_arrivals = list(
        VehicleArrival.objects.filter(
            is_active=True,
            status__in=(VehicleArrivalStatus.INSIDE, VehicleArrivalStatus.LOADING),
        ).only("gate_in_date", "in_time", "status")
    )

    if not open_arrivals:
        return viz.figure(
            value="0",
            unit="on site",
            sub="Nothing open at the gate",
            viz=viz.nothing(),
        )

    today = timezone.localdate()
    ages = [(today - arrival.gate_in_date).days for arrival in open_arrivals]
    oldest = max(ages)
    inside = sum(1 for a in open_arrivals if a.status == VehicleArrivalStatus.INSIDE)
    loading = len(open_arrivals) - inside

    # A truck that has been inside for days is not a truck, it is a gate-in
    # nobody retired -- the condition this card exists to make impossible to
    # ignore. Two days is the threshold because a legitimate overnight load
    # spans one.
    stale = sum(1 for age in ages if age >= 2)
    tone = "bad" if oldest >= 7 else "warn" if stale else "ok"

    return viz.figure(
        value=str(len(open_arrivals)),
        unit="on site",
        sub=f"Oldest {oldest} day{'s' if oldest != 1 else ''} · every company",
        tag=viz.tag(f"{stale} stale" if stale else "all fresh", tone),
        viz=viz.split(
            [
                ("Inside", str(inside)),
                ("Loading", str(loading)),
                ("2 days+", str(stale)),
            ]
        ),
    )


register(
    CardSpec(
        key="gate_truck_turnaround",
        title="Truck turnaround",
        summary="How long a truck is on site, gate to gate, and which weekday is worst.",
        category="Gate",
        feed="gate",
        columns=2,
        rows=1,
        accent="transport",
        options=(WINDOW,),
        build=_turnaround,
        note=(
            "Gate-in to gate-out for trips that completed in the window, from "
            "the local date/time pairs rather than departed_at -- the two are "
            "recorded in different timezones and subtracting across them gives "
            "a negative answer. Not filtered by company: one truck carries "
            "several companies' bills."
        ),
    )
)

register(
    CardSpec(
        key="gate_trucks_on_site",
        title="Trucks on site",
        summary="Open arrivals right now, and how long the oldest has been in.",
        category="Gate",
        feed="gate",
        columns=1,
        rows=1,
        accent="transport",
        build=_on_site,
        note=(
            "Counts arrivals still INSIDE or LOADING. An arrival open for days "
            "is almost always a gate-in nobody retired rather than a truck "
            "still here, which is what the stale count is for."
        ),
    )
)
