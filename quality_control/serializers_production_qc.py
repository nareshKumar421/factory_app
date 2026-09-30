# quality_control/serializers_production_qc.py

from rest_framework import serializers

from .enums import ParameterType
from .models import (
    ProductionParameter,
    ProductionParameterType,
    ProductionParameterTypeItem,
    ProductionQCEntry,
    ProductionQCResult,
)


def _user_name(user):
    if user is None:
        return None
    return getattr(user, "full_name", "") or user.email


# ==================== Masters ====================


class ProductionParameterTypeItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductionParameterTypeItem
        fields = ["id", "item_code", "item_name"]


class ProductionParameterTypeSerializer(serializers.ModelSerializer):
    parameter_count = serializers.SerializerMethodField()
    items = serializers.SerializerMethodField()
    # The form's number, kept in Master Data > Print Documents; read here so a
    # sheet prints it for whoever can see the type.
    print_document_id = serializers.SerializerMethodField()

    class Meta:
        model = ProductionParameterType
        fields = [
            "id", "code", "name", "description", "print_document_id", "revision", "revision_date",
            "is_active", "parameter_count", "items", "created_at", "updated_at",
        ]

    def get_parameter_count(self, obj):
        annotated = getattr(obj, "active_parameter_count", None)
        if annotated is not None:
            return annotated
        return obj.parameters.filter(is_active=True).count()

    def get_items(self, obj):
        items = [item for item in obj.items.all() if item.is_active]
        return ProductionParameterTypeItemSerializer(items, many=True).data

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


class ProductionParameterTypeItemWriteSerializer(serializers.Serializer):
    item_code = serializers.CharField(max_length=50)
    item_name = serializers.CharField(max_length=200, required=False, allow_blank=True, default="")

    def validate_item_code(self, value):
        value = value.strip().upper()
        if not value:
            raise serializers.ValidationError("Enter an item code.")
        return value


# ==================== Running lines ====================


class _TypeRefSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    code = serializers.CharField()
    name = serializers.CharField()


class RunningLineSerializer(serializers.Serializer):
    line_id = serializers.IntegerField()
    line_name = serializers.CharField()
    run_id = serializers.IntegerField()
    run_number = serializers.IntegerField()
    run_date = serializers.DateField()
    item_code = serializers.CharField()
    product = serializers.CharField()
    is_running_now = serializers.BooleanField()
    last_started_at = serializers.DateTimeField()
    stopped_at = serializers.DateTimeField(allow_null=True)
    linked_parameter_types = serializers.SerializerMethodField()

    def get_linked_parameter_types(self, obj):
        linked = self.context.get("linked", {}).get(obj.item_code, [])
        return _TypeRefSerializer(linked, many=True).data


# ==================== Entries ====================


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
    line_name = serializers.CharField(source="line.name", read_only=True)
    run_id = serializers.IntegerField(source="production_run_id", read_only=True)
    run_number = serializers.IntegerField(source="production_run.run_number", read_only=True)
    parameter_type = _TypeRefSerializer(read_only=True)
    status_label = serializers.CharField(source="get_status_display", read_only=True)
    out_of_spec_count = serializers.SerializerMethodField()
    submitted_by_name = serializers.SerializerMethodField()
    approved_by_name = serializers.SerializerMethodField()
    sent_back_by_name = serializers.SerializerMethodField()

    class Meta:
        model = ProductionQCEntry
        fields = [
            "id", "line_id", "line_name", "run_id", "run_number", "item_code", "product",
            "parameter_type", "checked_at", "status", "status_label", "out_of_spec_count",
            "submitted_by_name", "submitted_at", "approved_by_name", "approved_at",
            "sent_back_by_name", "sent_back_at", "send_back_remarks",
        ]

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


class _ReadingsMixin(serializers.Serializer):
    results = ProductionQCReadingSerializer(many=True)
    remarks = serializers.CharField(required=False, allow_blank=True, default="")

    def validate_results(self, value):
        ids = [reading["parameter_id"] for reading in value]
        if len(ids) != len(set(ids)):
            raise serializers.ValidationError("Each parameter can have only one reading.")
        return value

    def readings(self):
        return {r["parameter_id"]: r for r in self.validated_data["results"]}


class ProductionQCEntryCreateSerializer(_ReadingsMixin):
    run_id = serializers.IntegerField()
    parameter_type_id = serializers.IntegerField()


class ProductionQCEntryUpdateSerializer(_ReadingsMixin):
    pass


class ProductionQCDecisionSerializer(serializers.Serializer):
    remarks = serializers.CharField(required=False, allow_blank=True, default="")
