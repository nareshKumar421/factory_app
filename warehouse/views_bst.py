"""API views for the Branch Stock Transfer (BST) sender flow."""

import logging

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from company.permissions import HasCompanyContext
from gate_core.permissions import HasRequiredDjangoPermission
from sap_client.exceptions import SAPConnectionError, SAPDataError

from .serializers_bst import (
    BSTBoxScanBatchSerializer,
    BSTBoxScanBulkDeleteSerializer,
    BSTBoxScanCreateSerializer,
    BSTBoxScanSerializer,
    BSTManualItemEntrySaveSerializer,
    BSTPartialTransferApprovalSerializer,
    BSTPartialTransferRequestCreateSerializer,
    BSTPartialTransferReviewSerializer,
    BSTReceiveScanSerializer,
    BSTSapDocumentSerializer,
    BSTTransferCancelSerializer,
    BSTTransferCreateSerializer,
    BSTTransferDetailSerializer,
    BSTTransferListSerializer,
    BSTTransferUpdateSerializer,
)
from .services.bst_service import BSTError, BSTService

logger = logging.getLogger(__name__)


def _service(request) -> BSTService:
    return BSTService(request.company.company.code, request.user)


def _apply_date_filter(qs, request, field="created_at"):
    """Filter a transfer queryset by the global ?from_date/&to_date range.

    `field` selects which datetime column to filter on — the gate-out queue
    filters on `scan_approved_at` (when the warehouse approved it), not creation.
    """
    from_date = request.query_params.get("from_date")
    to_date = request.query_params.get("to_date")
    if from_date:
        qs = qs.filter(**{f"{field}__date__gte": from_date})
    if to_date:
        qs = qs.filter(**{f"{field}__date__lte": to_date})
    return qs


def _bst_error(exc) -> Response:
    return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)


