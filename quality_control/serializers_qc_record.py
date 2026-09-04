# quality_control/serializers_qc_record.py
"""Serializers for fillable QC record forms (the "Documents" screen)."""

from django.db import transaction
from django.db.models import Q
from rest_framework import serializers

from .models import (
    QCRecord,
    RecordTemplate,
    RecordTemplateParameter,
    RecordTemplateSection,
    RecordTimeSlot,
    RecordValue,
)


# ---------------------------------------------------------------------------
# Template (the blank form)
# ---------------------------------------------------------------------------


class RecordTemplateParameterSerializer(serializers.ModelSerializer):
    class Meta:
        model = RecordTemplateParameter
        fields = [
            "id",
            "sequence",
            "sr_no",
            "name",
            "frequency",
            "specification",
            "unit",
            "value_type",
            "min_value",
            "max_value",
            "allowed_values",
            "conforming_values",
        ]
        read_only_fields = ["id"]


class RecordTemplateSectionSerializer(serializers.ModelSerializer):
    parameters = RecordTemplateParameterSerializer(many=True, required=False)

    class Meta:
        model = RecordTemplateSection
        fields = ["id", "sequence", "title", "parameters"]
        read_only_fields = ["id"]


class RecordTemplateListSerializer(serializers.ModelSerializer):
    revision_label = serializers.CharField(read_only=True)
    parameter_count = serializers.IntegerField(read_only=True)
    record_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = RecordTemplate
        fields = [
            "id",
            "document_code",
            "title",
            "organisation",
            "revision_number",
            "revision_date",
            "revision_label",
            "classification",
            "description",
            "parameter_count",
            "record_count",
        ]


class RecordTemplateSerializer(serializers.ModelSerializer):
    sections = RecordTemplateSectionSerializer(many=True, required=False)
    revision_label = serializers.CharField(read_only=True)

    class Meta:
        model = RecordTemplate
        fields = [
            "id",
            "document_code",
            "title",
            "organisation",
            "revision_number",
            "revision_date",
            "revision_label",
            "classification",
            "description",
            "sections",
        ]
        read_only_fields = ["id", "revision_label"]

    def validate_document_code(self, value):
        value = (value or "").strip().upper()
        if not value:
            raise serializers.ValidationError("Document code is required.")
        return value

    def validate(self, attrs):
        company = self.context.get("company")
        code = attrs.get("document_code") or getattr(
            self.instance, "document_code", None
        )
        if company and code:
            # Checked against everything this company can see -- shared forms
            # as well as its own -- because forms are one shared library now
            # and a code has to mean a single form across it.
            clash = RecordTemplate.objects.filter(
                is_active=True, document_code=code
            ).filter(Q(company=company) | Q(company__isnull=True))
            if self.instance:
                clash = clash.exclude(pk=self.instance.pk)
            if clash.exists():
                raise serializers.ValidationError(
                    {"document_code": f"A form with code '{code}' already exists."}
                )
        return attrs

    def _write_sections(self, template, sections_data):
        template.sections.all().delete()
        for s_index, section_data in enumerate(sections_data):
            parameters = section_data.pop("parameters", [])
            section = RecordTemplateSection.objects.create(
                template=template,
                **{**section_data, "sequence": section_data.get("sequence", s_index)},
            )
            RecordTemplateParameter.objects.bulk_create(
                [
                    RecordTemplateParameter(
                        section=section,
                        **{**param, "sequence": param.get("sequence", p_index)},
                    )
                    for p_index, param in enumerate(parameters)
                ]
            )

    @transaction.atomic
    def create(self, validated_data):
        sections_data = validated_data.pop("sections", [])
        template = RecordTemplate.objects.create(**validated_data)
        self._write_sections(template, sections_data)
        return template

    @transaction.atomic
    def update(self, instance, validated_data):
        sections_data = validated_data.pop("sections", None)
        for field, value in validated_data.items():
            setattr(instance, field, value)
        instance.save()
        if sections_data is not None:
            # Refuse to silently orphan captured values: a parameter that any
            # record has already been filled against cannot be rewritten away.
            if RecordValue.objects.filter(
                parameter__section__template=instance
            ).exists():
                raise serializers.ValidationError(
                    {
                        "sections": (
                            "This form already has filled records, so its "
                            "parameters cannot be changed. Create a new "
                            "revision of the form instead."
                        )
                    }
                )
            self._write_sections(instance, sections_data)
        return instance


# ---------------------------------------------------------------------------
# Record (the filled sheet)
# ---------------------------------------------------------------------------


class RecordValueSerializer(serializers.ModelSerializer):
    in_spec = serializers.BooleanField(read_only=True, allow_null=True)

    class Meta:
        model = RecordValue
        fields = ["id", "time_slot", "parameter", "value", "in_spec"]
        read_only_fields = ["id", "in_spec"]


class RecordTimeSlotSerializer(serializers.ModelSerializer):
    class Meta:
        model = RecordTimeSlot
        fields = ["id", "sequence", "slot_time"]
        read_only_fields = ["id"]


class QCRecordListSerializer(serializers.ModelSerializer):
    template_title = serializers.CharField(source="template.title", read_only=True)
    template_code = serializers.CharField(
        source="template.document_code", read_only=True
    )
    status_label = serializers.CharField(source="get_status_display", read_only=True)
    slot_count = serializers.IntegerField(read_only=True)
    filled_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = QCRecord
        fields = [
            "id",
            "template",
            "template_title",
            "template_code",
            "record_date",
            "shift",
            "status",
            "status_label",
            "slot_count",
            "filled_count",
            "created_at",
            "updated_at",
        ]


class QCRecordSerializer(serializers.ModelSerializer):
    """The whole sheet: the blank form, its time columns, and every cell."""

    time_slots = RecordTimeSlotSerializer(many=True, required=False)
    values = RecordValueSerializer(many=True, read_only=True)
    template_detail = RecordTemplateSerializer(source="template", read_only=True)
    status_label = serializers.CharField(source="get_status_display", read_only=True)
    submitted_by_name = serializers.CharField(
        source="submitted_by.full_name", read_only=True, default=""
    )
    approved_by_name = serializers.CharField(
        source="approved_by.full_name", read_only=True, default=""
    )

    class Meta:
        model = QCRecord
        fields = [
            "id",
            "template",
            "template_detail",
            "record_date",
            "shift",
            "remarks",
            "status",
            "status_label",
            "time_slots",
            "values",
            "submitted_by_name",
            "submitted_at",
            "approved_by_name",
            "approved_at",
            "approval_remarks",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "template_detail",
            "status",
            "status_label",
            "values",
            "submitted_by_name",
            "submitted_at",
            "approved_by_name",
            "approved_at",
            "approval_remarks",
            "created_at",
            "updated_at",
        ]


class RecordCellWriteSerializer(serializers.Serializer):
    """One cell of the grid, as sent by the fill screen."""

    slot_time = serializers.TimeField()
    parameter = serializers.IntegerField()
    value = serializers.CharField(allow_blank=True, max_length=255)


class RecordValuesWriteSerializer(serializers.Serializer):
    """A bulk cell save. Creates any time column that does not exist yet."""

    cells = RecordCellWriteSerializer(many=True)
