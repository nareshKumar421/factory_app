"""
Activity Center services — turn the registry into per-user pending / completed work.

Everything here is read-only: it queries the modules' own records, so the numbers can
never drift from reality and no user can mark their own work done without actually
doing it in the module that owns the record.

Query cost is bounded by the size of the registry, not by the number of users. The
per-user endpoints run one query per source the user is responsible for; the all-users
overview runs a fixed two queries per source plus one to map permission holders.
"""

from datetime import timedelta

from django.apps import apps
from django.contrib.auth import get_user_model
from django.db.models import Count
from django.utils import timezone

from .registry import (
    ACTIVITY_SOURCES,
    OWNED,
    QUEUE,
    SOURCES_BY_KEY,
    all_permissions,
    sources_for_permissions,
)

User = get_user_model()

#: Hard cap per source so an administrator with every permission cannot pull the
#: entire database into one response.
PER_SOURCE_LIMIT = 50


# ---------------------------------------------------------------------------
# Permission holders
# ---------------------------------------------------------------------------


def permission_holders():
    """
    Map ``app_label.codename`` -> ``set`` of user ids that hold it, for every
    permission the registry references.

    Both routes to a permission are resolved, one query each: group membership (the
    normal case here) and a direct grant on the user. Missing the direct route would
    make a user look like they have no work at all on the all-users board, which is a
    worse lie than showing nothing — so it is worth the second query.

    Superusers implicitly hold everything, which is why they are unioned into each
    entry.
    """
    wanted = all_permissions()
    codenames = {perm.split(".", 1)[1] for perm in wanted}

    holders = {perm: set() for perm in wanted}

    def collect(rows):
        for app_label, codename, user_id in rows:
            key = "%s.%s" % (app_label, codename)
            if key in holders:
                holders[key].add(user_id)

    collect(
        User.objects.filter(
            is_active=True,
            groups__permissions__codename__in=codenames,
        )
        .values_list(
            "groups__permissions__content_type__app_label",
            "groups__permissions__codename",
            "id",
        )
        .distinct()
    )
    collect(
        User.objects.filter(
            is_active=True,
            user_permissions__codename__in=codenames,
        )
        .values_list(
            "user_permissions__content_type__app_label",
            "user_permissions__codename",
            "id",
        )
        .distinct()
    )

    superusers = set(
        User.objects.filter(is_active=True, is_superuser=True).values_list("id", flat=True)
    )
    for perm in holders:
        holders[perm] |= superusers

    return holders


def held_permissions(user):
    """The registry permissions ``user`` actually holds."""
    return {perm for perm in all_permissions() if user.has_perm(perm)}


def scoped_sources(held):
    """Sources a user could act on: their permissions, plus every OWNED source.

    OWNED rows are included unconditionally because a record naming the user is
    theirs whether or not they still hold the permission — losing access should not
    make your own draft disappear.
    """
    scoped = {source.key: source for source in sources_for_permissions(held)}
    for source in ACTIVITY_SOURCES:
        if source.owner_field:
            scoped.setdefault(source.key, source)
    return [source for source in ACTIVITY_SOURCES if source.key in scoped]


def local_midnight(days_back=0):
    """Midnight at the start of the local day, ``days_back`` days ago.

    The single definition of "today" for this app. A daily job sheet is a local
    calendar artifact; using UTC midnight would fold the previous evening into today
    for the first 5h30m of every IST day.
    """
    midnight = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight - timedelta(days=days_back) if days_back else midnight


# ---------------------------------------------------------------------------
# Querysets
# ---------------------------------------------------------------------------


def _base_queryset(source, company=None):
    model = apps.get_model(*source.model.split("."))
    queryset = model.objects.filter(**source.pending_filter)
    if company is not None and source.company_field:
        queryset = queryset.filter(**{source.company_field: company})
    return queryset


def _select_related(source, *extra):
    hops = [field for field in extra if field]
    hops.extend(source.extra_select)
    return hops


def _age_days(value, now):
    if not value:
        return None
    return max((now - value).days, 0)


def _reference(record, source):
    if not source.reference_field:
        return None
    value = getattr(record, source.reference_field, None)
    return str(value) if value not in (None, "") else None


def _status_of(record):
    status = getattr(record, "status", None)
    if not status:
        return None
    getter = getattr(record, "get_status_display", None)
    return getter() if callable(getter) else str(status)