def _sap_error(exc) -> Response:
    logger.error("SAP error in BST flow: %s", exc)
    return Response({"error": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)


# ---------------------------------------------------------------------------
# SAP stock-transfer lookup
# ---------------------------------------------------------------------------

class BSTSAPTransferListView(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def get(self, request):
        svc = _service(request)
        try:
            documents = svc.list_sap_documents(
                document_type=request.query_params.get("document_type"),
                search=request.query_params.get("search"),
                from_date=request.query_params.get("from_date") or None,
                to_date=request.query_params.get("to_date") or None,
                limit=int(request.query_params.get("limit") or 50),
            )
        except (SAPConnectionError, SAPDataError) as exc:
            return _sap_error(exc)
        return Response(BSTSapDocumentSerializer(documents, many=True).data)


class BSTSAPTransferDetailView(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def get(self, request, doc_entry):
        svc = _service(request)
        try:
            document = svc.get_sap_document(
                doc_entry, document_type=request.query_params.get("document_type"),
            )
        except BSTError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_404_NOT_FOUND)
        except (SAPConnectionError, SAPDataError) as exc:
            return _sap_error(exc)
        return Response(BSTSapDocumentSerializer(document).data)


# ---------------------------------------------------------------------------
# BST transfers — list / create / detail
# ---------------------------------------------------------------------------

class BSTTransferListCreateView(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def get(self, request):
        svc = _service(request)
        qs = svc.list_queryset()
        status_filter = request.query_params.get("status")
        if status_filter:
            qs = qs.filter(status=status_filter)
        qs = _apply_date_filter(qs, request)
        return Response(BSTTransferListSerializer(qs, many=True).data)

    def post(self, request):
        serializer = BSTTransferCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        svc = _service(request)
        try:
            transfer = svc.create_transfer(serializer.validated_data)
        except BSTError as exc:
            return _bst_error(exc)
        except (SAPConnectionError, SAPDataError) as exc:
            return _sap_error(exc)
        transfer = svc.get_transfer(transfer.id)
        return Response(
            BSTTransferDetailSerializer(transfer).data,
            status=status.HTTP_201_CREATED,
        )


class BSTTransferDetailView(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def get(self, request, transfer_id):
        svc = _service(request)
        try:
            transfer = svc.get_transfer(transfer_id)
        except BSTError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_404_NOT_FOUND)
        return Response(BSTTransferDetailSerializer(transfer).data)

    def put(self, request, transfer_id):
        serializer = BSTTransferUpdateSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        svc = _service(request)
        try:
            transfer = svc.get_transfer(transfer_id)
            svc.update_transfer(transfer, serializer.validated_data)
        except BSTError as exc:
            return _bst_error(exc)
        transfer = svc.get_transfer(transfer_id)
        return Response(BSTTransferDetailSerializer(transfer).data)


# ---------------------------------------------------------------------------
# Box scans
# ---------------------------------------------------------------------------

class BSTBoxScanListCreateView(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def get(self, request, transfer_id):
        svc = _service(request)
        try:
            transfer = svc.get_transfer(transfer_id)
        except BSTError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_404_NOT_FOUND)
        scans = transfer.box_scans.select_related("scanned_by", "received_by")
        return Response(BSTBoxScanSerializer(scans, many=True).data)

    def post(self, request, transfer_id):
        serializer = BSTBoxScanCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        svc = _service(request)
        barcode_raw = serializer.validated_data["barcode_raw"]
        try:
            transfer = svc.get_transfer(transfer_id)
        except BSTError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_404_NOT_FOUND)
        try:
            result = svc.scan(transfer, barcode_raw)
        except BSTError as exc:
            # scan() rolled back its own transaction; log the miss here (request
            # runs in autocommit) so BST scan failures stop being invisible in
            # ScanLog. No-op unless the barcode truly didn't resolve.
            svc.scanner.log_scan_miss_if_unknown(
                barcode_raw,
                context_ref_type="BST_TRANSFER",
                context_ref_id=transfer.id,
                user=request.user,
                device_info=request.META.get("HTTP_USER_AGENT", "")[:500],
            )
            return _bst_error(exc)
        created = BSTBoxScanSerializer(result.pop("created"), many=True).data
        result["created"] = created
        http_status = status.HTTP_201_CREATED if result["created_count"] else status.HTTP_200_OK
        return Response(result, status=http_status)


class BSTBoxScanBatchView(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def post(self, request, transfer_id):
        serializer = BSTBoxScanBatchSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        svc = _service(request)
        try:
            transfer = svc.get_transfer(transfer_id)
            result = svc.scan_batch(transfer, serializer.validated_data["barcodes"])
        except BSTError as exc:
            return _bst_error(exc)
        # Log per-code misses (scan_batch swallows them into result["failed"]) so
        # BST batch scan failures are visible in ScanLog. Each call no-ops unless
        # the barcode genuinely didn't resolve for this company.
        device_info = request.META.get("HTTP_USER_AGENT", "")[:500]
        for item in result.get("failed", []):
            svc.scanner.log_scan_miss_if_unknown(
                item.get("barcode", ""),
                context_ref_type="BST_TRANSFER",
                context_ref_id=transfer.id,
                user=request.user,
                device_info=device_info,
            )
        return Response(result)


class BSTBoxScanDetailView(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def delete(self, request, transfer_id, scan_id):
        svc = _service(request)
        try:
            transfer = svc.get_transfer(transfer_id)
            svc.remove_scan(transfer, scan_id)
        except BSTError as exc:
            return _bst_error(exc)
        return Response(status=status.HTTP_204_NO_CONTENT)


class BSTBoxScanBulkDeleteView(APIView):
    """POST /bst/<transfer_id>/box-scans/bulk-delete/ — drop the ticked scans.

    A DELETE per row is what the screen used to do; a wrong pallet is dozens of rows,
    so the operator ticks them and they go in one atomic call (see
    ``BSTService.remove_scans``).
    """

    permission_classes = [IsAuthenticated, HasCompanyContext]

    def post(self, request, transfer_id):
        serializer = BSTBoxScanBulkDeleteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        svc = _service(request)
        try:
            transfer = svc.get_transfer(transfer_id)
            removed = svc.remove_scans(transfer, serializer.validated_data["scan_ids"])
        except BSTError as exc:
            return _bst_error(exc)
        return Response({"removed": removed})


# ---------------------------------------------------------------------------
# Manual entry (scan-exempt / PM lines)
# ---------------------------------------------------------------------------

class BSTManualEntryView(APIView):
    """POST /bst/<transfer_id>/manual-entries/ — type a PM line's quantity.

    Returns the refreshed transfer so the caller's bill table (and its scan status)
    updates in one round trip, like the other sender mutations.
    """

    permission_classes = [IsAuthenticated, HasCompanyContext]

    def post(self, request, transfer_id):
        serializer = BSTManualItemEntrySaveSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        svc = _service(request)
        try:
            transfer = svc.get_transfer(transfer_id)
            svc.set_manual_item_qty(
                transfer,
                data["item_code"],
                data["quantity"],
                notes=data.get("notes", ""),
            )
        except BSTError as exc:
            return _bst_error(exc)
        return Response(BSTTransferDetailSerializer(svc.get_transfer(transfer_id)).data)


# ---------------------------------------------------------------------------
# Dispatch / cancel
# ---------------------------------------------------------------------------

class BSTApproveView(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def post(self, request, transfer_id):
        svc = _service(request)
        try:
            transfer = svc.get_transfer(transfer_id)
            svc.approve(transfer)
        except BSTError as exc:
            return _bst_error(exc)
        transfer = svc.get_transfer(transfer_id)
        return Response(BSTTransferDetailSerializer(transfer).data)


class BSTCancelView(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def post(self, request, transfer_id):
        serializer = BSTTransferCancelSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        svc = _service(request)
        try:
            transfer = svc.get_transfer(transfer_id)
            svc.cancel(transfer, serializer.validated_data.get("cancel_reason", ""))
        except BSTError as exc:
            return _bst_error(exc)
        transfer = svc.get_transfer(transfer_id)
        return Response(BSTTransferDetailSerializer(transfer).data)


# ---------------------------------------------------------------------------
# Partial-transfer approval (seal a short scan with admin sign-off)
# ---------------------------------------------------------------------------

class BSTPartialTransferRequestView(APIView):
    """POST /bst/<transfer_id>/partial-transfer/request/ — operator raises a request."""
    permission_classes = [IsAuthenticated, HasCompanyContext, HasRequiredDjangoPermission]
    required_permissions = "warehouse.can_request_bst_partial_transfer"

    def post(self, request, transfer_id):
        serializer = BSTPartialTransferRequestCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        svc = _service(request)
        try:
            transfer = svc.get_transfer(transfer_id)
            req = svc.request_partial_transfer(transfer, serializer.validated_data["reason"])
        except BSTError as exc:
            return _bst_error(exc)
        return Response(
            BSTPartialTransferApprovalSerializer(req).data,
            status=status.HTTP_201_CREATED,
        )


class BSTPartialTransferListView(APIView):
    """GET /bst/partial-transfers/?status= — admin review queue for this company."""
    permission_classes = [IsAuthenticated, HasCompanyContext, HasRequiredDjangoPermission]
    required_permissions = "warehouse.can_view_bst_partial_transfer"

    def get(self, request):
        svc = _service(request)
        qs = svc.partial_transfer_queryset(status=request.query_params.get("status"))
        qs = _apply_date_filter(qs, request, field="requested_at")
        return Response(BSTPartialTransferApprovalSerializer(qs, many=True).data)


class BSTPartialTransferReviewBaseView(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, HasRequiredDjangoPermission]
    required_permissions = "warehouse.can_approve_bst_partial_transfer"
    approve = None

    def post(self, request, pk):
        serializer = BSTPartialTransferReviewSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        svc = _service(request)
        try:
            req = svc.review_partial_transfer(
                pk, approve=self.approve,
                notes=serializer.validated_data.get("review_notes", ""),
            )
        except BSTError as exc:
            return _bst_error(exc)
        return Response(BSTPartialTransferApprovalSerializer(req).data)


class BSTPartialTransferApproveView(BSTPartialTransferReviewBaseView):
    approve = True


class BSTPartialTransferRejectView(BSTPartialTransferReviewBaseView):
    approve = False


# ---------------------------------------------------------------------------
# Receiver side (current company == destination)
# ---------------------------------------------------------------------------

class BSTIncomingListView(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def get(self, request):
        svc = _service(request)
        qs = _apply_date_filter(svc.incoming_view_queryset(), request)
        return Response(BSTTransferListSerializer(qs, many=True).data)


class BSTIncomingDetailView(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def get(self, request, transfer_id):
        svc = _service(request)
        try:
            transfer = svc.get_incoming_transfer(transfer_id)
        except BSTError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_404_NOT_FOUND)
        return Response(BSTTransferDetailSerializer(transfer).data)


class BSTReceiveScanView(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def post(self, request, transfer_id):
        serializer = BSTReceiveScanSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        svc = _service(request)
        data = serializer.validated_data
        try:
            transfer = svc.get_incoming_transfer(transfer_id)
            result = svc.receive_scan(
                transfer, data["barcode_raw"],
                decision=data["decision"], reject_reason=data.get("reject_reason", ""),
            )
        except BSTError as exc:
            return _bst_error(exc)
        return Response(result)


class BSTReceiveCompleteView(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def post(self, request, transfer_id):
        svc = _service(request)
        try:
            transfer = svc.get_incoming_transfer(transfer_id)
            svc.receive_complete(transfer)
        except BSTError as exc:
            return _bst_error(exc)
        transfer = svc.get_incoming_transfer(transfer_id)
        return Response(BSTTransferDetailSerializer(transfer).data)


# ---------------------------------------------------------------------------
# Gate side (only for transfers that require a gate movement)
# ---------------------------------------------------------------------------

class BSTGateOutwardsListView(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, HasRequiredDjangoPermission]
    required_permissions = "warehouse.can_gate_bst"

    def get(self, request):
        svc = _service(request)
        # A gate view: the date range filters the history by gate-out date, while
        # the awaiting queue always stays visible (see the queryset docstring).
        qs = svc.gate_outwards_view_queryset(
            from_date=request.query_params.get("from_date") or None,
            to_date=request.query_params.get("to_date") or None,
        )
        return Response(BSTTransferListSerializer(qs, many=True).data)


class BSTGateInwardsListView(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, HasRequiredDjangoPermission]
    required_permissions = "warehouse.can_gate_bst"

    def get(self, request):
        svc = _service(request)
        return Response(BSTTransferListSerializer(svc.gate_inwards_queryset(), many=True).data)


class BSTGateMarkOutView(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, HasRequiredDjangoPermission]
    required_permissions = "warehouse.can_gate_bst"

    def post(self, request, transfer_id):
        svc = _service(request)
        try:
            transfer = svc.get_outward_transfer(transfer_id)
            svc.mark_gate_out(transfer)
        except BSTError as exc:
            return _bst_error(exc)
        transfer = svc.get_outward_transfer(transfer_id)
        return Response(BSTTransferDetailSerializer(transfer).data)


class BSTGateMarkInView(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, HasRequiredDjangoPermission]
    required_permissions = "warehouse.can_gate_bst"

    def post(self, request, transfer_id):
        svc = _service(request)
        try:
            transfer = svc.get_incoming_transfer(transfer_id)
            svc.mark_gate_in(transfer)
        except BSTError as exc:
            return _bst_error(exc)
        transfer = svc.get_incoming_transfer(transfer_id)
        return Response(BSTTransferDetailSerializer(transfer).data)
