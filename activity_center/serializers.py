from rest_framework import serializers


class PendingActivitySerializer(serializers.Serializer):
    """One outstanding job, derived from a live record in its owning module."""

    source_key = serializers.CharField()
    label = serializers.CharField()
    module = serializers.CharField()
    mode = serializers.CharField(help_text="OWNED = yours alone; QUEUE = shared queue.")
    permission = serializers.CharField()
    record_id = serializers.IntegerField()
    reference = serializers.CharField(allow_null=True)
    status = serializers.CharField(allow_null=True)
    since = serializers.DateTimeField(allow_null=True)
    age_days = serializers.IntegerField(allow_null=True)
    is_overdue = serializers.BooleanField()
    url = serializers.CharField(allow_null=True)


class CompletedActivitySerializer(serializers.Serializer):
    """A job the user demonstrably finished — the record carries them as the actor."""

    source_key = serializers.CharField()
    label = serializers.CharField()
    module = serializers.CharField()
    record_id = serializers.IntegerField()
    reference = serializers.CharField(allow_null=True)
    status = serializers.CharField(allow_null=True)
    completed_at = serializers.DateTimeField(allow_null=True)
    url = serializers.CharField(allow_null=True)


class ModuleBreakdownSerializer(serializers.Serializer):
    module = serializers.CharField()
    pending = serializers.IntegerField()
    overdue = serializers.IntegerField()
    completed = serializers.IntegerField()


class ActivitySummarySerializer(serializers.Serializer):
    pending = serializers.IntegerField()
    overdue = serializers.IntegerField()
    owned = serializers.IntegerField()
    queued = serializers.IntegerField()
    completed = serializers.IntegerField()
    since = serializers.DateTimeField()
    modules = ModuleBreakdownSerializer(many=True)


class UserActivityRowSerializer(serializers.Serializer):
    """A row in the supervisor overview."""

    user_id = serializers.IntegerField()
    full_name = serializers.CharField()
    email = serializers.CharField()
    employee_code = serializers.CharField()
    is_superuser = serializers.BooleanField()
    owned_pending = serializers.IntegerField(help_text="Jobs assigned to this user alone.")
    owned_overdue = serializers.IntegerField()
    queue_pending = serializers.IntegerField(
        help_text="Shared-queue jobs visible to this user; also counted for other holders."
    )
    completed = serializers.IntegerField()


class ActivityDefinitionSerializer(serializers.Serializer):
    source_key = serializers.CharField()
    label = serializers.CharField()
    module = serializers.CharField()
    mode = serializers.CharField()
    permission = serializers.CharField()
    model = serializers.CharField()
    overdue_after_days = serializers.IntegerField()
    is_mine = serializers.BooleanField()
