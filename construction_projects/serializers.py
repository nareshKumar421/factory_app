from decimal import Decimal

from django.db.models import Sum
from rest_framework import serializers

from .constants import (
    AttachmentKind,
    DimensionUnit,
    ExpenseBatchStatus,
    ExpenseCategory,
    PaymentMode,
    StopReason,
)
from .models import (
    DailyLog,
    EstimateLine,
    ExpenseBatch,
    DailyLogPhoto,
    Expense,
    Project,
    ProjectAttachment,
    ProjectRevision,
)


def _name(user):
    return user.full_name if user else None


def _file_url(field_file, context):
    """An ABSOLUTE url for an uploaded file, or None.

    Django serves media on its own origin; the frontend runs on another in
    development (:5173 vs :8000). A bare ``FileField`` serialises to
    ``/media/...``, which the browser then resolves against the FRONTEND, and
    the image 404s. Every module in this repo returns absolute media urls for
    exactly this reason -- see ``cash_book.CashEntryAttachmentSerializer`` and
    ``employee_hierarchy.photo_url``.

    Falls back to the relative url when there is no request in the context, so
    a serializer used outside a view still produces something usable.
    """
    if not field_file:
        return None
    url = field_file.url
    request = context.get("request")
    return request.build_absolute_uri(url) if request else url


# ---------------------------------------------------------------------------
# Read serializers
# ---------------------------------------------------------------------------


class ProjectListSerializer(serializers.ModelSerializer):
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    manager_name = serializers.SerializerMethodField()
    remaining_budget = serializers.DecimalField(
        max_digits=14, decimal_places=2, read_only=True
    )
    percent_used = serializers.DecimalField(
        max_digits=6, decimal_places=2, read_only=True
    )
    is_over_budget = serializers.BooleanField(read_only=True)
    is_overdue = serializers.BooleanField(read_only=True)
    days_left = serializers.IntegerField(read_only=True)
    area = serializers.DecimalField(max_digits=20, decimal_places=2, read_only=True)
    volume = serializers.DecimalField(max_digits=30, decimal_places=2, read_only=True)
    area_unit = serializers.CharField(read_only=True)
    volume_unit = serializers.CharField(read_only=True)

    class Meta:
        model = Project
        fields = (
            "id",
            "code",
            "name",
            "location",
            "status",
            "status_display",
            "start_date",
            "expected_end_date",
            "actual_end_date",
            "estimated_cost",
            "length",
            "breadth",
            "height",
            "dimension_unit",
            "area",
            "area_unit",
            "volume",
            "volume_unit",
            "sanctioned_budget",
            "spent_amount",
            "remaining_budget",
            "percent_used",
            "is_over_budget",
            "is_overdue",
            "days_left",
            "progress_percent",
            "manager",
            "manager_name",
        )
        read_only_fields = fields

    def get_manager_name(self, obj):
        return _name(obj.manager)


