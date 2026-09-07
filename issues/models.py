"""
Issue tracker -- the software's own bug list, kept in the software.

Modelled on a GitHub issue list because that is the shape people already know:
an issue has a **number**, a title, a markdown body, an author, a state
(open / closed), any number of **labels** and **assignees**, and a **timeline**
made of comments and events.

Six tables carry it:

* :class:`IssueLabel`   -- the label master ("bug", "SAP", "urgent"). A team
  maintains its own labels; nothing is hardcoded.
* :class:`IssueArea`    -- which part of the software an issue is about
  ("Dispatch", "Gate", "Production"). Also a master row.
* :class:`Issue`        -- the issue itself.
* :class:`IssueComment` -- the conversation.
* :class:`IssueEvent`   -- everything else that happened to it (closed,
  labeled, assigned, renamed), so the timeline can show the history and not
  just the latest values.
* :class:`IssueAttachment` -- screenshots and logs, referenced from the
  markdown body of an issue or a comment.

``Issue.number`` is the human handle -- "#41" -- and is allocated sequentially
by :func:`issues.services.create_issue`, never by the caller. The primary key
stays the surrogate ``id``; every URL and every mention uses the number.
"""

import os

from django.conf import settings
from django.db import models
from django.utils import timezone

from gate_core.models.base import BaseModel

from .constants import (
    MAX_TITLE,
    IssuePriority,
    IssueState,
    StateReason,
    TimelineEvent,
)


class IssuePermission(models.Model):
    """Sentinel model carrying the module's permissions (no table of its own).

    Four rights, split by what they let someone do rather than by screen:
    read the list, file a new issue, triage anyone's issue (label, assign,
    close, reopen, edit), and maintain the label / area masters. Filing is
    separated from triage on purpose -- everyone who uses the software should be
    able to report a problem, while only the people who own the backlog should
    be moving other people's issues around.
    """

    class Meta:
        managed = False
        default_permissions = ()
        verbose_name = "Issue Tracker"
        verbose_name_plural = "Issue Tracker"
        permissions = [
            ("can_view_issues", "Can view the issue tracker"),
            ("can_create_issues", "Can report a new issue"),
            (
                "can_triage_issues",
                "Can triage any issue (label, assign, close, reopen, edit)",
            ),
            ("can_manage_issue_settings", "Can manage issue labels and areas"),
        ]


class IssueNumberSequence(models.Model):
    """The tracker's issue-number counter. Exactly one row, never deleted.

    A counter rather than ``MAX(number) + 1`` because a number must never be
    handed out twice: #7 gets quoted in a chat message and referenced in a
    commit long after the issue itself is deleted, and a second #7 would make
    both references wrong. It also makes allocation a single locked row rather
    than a read-then-insert race -- see
    :func:`issues.services.allocate_issue_number`.
    """

    #: The one row's primary key. Fixed so the allocator can address it directly.
    SINGLETON_PK = 1

    last_number = models.PositiveIntegerField(default=0)

    class Meta:
        verbose_name = "Issue Number Sequence"
        verbose_name_plural = "Issue Number Sequence"

    def __str__(self):
        return f"last issue number: {self.last_number}"


class IssueLabel(BaseModel):
    """One label.

    Colour is stored so the list looks like the label chips people are used to,
    and is a plain hex string rather than a palette enum -- a team inventing
    "regression" should be able to pick its colour too.
    """

    name = models.CharField(max_length=50, unique=True)
    color = models.CharField(
        max_length=7,
        default="#6b7280",
        help_text="Chip colour as a hex string, e.g. '#d73a4a'.",
    )
    description = models.CharField(max_length=200, blank=True, default="")
    sequence = models.PositiveSmallIntegerField(
        default=0, help_text="Order in the label picker."
    )

    class Meta:
        ordering = ["sequence", "name"]
        verbose_name = "Issue Label"
        verbose_name_plural = "Issue Labels"

    def __str__(self):
        return self.name


class IssueArea(BaseModel):
    """A part of the software issues get filed against.

    Roughly one row per module in the app's sidebar. It is a master rather than
    a choices list because the app grows a module every few weeks, and because
    ``owners`` lets a new issue in an area suggest who normally picks it up.
    """

    name = models.CharField(max_length=100, unique=True)
    code = models.CharField(
        max_length=40,
        unique=True,
        help_text="Short slug used in the search box, e.g. 'dispatch'.",
    )
    description = models.CharField(max_length=200, blank=True, default="")
    owners = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        blank=True,
        related_name="owned_issue_areas",
        help_text="Suggested assignees for a new issue in this area.",
    )
    sequence = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["sequence", "name"]
        verbose_name = "Issue Area"
        verbose_name_plural = "Issue Areas"

    def __str__(self):
        return self.name


