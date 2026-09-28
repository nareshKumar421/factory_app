"""SAP Documents API.

The document browser SAP Portal served at ``/api/sap/documents/*``,
``/payment-drafts/:entry`` and ``/attachments/*`` (``backend_v1/routes/sap.js``).

Every view: login, the ``Company-Code`` context, then its own right — the
portal took the company from a ``?company=`` any login could set. SAP errors map
the way every JI SAP endpoint maps them — SAP's refusal → 400, SAP unreachable →
503, SAP broken → 502 — where the portal answered an unreadable list with an
empty one.
"""

import logging

from rest_framework import status
from rest_framework.exceptions import APIException, NotFound
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from company.permissions import HasCompanyContext
from sap_client.exceptions import SAPConnectionError, SAPDataError, SAPValidationError

from . import services
from .constants import DOCUMENT_TYPES
from .permissions import CanDownloadSapAttachments, CanViewSapDocuments
from .serializers import DocumentFilterSerializer

logger = logging.getLogger(__name__)

VIEW_RIGHTS = [IsAuthenticated, HasCompanyContext, CanViewSapDocuments]


class UnknownDocumentType(APIException):
    status_code = status.HTTP_400_BAD_REQUEST
    default_code = "unknown_document_type"


def _document_type(key: str):
    doc_type = DOCUMENT_TYPES.get(key)
    if doc_type is None:
        raise UnknownDocumentType(f"Unknown document type: {key}")
    return doc_type


class _SapDocumentsView(APIView):
    """Company context and SAP error shaping shared by every endpoint."""

    permission_classes = VIEW_RIGHTS

    def handle_exception(self, exc):
        if isinstance(exc, SAPValidationError):
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        if isinstance(exc, SAPConnectionError):
            logger.error("SAP unreachable in %s: %s", type(self).__name__, exc)
            return Response(
                {"detail": "SAP system is currently unavailable. Please try again later."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        if isinstance(exc, SAPDataError):
            logger.error("SAP data error in %s: %s", type(self).__name__, exc)
            return Response({"detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        return super().handle_exception(exc)

    @property
    def company(self):
        return self.request.company.company


class DocumentTypesAPI(_SapDocumentsView):
    """GET — the document types the browser opens, with the filters each takes."""

    def get(self, request):
        return Response([doc_type.as_dict() for doc_type in DOCUMENT_TYPES.values()])


class DocumentListAPI(_SapDocumentsView):
    """GET — one page of a document type, newest first.
    ?number ?partner ?date_from ?date_to ?status=O|C|L ?top (≤ 100) ?skip"""

    def get(self, request, doc_type):
        document_type = _document_type(doc_type)
        filters = DocumentFilterSerializer(data=request.query_params, document_type=document_type)
        filters.is_valid(raise_exception=True)
        return Response(services.list_documents(self.company.code, document_type, filters.validated_data))


class DocumentDetailAPI(_SapDocumentsView):
    """GET — one document: header, lines with names, totals, partner and
    ship-from, base documents, the journal (or a draft's reconstruction)."""

    def get(self, request, doc_type, doc_entry):
        document_type = _document_type(doc_type)
        document = services.document_detail(self.company.code, document_type, doc_entry)
        if document is None:
            raise NotFound(f"{document_type.label} {doc_entry} was not found in SAP.")
        return Response(document)


class PaymentDraftAPI(_SapDocumentsView):
    """GET — one outgoing-payment draft (OPDF) with its G/L lines, settled
    documents, cheques, withholding and journal (posted or previewed)."""

    def get(self, request, doc_entry):
        document = services.payment_draft(self.company.code, doc_entry)
        if document is None:
            raise NotFound(f"Payment draft {doc_entry} was not found in SAP.")
        return Response(document)


class AttachmentLinesAPI(_SapDocumentsView):
    """GET — the files of one SAP attachment entry (ATC1)."""

    def get(self, request, abs_entry):
        return Response({"abs_entry": abs_entry, "lines": services.attachment_lines(self.company.code, abs_entry)})


class AttachmentDownloadAPI(_SapDocumentsView):
    """GET — one attachment file, streamed from the SAP file service.

    Needs the view right *and* the download right. PDFs, pictures and plain
    text are sent inline; everything else as a download of
    ``application/octet-stream``. Each file served is recorded.
    """

    permission_classes = [*VIEW_RIGHTS, CanDownloadSapAttachments]

    def get(self, request, abs_entry, line):
        try:
            served = services.fetch_attachment(self.company, request.user, abs_entry, line)
        except services.AttachmentNotFound as e:
            return Response({"detail": str(e)}, status=status.HTTP_404_NOT_FOUND)
        except SAPValidationError as e:
            if getattr(e, "status", None) == 404:
                return Response(
                    {"detail": f"The attachment file service has no copy of this file. {e}"},
                    status=status.HTTP_404_NOT_FOUND,
                )
            raise
        return services.served_file_response(served)
