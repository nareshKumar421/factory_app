from rest_framework import serializers

from .models import SapPosting, SapPostingAttempt
from .services import kind_label


def _name(user):
    if user is None:
        return ""
    return getattr(user, "full_name", "") or user.get_username()


class SapPostingAttemptSerializer(serializers.ModelSerializer):
    outcome_label = serializers.CharField(source="get_outcome_display", read_only=True)

    class Meta:
        model = SapPostingAttempt
        fields = [
            "number", "by_worker", "started_at", "finished_at",
            "outcome", "outcome_label", "message", "detail",
        ]


class SapPostingSerializer(serializers.ModelSerializer):
    status_label = serializers.CharField(source="get_status_display", read_only=True)
    company_code = serializers.CharField(source="company.code", read_only=True)
    kind_label = serializers.SerializerMethodField()
    created_by_name = serializers.SerializerMethodField()

    class Meta:
        model = SapPosting
        fields = [
            "id", "company_code", "kind", "kind_label", "source_id", "title", "link",
            "status", "status_label", "attempts", "next_attempt_at", "last_error",
            "result", "posted_at", "created_by_name", "created_at", "updated_at",
            "cancel_reason",
        ]

    def get_created_by_name(self, obj):
        return _name(obj.created_by)

    def get_kind_label(self, obj):
        return kind_label(obj.kind)


class SapPostingDetailSerializer(SapPostingSerializer):
    attempts_log = SapPostingAttemptSerializer(source="attempt_log", many=True, read_only=True)
    cancelled_by_name = serializers.SerializerMethodField()

    class Meta(SapPostingSerializer.Meta):
        fields = SapPostingSerializer.Meta.fields + ["params", "attempts_log", "cancelled_by_name"]

    def get_cancelled_by_name(self, obj):
        return _name(obj.cancelled_by)
