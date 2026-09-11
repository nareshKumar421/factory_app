"""
Everything that changes an issue, and everything that finds one.

The views are thin on purpose: each one validates its body and calls a function
here. Two rules hold this module together.

**Every change writes its own timeline event.** ``set_labels`` does not just
rewrite the M2M -- it works out what was added and removed and appends a
``LABELED`` / ``UNLABELED`` event for each, with the label's name and colour
frozen into ``detail``. That is why the timeline can be trusted later: nothing
can change an issue without the change being recorded, because the only way to
change one is through here.

**Numbers are allocated, never supplied.** :func:`create_issue` takes the next
number off a locked counter row, so two people filing an issue at the same
moment get #41 and #42 rather than one of them getting an error -- and a deleted
number is never handed out again.
"""

import os

from django.db import connection, transaction
from django.db.models import Case, Count, F, IntegerField, Max, Q, Value, When
from django.utils import timezone

from accounts.models import User

from .constants import (
    ALLOWED_ATTACHMENT_EXTENSIONS,
    MAX_ATTACHMENT_BYTES,
    PRIORITY_RANK,
    IssuePriority,
    IssueState,
    StateReason,
    TimelineEvent,
)
from .models import (
    Issue,
    IssueAttachment,
    IssueComment,
    IssueEvent,
    IssueNumberSequence,
)


class IssueError(Exception):
    """A rejected issue operation. Views turn this into a 400."""


# ---------------------------------------------------------------------------
# Timeline
# ---------------------------------------------------------------------------


def record_event(issue, actor, event, **detail):
    """Append one event to an issue's timeline."""
    return IssueEvent.objects.create(
        issue=issue, actor=actor, event=event, detail=detail
    )


def touch(issue, *, save=True):
    """Bump ``last_activity_at`` -- what the "recently updated" sort reads."""
    issue.last_activity_at = timezone.now()
    if save:
        issue.save(update_fields=["last_activity_at", "updated_at"])


def _user_label(user):
    """How a person is named inside a frozen event detail."""
    if not user:
        return ""
    return user.full_name or user.email


# ---------------------------------------------------------------------------
# Creating and editing
# ---------------------------------------------------------------------------


def allocate_issue_number():
    """Take the next issue number off the counter.

    The counter row is locked for the duration, so two people filing at the same
    moment get #41 and #42 rather than colliding. The row is seeded from the
    existing high-water mark the first time it is needed, which is what lets the
    counter be added to a tracker that already holds issues.

    Deliberately a counter and not ``MAX(number) + 1``: deleting #7 must not
    hand #7 out again, because people quote the number long after the issue is
    gone.
    """
    rows = IssueNumberSequence.objects.filter(pk=IssueNumberSequence.SINGLETON_PK)
    # SQLite has no row locking; the tests run there, production runs Postgres.
    if connection.features.has_select_for_update:
        rows = rows.select_for_update()
    row = rows.first()
    if row is None:
        seed = Issue.objects.aggregate(highest=Max("number"))["highest"] or 0
        IssueNumberSequence.objects.get_or_create(
            pk=IssueNumberSequence.SINGLETON_PK, defaults={"last_number": seed}
        )
        rows = IssueNumberSequence.objects.filter(pk=IssueNumberSequence.SINGLETON_PK)
        if connection.features.has_select_for_update:
            rows = rows.select_for_update()
        row = rows.get()

    row.last_number += 1
    row.save(update_fields=["last_number"])
    return row.last_number


