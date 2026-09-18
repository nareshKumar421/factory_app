"""
The punches, read from our own database.

The punching machines write into a SQL Server box (``Biometrics``) on the factory
LAN, and **this server cannot reach it**. An agent running on a Windows machine
inside the plant copies punches across into :class:`~attendance.models.PunchEvent`;
this module is what the roll-up in :mod:`attendance.services` reads instead of the
machines. The agent lives in the companion ``sync/`` repository.

**Why the split, and what stayed where.** The agent is deliberately dumb: it
copies rows and resolves nothing. Every rule about what punches *mean* --
aliases, the weekly off, the half-day threshold -- stays here and in
:mod:`attendance.services`, where it can be re-applied to punches that were
copied across months ago. If the agent resolved aliases before writing, fixing a
wrong alias would only repair punches that arrived after the fix, and the older
ones would need the LAN link that does not exist to be re-pulled.

This module keeps :func:`fetch_punches` signature-compatible with the SQL Server
client it replaces, so :func:`attendance.services.sync_range` changed by one line.

**The one thing that is genuinely different: time zones.** The old client got
naive datetimes out of pymssql -- plant wall-clock time, no offset. Postgres
stores these as ``timestamptz`` and Django hands them back in UTC, where
``.date()`` and ``.time()`` are five and a half hours out. A punch at 02:00 would
land on the previous day. So every punch is converted back to plant-local time
before it leaves this module, and :class:`~attendance.models.PunchEvent` rows are
written by the agent with an explicit IST offset rather than as bare timestamps.
Both halves of that are load-bearing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from django.utils import timezone

from .models import PunchAlias, PunchEvent, PunchSyncRun


@dataclass(frozen=True)
class Punch:
    """One read of one finger, as the machine recorded it.

    ``punched_at`` is **naive plant-local time**, matching what the SQL Server
    client used to return, because everything downstream takes ``.date()`` and
    ``.time()`` off it and means the factory's clock by both.
    """

    employee_code: str
    punched_at: datetime
    device: str


def _local_naive(value):
    """Plant wall-clock time, with the offset stripped.

    Stored punches are timezone-aware (see the module docstring). Converting to
    the current timezone and then dropping the offset reproduces exactly what
    pymssql handed the old client, so ``derive_status`` needs no change.
    """
    if timezone.is_aware(value):
        value = timezone.localtime(value)
        return value.replace(tzinfo=None)
    return value


def alias_map():
    """``{alias code: real JWPL code}`` for people enrolled under a second code.

    A handful of workers were enrolled on a machine under a ``fac####`` code
    instead of their JWPL one. It matters more than its size suggests: three
    people punch *only* under the alias, so without this they punch every day and
    read as absent every day.

    Mirrored from the machine's ``factory_codes`` table by the agent. An empty
    map is survivable and is what an un-mirrored table looks like; the codes that
    need no alias are unaffected.
    """
    return {
        alias.upper(): real.upper()
        for alias, real in PunchAlias.objects.values_list("alias_code", "employee_code")
        if alias and real and alias.upper() != real.upper()
    }


def fetch_punches(date_from: date, date_to: date, *, codes=None):
    """Every stored punch between two dates inclusive, aliases already resolved.

    ``codes`` restricts the pull to a set of employee codes. It is applied in
    Python rather than in the query, and **after** alias resolution: resolving
    second would exclude the three alias-only people by the very filter meant to
    include them.
    """
    if date_from > date_to:
        raise ValueError("date_from is after date_to")

    aliases = alias_map()

    # Inclusive of date_to, in plant-local time: the window runs to the start of
    # the following day, which is also how the old SQL Server query expressed it
    # (`< DATEADD(day, 1, @date_to)`).
    start = timezone.make_aware(datetime.combine(date_from, time.min))
    end = timezone.make_aware(datetime.combine(date_to + timedelta(days=1), time.min))

    rows = (
        PunchEvent.objects.filter(punched_at__gte=start, punched_at__lt=end)
        .order_by("raw_code", "punched_at")
        .values_list("raw_code", "punched_at", "device")
    )

    wanted = {code.upper() for code in codes} if codes is not None else None
    punches = []
    for raw_code, punched_at, device in rows.iterator(chunk_size=5000):
        code = (raw_code or "").strip().upper()
        code = aliases.get(code, code)
        if not code:
            continue
        if wanted is not None and code not in wanted:
            continue
        punches.append(Punch(code, _local_naive(punched_at), (device or "").strip()))
    return punches


def health():
    """Whether the punch data can be trusted right now, for the status endpoint.

    The old client proved this by querying the machines. There is nothing left to
    query, so the question becomes "did the agent run, and did it work?" -- which
    is the question that actually matters: on the sheet, "the factory was shut"
    and "the agent has not run since Tuesday" look identical, and only one of
    them means three hundred people are wrongly marked absent.

    Never raises. Returns the same keys the old client did, so the dashboard's
    contract holds, plus the agent's own freshness.
    """
    from django.conf import settings

    stale_after = timedelta(
        hours=getattr(settings, "ATTENDANCE_SYNC_STALE_HOURS", 36)
    )

    run = PunchSyncRun.objects.order_by("-started_at", "-id").first()
    punches = PunchEvent.objects.count()
    latest = PunchEvent.objects.order_by("-punched_at").values_list(
        "punched_at", flat=True
    ).first()

    if run is None:
        return {
            "reachable": False,
            "detail": "The punch sync has never run. Attendance is not being collected.",
            "table": "attendance_punchevent",
            "punches": punches,
            "latest_punch": _local_naive(latest) if latest else None,
            "last_agent_run": None,
            "stale": True,
        }

    age = timezone.now() - run.started_at
    stale = age > stale_after

    if not run.ok:
        detail = run.detail or "The last punch sync failed."
    elif stale:
        hours = int(age.total_seconds() // 3600)
        detail = f"The last punch sync succeeded {hours}h ago. Today's sheet may be incomplete."
    else:
        detail = ""

    return {
        # `reachable` now means "the punch data is current", which is what every
        # caller was really asking. A failed or stale run is not trustworthy.
        "reachable": bool(run.ok and not stale),
        "detail": detail,
        "table": "attendance_punchevent",
        "punches": punches,
        "latest_punch": _local_naive(latest) if latest else None,
        "last_agent_run": run.started_at,
        "stale": stale,
    }