def _url(source, record):
    if not source.url_template:
        return None
    return source.url_template.format(id=record.pk)


def _item(source, record, now):
    since = getattr(record, source.age_field, None) or getattr(record, "created_at", None)
    age = _age_days(since, now)
    return {
        "source_key": source.key,
        "label": source.label,
        "module": source.module,
        "mode": source.mode,
        "permission": source.permission,
        "record_id": record.pk,
        "reference": _reference(record, source),
        "status": _status_of(record),
        "since": since,
        "age_days": age,
        "is_overdue": age is not None and age > source.overdue_after_days,
        "url": _url(source, record),
    }


# ---------------------------------------------------------------------------
# Per-user pending work
# ---------------------------------------------------------------------------


def pending_for_user(user, company=None, modules=None, source_keys=None):
    """
    Every pending item ``user`` is responsible for, newest-first within each source.

    ``OWNED`` sources are matched on the record's owner field and are always
    returned to that owner — a draft they created is theirs to submit even if their
    permissions changed since. ``QUEUE`` sources are returned only while the user
    still holds the permission, because a shared queue is not anyone's property.
    """
    now = timezone.now()
    held = held_permissions(user)
    items = []

    for source in ACTIVITY_SOURCES:
        if source_keys and source.key not in source_keys:
            continue
        if modules and source.module not in modules:
            continue

        if source.mode == OWNED:
            if not source.owner_field:
                continue
            queryset = _base_queryset(source, company).filter(
                **{source.owner_field: user}
            )
        else:
            if source.permission not in held:
                continue
            queryset = _base_queryset(source, company)

        queryset = queryset.select_related(*_select_related(source))
        for record in queryset.order_by("-pk")[:PER_SOURCE_LIMIT]:
            items.append(_item(source, record, now))

    # Overdue first, then oldest — the order someone should work through them.
    items.sort(
        key=lambda item: (
            not item["is_overdue"],
            -(item["age_days"] or 0),
            item["module"],
        )
    )
    return items


# ---------------------------------------------------------------------------
# Per-user completed work
# ---------------------------------------------------------------------------


def completed_for_user(user, company=None, since=None, modules=None):
    """
    Work ``user`` demonstrably finished: records carrying them in the source's
    ``actor_field`` with the paired timestamp inside the window.

    Sources without an ``actor_field`` cannot prove who acted, so they contribute
    nothing here rather than guessing.

    Only sources the user could plausibly have acted on are queried: the ones their
    permissions cover, plus every OWNED source so a draft they created stays visible
    even after their access changes. Scanning the whole registry instead would fire a
    query per source for every caller, including users who hold nothing at all.
    """
    since = since or timezone.now() - timedelta(days=1)
    items = []

    in_scope = scoped_sources(held_permissions(user))

    for source in in_scope:
        if not source.actor_field:
            continue
        if modules and source.module not in modules:
            continue

        model = apps.get_model(*source.model.split("."))
        date_field = source.actor_date_field or "updated_at"
        queryset = model.objects.filter(
            **{
                source.actor_field: user,
                "%s__gte" % date_field: since,
            }
        )
        if company is not None and source.company_field:
            queryset = queryset.filter(**{source.company_field: company})

        for record in queryset.order_by("-pk")[:PER_SOURCE_LIMIT]:
            items.append(
                {
                    "source_key": source.key,
                    "label": source.label,
                    "module": source.module,
                    "record_id": record.pk,
                    "reference": _reference(record, source),
                    "status": _status_of(record),
                    "completed_at": getattr(record, date_field, None),
                    "url": _url(source, record),
                }
            )

    items.sort(key=lambda item: item["completed_at"] or since, reverse=True)
    return items


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


