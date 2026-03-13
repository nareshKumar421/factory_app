import json
import logging
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser

from company.permissions import HasCompanyContext
from sap_client.exceptions import SAPConnectionError, SAPDataError, SAPValidationError

from .services import GRPOService
from .serializers import (
    GRPOPreviewSerializer,
    GRPOPostRequestSerializer,
    GRPOPostingSerializer,
    GRPOPostResponseSerializer,
    GRPOAttachmentSerializer,
    GRPOAttachmentUploadSerializer,
)
from .permissions import (
    CanViewPendingGRPO,
    CanPreviewGRPO,
    CanCreateGRPOPosting,
    CanViewGRPOHistory,
    CanViewGRPOPosting,
    CanManageGRPOAttachments,
)

logger = logging.getLogger(__name__)


class PendingGRPOListAPI(APIView):
    """
    Returns list of completed gate entries pending GRPO posting.

    GET /api/grpo/pending/
    """
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewPendingGRPO]

    def get(self, request):
        service = GRPOService(company_code=request.company.company.code)
        entries = service.get_pending_grpo_entries()

        # Build response with summary for each entry
        result = []
        for entry in entries:
            po_receipts = entry.po_receipts.all()
            posted_count = entry.grpo_postings.filter(status="POSTED").count()
            total_count = po_receipts.count()
            pending_count = total_count - posted_count

            # Only include entries that have pending POs
            if pending_count > 0:
                result.append({
                    "vehicle_entry_id": entry.id,
                    "entry_no": entry.entry_no,
                    "status": entry.status,
                    "entry_time": entry.entry_time,
                    "total_po_count": total_count,
                    "posted_po_count": posted_count,
                    "pending_po_count": pending_count,
                    "is_fully_posted": False
                })

        return Response(result)


class GRPOPreviewAPI(APIView):
    """
    Returns all data required for GRPO posting for a specific gate entry.
    Shows PO details, items, QC status, and accepted quantities.

    GET /api/grpo/preview/<vehicle_entry_id>/
    """
    permission_classes = [IsAuthenticated, HasCompanyContext, CanPreviewGRPO]

    def get(self, request, vehicle_entry_id):
        service = GRPOService(company_code=request.company.company.code)

        try:
            preview_data = service.get_grpo_preview_data(vehicle_entry_id)
        except ValueError as e:
            return Response(
                {"detail": str(e)},
                status=status.HTTP_404_NOT_FOUND
            )

        serializer = GRPOPreviewSerializer(preview_data, many=True)
        return Response(serializer.data)


