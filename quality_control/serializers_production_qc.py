# quality_control/serializers_production_qc.py

from rest_framework import serializers

from .enums import ParameterType
from .models import (
    ProductionParameter,
    ProductionParameterDefaultValue,
    ProductionParameterType,
    ProductionParameterTypeDefault,
    ProductionQCEntry,
    ProductionQCResult,
)


def _user_name(user):
    if user is None:
        return None
    return getattr(user, "full_name", "") or user.email


# ==================== Masters ====================


class ProductionParameterTypeSerializer(serializers.ModelSerializer):
    parameter_count = serializers.SerializerMethodField()
    default_count = serializers.SerializerMethodField()
    # The form's number, kept in Master Data > Print Documents; read here so a
    # sheet prints it for whoever can see the type.
    print_document_id = serializers.SerializerMethodField()

    class Meta:
        model = ProductionParameterType
        fields = [
            "id", "code", "name", "description", "print_document_id", "revision", "revision_date",
            "is_active", "parameter_count", "default_count", "created_at", "updated_at",
        ]

    def get_parameter_count(self, obj):
        annotated = getattr(obj, "active_parameter_count", None)
        if annotated is not None:
            return annotated
        return obj.parameters.filter(is_active=True).count()

    def get_default_count(self, obj):
        annotated = getattr(obj, "active_default_count", None)
        if annotated is not None:
            return annotated
        return obj.defaults.filter(is_active=True).count()

    def get_print_document_id(self, obj):
        document = next((d for d in obj.print_documents.all() if d.is_active), None)
        return document.document_id if document else ""


