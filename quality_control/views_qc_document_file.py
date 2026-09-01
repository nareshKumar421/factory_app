# quality_control/views_qc_document_file.py
"""APIs for the QC PDF document library."""

import os

from django.db.models import Q
from django.shortcuts import get_object_or_404
from rest_framework import serializers, status
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from company.permissions import HasCompanyContext

from .models import QCDocumentFile

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
            "url",
            "original_name",
            "content_type",
            "file_size",
            "uploaded_by_name",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

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
    return QCDocumentFile.objects.filter(company=company, is_active=True).select_related(
        "created_by"
    )


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

        errors = {}
        if not document_code:
            errors["document_code"] = "Document code is required."
        if not title:
            errors["title"] = "Title is required."

        upload = request.FILES.get("file")
        if not upload:
            errors["file"] = "Attach the PDF."
        else:
            ok, message = _validate_pdf(upload)
            if not ok:
                errors["file"] = message

        if not errors and _queryset(company).filter(document_code=document_code).exists():
            errors["document_code"] = (
                f"A document with code '{document_code}' already exists."
            )

        if errors:
            return Response(errors, status=status.HTTP_400_BAD_REQUEST)

        document = QCDocumentFile.objects.create(
            company=company,
            document_code=document_code,
            title=title,
            revision=revision,
            file=upload,
            original_name=(upload.name or "")[:255],
            content_type=(getattr(upload, "content_type", "") or "")[:100],
            file_size=upload.size,
            created_by=request.user,
            updated_by=request.user,
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

        if "document_code" in request.data:
            code = (request.data.get("document_code") or "").strip().upper()
            if not code:
                return Response(
                    {"document_code": "Document code is required."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            clash = (
                _queryset(company).filter(document_code=code).exclude(pk=document.pk)
            )
            if clash.exists():
                return Response(
                    {"document_code": f"A document with code '{code}' already exists."},
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

        document.updated_by = request.user
        document.save()
        return Response(
            QCDocumentFileSerializer(document, context={"request": request}).data
        )

    def delete(self, request, document_id):
        """Soft retire — a controlled document is never erased."""
        document = self._get(request, document_id)
        document.is_active = False
        document.updated_by = request.user
        document.save(update_fields=["is_active", "updated_by", "updated_at"])
        return Response(status=status.HTTP_204_NO_CONTENT)
