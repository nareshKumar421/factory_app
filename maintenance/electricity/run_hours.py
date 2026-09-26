"""How long each production line and blowing machine ran, per calendar day.

A ``RUN_HOURS`` split needs "hours line L ran on day D". Nothing in the product
answered that before, and the obvious answers are wrong:

* **Not the run's date or its total.** A run is dated when it was planned, and
  a night shift runs on past midnight — blowing runs cross it by design. The
  total also leaves out a segment still running.
* **Not the run's status.** A run ran if it has segments; plenty of real runs
  were never marked complete (Beverages' Sidel runs of late August sit in
  progress to this day). Filtering on COMPLETED would put nearly every shared
  unit on Oil.

So hours are read from the **segments** — the stretches the machine actually ran
between a start and a stop (breakdowns are already carved out of them) — clipped
to each calendar day in the factory's time zone.

Three rules keep bad rows from swamping a meter:

* Segments of one source are **merged** before they are added up. Two runs on
  the same line can overlap, and the line did not run twice as long for it.
* A segment counts for at most :data:`MAX_SEGMENT_HOURS` from its start. A few
  "closed" segments span weeks, and one left open since July would otherwise
  count as running every day since.
* An open segment runs until now, and never past the cap above.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from django.db.models import Q
from django.utils import timezone

#: The most a single segment may count for. A shift and its overtime fit well
#: inside a day; a segment claiming more is a stop somebody forgot to press.
MAX_SEGMENT_HOURS = 24

SECONDS_PER_HOUR = Decimal("3600")

Interval = Tuple[datetime, datetime]


def day_bounds(day: date, tz) -> Interval:
    """[midnight, next midnight) of ``day`` in ``tz``, as aware datetimes."""
    start = timezone.make_aware(datetime.combine(day, time.min), tz)
    return start, timezone.make_aware(datetime.combine(day + timedelta(days=1), time.min), tz)


def merge(intervals: Iterable[Interval]) -> List[Interval]:
    """Overlapping or touching intervals joined into one."""
    merged: List[List[datetime]] = []
    for start, end in sorted(intervals):
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged]


def capped(start: datetime, end: Optional[datetime], now: datetime) -> Optional[Interval]:
    """A segment as it may be counted: open ones run to now, none past the cap."""
    limit = start + timedelta(hours=MAX_SEGMENT_HOURS)
    stop = min(end or now, limit, now)
    if stop <= start:
        return None
    return start, stop


def hours_by_day(
    intervals: Iterable[Interval], days: Sequence[date], tz
) -> Dict[date, Decimal]:
    """Hours of the merged ``intervals`` falling on each of ``days``."""
    merged = merge(intervals)
    hours: Dict[date, Decimal] = {}
    for day in days:
        day_start, day_end = day_bounds(day, tz)
        seconds = 0.0
        for start, end in merged:
            if end <= day_start or start >= day_end:
                continue
            seconds += (min(end, day_end) - max(start, day_start)).total_seconds()
        if seconds:
            hours[day] = Decimal(str(round(seconds))) / SECONDS_PER_HOUR
    return hours


def run_hours(
    sources: Iterable[str],
    date_from: date,
    date_to: date,
    *,
    now: Optional[datetime] = None,
) -> Dict[Tuple[str, date], Decimal]:
    """``{(source, day): hours}`` for ``"line:<id>"`` and ``"blowing:<id>"`` sources.

    Days a source did not run are absent rather than zero.
    """
    from blowing.models import BlowingSegment
    from production_execution.models import ProductionSegment

    line_ids, machine_ids = set(), set()
    for source in sources:
        kind, _, raw_id = source.partition(":")
        if not raw_id.isdigit():
            continue
        if kind == "line":
            line_ids.add(int(raw_id))
        elif kind == "blowing":
            machine_ids.add(int(raw_id))
    if not line_ids and not machine_ids:
        return {}

    tz = timezone.get_current_timezone()
    now = now or timezone.now()
    window_start, _ = day_bounds(date_from, tz)
    _, window_end = day_bounds(date_to, tz)
    # A segment that started more than the cap before the window cannot reach
    # into it, whatever its end says.
    earliest = window_start - timedelta(hours=MAX_SEGMENT_HOURS)
    overlaps = Q(end_time__gt=window_start) | Q(end_time__isnull=True)

    segments: Dict[str, List[Interval]] = defaultdict(list)
    if line_ids:
        rows = (
            ProductionSegment.objects.filter(
                production_run__line_id__in=line_ids,
                # The join bypasses the run's live-only manager, so deleted
                # runs are dropped here by hand.
                production_run__is_deleted=False,
                start_time__gte=earliest,
                start_time__lt=window_end,
            )
            .filter(overlaps)
            .values_list("production_run__line_id", "start_time", "end_time")
        )
        for line_id, start, end in rows:
            interval = capped(start, end, now)
            if interval:
                segments[f"line:{line_id}"].append(interval)
    if machine_ids:
        rows = (
            BlowingSegment.objects.filter(
                blowing_run__machine_id__in=machine_ids,
                blowing_run__is_active=True,
                start_time__gte=earliest,
                start_time__lt=window_end,
            )
            .filter(overlaps)
            .values_list("blowing_run__machine_id", "start_time", "end_time")
        )
        for machine_id, start, end in rows:
            interval = capped(start, end, now)
            if interval:
                segments[f"blowing:{machine_id}"].append(interval)

    days = [date_from + timedelta(days=n) for n in range((date_to - date_from).days + 1)]
    result: Dict[Tuple[str, date], Decimal] = {}
    for source, intervals in segments.items():
        for day, hours in hours_by_day(intervals, days, tz).items():
            result[(source, day)] = hours
    return result