def summary_for_user(user, company=None, since=None):
    """Counts for the header cards plus a per-module breakdown."""
    since = since or local_midnight()
    pending = pending_for_user(user, company)
    completed = completed_for_user(user, company, since=since)

    modules = {}
    for item in pending:
        bucket = modules.setdefault(
            item["module"], {"module": item["module"], "pending": 0, "overdue": 0, "completed": 0}
        )
        bucket["pending"] += 1
        if item["is_overdue"]:
            bucket["overdue"] += 1
    for item in completed:
        bucket = modules.setdefault(
            item["module"], {"module": item["module"], "pending": 0, "overdue": 0, "completed": 0}
        )
        bucket["completed"] += 1

    # Two sources can legitimately select the same record — a returnable pass is both
    # "yours to return" and "the gate's to take back in", and both rows belong on the
    # list. The headline count must still say how many things need attention, so it
    # counts distinct records; a user seeing their own total doubled stops believing
    # any number on the page.
    distinct_pending = {
        (SOURCES_BY_KEY[item["source_key"]].model, item["record_id"]) for item in pending
    }

    return {
        "pending": len(distinct_pending),
        "overdue": sum(1 for item in pending if item["is_overdue"]),
        "owned": sum(1 for item in pending if item["mode"] == OWNED),
        "queued": sum(1 for item in pending if item["mode"] == QUEUE),
        "completed": len(completed),
        "since": since,
        "modules": sorted(modules.values(), key=lambda row: row["module"]),
    }


# ---------------------------------------------------------------------------
# All-users overview (supervisor view)
# ---------------------------------------------------------------------------


def overview_all_users(company=None, since=None):
    """
    One row per active user: pending, overdue and completed counts.

    Cost is fixed per source rather than per user. ``QUEUE`` pending is a shared
    backlog, so the same count is attributed to every holder of the permission and
    the response flags it as shared — otherwise a queue of 10 seen by 5 approvers
    would read as 50 outstanding jobs.
    """
    now = timezone.now()
    since = since or now.replace(hour=0, minute=0, second=0, microsecond=0)
    holders = permission_holders()

    users = list(
        User.objects.filter(is_active=True).order_by("full_name", "email")
    )
    rows = {
        user.id: {
            "user_id": user.id,
            "full_name": user.full_name or user.email,
            "email": user.email,
            "employee_code": user.employee_code,
            "is_superuser": user.is_superuser,
            "owned_pending": 0,
            "owned_overdue": 0,
            "queue_pending": 0,
            "completed": 0,
        }
        for user in users
    }

    for source in ACTIVITY_SOURCES:
        overdue_before = now - timedelta(days=source.overdue_after_days)

        if source.mode == OWNED and source.owner_field:
            grouped = (
                _base_queryset(source, company)
                .exclude(**{"%s__isnull" % source.owner_field: True})
                .values(source.owner_field)
                .annotate(total=Count("pk"))
            )
            for entry in grouped:
                row = rows.get(entry[source.owner_field])
                if row:
                    row["owned_pending"] += entry["total"]

            stale = (
                _base_queryset(source, company)
                .exclude(**{"%s__isnull" % source.owner_field: True})
                .filter(**{"%s__lt" % source.age_field: overdue_before})
                .values(source.owner_field)
                .annotate(total=Count("pk"))
            )
            for entry in stale:
                row = rows.get(entry[source.owner_field])
                if row:
                    row["owned_overdue"] += entry["total"]
        else:
            queue_total = _base_queryset(source, company).count()
            if queue_total:
                for user_id in holders.get(source.permission, ()):
                    row = rows.get(user_id)
                    if row:
                        row["queue_pending"] += queue_total

        if source.actor_field:
            model = apps.get_model(*source.model.split("."))
            date_field = source.actor_date_field or "updated_at"
            done = model.objects.filter(**{"%s__gte" % date_field: since})
            if company is not None and source.company_field:
                done = done.filter(**{source.company_field: company})
            done = (
                done.exclude(**{"%s__isnull" % source.actor_field: True})
                .values(source.actor_field)
                .annotate(total=Count("pk"))
            )
            for entry in done:
                row = rows.get(entry[source.actor_field])
                if row:
                    row["completed"] += entry["total"]

    return {
        "since": since,
        "users": sorted(
            rows.values(),
            key=lambda row: (-row["owned_overdue"], -row["owned_pending"], row["full_name"]),
        ),
    }


# ---------------------------------------------------------------------------
# Registry metadata (drives UI filters and the permission admin screen)
# ---------------------------------------------------------------------------


def definitions(user=None):
    """The registry as data. When ``user`` is given, flags what they are responsible for."""
    held = held_permissions(user) if user is not None else set()
    responsible = {source.key for source in sources_for_permissions(held)}
    return [
        {
            "source_key": source.key,
            "label": source.label,
            "module": source.module,
            "mode": source.mode,
            "permission": source.permission,
            "model": source.model,
            "overdue_after_days": source.overdue_after_days,
            "is_mine": source.key in responsible,
        }
        for source in ACTIVITY_SOURCES
    ]