class Issue(BaseModel):
    """One reported issue.

    ``last_activity_at`` is denormalised so the default "recently updated"
    ordering does not have to reach into the comment table, and
    ``comment_count`` so the list can show the speech-bubble count without a
    per-row aggregate. Both are maintained in :mod:`issues.services`.
    """

    number = models.PositiveIntegerField(
        unique=True,
        editable=False,
        help_text="The issue's human handle, e.g. 41 for '#41'. Allocated on create.",
    )
    title = models.CharField(max_length=MAX_TITLE)
    body = models.TextField(
        blank=True,
        default="",
        help_text="Markdown. Rendered read-only by the client.",
    )

    state = models.CharField(
        max_length=10, choices=IssueState.choices, default=IssueState.OPEN
    )
    state_reason = models.CharField(
        max_length=15, choices=StateReason.choices, blank=True, default=""
    )
    priority = models.CharField(
        max_length=10, choices=IssuePriority.choices, default=IssuePriority.MEDIUM
    )

    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="issues_reported",
    )
    assignees = models.ManyToManyField(
        settings.AUTH_USER_MODEL, blank=True, related_name="issues_assigned"
    )
    labels = models.ManyToManyField(IssueLabel, blank=True, related_name="issues")
    area = models.ForeignKey(
        IssueArea,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="issues",
    )
    company = models.ForeignKey(
        "company.Company",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="issues",
        help_text="The company unit the reporter was working in, when it matters.",
    )

    page_url = models.CharField(
        max_length=500,
        blank=True,
        default="",
        help_text=(
            "Where in the app it happened, e.g. '/dispatch/bills-linking'. "
            "Captured from the reporter's browser so a bug report says where to look."
        ),
    )

    duplicate_of = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="duplicates",
    )

    locked = models.BooleanField(
        default=False, help_text="Locked conversations take no new comments."
    )
    pinned = models.BooleanField(
        default=False, help_text="Pinned issues sort to the top of the open list."
    )

    comment_count = models.PositiveIntegerField(default=0, editable=False)
    last_activity_at = models.DateTimeField(default=timezone.now, editable=False)

    closed_at = models.DateTimeField(null=True, blank=True)
    closed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="issues_closed",
    )

    class Meta:
        ordering = ["-number"]
        verbose_name = "Issue"
        verbose_name_plural = "Issues"
        indexes = [
            models.Index(fields=["state", "-last_activity_at"]),
            models.Index(fields=["-number"]),
        ]

    def __str__(self):
        return f"#{self.number} {self.title}"

    @property
    def is_open(self):
        return self.state == IssueState.OPEN


class IssueComment(BaseModel):
    """One comment on an issue.

    Editing keeps the row and stamps ``edited_at`` rather than rewriting history
    silently.
    """

    issue = models.ForeignKey(Issue, on_delete=models.CASCADE, related_name="comments")
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="issue_comments",
    )
    body = models.TextField(help_text="Markdown.")
    edited_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["created_at", "id"]
        verbose_name = "Issue Comment"
        verbose_name_plural = "Issue Comments"

    def __str__(self):
        return f"Comment on issue {self.issue_id}"


class IssueEvent(models.Model):
    """A non-comment thing that happened to an issue.

    Append-only: nothing edits or deletes an event, which is what makes the
    timeline trustworthy. ``detail`` holds whatever the event is about -- the
    label name and colour, the assignee's name, the old and new title -- frozen
    at the time it happened, so a renamed label does not rewrite the past.
    """

    issue = models.ForeignKey(Issue, on_delete=models.CASCADE, related_name="events")
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="issue_events",
    )
    event = models.CharField(max_length=20, choices=TimelineEvent.choices)
    detail = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]
        verbose_name = "Issue Event"
        verbose_name_plural = "Issue Events"
        indexes = [models.Index(fields=["issue", "created_at"])]

    def __str__(self):
        return f"{self.event} on issue {self.issue_id}"


class IssueAttachment(models.Model):
    """A file uploaded for an issue -- almost always a screenshot.

    ``issue`` and ``comment`` are both nullable because the upload happens while
    the reporter is still typing: the client uploads first, gets a URL back to
    embed in the markdown, and the row is claimed by the issue or comment on
    submit. An unclaimed row is a draft the reporter abandoned.
    """

    issue = models.ForeignKey(
        Issue,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="attachments",
    )
    comment = models.ForeignKey(
        IssueComment,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="attachments",
    )
    file = models.FileField(upload_to="issue_attachments/%Y/%m/")
    original_filename = models.CharField(max_length=255)
    content_type = models.CharField(max_length=120, blank=True, default="")
    size_bytes = models.PositiveIntegerField(default=0)
    uploaded_at = models.DateTimeField(auto_now_add=True)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="issue_attachments",
    )

    #: Extensions that render inline in the client rather than as a download.
    IMAGE_EXTENSIONS = frozenset(
        {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
    )

    class Meta:
        ordering = ["id"]
        verbose_name = "Issue Attachment"
        verbose_name_plural = "Issue Attachments"

    def __str__(self):
        return self.original_filename

    @property
    def is_image(self):
        extension = os.path.splitext(self.original_filename)[1].lower()
        return extension in self.IMAGE_EXTENSIONS
