"""
sap_reports/serializers.py

Input validation and response shapes for the SAP Reports API.

Result *rows* are deliberately not serialised field-by-field: a report's columns
are only known once it has run, and pushing thousands of already-typed values
through a serialiser would cost time without adding a check. They are returned
as the reader produced them, with the column metadata that describes them.
"""

from rest_framework import serializers

from .models import SapReport, SapReportParameter, SapReportRun
from .parameters import ParameterKind
from .services.runner import MAX_ROW_LIMIT


# ---------------------------------------------------------------------------
# Input
# ---------------------------------------------------------------------------


class RunReportSerializer(serializers.Serializer):
    """Body of a report run: the filter values, and how many rows to bring back."""

    parameters = serializers.DictField(
        required=False,
        default=dict,
        help_text='Values keyed by prompt position, e.g. {"0": "2026-08-01", "1": "2026-08-22"}.',
    )
    row_limit = serializers.IntegerField(
        required=False,
        min_value=1,
        max_value=MAX_ROW_LIMIT,
        help_text="Row ceiling for this run; omit to use the report's default.",
    )


class ExportReportSerializer(RunReportSerializer):
    """A run whose result is streamed back as a file."""

    export_format = serializers.ChoiceField(choices=["csv", "xlsx"], default="xlsx")


class SyncReportsSerializer(serializers.Serializer):
    """Which SAP query category to mirror, and whether to only preview it."""

    category = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text='SAP query category name; blank means the module default ("Factory").',
    )
    all_categories = serializers.BooleanField(
        default=False,
        help_text="Sync every category instead of one. Use sparingly.",
    )
    dry_run = serializers.BooleanField(
        default=False,
        help_text="Report what would change without writing anything.",
    )


class LookupQuerySerializer(serializers.Serializer):
    """Query string for a parameter's picklist."""

    kind = serializers.ChoiceField(choices=[kind for kind, _ in ParameterKind.CHOICES])
    search = serializers.CharField(required=False, allow_blank=True, default="")


class UpdateParameterSerializer(serializers.Serializer):
    """An admin's correction to one prompt's description."""

    position = serializers.IntegerField(min_value=0)
    label = serializers.CharField(max_length=120, required=False)
    kind = serializers.ChoiceField(
        choices=[kind for kind, _ in ParameterKind.CHOICES], required=False
    )
    is_required = serializers.BooleanField(required=False)
    default_value = serializers.CharField(max_length=120, required=False, allow_blank=True)
    blank_value = serializers.CharField(max_length=64, required=False, allow_blank=True)
    help_text = serializers.CharField(max_length=200, required=False, allow_blank=True)


class UpdateReportSerializer(serializers.Serializer):
    """
    The parts of a report an admin owns.

    Everything SAP owns -- the name it is saved under, its SQL -- is absent on
    purpose: editing it here would be silently overwritten by the next sync.
    """

    display_name = serializers.CharField(max_length=200, required=False, allow_blank=True)
    description = serializers.CharField(required=False, allow_blank=True)
    is_enabled = serializers.BooleanField(required=False)
    sort_order = serializers.IntegerField(required=False)
    row_limit = serializers.IntegerField(
        required=False, allow_null=True, min_value=1, max_value=MAX_ROW_LIMIT
    )
    parameters = UpdateParameterSerializer(many=True, required=False)


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


class SapReportParameterSerializer(serializers.ModelSerializer):
    """One filter field, described well enough for the frontend to render it."""

    has_lookup = serializers.BooleanField(read_only=True)

    class Meta:
        model = SapReportParameter
        fields = [
            "position",
            "label",
            "kind",
            "is_required",
            "default_value",
            "help_text",
            "has_lookup",
            "occurrences",
            "is_customised",
        ]


class SapReportListSerializer(serializers.ModelSerializer):
    """A report as it appears in the module's list."""

    title = serializers.CharField(read_only=True)
    parameter_count = serializers.IntegerField(source="parameters.count", read_only=True)

    class Meta:
        model = SapReport
        fields = [
            "slug",
            "title",
            "sap_name",
            "display_name",
            "description",
            "sap_category_name",
            "statement_kind",
            "parameter_count",
            "is_enabled",
            "is_runnable",
            "not_runnable_reason",
            "is_missing_in_sap",
            "sort_order",
            "last_run_at",
            "last_synced_at",
        ]


class SapReportDetailSerializer(SapReportListSerializer):
    """A report plus the filters it asks for."""

    parameters = SapReportParameterSerializer(many=True, read_only=True)

    class Meta(SapReportListSerializer.Meta):
        fields = SapReportListSerializer.Meta.fields + [
            "parameters",
            "row_limit",
            "effective_row_limit",
            "sap_changed_at",
        ]


class SapReportSqlSerializer(serializers.ModelSerializer):
    """The saved query's SQL. Only ever shown to someone who can manage reports."""

    class Meta:
        model = SapReport
        fields = ["slug", "sap_name", "sql_text", "sql_hash", "statement_kind"]


class SapReportRunSerializer(serializers.ModelSerializer):
    """One line of a report's run history."""

    run_by_name = serializers.CharField(source="run_by.username", read_only=True, default="")
    report_title = serializers.CharField(source="report.title", read_only=True)

    class Meta:
        model = SapReportRun
        fields = [
            "id",
            "report_title",
            "run_by_name",
            "parameters",
            "status",
            "row_count",
            "was_truncated",
            "duration_ms",
            "export_format",
            "error_message",
            "created_at",
        ]


class CategorySerializer(serializers.Serializer):
    """A SAP query category, as offered to an admin before a sync."""

    category_id = serializers.IntegerField()
    category_name = serializers.CharField()
    query_count = serializers.IntegerField()


class LookupOptionSerializer(serializers.Serializer):
    value = serializers.CharField()
    label = serializers.CharField()
