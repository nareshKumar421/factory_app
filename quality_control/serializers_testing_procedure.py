# quality_control/serializers_testing_procedure.py
"""Serializers for controlled testing procedures.

The write path is deliberately whole-document: a procedure arrives with its
sections and their lines nested, and a save replaces the children wholesale.
Procedures are edited as documents, not field by field, so a nested write
keeps the stored order identical to what the analyst approved on screen.
"""

from django.db import transaction
from rest_framework import serializers

from .models import (
    TestingProcedure,
    TestingProcedureLine,
    TestingProcedureSection,
)


class TestingProcedureLineSerializer(serializers.ModelSerializer):
    class Meta:
        model = TestingProcedureLine
        fields = ["id", "sequence", "kind", "marker", "text", "interpretation"]
        read_only_fields = ["id"]


class TestingProcedureSectionSerializer(serializers.ModelSerializer):
    lines = TestingProcedureLineSerializer(many=True, required=False)
    section_key_label = serializers.CharField(
        source="get_section_key_display", read_only=True
    )

    class Meta:
        model = TestingProcedureSection
        fields = [
            "id",
            "sequence",
            "section_number",
            "section_key",
            "section_key_label",
            "title",
            "body",
            "lines",
        ]
        read_only_fields = ["id", "section_key_label"]


class TestingProcedureListSerializer(serializers.ModelSerializer):
    """Slim shape for the list screens -- counts instead of full content."""

    procedure_type_label = serializers.CharField(
        source="get_procedure_type_display", read_only=True
    )
    status_label = serializers.CharField(source="get_status_display", read_only=True)
    revision_label = serializers.CharField(read_only=True)
    section_count = serializers.IntegerField(read_only=True)
    line_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = TestingProcedure
        fields = [
            "id",
            "document_code",
            "title",
            "procedure_type",
            "procedure_type_label",
            "heading",
            "organisation",
            "revision_number",
            "revision_date",
            "revision_label",
            "total_pages",
            "classification",
            "status",
            "status_label",
            "section_count",
            "line_count",
            "created_at",
            "updated_at",
        ]


class TestingProcedureSerializer(serializers.ModelSerializer):
    """Full document, sections and lines nested."""

    sections = TestingProcedureSectionSerializer(many=True, required=False)
    procedure_type_label = serializers.CharField(
        source="get_procedure_type_display", read_only=True
    )
    status_label = serializers.CharField(source="get_status_display", read_only=True)
    revision_label = serializers.CharField(read_only=True)

    class Meta:
        model = TestingProcedure
        fields = [
            "id",
            "document_code",
            "title",
            "procedure_type",
            "procedure_type_label",
            "heading",
            "organisation",
            "revision_number",
            "revision_date",
            "total_pages",
            "classification",
            "status",
            "status_label",
            "revision_label",
            "source_text",
            "notes",
            "sections",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "procedure_type_label",
            "status_label",
            "revision_label",
            "created_at",
            "updated_at",
        ]

    def validate_document_code(self, value):
        value = (value or "").strip().upper()
        if not value:
            raise serializers.ValidationError("Document code is required.")
        return value

    def validate_title(self, value):
        value = (value or "").strip()
        if not value:
            raise serializers.ValidationError("Title is required.")
        return value

    def validate(self, attrs):
        """Reject a duplicate code up front so the client gets a field error.

        The DB constraint would catch this too, but as an IntegrityError with
        no field attached -- the form could not highlight the offending input.
        """
        company = self.context.get("company")
        code = attrs.get("document_code") or getattr(self.instance, "document_code", None)
        if company and code:
            clash = TestingProcedure.objects.filter(
                company=company, document_code=code
            )
            if self.instance:
                clash = clash.exclude(pk=self.instance.pk)
            if clash.exists():
                raise serializers.ValidationError(
                    {
                        "document_code": (
                            f"A procedure with document code '{code}' already "
                            "exists. Open it to add a revision instead."
                        )
                    }
                )
        return attrs

    # -- nested write ----------------------------------------------------

    def _write_sections(self, procedure, sections_data):
        """Replace every section/line of ``procedure`` with ``sections_data``.

        Children cascade on delete, so clearing sections clears their lines.
        """
        procedure.sections.all().delete()
        for s_index, section_data in enumerate(sections_data):
            lines_data = section_data.pop("lines", [])
            section = TestingProcedureSection.objects.create(
                procedure=procedure,
                created_by=procedure.updated_by or procedure.created_by,
                updated_by=procedure.updated_by,
                **{**section_data, "sequence": section_data.get("sequence", s_index)},
            )
            TestingProcedureLine.objects.bulk_create(
                [
                    TestingProcedureLine(
                        section=section,
                        created_by=procedure.updated_by or procedure.created_by,
                        updated_by=procedure.updated_by,
                        **{**line, "sequence": line.get("sequence", l_index)},
                    )
                    for l_index, line in enumerate(lines_data)
                ]
            )

    @transaction.atomic
    def create(self, validated_data):
        sections_data = validated_data.pop("sections", [])
        procedure = TestingProcedure.objects.create(**validated_data)
        self._write_sections(procedure, sections_data)
        return procedure

    @transaction.atomic
    def update(self, instance, validated_data):
        sections_data = validated_data.pop("sections", None)
        for field, value in validated_data.items():
            setattr(instance, field, value)
        instance.save()
        # `None` means "header-only edit" -- leave the body untouched.
        if sections_data is not None:
            self._write_sections(instance, sections_data)
        return instance