class PostGRPOAPI(APIView):
    """
    Post GRPO to SAP for a specific PO receipt.
    Supports multipart/form-data to include attachments during posting.
    Attachments are uploaded to SAP first and included in the GRPO document
    creation, avoiding the approval re-trigger on PATCH.

    Supports two content types:
    1. application/json — JSON body (no attachments)
    2. multipart/form-data — JSON in 'data' field + files in 'attachments' field(s)

    SAP requires attachments at GRPO creation time. When attachments are provided,
    they are uploaded to SAP Attachments2 first, and the resulting AttachmentEntry
    is included in the GRPO payload.

    POST /api/grpo/post/

    For multipart/form-data:
      - Send JSON fields as a "data" part (JSON string)
      - Send files as "attachments" parts

    For application/json (no attachments):
      - Send JSON body as before (backward compatible)
    """
    permission_classes = [IsAuthenticated, HasCompanyContext, CanCreateGRPOPosting]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def post(self, request):
        # Handle multipart form data: JSON in 'data' field + files in 'attachments'
        if request.content_type and 'multipart' in request.content_type:
            try:
                raw_data = request.data.get("data", "{}")
                if isinstance(raw_data, str):
                    parsed_data = json.loads(raw_data)
                else:
                    parsed_data = raw_data
            except json.JSONDecodeError:
                return Response(
                    {"detail": "Invalid JSON in 'data' field"},
                    status=status.HTTP_400_BAD_REQUEST
                )
            attachments = request.FILES.getlist("attachments")
        else:
            parsed_data = request.data
            attachments = []

        serializer = GRPOPostRequestSerializer(data=parsed_data)
        if not serializer.is_valid():
            return Response(
                {"detail": "Invalid request data", "errors": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST
            )

        service = GRPOService(company_code=request.company.company.code)

        try:
            grpo_posting = service.post_grpo(
                vehicle_entry_id=serializer.validated_data["vehicle_entry_id"],
                po_receipt_id=serializer.validated_data["po_receipt_id"],
                user=request.user,
                items=serializer.validated_data["items"],
                branch_id=serializer.validated_data["branch_id"],
                warehouse_code=serializer.validated_data.get("warehouse_code"),
                comments=serializer.validated_data.get("comments"),
                vendor_ref=serializer.validated_data.get("vendor_ref"),
                extra_charges=serializer.validated_data.get("extra_charges"),
                attachments=attachments,
                doc_date=serializer.validated_data.get("doc_date"),
                doc_due_date=serializer.validated_data.get("doc_due_date"),
                tax_date=serializer.validated_data.get("tax_date"),
                should_roundoff=serializer.validated_data.get("should_roundoff", False),
            )

            response_data = {
                "success": True,
                "grpo_posting_id": grpo_posting.id,
                "sap_doc_entry": grpo_posting.sap_doc_entry,
                "sap_doc_num": grpo_posting.sap_doc_num,
                "sap_doc_total": grpo_posting.sap_doc_total,
                "message": f"GRPO posted successfully. SAP Doc Num: {grpo_posting.sap_doc_num}",
                "attachments": grpo_posting.attachments.all(),
            }

            return Response(
                GRPOPostResponseSerializer(
                    response_data, context={"request": request}
                ).data,
                status=status.HTTP_201_CREATED
            )

        except ValueError as e:
            return Response(
                {"detail": str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )

        except SAPValidationError as e:
            return Response(
                {"detail": f"SAP validation error: {str(e)}"},
                status=status.HTTP_400_BAD_REQUEST
            )

        except SAPConnectionError:
            return Response(
                {"detail": "SAP system is currently unavailable. Please try again later."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE
            )

        except SAPDataError as e:
            return Response(
                {"detail": f"SAP error: {str(e)}"},
                status=status.HTTP_502_BAD_GATEWAY
            )


class GRPOPostingHistoryAPI(APIView):
    """
    Returns GRPO posting history.

    GET /api/grpo/history/
    GET /api/grpo/history/?vehicle_entry_id=123
    """
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewGRPOHistory]

    def get(self, request):
        vehicle_entry_id = request.GET.get("vehicle_entry_id")

        service = GRPOService(company_code=request.company.company.code)
        postings = service.get_grpo_posting_history(
            vehicle_entry_id=int(vehicle_entry_id) if vehicle_entry_id else None
        )

        serializer = GRPOPostingSerializer(postings, many=True)
        return Response(serializer.data)


class GRPOPostingDetailAPI(APIView):
    """
    Returns details of a specific GRPO posting.

    GET /api/grpo/<posting_id>/
    """
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewGRPOPosting]

    def get(self, request, posting_id):
        from .models import GRPOPosting

        try:
            posting = GRPOPosting.objects.select_related(
                "vehicle_entry",
                "po_receipt",
                "posted_by"
            ).prefetch_related("lines", "attachments").get(id=posting_id)
        except GRPOPosting.DoesNotExist:
            return Response(
                {"detail": "GRPO posting not found"},
                status=status.HTTP_404_NOT_FOUND
            )

        serializer = GRPOPostingSerializer(posting,context={"request": request})
        return Response(serializer.data)


class GRPOAttachmentListCreateAPI(APIView):
    """
    List and upload attachments for a GRPO posting.

    GET  /api/grpo/<posting_id>/attachments/
    POST /api/grpo/<posting_id>/attachments/  (multipart/form-data)
    """
    permission_classes = [IsAuthenticated, HasCompanyContext, CanManageGRPOAttachments]
    parser_classes = [MultiPartParser, FormParser]

    def get(self, request, posting_id):
        from .models import GRPOAttachment

        attachments = GRPOAttachment.objects.filter(
            grpo_posting_id=posting_id
        ).order_by("-uploaded_at")

        serializer = GRPOAttachmentSerializer(
            attachments, many=True, context={"request": request}
        )
        return Response(serializer.data)

    def post(self, request, posting_id):
        serializer = GRPOAttachmentUploadSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                {"detail": "Invalid file upload", "errors": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST
            )

        service = GRPOService(company_code=request.company.company.code)

        try:
            attachment = service.upload_grpo_attachment(
                grpo_posting_id=posting_id,
                file=serializer.validated_data["file"],
                user=request.user,
            )

            response_serializer = GRPOAttachmentSerializer(
                attachment, context={"request": request}
            )

            return Response(
                response_serializer.data,
                status=status.HTTP_201_CREATED
            )

        except ValueError as e:
            return Response(
                {"detail": str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )


class GRPOAttachmentDeleteAPI(APIView):
    """
    Delete a GRPO attachment.

    DELETE /api/grpo/<posting_id>/attachments/<attachment_id>/
    """
    permission_classes = [IsAuthenticated, HasCompanyContext, CanManageGRPOAttachments]

    def delete(self, request, posting_id, attachment_id):
        from .models import GRPOAttachment

        try:
            attachment = GRPOAttachment.objects.get(
                id=attachment_id,
                grpo_posting_id=posting_id,
            )
        except GRPOAttachment.DoesNotExist:
            return Response(
                {"detail": "Attachment not found"},
                status=status.HTTP_404_NOT_FOUND
            )

        if attachment.file:
            attachment.file.delete(save=False)

        attachment.delete()

        return Response(status=status.HTTP_204_NO_CONTENT)


class GRPOAttachmentRetryAPI(APIView):
    """
    Retry uploading a FAILED attachment to SAP.

    POST /api/grpo/<posting_id>/attachments/<attachment_id>/retry/
    """
    permission_classes = [IsAuthenticated, HasCompanyContext, CanManageGRPOAttachments]

    def post(self, request, posting_id, attachment_id):
        from .models import GRPOAttachment

        if not GRPOAttachment.objects.filter(
            id=attachment_id, grpo_posting_id=posting_id
        ).exists():
            return Response(
                {"detail": "Attachment not found"},
                status=status.HTTP_404_NOT_FOUND
            )

        service = GRPOService(company_code=request.company.company.code)

        try:
            attachment = service.retry_attachment_upload(
                attachment_id=attachment_id
            )

            serializer = GRPOAttachmentSerializer(
                attachment, context={"request": request}
            )
            return Response(serializer.data)

        except ValueError as e:
            return Response(
                {"detail": str(e)},
                status=status.HTTP_400_BAD_REQUEST
            )