class ProjectDetailSerializer(ProjectListSerializer):
    site_incharge_name = serializers.SerializerMethodField()
    submitted_by_name = serializers.SerializerMethodField()
    decided_by_name = serializers.SerializerMethodField()
    created_by_name = serializers.SerializerMethodField()
    is_editable = serializers.BooleanField(read_only=True)
    days_elapsed = serializers.IntegerField(read_only=True)

    class Meta(ProjectListSerializer.Meta):
        fields = ProjectListSerializer.Meta.fields + (
            "description",
            "site_incharge",
            "site_incharge_name",
            "submitted_at",
            "submitted_by_name",
            "decided_at",
            "decided_by_name",
            "sanctioned_at",
            "decision_note",
            "is_editable",
            "days_elapsed",
            "created_by_name",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields

    def get_site_incharge_name(self, obj):
        return _name(obj.site_incharge)

    def get_submitted_by_name(self, obj):
        return _name(obj.submitted_by)

    def get_decided_by_name(self, obj):
        return _name(obj.decided_by)

    def get_created_by_name(self, obj):
        return _name(obj.created_by)


class EstimateLineSerializer(serializers.ModelSerializer):
    class Meta:
        model = EstimateLine
        fields = (
            "id",
            "line_no",
            "material",
            "quantity",
            "unit",
            "rate",
            "amount",
            "notes",
        )
        read_only_fields = fields


class EstimateLineWriteSerializer(serializers.Serializer):
    """One row of the sheet. ``amount`` is derived, never sent."""

    line_no = serializers.IntegerField(min_value=1, required=False)
    material = serializers.CharField(max_length=200)
    quantity = serializers.DecimalField(
        max_digits=14, decimal_places=3, min_value=Decimal("0"), required=False
    )
    unit = serializers.CharField(
        max_length=20, required=False, allow_blank=True, default=""
    )
    rate = serializers.DecimalField(
        max_digits=14, decimal_places=2, min_value=Decimal("0"), required=False
    )
    notes = serializers.CharField(
        max_length=255, required=False, allow_blank=True, default=""
    )


class EstimateWriteSerializer(serializers.Serializer):
    """The whole breakdown, saved in one go -- it is a table, not a form."""

    lines = EstimateLineWriteSerializer(many=True, allow_empty=True)

    def validate_lines(self, lines):
        numbers = [row["line_no"] for row in lines if row.get("line_no")]
        if len(set(numbers)) != len(numbers):
            raise serializers.ValidationError("Two rows share a Sr. No.")
        return lines


class ProjectAttachmentSerializer(serializers.ModelSerializer):
    file = serializers.SerializerMethodField()
    uploaded_by_name = serializers.SerializerMethodField()
    filename = serializers.SerializerMethodField()
    kind_display = serializers.CharField(source="get_kind_display", read_only=True)

    class Meta:
        model = ProjectAttachment
        fields = (
            "id",
            "file",
            "filename",
            "title",
            "kind",
            "kind_display",
            "uploaded_by_name",
            "created_at",
        )
        read_only_fields = fields

    def get_file(self, obj):
        return _file_url(obj.file, self.context)

    def get_filename(self, obj):
        return obj.file.name.rsplit("/", 1)[-1] if obj.file else None

    def get_uploaded_by_name(self, obj):
        return _name(obj.created_by)


class ProjectAttachmentWriteSerializer(serializers.Serializer):
    file = serializers.FileField()
    title = serializers.CharField(
        max_length=200, required=False, allow_blank=True, default=""
    )
    kind = serializers.ChoiceField(
        choices=AttachmentKind.choices,
        required=False,
        default=AttachmentKind.DOCUMENT,
    )


class DailyLogPhotoSerializer(serializers.ModelSerializer):
    photo = serializers.SerializerMethodField()

    class Meta:
        model = DailyLogPhoto
        fields = ("id", "photo", "caption", "created_at")
        read_only_fields = fields

    def get_photo(self, obj):
        return _file_url(obj.photo, self.context)


class DailyLogSerializer(serializers.ModelSerializer):
    photos = DailyLogPhotoSerializer(many=True, read_only=True)
    stopped_reasons = serializers.SerializerMethodField()
    stopped_reasons_display = serializers.SerializerMethodField()
    logged_by_name = serializers.SerializerMethodField()
    spent_on_day = serializers.SerializerMethodField()

    class Meta:
        model = DailyLog
        fields = (
            "id",
            "project",
            "log_date",
            "work_done",
            "workers_count",
            "progress_percent",
            "work_stopped",
            "stopped_reasons",
            "stopped_reasons_display",
            "notes",
            "photos",
            "logged_by_name",
            "spent_on_day",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields

    def get_logged_by_name(self, obj):
        return _name(obj.created_by)

    def get_stopped_reasons(self, obj):
        return [row.reason for row in obj.stop_reasons.all()]

    def get_stopped_reasons_display(self, obj):
        labels = dict(StopReason.choices)
        return [labels.get(row.reason, row.reason) for row in obj.stop_reasons.all()]

    def get_spent_on_day(self, obj):
        """What the day cost.

        The list view annotates this in one query for the whole page; a
        single-log read falls through and computes it, so the field is correct
        either way. Always a number -- a day with no spend is 0.00, not null.
        """
        annotated = getattr(obj, "day_spend", None)
        if annotated is None:
            annotated = obj.project.expenses.filter(
                spend_date=obj.log_date, is_active=True
            ).aggregate(total=Sum("amount"))["total"]
        # A string, not a Decimal: a method field lands in a plain dict, where
        # DRF's encoder would turn a Decimal into a float.
        return str((annotated or Decimal("0.00")).quantize(Decimal("0.01")))


class ExpenseSerializer(serializers.ModelSerializer):
    bill = serializers.SerializerMethodField()
    # The approvals queue groups a site's unchecked payments by project, so it
    # needs to name the project without a second call per row.
    project_code = serializers.CharField(source="project.code", read_only=True)
    project_name = serializers.CharField(source="project.name", read_only=True)
    category_display = serializers.CharField(
        source="get_category_display", read_only=True
    )
    payment_mode_display = serializers.CharField(
        source="get_payment_mode_display", read_only=True
    )
    recorded_by_name = serializers.SerializerMethodField()
    batch_no = serializers.IntegerField(source="batch.batch_no", read_only=True)
    batch_status = serializers.CharField(source="batch.status", read_only=True)
    is_editable = serializers.BooleanField(read_only=True)

    class Meta:
        model = Expense
        fields = (
            "id",
            "project",
            "project_code",
            "project_name",
            "spend_date",
            "category",
            "category_display",
            "description",
            "amount",
            "paid_to",
            "payment_mode",
            "payment_mode_display",
            "reference_no",
            "bill",
            "batch",
            "batch_no",
            "batch_status",
            "is_editable",
            "recorded_by_name",
            "created_at",
        )
        read_only_fields = fields

    def get_recorded_by_name(self, obj):
        return _name(obj.created_by)

    def get_bill(self, obj):
        return _file_url(obj.bill, self.context)


class ProjectRevisionSerializer(serializers.ModelSerializer):
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    requested_by_name = serializers.SerializerMethodField()
    decided_by_name = serializers.SerializerMethodField()
    budget_after = serializers.DecimalField(
        max_digits=14, decimal_places=2, read_only=True
    )
    end_date_after = serializers.DateField(read_only=True)
    extension_days = serializers.IntegerField(read_only=True)
    project_code = serializers.CharField(source="project.code", read_only=True)
    project_name = serializers.CharField(source="project.name", read_only=True)

    class Meta:
        model = ProjectRevision
        fields = (
            "id",
            "project",
            "project_code",
            "project_name",
            "revision_no",
            "additional_amount",
            "new_end_date",
            "reason",
            "budget_before",
            "budget_after",
            "end_date_before",
            "end_date_after",
            "extension_days",
            "status",
            "status_display",
            "requested_by_name",
            "requested_at",
            "decided_by_name",
            "decided_at",
            "decision_note",
        )
        read_only_fields = fields

    def get_requested_by_name(self, obj):
        return _name(obj.requested_by)

    def get_decided_by_name(self, obj):
        return _name(obj.decided_by)


# ---------------------------------------------------------------------------
# Write serializers
# ---------------------------------------------------------------------------


class ProjectWriteSerializer(serializers.Serializer):
    """The user ids come in as plain integers and are resolved in the view,
    matching ``labour_request.RaiseRequestSerializer`` -- a related field needs
    its queryset at class-definition time, which is too early to touch the
    user model."""

    # Nothing here is required, because this serializer also writes drafts and
    # a draft is a form somebody started. What a project must have before it is
    # sent for approval is ``services.REQUIRED_TO_SUBMIT``, checked there.
    name = serializers.CharField(
        max_length=200, required=False, allow_blank=True, default=""
    )
    description = serializers.CharField(
        max_length=5000, required=False, allow_blank=True, default=""
    )
    location = serializers.CharField(
        max_length=200, required=False, allow_blank=True, default=""
    )
    start_date = serializers.DateField(required=False, allow_null=True)
    expected_end_date = serializers.DateField(required=False, allow_null=True)
    estimated_cost = serializers.DecimalField(
        max_digits=14,
        decimal_places=2,
        min_value=Decimal("0.01"),
        required=False,
        allow_null=True,
    )
    manager = serializers.IntegerField(required=False, allow_null=True)
    site_incharge = serializers.IntegerField(required=False, allow_null=True)
    # How big it is. All optional: a boundary wall has no meaningful breadth.
    length = serializers.DecimalField(
        max_digits=10, decimal_places=2, min_value=Decimal("0"),
        required=False, allow_null=True,
    )
    breadth = serializers.DecimalField(
        max_digits=10, decimal_places=2, min_value=Decimal("0"),
        required=False, allow_null=True,
    )
    height = serializers.DecimalField(
        max_digits=10, decimal_places=2, min_value=Decimal("0"),
        required=False, allow_null=True,
    )
    dimension_unit = serializers.ChoiceField(
        choices=DimensionUnit.choices, required=False, default=DimensionUnit.FEET
    )


class ProjectPatchSerializer(ProjectWriteSerializer):
    """Same fields, all optional -- a draft is edited a field at a time."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.required = False

    def validate(self, attrs):
        if not attrs:
            raise serializers.ValidationError("Nothing to update.")
        return attrs


class DecisionSerializer(serializers.Serializer):
    """An approval may be silent; a rejection may not.

    Refusing somebody's budget without saying why leaves them with nothing to
    act on, so the note is required on the reject paths -- enforced in the
    service as well, since that is the floor.
    """

    note = serializers.CharField(
        max_length=255, required=False, allow_blank=True, default=""
    )


class CompleteProjectSerializer(DecisionSerializer):
    actual_end_date = serializers.DateField(required=False)


class ExpenseWriteSerializer(serializers.Serializer):
    spend_date = serializers.DateField()
    category = serializers.ChoiceField(choices=ExpenseCategory.choices)
    description = serializers.CharField(max_length=300)
    amount = serializers.DecimalField(
        max_digits=14, decimal_places=2, min_value=Decimal("0.01")
    )
    paid_to = serializers.CharField(
        max_length=200, required=False, allow_blank=True, default=""
    )
    payment_mode = serializers.ChoiceField(
        choices=PaymentMode.choices, required=False, default=PaymentMode.CASH
    )
    reference_no = serializers.CharField(
        max_length=60, required=False, allow_blank=True, default=""
    )
    bill = serializers.FileField(required=False, allow_null=True)


class DailyLogWriteSerializer(serializers.Serializer):
    log_date = serializers.DateField()
    work_done = serializers.CharField(max_length=5000)
    workers_count = serializers.IntegerField(min_value=0, required=False, default=0)
    progress_percent = serializers.DecimalField(
        max_digits=5, decimal_places=2, min_value=Decimal("0"), max_value=Decimal("100"),
        required=False, allow_null=True,
    )
    work_stopped = serializers.BooleanField(required=False, default=False)
    stopped_reasons = serializers.ListField(
        child=serializers.ChoiceField(choices=StopReason.choices),
        required=False,
        default=list,
        help_text="A day can be stopped for several reasons at once.",
    )
    notes = serializers.CharField(
        max_length=5000, required=False, allow_blank=True, default=""
    )

    def validate(self, attrs):
        reasons = attrs.get("stopped_reasons") or []
        if attrs.get("work_stopped") and not reasons:
            raise serializers.ValidationError(
                {"stopped_reasons": "Say why work stopped."}
            )
        if len(set(reasons)) != len(reasons):
            raise serializers.ValidationError(
                {"stopped_reasons": "The same reason is listed twice."}
            )
        return attrs


class DailyLogPhotoWriteSerializer(serializers.Serializer):
    photo = serializers.FileField()
    caption = serializers.CharField(
        max_length=200, required=False, allow_blank=True, default=""
    )


class ExpenseBatchSerializer(serializers.ModelSerializer):
    """A project's running set of payments, settled by one decision."""

    status_display = serializers.CharField(source="get_status_display", read_only=True)
    project_code = serializers.CharField(source="project.code", read_only=True)
    project_name = serializers.CharField(source="project.name", read_only=True)
    total = serializers.DecimalField(max_digits=16, decimal_places=2, read_only=True)
    line_count = serializers.IntegerField(read_only=True)
    is_editable = serializers.BooleanField(read_only=True)
    submitted_by_name = serializers.SerializerMethodField()
    decided_by_name = serializers.SerializerMethodField()

    class Meta:
        model = ExpenseBatch
        fields = (
            "id",
            "project",
            "project_code",
            "project_name",
            "batch_no",
            "status",
            "status_display",
            "total",
            "line_count",
            "is_editable",
            "submitted_at",
            "submitted_by_name",
            "decided_at",
            "decided_by_name",
            "decision_note",
            "created_at",
        )
        read_only_fields = fields

    def get_submitted_by_name(self, obj):
        return _name(obj.submitted_by)

    def get_decided_by_name(self, obj):
        return _name(obj.decided_by)


class ExpenseBatchDetailSerializer(ExpenseBatchSerializer):
    expenses = serializers.SerializerMethodField()

    class Meta(ExpenseBatchSerializer.Meta):
        fields = ExpenseBatchSerializer.Meta.fields + ("expenses",)
        read_only_fields = fields

    def get_expenses(self, obj):
        return ExpenseSerializer(
            obj.expenses.filter(is_active=True).select_related("created_by", "project"),
            many=True,
            context=self.context,
        ).data


class SubmitBatchSerializer(serializers.Serializer):
    """Which of the open batch's payments to send. Omit to send them all."""

    expense_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        required=False,
        allow_empty=False,
        max_length=500,
    )


class BatchDecisionSerializer(serializers.Serializer):
    """Approve the whole batch, or send it back to be corrected."""

    decision = serializers.ChoiceField(
        choices=[ExpenseBatchStatus.APPROVED, ExpenseBatchStatus.RETURNED]
    )
    note = serializers.CharField(
        max_length=255, required=False, allow_blank=True, default=""
    )


class RevisionWriteSerializer(serializers.Serializer):
    additional_amount = serializers.DecimalField(
        max_digits=14, decimal_places=2, min_value=Decimal("0"), required=False
    )
    new_end_date = serializers.DateField(required=False, allow_null=True)
    reason = serializers.CharField(max_length=5000)
