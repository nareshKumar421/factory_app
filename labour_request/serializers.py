from datetime import timedelta

from django.utils import timezone
from rest_framework import serializers

from .models import (
    LabourRequest,
    LabourRequestAudit,
    LabourRequestStatus,
    LabourShift,
)

# How long after a request is deleted it can still be undone.
UNDO_WINDOW_MINUTES = 10

REASON_ERRORS = {
    "required": "Give a reason for this request.",
    "blank": "Give a reason for this request.",
}


class LabourRequestSerializer(serializers.ModelSerializer):
    department_name = serializers.CharField(source="department.name", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    effective_count = serializers.IntegerField(read_only=True)
    is_deleted = serializers.SerializerMethodField()
    can_restore = serializers.SerializerMethodField()
    created_by_name = serializers.SerializerMethodField()
    updated_by_name = serializers.SerializerMethodField()
    decided_by_name = serializers.SerializerMethodField()
    deleted_by_name = serializers.SerializerMethodField()

    class Meta:
        model = LabourRequest
        fields = (
            "id",
            "company",
            "department",
            "department_name",
            "work_date",
            "shift",
            "requested_count",
            "note",
            "status",
            "status_display",
            "approved_count",
            "effective_count",
            "decision_note",
            "decided_at",
            "decided_by_name",
            "is_deleted",
            "can_restore",
            "created_by_name",
            "updated_by_name",
            "deleted_by_name",
            "deleted_at",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields

    def get_is_deleted(self, obj):
        return not obj.is_active

    def get_can_restore(self, obj):
        if obj.is_active or not obj.deleted_at:
            return False
        return timezone.now() - obj.deleted_at <= timedelta(minutes=UNDO_WINDOW_MINUTES)

    def get_created_by_name(self, obj):
        return obj.created_by.full_name if obj.created_by else None

    def get_updated_by_name(self, obj):
        return obj.updated_by.full_name if obj.updated_by else None

    def get_decided_by_name(self, obj):
        return obj.decided_by.full_name if obj.decided_by else None

    def get_deleted_by_name(self, obj):
        return obj.deleted_by.full_name if obj.deleted_by else None


class LabourRequestAuditSerializer(serializers.ModelSerializer):
    action_display = serializers.CharField(source="get_action_display", read_only=True)
    performed_by_name = serializers.SerializerMethodField()

    class Meta:
        model = LabourRequestAudit
        fields = (
            "id",
            "action",
            "action_display",
            "detail",
            "old_value",
            "new_value",
            "performed_by_name",
            "created_at",
        )

    def get_performed_by_name(self, obj):
        return obj.performed_by.full_name if obj.performed_by else None


# ---- request serializers ----


class RaiseRequestSerializer(serializers.Serializer):
    """Create-or-update one department's ask for a day + shift."""

    department = serializers.IntegerField()
    work_date = serializers.DateField()
    shift = serializers.ChoiceField(
        choices=LabourShift.choices, required=False, default=LabourShift.DAY
    )
    requested_count = serializers.IntegerField(min_value=1)
    # The reason -- why the department needs these people. Required, because an
    # approver deciding tomorrow's headcount has nothing to weigh without it.
    note = serializers.CharField(max_length=255, error_messages=REASON_ERRORS)


class UpdateRequestSerializer(serializers.Serializer):
    """Edit an existing request in place.

    The reason may be left out (a count-only edit), but not blanked: an ask
    raised with a reason keeps one.
    """

    requested_count = serializers.IntegerField(min_value=1, required=False)
    note = serializers.CharField(
        max_length=255, required=False, error_messages=REASON_ERRORS
    )

    def validate(self, attrs):
        if not attrs:
            raise serializers.ValidationError("Nothing to update.")
        return attrs


class DecisionSerializer(serializers.Serializer):
    """Approve or reject a pending request.

    ``approved_count`` is only read on an approval and defaults to the full ask;
    it is capped against the request in the view, where the asked-for number is
    known.
    """

    decision = serializers.ChoiceField(
        choices=[LabourRequestStatus.APPROVED, LabourRequestStatus.REJECTED]
    )
    approved_count = serializers.IntegerField(min_value=0, required=False)
    note = serializers.CharField(
        max_length=255, required=False, allow_blank=True, default=""
    )
