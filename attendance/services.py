"""
The writes and their invariants: rolling punches up, and correcting the result.

Two jobs, and they are kept apart on purpose.

:func:`sync_day` turns raw punches into a day's verdict. It owns
``machine_status`` and everything beside it, and it is the *only* thing that
writes those columns.

:func:`override_status` records that a human disagreed. It owns
``effective_status`` and never touches the machine's reading -- re-running the
sync over a corrected day therefore refreshes the punch detail without undoing
anybody's correction, which is what lets the sync be run again safely after the
LAN link drops mid-pull.

The derivation rules live here rather than in the client because they are
business policy, not schema. They are deliberately simple, and the override
exists because simple rules are wrong a few times a day.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Count
from django.utils import timezone

from employee_hierarchy.constants import IN_SERVICE_STATUSES
from employee_hierarchy.models import Employee

from . import punch_store
from .models import (
    AttendanceOverrideLog,
    AttendanceStatus,
    DailyAttendance,
    OverrideAction,
)


class OverrideRefused(ValueError):
    """An override that would destroy the thing the module exists to preserve."""


def _half_day_minutes():
    return getattr(settings, "ATTENDANCE_HALF_DAY_MINUTES", 240)


def _weekly_off_days():
    return set(getattr(settings, "ATTENDANCE_WEEKLY_OFF_DAYS", [6]))


def derive_status(punches, day):
    """What a day's punches amount to, on their own.

    ``punches`` are that employee's punches for that day, in any order.

    The rules, and why each is what it is:

    * **No punches** is absent -- unless it is the weekly off, when absence is
      the expected state and flagging it would bury the real absences under
      three hundred false ones every Sunday.
    * **One punch** is :attr:`~.AttendanceStatus.MISSING_PUNCH`, never present
      and never absent. They were at the gate, so absent is a lie; nothing says
      how long they stayed, so present is a guess. It is 14% of person-days and
      the honest answer is that a human has to look.
    * **Two or more** gives arrival and departure, and the span between them
      decides full or half day. The span is gate-to-gate: it is not overtime
      and it is not productive hours, and no report should treat it as either.

    Punching *on* a weekly off means they came in, so the day is scored
    normally -- the off only changes what an empty day means.

    **Known limitation: a shift that crosses midnight is split.** The roll-up is
    per calendar day, so a night worker who clocks in at 21:00 and out at 05:00
    appears as two days, each with one punch. Measured over 30 days this is 63
    punches by 46 people -- 0.3% of the total -- and correcting it properly needs
    a shift master this business does not keep, which would mean guessing which
    of two plausible shift patterns each person is on. Guessing would be wrong
    silently; :attr:`~.AttendanceStatus.MISSING_PUNCH` plus an override is wrong
    visibly, and somebody can fix it in ten seconds. Revisit if shift data ever
    arrives.
    """
    if not punches:
        if day.weekday() in _weekly_off_days():
            return AttendanceStatus.WEEKLY_OFF, None, None, 0, 0
        return AttendanceStatus.ABSENT, None, None, 0, 0

    times = sorted(punch.punched_at for punch in punches)
    first, last = times[0], times[-1]
    worked = int((last - first).total_seconds() // 60)

    if len(times) == 1:
        return AttendanceStatus.MISSING_PUNCH, first.time(), last.time(), 1, 0

    status = (
        AttendanceStatus.HALF_DAY
        if worked < _half_day_minutes()
        else AttendanceStatus.PRESENT
    )
    return status, first.time(), last.time(), len(times), worked


@transaction.atomic
def sync_day(day, punches_by_code, employees):
    """Write one day's roll-up for every employee given.

    Returns ``(created, updated, skipped_overridden_effective)``.

    An employee with no punches still gets a row: "absent" is a fact about the
    day that has to be queryable, and a missing row would be indistinguishable
    from a day nobody synced.

    A corrected day keeps its correction. Only the machine columns are
    refreshed, so this is safe to re-run over any date range.
    """
    existing = {
        row.employee_id: row
        for row in DailyAttendance.objects.filter(
            date=day, employee__in=employees
        ).select_related(None)
    }
    now = timezone.now()
    created, updated, kept = 0, 0, 0
    to_create, to_update = [], []

    for employee in employees:
        punches = punches_by_code.get(employee.employee_code.upper(), [])
        status, first, last, count, worked = derive_status(punches, day)
        devices = ",".join(sorted({p.device for p in punches if p.device}))[:255]

        row = existing.get(employee.pk)
        if row is None:
            to_create.append(
                DailyAttendance(
                    employee=employee,
                    date=day,
                    machine_status=status,
                    machine_first_punch=first,
                    machine_last_punch=last,
                    machine_punch_count=count,
                    machine_worked_minutes=worked,
                    devices=devices,
                    effective_status=status,
                    synced_at=now,
                )
            )
            created += 1
            continue

        row.machine_status = status
        row.machine_first_punch = first
        row.machine_last_punch = last
        row.machine_punch_count = count
        row.machine_worked_minutes = worked
        row.devices = devices
        row.synced_at = now
        # The correction stands. Re-deriving effective_status here would silently
        # undo somebody's decision on every nightly run.
        if row.is_overridden:
            kept += 1
        else:
            row.effective_status = status
        to_update.append(row)
        updated += 1

    if to_create:
        DailyAttendance.objects.bulk_create(to_create, batch_size=500)
    if to_update:
        DailyAttendance.objects.bulk_update(
            to_update,
            [
                "machine_status",
                "machine_first_punch",
                "machine_last_punch",
                "machine_punch_count",
                "machine_worked_minutes",
                "devices",
                "effective_status",
                "synced_at",
            ],
            batch_size=500,
        )
    return created, updated, kept


def sync_range(date_from, date_to, *, company=None, progress=None):
    """Read punches once for the whole range and roll each day up.

    One query for the range rather than one per day. The punches now come from
    our own database (:mod:`attendance.punch_store`), copied there by the agent
    inside the plant, so this no longer depends on the factory LAN being up --
    but a single scan still beats one per day over a 92-day backfill.
    """
    employees = Employee.objects.filter(employment_status__in=IN_SERVICE_STATUSES)
    if company is not None:
        employees = employees.filter(company=company)
    employees = list(employees.only("id", "employee_code", "full_name"))
    codes = {employee.employee_code.upper() for employee in employees}

    punches = punch_store.fetch_punches(date_from, date_to, codes=codes)

    by_day = defaultdict(lambda: defaultdict(list))
    for punch in punches:
        by_day[punch.punched_at.date()][punch.employee_code].append(punch)

    totals = {"days": 0, "created": 0, "updated": 0, "kept_overrides": 0, "punches": len(punches)}
    day = date_from
    while day <= date_to:
        created, updated, kept = sync_day(day, by_day.get(day, {}), employees)
        totals["days"] += 1
        totals["created"] += created
        totals["updated"] += updated
        totals["kept_overrides"] += kept
        if progress:
            progress(day, created, updated, kept)
        day += timedelta(days=1)
    return totals


@transaction.atomic
def override_status(row, *, status, reason_code, reason, user):
    """Record that a human disagreed with the machine, and why.

    The reason is mandatory in both forms. A blank one defeats the entire
    purpose: the point of keeping the machine's reading beside the effective one
    is that somebody can later ask why they differ, and "no reason given" is not
    an answer anybody can act on.

    The machine's columns are not touched here -- see the module docstring.
    """
    status = str(status)
    if status not in AttendanceStatus.values:
        raise OverrideRefused(f"{status!r} is not an attendance status.")
    if not (reason or "").strip():
        raise OverrideRefused("A reason is required to change an attendance status.")
    if not reason_code:
        raise OverrideRefused("A reason code is required to change an attendance status.")

    previous = row.effective_status
    if previous == status and row.is_overridden:
        raise OverrideRefused(f"This day is already recorded as {status}.")

    action = OverrideAction.AMEND if row.is_overridden else OverrideAction.OVERRIDE

    row.effective_status = status
    row.is_overridden = True
    row.override_reason_code = reason_code
    row.override_reason = reason.strip()
    row.overridden_by = user if user and user.is_authenticated else None
    row.overridden_at = timezone.now()
    row.save(
        update_fields=[
            "effective_status",
            "is_overridden",
            "override_reason_code",
            "override_reason",
            "overridden_by",
            "overridden_at",
            "updated_at",
        ]
    )

    AttendanceOverrideLog.objects.create(
        daily_attendance=row,
        action=action,
        from_status=previous,
        to_status=status,
        machine_status=row.machine_status,
        reason_code=reason_code,
        reason=reason.strip(),
        performed_by=user if user and user.is_authenticated else None,
    )
    return row


@transaction.atomic
def revert_to_machine(row, *, reason, user):
    """Drop the correction and go back to what the machine said.

    Logged like any other change. A revert is a decision too -- somebody
    concluded the earlier correction was wrong -- and leaving it out of the trail
    would make the log stop explaining the row it describes.
    """
    if not row.is_overridden:
        raise OverrideRefused("This day has not been overridden.")

    previous = row.effective_status
    row.effective_status = row.machine_status
    row.is_overridden = False
    row.override_reason_code = ""
    row.override_reason = ""
    row.overridden_by = None
    row.overridden_at = None
    row.save(
        update_fields=[
            "effective_status",
            "is_overridden",
            "override_reason_code",
            "override_reason",
            "overridden_by",
            "overridden_at",
            "updated_at",
        ]
    )

    AttendanceOverrideLog.objects.create(
        daily_attendance=row,
        action=OverrideAction.REVERT,
        from_status=previous,
        to_status=row.machine_status,
        machine_status=row.machine_status,
        reason=(reason or "").strip(),
        performed_by=user if user and user.is_authenticated else None,
    )
    return row


def summarise(queryset):
    """Counts for one day's sheet, by machine reading and by what stands.

    Both, always. A summary that reported only the effective numbers would hide
    the size of the correction being applied, which is the one thing somebody
    reviewing the day needs to see.
    """
    # Counted in the database, three grouped scans rather than three hundred
    # rows across the wire. ``values(...)`` also drops the caller's
    # ``select_related``, which ``only(...)`` would collide with.
    #
    # ``order_by()`` clears the sort first, and it is load-bearing. Django folds
    # any surviving ORDER BY into the GROUP BY, and the caller here is
    # ``DailyAttendanceViewSet.get_queryset``, which ends
    # ``.order_by("employee__full_name")`` -- so the scan grouped by
    # (status, employee) instead of (status), one row per person, and the dict
    # comprehension below kept whichever landed last. Every tile read 1.
    queryset = queryset.order_by()
    machine = {
        row["machine_status"]: row["n"]
        for row in queryset.values("machine_status").annotate(n=Count("id"))
    }
    effective = {
        row["effective_status"]: row["n"]
        for row in queryset.values("effective_status").annotate(n=Count("id"))
    }
    overridden = queryset.filter(is_overridden=True).count()
    return {
        "total": sum(machine.values()),
        "overridden": overridden,
        "machine": dict(machine),
        "effective": dict(effective),
    }
