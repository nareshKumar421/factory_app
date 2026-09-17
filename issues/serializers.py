"""
Serializers for the issue tracker.

Two shapes per resource, for the same reason GitHub's API has them: a **list**
shape carrying just what a row needs (title, labels, assignee avatars, comment
count) and a **detail** shape carrying the body, the timeline and everything the
sidebar edits. The list of 400 issues does not need 400 bodies.

Writes are plain ``Serializer`` classes rather than ``ModelSerializer`` saves --
every change goes through :mod:`issues.services` so that it writes its timeline
event, so the serializers validate and hand over rather than calling ``save()``.
"""

from rest_framework import serializers

from accounts.models import User
from company.models import Company

from .constants import MAX_TITLE, IssuePriority, StateReason
from .models import (
    Issue,
    IssueAttachment,
    IssueComment,
    IssueEvent,
    IssueLabel,
    SupportContact,
)


class UserBriefSerializer(serializers.Serializer):
    """A person as the tracker shows them: name, initials for the avatar, email."""

    id = serializers.IntegerField()
    name = serializers.SerializerMethodField()
    email = serializers.EmailField()
    employee_code = serializers.CharField()
    initials = serializers.SerializerMethodField()

    def get_name(self, obj):
        # accounts.User has `full_name`, not Django's get_full_name().
        return obj.full_name or obj.email

    def get_initials(self, obj):
        source = (obj.full_name or obj.email or "").strip()
        parts = [part for part in source.replace(".", " ").split() if part]
        if not parts:
            return "?"
        if len(parts) == 1:
            return parts[0][:2].upper()
        return (parts[0][0] + parts[-1][0]).upper()


class SupportContactSerializer(serializers.ModelSerializer):
    """The support number, plus the form a phone can dial.

    ``dial`` is derived on the way out (see :attr:`SupportContact.dial`) so no
    client has to guess how to strip the spaces out of a number somebody
    typed.
    """

    dial = serializers.CharField(read_only=True)
    updated_by_name = serializers.CharField(
        source="updated_by.full_name", read_only=True, default=""
    )

    class Meta:
        model = SupportContact
        fields = ["phone", "dial", "updated_at", "updated_by_name"]
        read_only_fields = ["updated_at"]

    def validate_phone(self, value):
        """Blank, or something a phone could actually dial.

        Blank is meaningful -- it takes the support line off every screen --
        but a number with four digits in it is a typo or a placeholder, and
        publishing it to every user is worse than publishing nothing.
        """
        cleaned = value.strip()
        if not cleaned:
            return ""
        digits = sum(character.isdigit() for character in cleaned)
        if digits < 7:
            raise serializers.ValidationError(
                "That does not look like a phone number. Leave it blank to "
                "hide the support number instead."
            )
        return cleaned


class IssueLabelSerializer(serializers.ModelSerializer):
    open_issues = serializers.IntegerField(read_only=True, required=False)

    class Meta:
        model = IssueLabel
        fields = ["id", "name", "color", "description", "sequence", "open_issues"]


class IssueAttachmentSerializer(serializers.ModelSerializer):
    url = serializers.SerializerMethodField()
    is_image = serializers.BooleanField(read_only=True)

    class Meta:
        model = IssueAttachment
        fields = [
            "id",
            "original_filename",
            "content_type",
            "size_bytes",
            "uploaded_at",
            "url",
            "is_image",
        ]

    def get_url(self, obj):
        if not obj.file:
            return None
        request = self.context.get("request")
        url = obj.file.url
        return request.build_absolute_uri(url) if request else url


class IssueListSerializer(serializers.ModelSerializer):
    """One row of the issue list."""

    author = UserBriefSerializer(read_only=True)
    assignees = UserBriefSerializer(many=True, read_only=True)
    labels = IssueLabelSerializer(many=True, read_only=True)
    company_code = serializers.CharField(
        source="company.code", default="", read_only=True
    )
    state_reason_display = serializers.CharField(
        source="get_state_reason_display", read_only=True
    )
    priority_display = serializers.CharField(
        source="get_priority_display", read_only=True
    )

    class Meta:
        model = Issue
        fields = [
            "id",
            "number",
            "title",
            "state",
            "state_reason",
            "state_reason_display",
            "priority",
            "priority_display",
            "author",
            "assignees",
            "labels",
            "company",
            "company_code",
            "pinned",
            "locked",
            "comment_count",
            "created_at",
            "last_activity_at",
            "closed_at",
        ]


