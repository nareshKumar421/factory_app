"""
Fixed vocabularies for the issue tracker.

Everything a team would want to *maintain* -- the labels and the areas of the
software -- is a master row instead (see :class:`issues.models.IssueLabel` and
:class:`issues.models.IssueArea`). What lives here is the small set of states
and event kinds the code itself branches on, which is why they are choices
rather than tables.
"""

from django.db import models


class IssueState(models.TextChoices):
    OPEN = "OPEN", "Open"
    CLOSED = "CLOSED", "Closed"


class StateReason(models.TextChoices):
    """Why an issue was closed -- GitHub's "completed / not planned" split.

    It matters for reporting: a bug closed as fixed and a bug closed as
    "won't do" are both closed, but only one of them was work.
    """

    COMPLETED = "COMPLETED", "Completed"
    NOT_PLANNED = "NOT_PLANNED", "Not planned"
    DUPLICATE = "DUPLICATE", "Duplicate"


class IssuePriority(models.TextChoices):
    URGENT = "URGENT", "Urgent"
    HIGH = "HIGH", "High"
    MEDIUM = "MEDIUM", "Medium"
    LOW = "LOW", "Low"


#: Sort order for priority, most urgent first. Used by the ``sort=priority``
#: ordering, because alphabetical on the stored value is meaningless.
PRIORITY_RANK = {
    IssuePriority.URGENT: 0,
    IssuePriority.HIGH: 1,
    IssuePriority.MEDIUM: 2,
    IssuePriority.LOW: 3,
}


class TimelineEvent(models.TextChoices):
    """The non-comment entries on an issue's timeline.

    Every one of these is written by :mod:`issues.services` as a side effect of
    the change it describes, so the timeline is a record of what happened rather
    than a second source of truth. ``detail`` carries the specifics (which label,
    which assignee, the old and new title).
    """

    OPENED = "OPENED", "Opened"
    CLOSED = "CLOSED", "Closed"
    REOPENED = "REOPENED", "Reopened"
    LABELED = "LABELED", "Labeled"
    UNLABELED = "UNLABELED", "Unlabeled"
    ASSIGNED = "ASSIGNED", "Assigned"
    UNASSIGNED = "UNASSIGNED", "Unassigned"
    RENAMED = "RENAMED", "Renamed"
    EDITED = "EDITED", "Description edited"
    PRIORITY_CHANGED = "PRIORITY_CHANGED", "Priority changed"
    AREA_CHANGED = "AREA_CHANGED", "Area changed"
    MARKED_DUPLICATE = "MARKED_DUPLICATE", "Marked as duplicate"
    PINNED = "PINNED", "Pinned"
    UNPINNED = "UNPINNED", "Unpinned"
    LOCKED = "LOCKED", "Locked"
    UNLOCKED = "UNLOCKED", "Unlocked"


#: Longest an issue title may be. Matches the model field.
MAX_TITLE = 250

#: Biggest attachment the tracker accepts, in bytes. Screenshots of a broken
#: screen are the common case; a 10 MB ceiling takes those and a short screen
#: recording without letting the media volume fill up.
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024

#: Extensions the tracker accepts. Deliberately narrow: an issue attachment is
#: evidence (a screenshot, a log, a spreadsheet), never something executable.
ALLOWED_ATTACHMENT_EXTENSIONS = (
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp",
    ".pdf", ".txt", ".log", ".csv", ".json",
    ".xlsx", ".xls", ".docx", ".doc",
    ".mp4", ".webm", ".zip",
)

#: The group every account is meant to hold. Reporting a problem in the
#: software is not a privilege, so this one is handed out to everybody: new
#: accounts pick it up in ``issues.signals``, and existing accounts are
#: backfilled by ``manage.py setup_issue_groups --assign-everyone``.
REPORTER_GROUP = "Issue Reporter"

#: The groups that already imply reporting, so the backfill leaves them alone.
TRIAGE_GROUPS = ("Issue Maintainer", "Issue Admin")
