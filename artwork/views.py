"""
The artwork register's API.

One list endpoint carries the page: ``items/`` merges SAP's label and carton
items with what has been captured, so the screen can show the gaps. Everything
else hangs off a record.

Files are streamed through permission-checked endpoints rather than exposed on
a media URL, matching ``quality_control.QCDocumentFileDownloadAPI``.
"""

import logging
import os

from django.http import FileResponse
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from company.permissions import HasCompanyContext

from . import services
from .constants import (
    ARTWORK_SUB_GROUPS,
    MAX_CDR_BYTES,
    MAX_PDF_BYTES,
    RECENT_CHANGE_DAYS,
)
from .models import ArtworkRecord, ArtworkRevision
from .permissions import ArtworkPermission, CanManageArtwork, CanViewArtwork
from .serializers import (
    ArtworkItemRowSerializer,
    ArtworkRecordSerializer,
    ArtworkRevisionSerializer,
    CaptureArtworkSerializer,
    ReviseArtworkSerializer,
)

logger = logging.getLogger(__name__)

#: Streamed inline so the browser renders it; the CDR is a download.
PDF_CONTENT_TYPE = "application/pdf"
CDR_CONTENT_TYPE = "application/octet-stream"


def _company(request):
    return request.company.company


def _record(request, pk) -> ArtworkRecord:
    """One record, scoped to the active company.

    Scoped rather than looked up by id alone: item codes repeat across the
    three schemas, and a record only means anything read against its own
    company.
    """
    return get_object_or_404(
        ArtworkRecord.objects.select_related("company", "created_by", "updated_by"),
        pk=pk,
        company=_company(request),
    )


class ArtworkItemListAPI(APIView):
    """GET every label and carton item, with its artwork if it has any.

    Query parameters: ``sub_group`` (LABEL / CARTON), ``search``, ``status``
    (CAPTURED / PENDING), ``changed_recently`` (``true`` narrows to artwork
    touched inside the last :data:`RECENT_CHANGE_DAYS` days).
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewArtwork]

    def get(self, request):
        result = services.list_items(
            company=_company(request),
            sub_group=request.query_params.get("sub_group", ""),
            search=request.query_params.get("search", ""),
            status=request.query_params.get("status", ""),
            changed_recently=request.query_params.get("changed_recently") == "true",
        )
        return Response(
            {
                "sap_available": result["sap_available"],
                "sap_error": result["sap_error"],
                "recent_change_days": result["recent_change_days"],
                "summary": result["summary"],
                "rows": ArtworkItemRowSerializer(result["rows"], many=True).data,
            }
        )


class ArtworkOptionsAPI(APIView):
    """GET what the form needs to know: the kinds, and the upload limits.

    Sent rather than hardcoded in the client so the two cannot drift apart when
    a limit changes.
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewArtwork]

    def get(self, request):
        return Response(
            {
                "sub_groups": [
                    {"value": value, "label": value.title()}
                    for value in ARTWORK_SUB_GROUPS
                ],
                "statuses": [
                    {"value": services.STATUS_CAPTURED, "label": "Captured"},
                    {"value": services.STATUS_PENDING, "label": "Pending"},
                ],
                "recent_change_days": RECENT_CHANGE_DAYS,
                "max_pdf_bytes": MAX_PDF_BYTES,
                "max_cdr_bytes": MAX_CDR_BYTES,
                "accepted_pdf": ".pdf",
                "accepted_cdr": ".cdr",
                "can_manage": CanManageArtwork().has_permission(request, self),
            }
        )


