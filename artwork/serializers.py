"""Shapes the artwork register sends to the page."""

from rest_framework import serializers

from .models import ArtworkRecord, ArtworkRevision


class ArtworkRevisionSerializer(serializers.ModelSerializer):
    """One superseded state, with links to the files as they were."""

    revision_label = serializers.CharField(read_only=True)
    superseded_by_name = serializers.CharField(
        source="superseded_by.full_name", read_only=True, allow_null=True, default=None
    )
    pdf_download_url = serializers.SerializerMethodField()
    cdr_download_url = serializers.SerializerMethodField()

    class Meta:
        model = ArtworkRevision
        fields = [
            "id",
            "document_number",
            "revision_number",
            "revision_label",
            "revision_date",
            "barcode",
            "remarks",
            "pdf_original_name",
            "cdr_original_name",
            "pdf_download_url",
            "cdr_download_url",
            "superseded_at",
            "superseded_by_name",
        ]
        read_only_fields = fields

    def get_pdf_download_url(self, obj):
        return (
            f"/api/v1/artwork/revisions/{obj.pk}/download/pdf/" if obj.pdf_file else None
        )

    def get_cdr_download_url(self, obj):
        return (
            f"/api/v1/artwork/revisions/{obj.pk}/download/cdr/" if obj.cdr_file else None
        )


class ArtworkRecordSerializer(serializers.ModelSerializer):
    """An artwork on file.

    The two file links are API paths, not media URLs: they are permission
    checked, and the client fetches them with its auth header. That also
    sidesteps the site-wide ``X-Frame-Options: DENY`` that would stop a media
    URL rendering in the PDF preview frame.
    """

    revision_label = serializers.CharField(read_only=True)
    sub_group_label = serializers.CharField(
        source="get_sub_group_display", read_only=True
    )
    company_code = serializers.CharField(source="company.code", read_only=True)
    created_by_name = serializers.CharField(
        source="created_by.full_name", read_only=True, allow_null=True, default=None
    )
    updated_by_name = serializers.CharField(
        source="updated_by.full_name", read_only=True, allow_null=True, default=None
    )
    pdf_download_url = serializers.SerializerMethodField()
    cdr_download_url = serializers.SerializerMethodField()
    revision_count = serializers.SerializerMethodField()
    next_revision_number = serializers.SerializerMethodField()

    class Meta:
        model = ArtworkRecord
        fields = [
            "id",
            "company_code",
            "item_code",
            "item_name",
            "sub_group",
            "sub_group_label",
            "document_number",
            "revision_number",
            "revision_label",
            "revision_date",
            "barcode",
            "remarks",
            "pdf_original_name",
            "pdf_size",
            "pdf_download_url",
            "cdr_original_name",
            "cdr_size",
            "cdr_download_url",
            "revision_count",
            "next_revision_number",
            "is_active",
            "created_by_name",
            "updated_by_name",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    def get_pdf_download_url(self, obj):
        return f"/api/v1/artwork/records/{obj.pk}/download/pdf/" if obj.pdf_file else None

    def get_cdr_download_url(self, obj):
        return f"/api/v1/artwork/records/{obj.pk}/download/cdr/" if obj.cdr_file else None

    def get_revision_count(self, obj):
        return obj.revisions.count()

    def get_next_revision_number(self, obj):
        """Offered as the form default when the page opens the revise dialog."""
        return obj.revision_number + 1


class ArtworkItemRowSerializer(serializers.Serializer):
    """One row of the main list: a SAP item and its artwork, if any.

    Plain ``Serializer`` rather than a model one -- a row here is a merge of
    SAP and the register, and most rows have no model instance behind them at
    all. Output only.
    """

    item_code = serializers.CharField()
    item_name = serializers.CharField()
    sub_group = serializers.CharField()
    uom = serializers.CharField()
    in_sap = serializers.BooleanField()
    status = serializers.CharField()
    record_id = serializers.IntegerField(allow_null=True)
    document_number = serializers.CharField()
    revision_number = serializers.IntegerField(allow_null=True)
    revision_label = serializers.CharField()
    revision_date = serializers.DateField(allow_null=True)
    barcode = serializers.CharField()
    has_pdf = serializers.BooleanField()
    has_cdr = serializers.BooleanField()
    updated_at = serializers.DateTimeField(allow_null=True)


class CaptureArtworkSerializer(serializers.Serializer):
    """Input for filing artwork against an item that has none.

    Both files are required here and nowhere else: a record is not allowed to
    exist without them, but a later correction must not demand they be
    re-uploaded (see :class:`ReviseArtworkSerializer`).
    """

    item_code = serializers.CharField(max_length=50)
    document_number = serializers.CharField(max_length=64)
    revision_number = serializers.IntegerField(min_value=0, max_value=999, default=0)
    revision_date = serializers.DateField()
    barcode = serializers.CharField(
        max_length=64, required=False, allow_blank=True, default=""
    )
    remarks = serializers.CharField(required=False, allow_blank=True, default="")
    pdf_file = serializers.FileField()
    cdr_file = serializers.FileField()


class ReviseArtworkSerializer(serializers.Serializer):
    """Input for changing an artwork already on file. Every field optional."""

    document_number = serializers.CharField(max_length=64, required=False)
    revision_number = serializers.IntegerField(
        min_value=0, max_value=999, required=False
    )
    revision_date = serializers.DateField(required=False)
    barcode = serializers.CharField(max_length=64, required=False, allow_blank=True)
    remarks = serializers.CharField(required=False, allow_blank=True)
    pdf_file = serializers.FileField(required=False)
    cdr_file = serializers.FileField(required=False)

    def validate(self, attrs):
        if not attrs:
            raise serializers.ValidationError(
                "Nothing to change. Send at least one field."
            )
        return attrs