@transaction.atomic
def create_issue(
    *,
    author,
    title,
    body="",
    labels=(),
    assignees=(),
    company=None,
    priority=IssuePriority.MEDIUM,
    page_url="",
    attachment_ids=(),
):
    """File a new issue and open its timeline."""
    title = " ".join((title or "").split())
    if not title:
        raise IssueError("An issue needs a title.")

    issue = Issue.objects.create(
        number=allocate_issue_number(),
        title=title,
        body=body or "",
        author=author,
        company=company,
        priority=priority or IssuePriority.MEDIUM,
        page_url=page_url or "",
        created_by=author,
        updated_by=author,
        last_activity_at=timezone.now(),
    )

    if labels:
        issue.labels.set(labels)
    if assignees:
        issue.assignees.set(assignees)

    record_event(issue, author, TimelineEvent.OPENED)
    # The opening labels and assignees are part of the report, not later triage,
    # so they are folded into OPENED rather than emitting their own events --
    # otherwise every new issue starts with a wall of "labeled" lines.
    claim_attachments(attachment_ids, user=author, issue=issue)
    return issue


def update_issue(issue, user, **changes):
    """Apply a partial edit, writing one event per field that actually moved.

    Only keys present in ``changes`` are touched, and a key whose value equals
    what is already stored writes nothing -- saving a form without altering it
    should not add noise to the timeline.
    """
    updated_fields = []

    if "title" in changes:
        new_title = " ".join((changes["title"] or "").split())
        if not new_title:
            raise IssueError("An issue needs a title.")
        if new_title != issue.title:
            record_event(
                issue,
                user,
                TimelineEvent.RENAMED,
                previous=issue.title,
                current=new_title,
            )
            issue.title = new_title
            updated_fields.append("title")

    if "body" in changes:
        new_body = changes["body"] or ""
        if new_body != issue.body:
            record_event(issue, user, TimelineEvent.EDITED)
            issue.body = new_body
            updated_fields.append("body")

    if "priority" in changes:
        new_priority = changes["priority"] or IssuePriority.MEDIUM
        if new_priority != issue.priority:
            record_event(
                issue,
                user,
                TimelineEvent.PRIORITY_CHANGED,
                previous=issue.priority,
                current=new_priority,
            )
            issue.priority = new_priority
            updated_fields.append("priority")

    if "company" in changes:
        new_company = changes["company"]
        if (new_company.id if new_company else None) != issue.company_id:
            issue.company = new_company
            updated_fields.append("company")

    if "page_url" in changes:
        new_url = changes["page_url"] or ""
        if new_url != issue.page_url:
            issue.page_url = new_url
            updated_fields.append("page_url")

    if "pinned" in changes:
        new_pinned = bool(changes["pinned"])
        if new_pinned != issue.pinned:
            record_event(
                issue,
                user,
                TimelineEvent.PINNED if new_pinned else TimelineEvent.UNPINNED,
            )
            issue.pinned = new_pinned
            updated_fields.append("pinned")

    if "locked" in changes:
        new_locked = bool(changes["locked"])
        if new_locked != issue.locked:
            record_event(
                issue,
                user,
                TimelineEvent.LOCKED if new_locked else TimelineEvent.UNLOCKED,
            )
            issue.locked = new_locked
            updated_fields.append("locked")

    if "labels" in changes:
        set_labels(issue, user, changes["labels"])

    if "assignees" in changes:
        set_assignees(issue, user, changes["assignees"])

    if updated_fields:
        issue.updated_by = user
        issue.last_activity_at = timezone.now()
        issue.save(
            update_fields=updated_fields
            + ["updated_by", "last_activity_at", "updated_at"]
        )
    return issue


def set_labels(issue, user, labels):
    """Replace the label set, writing an event per label added or removed."""
    wanted = {label.id: label for label in labels}
    current = {label.id: label for label in issue.labels.all()}

    for label_id, label in wanted.items():
        if label_id not in current:
            record_event(
                issue,
                user,
                TimelineEvent.LABELED,
                label=label.name,
                color=label.color,
            )
    for label_id, label in current.items():
        if label_id not in wanted:
            record_event(
                issue,
                user,
                TimelineEvent.UNLABELED,
                label=label.name,
                color=label.color,
            )

    if set(wanted) != set(current):
        issue.labels.set(wanted.values())
        touch(issue)
    return issue