class ArtworkSummaryAPI(APIView):
    """GET how much of the item master has artwork on file."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewArtwork]

    def get(self, request):
        return Response(services.coverage_summary(company=_company(request)))


class ArtworkRecordListCreateAPI(APIView):
    """GET the artwork on file · POST to file artwork for an item."""

    parser_classes = [MultiPartParser, FormParser]
    permission_classes = [IsAuthenticated, HasCompanyContext, ArtworkPermission]

    def get(self, request):
        rows = services.list_records(
            company=_company(request),
            sub_group=request.query_params.get("sub_group", ""),
            search=request.query_params.get("search", ""),
            include_inactive=request.query_params.get("include_inactive") == "true",
        )
        return Response(ArtworkRecordSerializer(rows, many=True).data)

    def post(self, request):
        serializer = CaptureArtworkSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        record = services.capture(
            user=request.user,
            company=_company(request),
            item_code=data["item_code"],
            document_number=data.get("document_number", ""),
            revision_number=data.get("revision_number", 0),
            revision_date=data["revision_date"],
            barcode=data.get("barcode", ""),
            remarks=data.get("remarks", ""),
            pdf_upload=data["pdf_file"],
            cdr_upload=data["cdr_file"],
        )
        return Response(
            ArtworkRecordSerializer(record).data, status=status.HTTP_201_CREATED
        )


class ArtworkRecordDetailAPI(APIView):
    """GET one record · PATCH to revise it · DELETE to retire it."""

    parser_classes = [MultiPartParser, FormParser]
    permission_classes = [IsAuthenticated, HasCompanyContext, ArtworkPermission]

    def get(self, request, pk):
        return Response(ArtworkRecordSerializer(_record(request, pk)).data)

    def patch(self, request, pk):
        record = _record(request, pk)
        if not record.is_active:
            return Response(
                {"detail": "This artwork has been retired and cannot be revised."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = ReviseArtworkSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        record = services.revise(
            user=request.user,
            record=record,
            document_number=data.get("document_number"),
            revision_number=data.get("revision_number"),
            revision_date=data.get("revision_date"),
            barcode=data.get("barcode"),
            remarks=data.get("remarks"),
            pdf_upload=data.get("pdf_file"),
            cdr_upload=data.get("cdr_file"),
        )
        return Response(ArtworkRecordSerializer(record).data)

    # PUT behaves as PATCH: the form sends only what changed, and a true
    # replace would demand both files back on every edit.
    put = patch

    def delete(self, request, pk):
        record = _record(request, pk)
        if not record.is_active:
            return Response(status=status.HTTP_204_NO_CONTENT)
        services.retire(user=request.user, record=record)
        return Response(status=status.HTTP_204_NO_CONTENT)


class ArtworkRevisionListAPI(APIView):
    """GET one record's history, newest superseded state first."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewArtwork]

    def get(self, request, pk):
        record = _record(request, pk)
        return Response(
            ArtworkRevisionSerializer(record.revisions.all(), many=True).data
        )


def _stream(file_field, original_name, kind):
    """Send one stored file, or ``None`` if the record has no such file."""
    if not file_field:
        return None
    inline = kind == "pdf"
    response = FileResponse(
        file_field.open("rb"),
        content_type=PDF_CONTENT_TYPE if inline else CDR_CONTENT_TYPE,
    )
    filename = original_name or os.path.basename(file_field.name)
    disposition = "inline" if inline else "attachment"
    response["Content-Disposition"] = f'{disposition}; filename="{filename}"'
    return response


class ArtworkDownloadAPI(APIView):
    """Stream a record's PDF (inline) or CDR (download) to a permitted user."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewArtwork]

    def get(self, request, pk, kind):
        record = _record(request, pk)
        if kind == "pdf":
            response = _stream(record.pdf_file, record.pdf_original_name, kind)
        elif kind == "cdr":
            response = _stream(record.cdr_file, record.cdr_original_name, kind)
        else:
            return Response(
                {"detail": "Ask for 'pdf' or 'cdr'."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if response is None:
            return Response(
                {"detail": f"This artwork has no {kind.upper()} on file."},
                status=status.HTTP_404_NOT_FOUND,
            )
        return response


class ArtworkRevisionDownloadAPI(APIView):
    """Stream a superseded file, so an old artwork stays retrievable."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewArtwork]

    def get(self, request, pk, kind):
        revision = get_object_or_404(
            ArtworkRevision.objects.select_related("record"),
            pk=pk,
            record__company=_company(request),
        )
        if kind == "pdf":
            response = _stream(revision.pdf_file, revision.pdf_original_name, kind)
        elif kind == "cdr":
            response = _stream(revision.cdr_file, revision.cdr_original_name, kind)
        else:
            return Response(
                {"detail": "Ask for 'pdf' or 'cdr'."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if response is None:
            return Response(
                {"detail": f"This revision has no {kind.upper()} on file."},
                status=status.HTTP_404_NOT_FOUND,
            )
        return response