class ProductionParameterTypeWriteSerializer(serializers.Serializer):
    code = serializers.CharField(max_length=50)
    name = serializers.CharField(max_length=200)
    description = serializers.CharField(required=False, allow_blank=True, default="")
    revision = serializers.CharField(max_length=20, required=False, allow_blank=True, default="")
    revision_date = serializers.DateField(required=False, allow_null=True, default=None)
    # The form's number is the Print Documents row for this type — the same one
    # Master Data shows. Left out, it is not touched; blank removes it.
    print_document_id = serializers.CharField(max_length=100, required=False, allow_blank=True)

    def validate_code(self, value):
        value = value.strip().upper()
        if not value:
            raise serializers.ValidationError("Enter a code.")
        return value

    def validate_name(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("Enter a name.")
        return value


class ProductionParameterSerializer(serializers.ModelSerializer):
    parameter_type_id = serializers.IntegerField(read_only=True)

    class Meta:
        model = ProductionParameter
        fields = [
            "id", "parameter_type_id", "parameter_code", "parameter_name",
            "standard_value", "value_type", "min_value", "max_value", "uom",
            "sequence", "is_mandatory", "is_active",
        ]


class ProductionParameterWriteSerializer(serializers.Serializer):
    parameter_code = serializers.CharField(max_length=50)
    parameter_name = serializers.CharField(max_length=200)
    standard_value = serializers.CharField(max_length=200)
    value_type = serializers.ChoiceField(choices=ParameterType.choices, default=ParameterType.TEXT)
    min_value = serializers.DecimalField(
        max_digits=12, decimal_places=4, required=False, allow_null=True
    )
    max_value = serializers.DecimalField(
        max_digits=12, decimal_places=4, required=False, allow_null=True
    )
    uom = serializers.CharField(max_length=50, required=False, allow_blank=True, default="")
    sequence = serializers.IntegerField(min_value=0, required=False, default=0)
    is_mandatory = serializers.BooleanField(required=False, default=True)

    def validate_parameter_code(self, value):
        value = value.strip().upper()
        if not value:
            raise serializers.ValidationError("Enter a code.")
        return value

    def validate(self, attrs):
        low, high = attrs.get("min_value"), attrs.get("max_value")
        if low is not None and high is not None and low > high:
            raise serializers.ValidationError({"max_value": "Max must not be below min."})
        return attrs


# ==================== Defaults ====================


class ProductionParameterDefaultValueSerializer(serializers.ModelSerializer):
    parameter_id = serializers.IntegerField(read_only=True)

    class Meta:
        model = ProductionParameterDefaultValue
        fields = ["parameter_id", "standard_value", "min_value", "max_value", "value"]


class ProductionParameterTypeDefaultSerializer(serializers.ModelSerializer):
    parameter_type_id = serializers.IntegerField(read_only=True)
    values = ProductionParameterDefaultValueSerializer(many=True, read_only=True)

    class Meta:
        model = ProductionParameterTypeDefault
        fields = ["id", "parameter_type_id", "name", "is_active", "values", "created_at", "updated_at"]


class ProductionParameterDefaultValueWriteSerializer(serializers.Serializer):
    parameter_id = serializers.IntegerField()
    standard_value = serializers.CharField(
        max_length=200, required=False, allow_blank=True, default=""
    )
    min_value = serializers.DecimalField(
        max_digits=12, decimal_places=4, required=False, allow_null=True, default=None
    )
    max_value = serializers.DecimalField(
        max_digits=12, decimal_places=4, required=False, allow_null=True, default=None
    )
    value = serializers.CharField(max_length=200, required=False, allow_blank=True, default="")

    def validate(self, attrs):
        attrs["standard_value"] = attrs["standard_value"].strip()
        attrs["value"] = attrs["value"].strip()
        low, high = attrs["min_value"], attrs["max_value"]
        if low is not None and high is not None and low > high:
            raise serializers.ValidationError({"max_value": "Max must not be below min."})
        return attrs


class ProductionParameterTypeDefaultWriteSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=200)
    values = ProductionParameterDefaultValueWriteSerializer(many=True, required=False, default=list)

    def validate_name(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("Enter a name.")
        return value

    def validate_values(self, value):
        ids = [row["parameter_id"] for row in value]
        if len(ids) != len(set(ids)):
            raise serializers.ValidationError("Each parameter can appear only once.")
        # A row that sets nothing is no row at all.
        return [
            row for row in value
            if row["standard_value"] or row["value"]
            or row["min_value"] is not None or row["max_value"] is not None
        ]


# ==================== Entries ====================


class _TypeRefSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    code = serializers.CharField()
    name = serializers.CharField()


class ProductionQCResultSerializer(serializers.ModelSerializer):
    parameter_id = serializers.IntegerField(source="parameter_master_id", read_only=True)

    class Meta:
        model = ProductionQCResult
        fields = [
            "id", "parameter_id", "parameter_code", "parameter_name", "standard_value",
            "parameter_type", "min_value", "max_value", "uom", "sequence", "is_mandatory",
            "result_value", "result_numeric", "is_within_spec", "remarks",
        ]


class ProductionQCEntryListSerializer(serializers.ModelSerializer):
    parameter_type = _TypeRefSerializer(read_only=True)
    default_id = serializers.IntegerField(read_only=True, allow_null=True)
    submission_id = serializers.IntegerField(read_only=True, allow_null=True)
    # Every entry sent with it, itself included, in order: they are decided as one.
    submission_entry_ids = serializers.SerializerMethodField()
    status_label = serializers.CharField(source="get_status_display", read_only=True)
    out_of_spec_count = serializers.SerializerMethodField()
    submitted_by_name = serializers.SerializerMethodField()
    approved_by_name = serializers.SerializerMethodField()
    sent_back_by_name = serializers.SerializerMethodField()

    class Meta:
        model = ProductionQCEntry
        fields = [
            "id", "parameter_type", "default_id", "default_name", "submission_id",
            "submission_entry_ids", "checked_at", "status", "status_label", "out_of_spec_count",
            "submitted_by_name", "submitted_at", "approved_by_name", "approved_at",
            "sent_back_by_name", "sent_back_at", "send_back_remarks",
        ]

    def get_submission_entry_ids(self, obj):
        if obj.submission_id is None:
            return [obj.pk]
        # Prefetched by the views (`submission__entries`); sorted here, not in SQL.
        return sorted(e.pk for e in obj.submission.entries.all() if e.is_active)

    def get_out_of_spec_count(self, obj):
        annotated = getattr(obj, "out_of_spec", None)
        if annotated is not None:
            return annotated
        return obj.results.filter(is_within_spec=False).count()

    def get_submitted_by_name(self, obj):
        return _user_name(obj.submitted_by)

    def get_approved_by_name(self, obj):
        return _user_name(obj.approved_by)

    def get_sent_back_by_name(self, obj):
        return _user_name(obj.sent_back_by)


class ProductionQCEntryDetailSerializer(ProductionQCEntryListSerializer):
    results = ProductionQCResultSerializer(many=True, read_only=True)

    class Meta(ProductionQCEntryListSerializer.Meta):
        fields = ProductionQCEntryListSerializer.Meta.fields + [
            "remarks", "approval_remarks", "results",
        ]


class ProductionQCReadingSerializer(serializers.Serializer):
    parameter_id = serializers.IntegerField()
    result_value = serializers.CharField(max_length=200, required=False, allow_blank=True)
    result_numeric = serializers.DecimalField(
        max_digits=12, decimal_places=4, required=False, allow_null=True
    )
    is_within_spec = serializers.BooleanField(required=False, allow_null=True)
    remarks = serializers.CharField(required=False, allow_blank=True)


def _readings_of(results):
    ids = [reading["parameter_id"] for reading in results]
    if len(ids) != len(set(ids)):
        raise serializers.ValidationError("Each parameter can have only one reading.")
    return {r["parameter_id"]: r for r in results}


class _SampleSerializer(serializers.Serializer):
    results = ProductionQCReadingSerializer(many=True)


class _CorrectedSampleSerializer(_SampleSerializer):
    entry_id = serializers.IntegerField()


class ProductionQCEntryCreateSerializer(serializers.Serializer):
    """One entry (`results`), or several filled together (`samples`), sent as one."""

    parameter_type_id = serializers.IntegerField()
    default_id = serializers.IntegerField(required=False, allow_null=True, default=None)
    results = ProductionQCReadingSerializer(many=True, required=False)
    samples = _SampleSerializer(many=True, required=False)
    remarks = serializers.CharField(required=False, allow_blank=True, default="")

    def validate(self, attrs):
        if ("results" in attrs) == ("samples" in attrs):
            raise serializers.ValidationError({"samples": ["Send the readings as results or as samples."]})
        if "samples" in attrs and not attrs["samples"]:
            raise serializers.ValidationError({"samples": ["Fill at least one sample."]})
        try:
            attrs["readings"] = (
                [_readings_of(attrs["results"])] if "results" in attrs
                else [_readings_of(sample["results"]) for sample in attrs["samples"]]
            )
        except serializers.ValidationError as exc:
            raise serializers.ValidationError({"results": exc.detail}) from None
        return attrs

    def samples_readings(self):
        return self.validated_data["readings"]


class ProductionQCEntryUpdateSerializer(serializers.Serializer):
    """The entry's readings (`results`) — or, for one sent with others, every
    entry's (`samples`, each with its `entry_id`): they are corrected as one."""

    results = ProductionQCReadingSerializer(many=True, required=False)
    samples = _CorrectedSampleSerializer(many=True, required=False)
    remarks = serializers.CharField(required=False, allow_blank=True, default="")

    def validate(self, attrs):
        if ("results" in attrs) == ("samples" in attrs):
            raise serializers.ValidationError({"samples": ["Send the readings as results or as samples."]})
        if "samples" in attrs:
            ids = [sample["entry_id"] for sample in attrs["samples"]]
            if len(ids) != len(set(ids)):
                raise serializers.ValidationError({"samples": ["Each entry can be sent only once."]})
        try:
            if "samples" in attrs:
                attrs["readings"] = {
                    sample["entry_id"]: _readings_of(sample["results"]) for sample in attrs["samples"]
                }
            else:
                attrs["readings"] = {None: _readings_of(attrs["results"])}
        except serializers.ValidationError as exc:
            raise serializers.ValidationError({"results": exc.detail}) from None
        return attrs

    def readings_by_entry(self, entry):
        """Keyed by entry id; plain `results` are the entry's own."""
        readings = self.validated_data["readings"]
        return {entry.pk: readings[None]} if None in readings else readings


class ProductionQCDecisionSerializer(serializers.Serializer):
    remarks = serializers.CharField(required=False, allow_blank=True, default="")
