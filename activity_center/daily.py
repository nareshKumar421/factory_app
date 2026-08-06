"""
Daily job sheet — what one user is expected to do on one day, and what they recorded.

Two ideas, deliberately kept apart:

*Cadence* is how often a job is expected (see :mod:`activity_center.registry`).
*Countable* is whether the system can observe it being done at all — roughly a third
of the catalogue never records who acted.

    tally = jobs where cadence is expected AND countable
    shown = every in-scope job, grouped by cadence, uncountable ones flagged

Nothing here produces a score. There is no attendance, shift or roster data anywhere
in this project, so we cannot tell an idle day from a day off, and any percentage we
emitted would be read as one. The vocabulary is "not yet", never "missed".
"""

from datetime import timedelta

from django.apps import apps
from django.contrib.auth import get_user_model
from django.db.models import Count, Max, Min
from django.utils import timezone

from .registry import (
    ACTIVITY_SOURCES,
    CADENCES,
    EXPECTED_CADENCES,
    is_countable,
)
from .services import held_permissions, permission_holders, scoped_sources

User = get_user_model()

#: Human titles for each cadence, in display order. Kept beside the sheet rather than
#: in the registry because they are copy, not data.
CADENCE_TITLES = {
    "DAILY": "Every day",
    "SHIFT": "Once per shift",
    "EVENT": "When it happens",
    "PERIODIC": "When something changes",
}


# ---------------------------------------------------------------------------
# Day bounds
# ---------------------------------------------------------------------------


def local_day_bounds(day=None):
    """``(start, end)`` covering one local calendar day, end-exclusive.

    ``day`` is a ``datetime.date`` in the local timezone; ``None`` means today.
    """
    now = timezone.localtime()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if day is not None:
        start = start.replace(year=day.year, month=day.month, day=day.day)
    return start, start + timedelta(days=1)


# ---------------------------------------------------------------------------
# One user's sheet
# ---------------------------------------------------------------------------


def _job_row(source, company, user, start, end):
    """One job on the sheet: what was recorded today, and what is waiting now."""
    model = apps.get_model(*source.model.split("."))
    countable = is_countable(source)

    done_today = None
    last_done_at = None
    if countable:
        recorded = model.objects.filter(
            **{
                source.actor_field: user,
                "%s__gte" % source.actor_date_field: start,
                "%s__lt" % source.actor_date_field: end,
            }
        )
        if company is not None and source.company_field:
            recorded = recorded.filter(**{source.company_field: company})
        aggregate = recorded.aggregate(total=Count("pk"), last=Max(source.actor_date_field))
        done_today = aggregate["total"] or 0
        last_done_at = aggregate["last"]

    pending = model.objects.filter(**source.pending_filter)
    if company is not None and source.company_field:
        pending = pending.filter(**{source.company_field: company})
    if source.mode == "OWNED" and source.owner_field:
        pending = pending.filter(**{source.owner_field: user})
    pending_aggregate = pending.aggregate(total=Count("pk"), oldest=Min(source.age_field))

    oldest_days = None
    if pending_aggregate["oldest"]:
        oldest_days = max((timezone.now() - pending_aggregate["oldest"]).days, 0)

    return {
        "source_key": source.key,
        "label": source.label,
        "module": source.module,
        "cadence": source.cadence,
        "mode": source.mode,
        "countable": countable,
        # Null, never 0, when we cannot observe this job. A zero would read as
        # "you did nothing"; null lets the UI say "we cannot see this".
        "done_today": done_today,
        "last_done_at": last_done_at,
        "pending_now": pending_aggregate["total"] or 0,
        "oldest_pending_days": oldest_days,
        "url": source.list_url,
    }


def sheet_for_user(user, company=None, day=None):
    """The full day sheet for one user."""
    start, end = local_day_bounds(day)
    sources = scoped_sources(held_permissions(user))

    rows = [_job_row(source, company, user, start, end) for source in sources]

    groups = []
    counted_jobs = done = 0
    records_done = 0
    uncounted_jobs = 0

    for cadence in CADENCES:
        jobs = [row for row in rows if row["cadence"] == cadence]
        if not jobs:
            continue

        expected = cadence in EXPECTED_CADENCES
        group_counted = sum(1 for job in jobs if job["countable"] and expected)
        group_done = sum(
            1 for job in jobs if job["countable"] and expected and job["done_today"]
        )

        counted_jobs += group_counted
        done += group_done
        uncounted_jobs += sum(1 for job in jobs if not job["countable"])
        records_done += sum(job["done_today"] or 0 for job in jobs)

        groups.append(
            {
                "cadence": cadence,
                "title": CADENCE_TITLES[cadence],
                "counted_jobs": group_counted,
                "done": group_done,
                "jobs": jobs,
            }
        )

    today_start, _ = local_day_bounds()

    return {
        "date": start.date(),
        "is_today": start == today_start,
        "user": {
            "user_id": user.pk,
            "full_name": user.full_name,
            "employee_code": user.employee_code,
        },
        "tally": {
            "counted_jobs": counted_jobs,
            "done": done,
            "not_yet": counted_jobs - done,
            "records_done": records_done,
        },
        "uncounted_jobs": uncounted_jobs,
        "groups": groups,
    }


