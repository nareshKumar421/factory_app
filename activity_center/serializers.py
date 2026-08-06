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


# ---------------------------------------------------------------------------
# Daily job sheet
# ---------------------------------------------------------------------------


class DailyJobSerializer(serializers.Serializer):
    """One job on a day sheet.

    ``done_today`` and ``last_done_at`` are null — never 0 — when ``countable`` is
    false. That distinction is the whole point: a zero reads as "you did nothing",
    null lets the UI say "this job does not record who did it". Do not give these
    fields a default.
    """

    source_key = serializers.CharField()
    label = serializers.CharField()
    module = serializers.CharField()
    cadence = serializers.CharField()
    mode = serializers.CharField()
    countable = serializers.BooleanField()
    done_today = serializers.IntegerField(allow_null=True)
    last_done_at = serializers.DateTimeField(allow_null=True)
    pending_now = serializers.IntegerField()
    oldest_pending_days = serializers.IntegerField(allow_null=True)
    url = serializers.CharField(allow_null=True)


class DailyGroupSerializer(serializers.Serializer):
    """Jobs sharing a cadence. ``counted_jobs`` is 0 for EVENT and PERIODIC."""

    cadence = serializers.CharField()
    title = serializers.CharField()
    counted_jobs = serializers.IntegerField()
    done = serializers.IntegerField()
    jobs = DailyJobSerializer(many=True)


class DailyTallySerializer(serializers.Serializer):
    counted_jobs = serializers.IntegerField()
    done = serializers.IntegerField()
    not_yet = serializers.IntegerField(
        help_text="Tracked jobs with nothing recorded yet. Not 'missed' — we have no roster."
    )
    records_done = serializers.IntegerField()


class DailySheetUserSerializer(serializers.Serializer):
    user_id = serializers.IntegerField()
    full_name = serializers.CharField()
    employee_code = serializers.CharField()


class DailySheetSerializer(serializers.Serializer):
    date = serializers.DateField()
    is_today = serializers.BooleanField()
    user = DailySheetUserSerializer()
    tally = DailyTallySerializer()
    uncounted_jobs = serializers.IntegerField(
        help_text="Jobs shown on the sheet but deliberately excluded from the tally."
    )
    groups = DailyGroupSerializer(many=True)


class DailyBoardRowSerializer(serializers.Serializer):
    """One user's line on the all-users board.

    There is deliberately no score, rank or percentage field. With no attendance data
    we cannot tell an idle day from a day off, so any ratio here would be read as a
    performance measure it cannot support.
    """

    user_id = serializers.IntegerField()
    full_name = serializers.CharField()
    email = serializers.CharField()
    employee_code = serializers.CharField()
    is_superuser = serializers.BooleanField()
    expected_counted = serializers.IntegerField()
    expected_uncounted = serializers.IntegerField()
    jobs_done = serializers.IntegerField(help_text="Distinct kinds of job with a record today.")
    not_yet = serializers.IntegerField()
    records_done = serializers.IntegerField(help_text="Raw record count.")
    first_activity_at = serializers.DateTimeField(allow_null=True)
    last_activity_at = serializers.DateTimeField(allow_null=True)
    modules_touched = serializers.ListField(child=serializers.CharField())


class DailyBoardTotalsSerializer(serializers.Serializer):
    users = serializers.IntegerField()
    with_activity = serializers.IntegerField()
    no_activity_yet = serializers.IntegerField()
    records_done = serializers.IntegerField()


class DailyBoardSerializer(serializers.Serializer):
    date = serializers.DateField()
    is_today = serializers.BooleanField()
    totals = DailyBoardTotalsSerializer()
    users = DailyBoardRowSerializer(many=True)
