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

Shift hours
-----------
The meters on the Terrace — the chillers and what they serve — run for the
shift, not for the minutes a line was actually filling. A stop on the line
does not stop the chiller. So from 1 Oct 2026 a setup can say how long a stop
may be and still count as running (``short_stop_hours`` on the version). A
setup with it set counts a company's **shift hours** on the meter
(:func:`shift_hours`):

* every running segment of the company's lines and machines on that meter,
  merged — the company is on while any of them runs, so two lines running side
  by side count once;
* every stop between two running stretches shorter than the cut-off — an
  unlogged pause, lunch, the changeover to the next run;
* every **logged** breakdown between two running stretches, however long.
  Breakdowns are never subtracted.

A stop longer than the cut-off that nobody logged is the line switched off —
overnight, or a run left paused for days — and does not count. Nor does a
breakdown after the company's last stop: one left open with nobody restarting
the line would otherwise count as running for a day.

A setup without a cut-off keeps the rule above: running segments only, each
line on its own. September 2026's versions are left that way.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from django.db.models import Q
from django.utils import timezone

#: The most a single segment may count for. A shift and its overtime fit well
#: inside a day; a segment claiming more is a stop somebody forgot to press.
MAX_SEGMENT_HOURS = 24

SECONDS_PER_HOUR = Decimal("3600")

Interval = Tuple[datetime, datetime]


@dataclass
class SourceIntervals:
    """What one line or machine did: when it ran, and its logged breakdowns."""

    running: List[Interval] = field(default_factory=list)
    stopped: List[Interval] = field(default_factory=list)


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


def days_between(date_from: date, date_to: date) -> List[date]:
    return [date_from + timedelta(days=n) for n in range((date_to - date_from).days + 1)]


def load_intervals(
    sources: Iterable[str],
    date_from: date,
    date_to: date,
    *,
    now: Optional[datetime] = None,
) -> Dict[str, SourceIntervals]:
    """Running segments and logged breakdowns of ``"line:<id>"`` and
    ``"blowing:<id>"`` sources, for whatever could touch the span.

    Reaches a day past the span's end as well, so a stop on its last evening
    can be bridged to the restart the next morning.
    """
    from blowing.models import BlowingBreakdown, BlowingSegment
    from production_execution.models import MachineBreakdown, ProductionSegment

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
    latest = window_end + timedelta(hours=MAX_SEGMENT_HOURS)
    overlaps = Q(end_time__gt=earliest) | Q(end_time__isnull=True)

    found: Dict[str, SourceIntervals] = defaultdict(SourceIntervals)

    def add(prefix, rows, attr):
        for source_id, start, end in rows:
            interval = capped(start, end, now)
            if interval:
                getattr(found[f"{prefix}:{source_id}"], attr).append(interval)

    if line_ids:
        # The join bypasses the run's live-only manager, so deleted runs are
        # dropped here by hand.
        in_window = dict(
            production_run__line_id__in=line_ids,
            production_run__is_deleted=False,
            start_time__gte=earliest,
            start_time__lt=latest,
        )
        add(
            "line",
            ProductionSegment.objects.filter(**in_window)
            .filter(overlaps)
            .values_list("production_run__line_id", "start_time", "end_time"),
            "running",
        )
        add(
            "line",
            MachineBreakdown.objects.filter(**in_window)
            .filter(overlaps)
            .values_list("production_run__line_id", "start_time", "end_time"),
            "stopped",
        )
    if machine_ids:
        in_window = dict(
            blowing_run__machine_id__in=machine_ids,
            blowing_run__is_active=True,
            start_time__gte=earliest,
            start_time__lt=latest,
        )
        add(
            "blowing",
            BlowingSegment.objects.filter(**in_window)
            .filter(overlaps)
            .values_list("blowing_run__machine_id", "start_time", "end_time"),
            "running",
        )
        add(
            "blowing",
            BlowingBreakdown.objects.filter(**in_window)
            .filter(overlaps)
            .values_list("blowing_run__machine_id", "start_time", "end_time"),
            "stopped",
        )
    return dict(found)


def segment_hours(
    intervals: Mapping[str, SourceIntervals], days: Sequence[date], tz
) -> Dict[Tuple[str, date], Decimal]:
    """``{(source, day): hours}`` of running segments only, each source on its own."""
    result: Dict[Tuple[str, date], Decimal] = {}
    for source, found in intervals.items():
        for day, hours in hours_by_day(found.running, days, tz).items():
            result[(source, day)] = hours
    return result


def overlap_seconds(gap: Interval, intervals: Iterable[Interval]) -> float:
    """How much of ``gap`` the (merged) ``intervals`` cover."""
    seconds = 0.0
    for start, end in intervals:
        low, high = max(start, gap[0]), min(end, gap[1])
        if high > low:
            seconds += (high - low).total_seconds()
    return seconds


def on_intervals(found: Iterable[SourceIntervals], short_stop: timedelta) -> List[Interval]:
    """When a group of lines and machines was on, counting stops as running.

    See *Shift hours* above. A stop between two running stretches counts in
    full when what nobody logged of it is shorter than ``short_stop``;
    otherwise only its logged breakdowns count.
    """
    found = list(found)
    running = merge(interval for item in found for interval in item.running)
    stopped = merge(interval for item in found for interval in item.stopped)
    counted: List[Interval] = list(running)
    for (_, gap_start), (gap_end, _) in zip(running, running[1:]):
        gap = (gap_start, gap_end)
        logged = overlap_seconds(gap, stopped)
        unlogged = (gap_end - gap_start).total_seconds() - logged
        if unlogged < short_stop.total_seconds():
            counted.append(gap)
            continue
        for start, end in stopped:
            low, high = max(start, gap_start), min(end, gap_end)
            if high > low:
                counted.append((low, high))
    return merge(counted)


def shift_hours(
    found: Iterable[SourceIntervals], short_stop: timedelta, days: Sequence[date], tz
) -> Dict[date, Decimal]:
    """Hours on each of ``days`` a group of lines and machines was on."""
    return hours_by_day(on_intervals(found, short_stop), days, tz)

