# quality_control/views_qc_document_file.py
"""APIs for the QC PDF document library."""

import os

from django.db.models import Q
from django.http import FileResponse
from django.shortcuts import get_object_or_404
from rest_framework import serializers, status
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from company.permissions import HasCompanyContext

from .models import (
    ProcedureType,
    QCDocumentFile,
    QCDocumentFileAction,
    record_document_file_event,
)

MAX_PDF_BYTES = 25 * 1024 * 1024
ALLOWED_TYPES = {"application/pdf"}
ALLOWED_EXTS = {".pdf"}


class CanViewDocumentFiles(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm("quality_control.can_view_document_files")


class CanManageDocumentFiles(BasePermission):
    def has_permission(self, request, view):
        return request.user.has_perm("quality_control.can_manage_document_files")


class QCDocumentFileSerializer(serializers.ModelSerializer):
    url = serializers.SerializerMethodField()
    download_url = serializers.SerializerMethodField()
    procedure_type_label = serializers.CharField(
        source="get_procedure_type_display", read_only=True
    )
    uploaded_by_name = serializers.CharField(
        source="created_by.full_name", read_only=True, allow_null=True, default=None
    )

    class Meta:
        model = QCDocumentFile
        fields = [
            "id",
            "document_code",
            "title",
            "revision",
            "procedure_type",
            "procedure_type_label",
            "url",
            "download_url",
            "original_name",
            "content_type",
            "file_size",
            "uploaded_by_name",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    def get_download_url(self, obj):
        """API path the viewer streams the PDF from.

        Preferred over :meth:`get_url`: it is permission-checked, and the
        client fetches it with its auth header and renders the bytes, which
        also sidesteps the site-wide ``X-Frame-Options: DENY`` that stops a
        media URL rendering in a frame.
        """
        return f"/quality-control/document-files/{obj.pk}/download/"

    def get_url(self, obj):
        """Absolute URL so the viewer can load the PDF straight from media."""
        if not obj.file:
            return None
        request = self.context.get("request")
        url = obj.file.url
        return request.build_absolute_uri(url) if request else url


def _company(request):
    return request.company.company


def _queryset(company):
    """Everything this company may see: shared documents plus its own.

    A shared document has ``company = NULL``. Older rows are private to the
    company that uploaded them and stay that way.
    """
    return (
        QCDocumentFile.objects.filter(is_active=True)
        .filter(Q(company=company) | Q(company__isnull=True))
        .select_related("created_by")
    )


# The only fields a PUT may move. The PDF itself is never swapped -- see the
# docstring on :meth:`QCDocumentFileDetailAPI.put` -- so it never shows up in a
# diff after the upload row.
EDITABLE_FIELDS = ("document_code", "title", "revision", "procedure_type")


def _identifiers(document):
    """The editable fields as they stand right now."""
    return {field: getattr(document, field) for field in EDITABLE_FIELDS}


def _diff(before, after):
    """``{field: {"old": .., "new": ..}}`` for the fields that actually moved."""
    return {
        field: {"old": before[field], "new": after[field]}
        for field in EDITABLE_FIELDS
        if before[field] != after[field]
    }


def _validate_pdf(upload):
    """Return ``(ok, error_message)`` for an uploaded document file."""
    if upload.size > MAX_PDF_BYTES:
        return False, "File too large (max 25 MB)."
    content_type = (getattr(upload, "content_type", "") or "").lower()
    ext = os.path.splitext(upload.name or "")[1].lower()
    # Browsers are inconsistent about the MIME type on a pasted file, so a
    # correct extension is accepted on its own.
    if content_type not in ALLOWED_TYPES and ext not in ALLOWED_EXTS:
        return False, "Only PDF files are allowed."
    return True, ""


class QCDocumentFileListCreateAPI(APIView):
    """GET the library · POST a PDF with its code, title and revision."""

    parser_classes = [MultiPartParser, FormParser]

    def get_permissions(self):
        if self.request.method == "POST":
            return [IsAuthenticated(), HasCompanyContext(), CanManageDocumentFiles()]
        return [IsAuthenticated(), HasCompanyContext(), CanViewDocumentFiles()]

    def get(self, request):
        queryset = _queryset(_company(request))

        procedure_type = request.query_params.get("procedure_type")
        if procedure_type in ProcedureType.values:
            queryset = queryset.filter(procedure_type=procedure_type)

        search = (request.query_params.get("search") or "").strip()
        if search:
            queryset = queryset.filter(
                Q(title__icontains=search) | Q(document_code__icontains=search)
            )
        return Response(
            QCDocumentFileSerializer(
                queryset, many=True, context={"request": request}
            ).data
        )

    def post(self, request):
        company = _company(request)

        document_code = (request.data.get("document_code") or "").strip().upper()
        title = (request.data.get("title") or "").strip()
        revision = (request.data.get("revision") or "").strip()
        procedure_type = (
            request.data.get("procedure_type") or ProcedureType.INHOUSE
        ).strip().upper()

        errors = {}
        if procedure_type not in ProcedureType.values:
            errors["procedure_type"] = (
                f"Must be one of {', '.join(ProcedureType.values)}."
            )
        if not title:
            errors["title"] = "Title is required."

        upload = request.FILES.get("file")
        if not upload:
            errors["file"] = "Attach the PDF."
        else:
            ok, message = _validate_pdf(upload)
            if not ok:
                errors["file"] = message

        # Only a real code has to be unique; code-less documents never clash.
        if (
            not errors
            and document_code
            and _queryset(company).filter(document_code=document_code).exists()
        ):
            errors["document_code"] = (
                f"A document with code '{document_code}' already exists."
            )

        if errors:
            return Response(errors, status=status.HTTP_400_BAD_REQUEST)

        document = QCDocumentFile.objects.create(
            # Shared: uploaded once, readable from every company.
            company=None,
            document_code=document_code,
            title=title,
            revision=revision,
            procedure_type=procedure_type,
            file=upload,
            original_name=(upload.name or "")[:255],
            content_type=(getattr(upload, "content_type", "") or "")[:100],
            file_size=upload.size,
            created_by=request.user,
            updated_by=request.user,
        )
        record_document_file_event(
            request,
            document,
            QCDocumentFileAction.UPLOADED,
            # Not a diff -- an upload has no "before". The values it was filed
            # with are written in the same shape as an edit so the first row of
            # a trail renders through the same code as the rest.
            {
                "document_code": {"old": None, "new": document.document_code},
                "title": {"old": None, "new": document.title},
                "revision": {"old": None, "new": document.revision},
                "procedure_type": {"old": None, "new": document.procedure_type},
                "file": {"old": None, "new": document.original_name},
            },
        )
        return Response(
            QCDocumentFileSerializer(document, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )


class QCDocumentFileDetailAPI(APIView):
    """GET one document · PUT its details · DELETE (soft retire)."""

    def get_permissions(self):
        if self.request.method == "GET":
            return [IsAuthenticated(), HasCompanyContext(), CanViewDocumentFiles()]
        return [IsAuthenticated(), HasCompanyContext(), CanManageDocumentFiles()]

    def _get(self, request, document_id):
        return get_object_or_404(_queryset(_company(request)), id=document_id)

    def get(self, request, document_id):
        return Response(
            QCDocumentFileSerializer(
                self._get(request, document_id), context={"request": request}
            ).data
        )

    def put(self, request, document_id):
        """Edit the three identifiers. The PDF itself is never swapped —
        replacing the file under an unchanged code would silently change what
        a reader sees; upload a new revision instead."""
        document = self._get(request, document_id)
        company = _company(request)
        # Snapshot before anything moves, so the trail can show old -> new.
        before = _identifiers(document)

        if "document_code" in request.data:
            code = (request.data.get("document_code") or "").strip().upper()
            if code:
                clash = (
                    _queryset(company)
                    .filter(document_code=code)
                    .exclude(pk=document.pk)
                )
                if clash.exists():
                    return Response(
                        {
                            "document_code": (
                                f"A document with code '{code}' already exists."
                            )
                        },
                        status=status.HTTP_400_BAD_REQUEST,
                    )
            document.document_code = code

        if "title" in request.data:
            title = (request.data.get("title") or "").strip()
            if not title:
                return Response(
                    {"title": "Title is required."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            document.title = title

        if "revision" in request.data:
            document.revision = (request.data.get("revision") or "").strip()

        if "procedure_type" in request.data:
            next_type = (request.data.get("procedure_type") or "").strip().upper()
            if next_type not in ProcedureType.values:
                return Response(
                    {
                        "procedure_type": (
                            f"Must be one of {', '.join(ProcedureType.values)}."
                        )
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
            document.procedure_type = next_type

        changes = _diff(before, _identifiers(document))
        document.updated_by = request.user
        document.save()
        # Only a real change earns a row: a PUT that re-sends the same values
        # is not an event, and logging it would bury the ones that are.
        if changes:
            record_document_file_event(
                request, document, QCDocumentFileAction.EDITED, changes
            )
        return Response(
            QCDocumentFileSerializer(document, context={"request": request}).data
        )

    def delete(self, request, document_id):
        """Soft retire — a controlled document is never erased."""
        document = self._get(request, document_id)
        document.is_active = False
        document.updated_by = request.user
        document.save(update_fields=["is_active", "updated_by", "updated_at"])
        record_document_file_event(
            request,
            document,
            QCDocumentFileAction.RETIRED,
            {"is_active": {"old": True, "new": False}},
        )
        return Response(status=status.HTTP_204_NO_CONTENT)


class QCDocumentFileDownloadAPI(APIView):
    """Stream the stored PDF to a permitted user.

    The viewer fetches this with its auth header and renders the bytes from a
    blob, so the document is never exposed on an unauthenticated media URL and
    the site-wide ``X-Frame-Options: DENY`` cannot stop it displaying.
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewDocumentFiles]

    def get(self, request, document_id):
        document = get_object_or_404(_queryset(_company(request)), id=document_id)
        if not document.file:
            return Response(
                {"detail": "This document has no file attached."},
                status=status.HTTP_404_NOT_FOUND,
            )

        response = FileResponse(
            document.file.open("rb"),
            content_type=document.content_type or "application/pdf",
        )
        # `inline` so the browser renders it rather than forcing a save.
        filename = document.original_name or os.path.basename(document.file.name)
        response["Content-Disposition"] = f'inline; filename="{filename}"'
        return response
