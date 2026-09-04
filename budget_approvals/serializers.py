"""
budget_approvals/serializers.py

DRF serializers for validating query parameters and shaping API responses.
"""

import json

from rest_framework import serializers

from .services import (
    COLUMN_FILTER_FIELDS,
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    SORTABLE_FIELDS,
)


class ColumnFiltersField(serializers.CharField):
    """
    Excel-style column filters arrive as a JSON object in one query param:
    ?column_filters={"owner":["naresh"],"sub_budget":["DIESEL","POWER"]}
    Validates into {field: [values]} with only known filterable fields.
    """

    def __init__(self, **kwargs):
        kwargs.setdefault("required", False)
        # DRF uses defaults verbatim (to_internal_value is skipped), so the
        # default must already be the parsed shape.
        kwargs.setdefault("default", dict)
        kwargs.setdefault("allow_blank", True)
        super().__init__(**kwargs)

    def to_internal_value(self, data):
        raw = super().to_internal_value(data).strip()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except ValueError:
            raise serializers.ValidationError("column_filters must be valid JSON.")
        if not isinstance(parsed, dict):
            raise serializers.ValidationError("column_filters must be a JSON object.")

        cleaned = {}
        for field, values in parsed.items():
            if field not in COLUMN_FILTER_FIELDS:
                raise serializers.ValidationError(
                    f"'{field}' is not a filterable column."
                )
            if not isinstance(values, list) or not all(
                isinstance(v, str) for v in values
            ):
                raise serializers.ValidationError(
                    f"Values for '{field}' must be a list of strings."
                )
            if values:
                cleaned[field] = values
        return cleaned


# ---------------------------------------------------------------------------
# Query Parameter Serializers (Input Validation)
# ---------------------------------------------------------------------------


class BudgetApprovalFilterSerializer(serializers.Serializer):
    """Validates query parameters for the budget approvals report endpoint."""

    status = serializers.ChoiceField(
        required=False,
        default="",
        choices=["", "pending", "approved", "rejected"],
        allow_blank=True,
        help_text="Approval status; omit for all statuses",
    )
    branch = serializers.CharField(
        required=False,
        default="",
        allow_blank=True,
        help_text="Branch label from the procedure (OIL / BEVERAGE)",
    )
    effect_month = serializers.CharField(
        required=False,
        default="",
        allow_blank=True,
        help_text="Effect month exactly as stamped on the draft line (MM-YYYY)",
    )
    search = serializers.CharField(
        required=False,
        default="",
        allow_blank=True,
        help_text="Free text across vendor, account, owner, remarks and doc entry",
    )
    column_filters = ColumnFiltersField(
        help_text='JSON object of per-column value filters, e.g. {"owner":["naresh"]}',
    )
    sort_by = serializers.ChoiceField(
        required=False,
        default="",
        choices=[""] + sorted(SORTABLE_FIELDS),
        allow_blank=True,
        help_text="Column to sort by; omit for newest-first",
    )
    sort_dir = serializers.ChoiceField(
        required=False,
        default="desc",
        choices=["asc", "desc"],
    )
    page = serializers.IntegerField(required=False, default=1, min_value=1)
    page_size = serializers.IntegerField(
        required=False,
        default=DEFAULT_PAGE_SIZE,
        min_value=1,
        max_value=MAX_PAGE_SIZE,
    )
    refresh = serializers.BooleanField(
        required=False,
        default=False,
        help_text="Bypass the short-lived server cache and re-read SAP",
    )


class ColumnValuesFilterSerializer(BudgetApprovalFilterSerializer):
    """Same filter surface as the report, plus which column to enumerate."""

    field = serializers.ChoiceField(choices=sorted(COLUMN_FILTER_FIELDS))


# ---------------------------------------------------------------------------
# Response Serializers (Output Shape)
# ---------------------------------------------------------------------------


class BudgetApprovalLineSerializer(serializers.Serializer):
    """One draft approval line from DRAFT_APPROVAL_Budget."""

    branch = serializers.CharField()
    doc_entry = serializers.IntegerField()
    obj_type = serializers.CharField()
    obj_type_label = serializers.CharField()
    line_num = serializers.IntegerField(allow_null=True)
    acct_code = serializers.CharField(allow_blank=True)
    acct_name = serializers.CharField(allow_blank=True)
    card_code = serializers.CharField(allow_blank=True)
    card_name = serializers.CharField(allow_blank=True)
    effect_month = serializers.CharField(allow_blank=True)
    budget = serializers.CharField(allow_blank=True)
    sub_budget = serializers.CharField(allow_blank=True)
    state = serializers.CharField(allow_blank=True)
    doc_date = serializers.CharField(allow_null=True)
    amount = serializers.FloatField()
    current_month = serializers.CharField(allow_blank=True)
    current_month_posted_amount = serializers.FloatField()
    status = serializers.CharField(allow_blank=True)
    owner = serializers.CharField(allow_blank=True)
    approver = serializers.CharField(allow_blank=True)
    created_date = serializers.CharField(allow_null=True)
    created_time = serializers.CharField(allow_blank=True)
    line_remarks = serializers.CharField(allow_blank=True)
    comments = serializers.CharField(allow_blank=True)
    process_status = serializers.CharField(allow_blank=True)
    update_date = serializers.CharField(allow_null=True)
    ocr_code = serializers.CharField(allow_blank=True)


class StatusSummarySerializer(serializers.Serializer):
    status = serializers.CharField()
    status_label = serializers.CharField()
    line_count = serializers.IntegerField()
    total_amount = serializers.FloatField()


class ReportSummarySerializer(serializers.Serializer):
    total_lines = serializers.IntegerField()
    total_documents = serializers.IntegerField()
    total_amount = serializers.FloatField()
    pending_lines = serializers.IntegerField()
    pending_amount = serializers.FloatField()
    by_status = StatusSummarySerializer(many=True)


class ReportOptionsSerializer(serializers.Serializer):
    branches = serializers.ListField(child=serializers.CharField())
    effect_months = serializers.ListField(child=serializers.CharField())


class ReportMetaSerializer(serializers.Serializer):
    budget = serializers.CharField()
    page = serializers.IntegerField()
    page_size = serializers.IntegerField()
    total_rows = serializers.IntegerField()
    total_pages = serializers.IntegerField()
    fetched_at = serializers.CharField()
    from_cache = serializers.BooleanField()


class BudgetApprovalReportResponseSerializer(serializers.Serializer):
    data = BudgetApprovalLineSerializer(many=True)
    summary = ReportSummarySerializer()
    options = ReportOptionsSerializer()
    meta = ReportMetaSerializer()


# ---------------------------------------------------------------------------
# Column Values Response (Excel-style filter dropdowns)
# ---------------------------------------------------------------------------


class ColumnValueSerializer(serializers.Serializer):
    value = serializers.CharField(allow_blank=True)
    count = serializers.IntegerField()


class ColumnValuesMetaSerializer(serializers.Serializer):
    total_values = serializers.IntegerField()
    truncated = serializers.BooleanField()
    fetched_at = serializers.CharField()


class ColumnValuesResponseSerializer(serializers.Serializer):
    field = serializers.CharField()
    values = ColumnValueSerializer(many=True)
    meta = ColumnValuesMetaSerializer()