def set_assignees(issue, user, assignees):
    """Replace the assignee set, writing an event per person added or removed."""
    wanted = {person.id: person for person in assignees}
    current = {person.id: person for person in issue.assignees.all()}

    for person_id, person in wanted.items():
        if person_id not in current:
            record_event(
                issue,
                user,
                TimelineEvent.ASSIGNED,
                user_id=person_id,
                name=_user_label(person),
            )
    for person_id, person in current.items():
        if person_id not in wanted:
            record_event(
                issue,
                user,
                TimelineEvent.UNASSIGNED,
                user_id=person_id,
                name=_user_label(person),
            )

    if set(wanted) != set(current):
        issue.assignees.set(wanted.values())
        touch(issue)
    return issue


def close_issue(issue, user, reason=StateReason.COMPLETED, duplicate_of=None):
    """Close an issue. Closing an already-closed one is a no-op, not an error."""
    if reason not in StateReason.values:
        raise IssueError(f"Unknown close reason: {reason}")
    if reason == StateReason.DUPLICATE and duplicate_of is None:
        raise IssueError("Closing as a duplicate needs the issue it duplicates.")
    if duplicate_of is not None and duplicate_of.pk == issue.pk:
        raise IssueError("An issue cannot duplicate itself.")

    if issue.state == IssueState.CLOSED and issue.state_reason == reason:
        return issue

    issue.state = IssueState.CLOSED
    issue.state_reason = reason
    issue.closed_at = timezone.now()
    issue.closed_by = user
    issue.updated_by = user
    issue.last_activity_at = issue.closed_at
    fields = [
        "state",
        "state_reason",
        "closed_at",
        "closed_by",
        "updated_by",
        "last_activity_at",
        "updated_at",
    ]

    if duplicate_of is not None:
        issue.duplicate_of = duplicate_of
        fields.append("duplicate_of")
        record_event(
            issue,
            user,
            TimelineEvent.MARKED_DUPLICATE,
            number=duplicate_of.number,
            title=duplicate_of.title,
        )

    issue.save(update_fields=fields)
    record_event(issue, user, TimelineEvent.CLOSED, reason=reason)
    return issue


def reopen_issue(issue, user):
    """Reopen a closed issue. The close reason is cleared -- it no longer holds."""
    if issue.state == IssueState.OPEN:
        return issue
    issue.state = IssueState.OPEN
    issue.state_reason = ""
    issue.closed_at = None
    issue.closed_by = None
    issue.updated_by = user
    issue.last_activity_at = timezone.now()
    issue.save(
        update_fields=[
            "state",
            "state_reason",
            "closed_at",
            "closed_by",
            "updated_by",
            "last_activity_at",
            "updated_at",
        ]
    )
    record_event(issue, user, TimelineEvent.REOPENED)
    return issue


# ---------------------------------------------------------------------------
# Comments
# ---------------------------------------------------------------------------


def add_comment(issue, user, body, attachment_ids=()):
    """Add a comment and bump the issue's counters."""
    body = (body or "").strip()
    if not body:
        raise IssueError("A comment cannot be empty.")
    if issue.locked:
        raise IssueError("This conversation is locked.")

    comment = IssueComment.objects.create(
        issue=issue, author=user, body=body, created_by=user, updated_by=user
    )
    Issue.objects.filter(pk=issue.pk).update(
        comment_count=F("comment_count") + 1, last_activity_at=timezone.now()
    )
    issue.refresh_from_db(fields=["comment_count", "last_activity_at"])
    claim_attachments(attachment_ids, user=user, issue=issue, comment=comment)
    return comment


def update_comment(comment, user, body):
    """Edit a comment in place, stamping ``edited_at``."""
    body = (body or "").strip()
    if not body:
        raise IssueError("A comment cannot be empty.")
    if body == comment.body:
        return comment
    comment.body = body
    comment.edited_at = timezone.now()
    comment.updated_by = user
    comment.save(update_fields=["body", "edited_at", "updated_by", "updated_at"])
    touch(comment.issue)
    return comment