class IssueDetailSerializer(IssueListSerializer):
    """The issue page: everything a row has, plus the body and its files."""

    closed_by = UserBriefSerializer(read_only=True)
    attachments = serializers.SerializerMethodField()
    duplicate_of_number = serializers.IntegerField(
        source="duplicate_of.number", default=None, read_only=True
    )
    duplicate_of_title = serializers.CharField(
        source="duplicate_of.title", default="", read_only=True
    )

    class Meta(IssueListSerializer.Meta):
        fields = IssueListSerializer.Meta.fields + [
            "body",
            "page_url",
            "closed_by",
            "attachments",
            "duplicate_of",
            "duplicate_of_number",
            "duplicate_of_title",
            "updated_at",
        ]

    def get_attachments(self, obj):
        # Only the issue's own files -- a comment's files ride with the comment.
        rows = [row for row in obj.attachments.all() if row.comment_id is None]
        return IssueAttachmentSerializer(rows, many=True, context=self.context).data


class IssueCommentSerializer(serializers.ModelSerializer):
    author = UserBriefSerializer(read_only=True)
    attachments = IssueAttachmentSerializer(many=True, read_only=True)

    class Meta:
        model = IssueComment
        fields = [
            "id",
            "body",
            "author",
            "created_at",
            "edited_at",
            "attachments",
        ]


class IssueEventSerializer(serializers.ModelSerializer):
    actor = UserBriefSerializer(read_only=True)
    event_display = serializers.CharField(source="get_event_display", read_only=True)

    class Meta:
        model = IssueEvent
        fields = ["id", "event", "event_display", "actor", "detail", "created_at"]


class TimelineEntrySerializer(serializers.Serializer):
    """One row of the merged comment + event timeline."""

    kind = serializers.CharField()
    at = serializers.DateTimeField()
    comment = serializers.SerializerMethodField()
    event = serializers.SerializerMethodField()

    def get_comment(self, entry):
        if entry["kind"] != "comment":
            return None
        return IssueCommentSerializer(entry["object"], context=self.context).data

    def get_event(self, entry):
        if entry["kind"] != "event":
            return None
        return IssueEventSerializer(entry["object"], context=self.context).data


# ---------------------------------------------------------------------------
# Write bodies
# ---------------------------------------------------------------------------


def _assignable_users():
    """Who may be assigned an issue: any active user.

    Deliberately not narrowed to holders of the triage right -- an issue is
    often assigned to the person who knows the answer rather than to whoever
    owns the backlog.
    """
    return User.objects.filter(is_active=True)


class IssueCreateSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=MAX_TITLE)
    body = serializers.CharField(required=False, allow_blank=True, default="")
    priority = serializers.ChoiceField(
        choices=IssuePriority.choices, required=False, default=IssuePriority.MEDIUM
    )
    company = serializers.PrimaryKeyRelatedField(
        queryset=Company.objects.all(), required=False, allow_null=True
    )
    label_ids = serializers.PrimaryKeyRelatedField(
        many=True, queryset=IssueLabel.objects.all(), required=False, default=list
    )
    assignee_ids = serializers.PrimaryKeyRelatedField(
        many=True, queryset=_assignable_users(), required=False, default=list
    )
    page_url = serializers.CharField(
        max_length=500, required=False, allow_blank=True, default=""
    )
    attachment_ids = serializers.ListField(
        child=serializers.IntegerField(), required=False, default=list
    )


class IssueUpdateSerializer(serializers.Serializer):
    """A partial edit. Every field is optional; only what is sent is changed."""

    title = serializers.CharField(max_length=MAX_TITLE, required=False)
    body = serializers.CharField(required=False, allow_blank=True)
    priority = serializers.ChoiceField(choices=IssuePriority.choices, required=False)
    company = serializers.PrimaryKeyRelatedField(
        queryset=Company.objects.all(), required=False, allow_null=True
    )
    label_ids = serializers.PrimaryKeyRelatedField(
        many=True, queryset=IssueLabel.objects.all(), required=False
    )
    assignee_ids = serializers.PrimaryKeyRelatedField(
        many=True, queryset=_assignable_users(), required=False
    )
    page_url = serializers.CharField(max_length=500, required=False, allow_blank=True)
    pinned = serializers.BooleanField(required=False)
    locked = serializers.BooleanField(required=False)

    def to_changes(self):
        """Map the validated body onto the keyword names services expects."""
        data = dict(self.validated_data)
        changes = {}
        for key in ("title", "body", "priority", "company", "page_url", "pinned", "locked"):
            if key in data:
                changes[key] = data[key]
        if "label_ids" in data:
            changes["labels"] = data["label_ids"]
        if "assignee_ids" in data:
            changes["assignees"] = data["assignee_ids"]
        return changes


class IssueStateSerializer(serializers.Serializer):
    """Close or reopen."""

    state = serializers.ChoiceField(choices=["OPEN", "CLOSED"])
    reason = serializers.ChoiceField(
        choices=StateReason.choices, required=False, default=StateReason.COMPLETED
    )
    duplicate_of = serializers.PrimaryKeyRelatedField(
        queryset=Issue.objects.all(), required=False, allow_null=True
    )


class CommentWriteSerializer(serializers.Serializer):
    body = serializers.CharField()
    attachment_ids = serializers.ListField(
        child=serializers.IntegerField(), required=False, default=list
    )