# ---------------------------------------------------------------------------
# All users, one day
# ---------------------------------------------------------------------------


def board_for_day(company=None, day=None):
    """Every active user's recorded activity for one day.

    Query cost is flat in the number of users: expectation is computed in Python from
    the registry crossed with the permission-holder map, and each countable source
    contributes exactly one grouped aggregate. Looping :func:`sheet_for_user` over the
    user table instead would be users x sources queries — see the test that pins this.
    """
    start, end = local_day_bounds(day)
    holders = permission_holders()

    users = list(
        User.objects.filter(is_active=True)
        .only("id", "full_name", "email", "employee_code", "is_superuser")
        .order_by("full_name", "email")
    )

    rows = {
        user.pk: {
            "user_id": user.pk,
            "full_name": user.full_name,
            "email": user.email,
            "employee_code": user.employee_code,
            "is_superuser": user.is_superuser,
            "expected_counted": 0,
            "expected_uncounted": 0,
            "jobs_done": 0,
            "not_yet": 0,
            "records_done": 0,
            "first_activity_at": None,
            "last_activity_at": None,
            "modules_touched": set(),
            # Every observable job done today, any cadence — this is what "recorded"
            # describes. `_done_expected_keys` is the subset that was an expectation
            # for the day, and is the only thing "not yet" may be measured against;
            # subtracting the wider set could drive it negative the moment someone
            # handles a few event-driven jobs.
            "_done_keys": set(),
            "_done_expected_keys": set(),
        }
        for user in users
    }

    # Expectation — pure Python, no queries.
    for source in ACTIVITY_SOURCES:
        if not is_countable(source):
            bucket = "expected_uncounted"
        elif source.cadence in EXPECTED_CADENCES:
            bucket = "expected_counted"
        else:
            continue  # observable, but never an expectation for a given day
        for user_id in holders.get(source.permission, ()):
            row = rows.get(user_id)
            if row is not None:
                row[bucket] += 1

    # Evidence — one grouped aggregate per countable source.
    for source in ACTIVITY_SOURCES:
        if not is_countable(source):
            continue

        model = apps.get_model(*source.model.split("."))
        queryset = model.objects.filter(
            **{
                "%s__gte" % source.actor_date_field: start,
                "%s__lt" % source.actor_date_field: end,
                "%s__isnull" % source.actor_field: False,
            }
        )
        if company is not None and source.company_field:
            queryset = queryset.filter(**{source.company_field: company})

        grouped = queryset.values(source.actor_field).annotate(
            total=Count("pk"),
            first=Min(source.actor_date_field),
            last=Max(source.actor_date_field),
        )
        for entry in grouped:
            row = rows.get(entry[source.actor_field])
            if row is None:
                continue
            row["records_done"] += entry["total"]
            row["_done_keys"].add(source.key)
            if source.cadence in EXPECTED_CADENCES:
                row["_done_expected_keys"].add(source.key)
            row["modules_touched"].add(source.module)
            row["first_activity_at"] = _earliest(row["first_activity_at"], entry["first"])
            row["last_activity_at"] = _latest(row["last_activity_at"], entry["last"])

    result = []
    for row in rows.values():
        # jobs_done counts distinct *kinds* of job; records_done counts records. Both
        # ship so the UI can say "9 of 14 kinds, 23 records" and never a percentage.
        row["jobs_done"] = len(row["_done_keys"])
        row["not_yet"] = max(row["expected_counted"] - len(row["_done_expected_keys"]), 0)
        row["modules_touched"] = sorted(row["modules_touched"])
        del row["_done_keys"]
        del row["_done_expected_keys"]
        result.append(row)

    today_start, _ = local_day_bounds()

    return {
        "date": start.date(),
        "is_today": start == today_start,
        "totals": {
            "users": len(result),
            "with_activity": sum(1 for row in result if row["records_done"]),
            "no_activity_yet": sum(1 for row in result if not row["records_done"]),
            "records_done": sum(row["records_done"] for row in result),
        },
        # Name order, deliberately. Sorting by who did least would make the payload
        # itself a ranking of people, which is what this feature must not be.
        "users": result,
    }


def _earliest(current, candidate):
    if candidate is None:
        return current
    return candidate if current is None or candidate < current else current


def _latest(current, candidate):
    if candidate is None:
        return current
    return candidate if current is None or candidate > current else current
