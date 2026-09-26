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
from .services import record_sheet


def _has_filled_records(template):
    """True once any sheet has been filled against this form."""
    if template.pk is None:
        return False
    if template.is_sheet:
        return QCRecord.objects.filter(template=template).exclude(cell_values={}).exists()
    return RecordValue.objects.filter(parameter__section__template=template).exists()


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
    kind = serializers.SerializerMethodField()
    field_count = serializers.SerializerMethodField()

    def get_kind(self, obj):
        # The list view annotates `sheet` and defers the layout itself.
        sheet = getattr(obj, "sheet", None)
        return "SHEET" if (obj.is_sheet if sheet is None else sheet) else "GRID"

    def get_field_count(self, obj):
        """Cells typed into on a sheet form -- its counterpart of parameters."""
        return sum(
            1
            for field in (obj.cell_fields or {}).values()
            if field.get("type") in record_sheet.VALUE_TYPES
        )

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
            "kind",
            "field_count",
        ]


class RecordTemplateSerializer(serializers.ModelSerializer):
    sections = RecordTemplateSectionSerializer(many=True, required=False)
    revision_label = serializers.CharField(read_only=True)
    kind = serializers.SerializerMethodField()
    # The sheet of an Excel-uploaded form. Accepted only together with the
    # token the import endpoint issued for it, so it is always the parser's
    # own output and never a hand-made layout.
    layout = serializers.JSONField(required=False, allow_null=True)
    layout_token = serializers.CharField(write_only=True, required=False, allow_blank=True)
    cell_fields = serializers.JSONField(required=False)
    # True once any sheet has been filled against the form: its rows / cells
    # are then fixed, and only the header may change.
    is_locked = serializers.SerializerMethodField()

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
            "kind",
            "layout",
            "layout_token",
            "cell_fields",
            "source_file_name",
            "is_locked",
        ]
        read_only_fields = ["id", "revision_label", "kind", "is_locked"]

    def get_kind(self, obj):
        return "SHEET" if obj.is_sheet else "GRID"

    def get_is_locked(self, obj):
        return _has_filled_records(obj)

    def validate_document_code(self, value):
        value = (value or "").strip().upper()
        if not value:
            raise serializers.ValidationError("Document code is required.")
        return value

    def validate(self, attrs):
        attrs = self._validate_sheet(attrs)
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

    def _validate_sheet(self, attrs):
        token = attrs.pop("layout_token", "")
        instance = self.instance
        layout_sent = "layout" in attrs
        fields_sent = "cell_fields" in attrs

        if layout_sent and attrs["layout"] is not None:
            if not record_sheet.verify_layout(attrs["layout"], token):
                raise serializers.ValidationError(
                    {
                        "layout": (
                            "This sheet layout was not produced by the Excel "
                            "import, or its upload has expired. Upload the "
                            "Excel file again."
                        )
                    }
                )
            attrs["layout"] = record_sheet.normalise(attrs["layout"])
            if instance is not None and not instance.is_sheet:
                raise serializers.ValidationError(
                    {"layout": "A form built from parameters cannot become a sheet; "
                     "upload the Excel file as a new form."}
                )
        elif layout_sent and instance is not None and instance.is_sheet:
            raise serializers.ValidationError(
                {"layout": "A sheet form keeps its sheet; upload a new revision instead."}
            )

        layout = attrs.get("layout") if layout_sent else getattr(instance, "layout", None)

        if fields_sent:
            if layout is None:
                if attrs["cell_fields"]:
                    raise serializers.ValidationError(
                        {"cell_fields": "Only a form uploaded as a sheet has cell fields."}
                    )
                attrs["cell_fields"] = {}
            else:
                cleaned, errors = record_sheet.clean_cell_fields(attrs["cell_fields"], layout)
                if errors:
                    raise serializers.ValidationError({"cell_fields": errors})
                attrs["cell_fields"] = cleaned
        elif layout_sent and layout is not None:
            # A new sheet without fields would keep fields pointing at cells
            # of the old one.
            attrs["cell_fields"] = {}

        # Compared after cleaning, so re-sending the form's own fields (a
        # header-only correction from the designer) is not mistaken for a
        # change.
        if instance is not None and instance.is_sheet and _has_filled_records(instance):
            if ("layout" in attrs and attrs["layout"] != instance.layout) or (
                "cell_fields" in attrs and attrs["cell_fields"] != instance.cell_fields
            ):
                raise serializers.ValidationError(
                    {
                        "cell_fields": (
                            "This form already has filled records, so its sheet "
                            "and fillable cells cannot be changed. Create a new "
                            "revision of the form instead."
                        )
                    }
                )

        if layout is not None and attrs.get("sections"):
            raise serializers.ValidationError(
                {"sections": "A form uploaded as a sheet has no parameter sections."}
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
    slot_count = serializers.SerializerMethodField()
    filled_count = serializers.SerializerMethodField()

    # A sheet-form record has no time-slot or value rows to count, so its
    # counts come from its cells: filled values, and filled time cells.
    @staticmethod
    def _is_sheet(obj):
        # The list view annotates this and defers the layout itself.
        sheet = getattr(obj, "template_is_sheet", None)
        return obj.template.is_sheet if sheet is None else sheet

    def get_slot_count(self, obj):
        if self._is_sheet(obj):
            fields = obj.template.cell_fields or {}
            return sum(
                1
                for cell, value in (obj.cell_values or {}).items()
                if str(value).strip()
                and fields.get(cell, {}).get("type") == record_sheet.FieldType.TIME
            )
        return getattr(obj, "slot_count", 0)

    def get_filled_count(self, obj):
        if self._is_sheet(obj):
            return sum(1 for value in (obj.cell_values or {}).values() if str(value).strip())
        return getattr(obj, "filled_count", 0)

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
    cell_values = serializers.JSONField(read_only=True)
    # Per filled cell of a sheet form: true = meets its spec, false = does not.
    # Cells that cannot be judged are left out.
    cell_checks = serializers.SerializerMethodField()

    def get_cell_checks(self, obj):
        fields = obj.template.cell_fields or {}
        checks = {}
        for cell, value in (obj.cell_values or {}).items():
            verdict = record_sheet.check_cell(fields.get(cell, {}), value)
            if verdict is not None:
                checks[cell] = verdict
        return checks

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
            "cell_values",
            "cell_checks",
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
            "cell_values",
            "cell_checks",
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


class RecordCellsWriteSerializer(serializers.Serializer):
    """A save from a sheet form: changed cells, and optionally the remarks."""

    cells = serializers.DictField(
        child=serializers.CharField(allow_blank=True, max_length=255), required=False
    )
    remarks = serializers.CharField(allow_blank=True, required=False)