def delete_comment(comment):
    """Remove a comment and keep the issue's count honest."""
    issue_pk = comment.issue_id
    comment.delete()
    Issue.objects.filter(pk=issue_pk, comment_count__gt=0).update(
        comment_count=F("comment_count") - 1
    )


def timeline(issue):
    """The issue's comments and events, merged into one chronological list.

    Returns a list of ``{"kind": "comment"|"event", "at": datetime, "object": …}``
    so the serializer can render both without the client having to interleave
    two sorted lists itself.
    """
    entries = [
        {"kind": "comment", "at": comment.created_at, "object": comment}
        for comment in issue.comments.select_related("author").prefetch_related(
            "attachments"
        )
    ]
    entries += [
        {"kind": "event", "at": event.created_at, "object": event}
        for event in issue.events.select_related("actor")
    ]
    entries.sort(key=lambda entry: (entry["at"], entry["object"].pk))
    return entries


# ---------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------


def validate_upload(upload):
    """Reject an upload that is too big or of a kind the tracker does not take."""
    extension = os.path.splitext(upload.name or "")[1].lower()
    if extension not in ALLOWED_ATTACHMENT_EXTENSIONS:
        allowed = ", ".join(ALLOWED_ATTACHMENT_EXTENSIONS)
        raise IssueError(f"'{extension or upload.name}' is not accepted. Allowed: {allowed}")
    if upload.size > MAX_ATTACHMENT_BYTES:
        limit_mb = MAX_ATTACHMENT_BYTES // (1024 * 1024)
        raise IssueError(f"That file is larger than {limit_mb} MB.")


def store_upload(upload, user):
    """Save one upload as an unclaimed attachment and hand back the row."""
    validate_upload(upload)
    return IssueAttachment.objects.create(
        file=upload,
        original_filename=upload.name[:255],
        content_type=getattr(upload, "content_type", "") or "",
        size_bytes=upload.size,
        uploaded_by=user,
    )


def claim_attachments(attachment_ids, *, user, issue=None, comment=None):
    """Attach previously uploaded files to the issue or comment now saving.

    Only the uploader's own unclaimed rows can be claimed, so one person cannot
    graft another's upload onto their issue by guessing an id.
    """
    ids = [int(value) for value in attachment_ids or []]
    if not ids:
        return 0
    updates = {}
    if issue is not None:
        updates["issue"] = issue
    if comment is not None:
        updates["comment"] = comment
    if not updates:
        return 0
    return IssueAttachment.objects.filter(
        id__in=ids, uploaded_by=user, issue__isnull=True, comment__isnull=True
    ).update(**updates)


# ---------------------------------------------------------------------------
# Searching
# ---------------------------------------------------------------------------


def _match_users(values, viewer):
    """Resolve ``assignee:`` / ``author:`` values to a user filter.

    A value may be ``@me``, an email, an employee code, or part of a name --
    whatever the person typing happens to know. Returns ``None`` when nothing
    matched, which callers treat as "match nothing" rather than "no filter", so
    ``assignee:nobodyhere`` correctly returns an empty list.
    """
    matched = set()
    for value in values:
        if value.lower() in {"@me", "me"}:
            if viewer is not None and viewer.is_authenticated:
                matched.add(viewer.pk)
            continue
        found = User.objects.filter(
            Q(email__iexact=value)
            | Q(employee_code__iexact=value)
            | Q(full_name__icontains=value)
        ).values_list("pk", flat=True)
        matched.update(found)
    return matched


def search_issues(parsed, viewer, base=None):
    """Apply a :class:`issues.search.ParsedQuery` to the issue queryset."""
    queryset = base if base is not None else Issue.objects.all()

    if parsed.state:
        queryset = queryset.filter(state=parsed.state)
    if parsed.numbers:
        queryset = queryset.filter(number__in=parsed.numbers)
    if parsed.priorities:
        queryset = queryset.filter(priority__in=parsed.priorities)
    if parsed.reasons:
        queryset = queryset.filter(state_reason__in=parsed.reasons)
    if parsed.companies:
        company_filter = Q()
        for value in parsed.companies:
            company_filter |= Q(company__code__iexact=value) | Q(
                company__name__icontains=value
            )
        queryset = queryset.filter(company_filter)

    # Repeated labels AND together (an issue must carry all of them), which is
    # what GitHub does and what "label:bug label:urgent" is asking for.
    for value in parsed.labels:
        queryset = queryset.filter(labels__name__iexact=value)
    for value in parsed.exclude_labels:
        queryset = queryset.exclude(labels__name__iexact=value)

    if parsed.assignees:
        queryset = queryset.filter(
            assignees__pk__in=_match_users(parsed.assignees, viewer)
        )
    if parsed.authors:
        queryset = queryset.filter(author__pk__in=_match_users(parsed.authors, viewer))
    if parsed.commenters:
        queryset = queryset.filter(
            comments__author__pk__in=_match_users(parsed.commenters, viewer)
        )
    if parsed.involves:
        people = _match_users(parsed.involves, viewer)
        queryset = queryset.filter(
            Q(author__pk__in=people)
            | Q(assignees__pk__in=people)
            | Q(comments__author__pk__in=people)
        )

    for empty in parsed.empty:
        if empty == "assignee":
            queryset = queryset.filter(assignees__isnull=True)
        elif empty == "label":
            queryset = queryset.filter(labels__isnull=True)
        elif empty == "flag:pinned":
            queryset = queryset.filter(pinned=True)
        elif empty == "flag:unpinned":
            queryset = queryset.filter(pinned=False)
        elif empty == "flag:locked":
            queryset = queryset.filter(locked=True)
        elif empty == "flag:unlocked":
            queryset = queryset.filter(locked=False)

    if parsed.text:
        queryset = queryset.filter(
            Q(title__icontains=parsed.text) | Q(body__icontains=parsed.text)
        )

    # Joining across the label / assignee / comment M2Ms can duplicate rows.
    return queryset.distinct()


def order_issues(queryset, sort):
    """Order the list. Pinned issues float to the top of every ordering."""
    if sort == "created":
        keys = ["created_at", "number"]
    elif sort == "-comments":
        keys = ["-comment_count", "-number"]
    elif sort == "comments":
        keys = ["comment_count", "number"]
    elif sort == "updated":
        keys = ["last_activity_at", "number"]
    elif sort == "priority":
        queryset = queryset.annotate(
            priority_rank=Case(
                *[
                    When(priority=value, then=Value(rank))
                    for value, rank in PRIORITY_RANK.items()
                ],
                default=Value(99),
                output_field=IntegerField(),
            )
        )
        keys = ["priority_rank", "-last_activity_at"]
    elif sort == "-created":
        keys = ["-created_at", "-number"]
    else:
        # The default, like GitHub's: most recently active first.
        keys = ["-last_activity_at", "-number"]
    return queryset.order_by("-pinned", *keys)


def state_counts(parsed, viewer, base=None):
    """Open / closed totals for a query with its own ``is:`` filter removed.

    The tabs have to show "12 Open / 30 Closed" for the *rest* of the filter,
    the way GitHub does -- if the open count already had ``is:open`` applied,
    the closed tab would always read zero.
    """
    stateless = type(parsed)(**{**parsed.__dict__, "state": ""})
    rows = (
        search_issues(stateless, viewer, base=base)
        # order_by() is load-bearing: Issue.Meta.ordering would otherwise be
        # added to the GROUP BY, returning one row per (state, number) and
        # collapsing to a count of 1.
        .order_by()
        .values("state")
        .annotate(total=Count("id", distinct=True))
    )
    counts = {row["state"]: row["total"] for row in rows}
    return {
        "open": counts.get(IssueState.OPEN, 0),
        "closed": counts.get(IssueState.CLOSED, 0),
    }


def list_queryset():
    """The issue queryset every list endpoint starts from, joins included."""
    return Issue.objects.select_related("author", "company").prefetch_related(
        "labels", "assignees"
    )

