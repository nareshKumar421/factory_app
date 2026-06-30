"""API views for the Branch Stock Transfer (BST) sender flow."""

import logging

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from company.permissions import HasCompanyContext
from sap_client.exceptions import SAPConnectionError, SAPDataError

from .serializers_bst import (
    BSTBoxScanBatchSerializer,
    BSTBoxScanCreateSerializer,
    BSTBoxScanSerializer,
    BSTReceiveScanSerializer,
    BSTTransferCancelSerializer,
    BSTTransferCreateSerializer,
    BSTTransferDetailSerializer,
    BSTTransferListSerializer,
    BSTTransferUpdateSerializer,
    SAPStockTransferSerializer,
)
from .services.bst_service import BSTError, BSTService

logger = logging.getLogger(__name__)


def _service(request) -> BSTService:
    return BSTService(request.company.company.code, request.user)


def _apply_date_filter(qs, request):
    """Filter a transfer queryset by the global ?from_date/&to_date range."""
    from_date = request.query_params.get("from_date")
    to_date = request.query_params.get("to_date")
    if from_date:
        qs = qs.filter(created_at__date__gte=from_date)
    if to_date:
        qs = qs.filter(created_at__date__lte=to_date)
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
            transfers = svc.list_sap_transfers(
                search=request.query_params.get("search"),
                from_date=request.query_params.get("from_date") or None,
                to_date=request.query_params.get("to_date") or None,
                limit=int(request.query_params.get("limit") or 50),
            )
        except (SAPConnectionError, SAPDataError) as exc:
            return _sap_error(exc)
        return Response(SAPStockTransferSerializer(transfers, many=True).data)


class BSTSAPTransferDetailView(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def get(self, request, doc_entry):
        svc = _service(request)
        try:
            transfer = svc.get_sap_transfer(doc_entry)
        except BSTError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_404_NOT_FOUND)
        except (SAPConnectionError, SAPDataError) as exc:
            return _sap_error(exc)
        return Response(SAPStockTransferSerializer(transfer).data)


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
        try:
            transfer = svc.get_transfer(transfer_id)
            result = svc.scan(transfer, serializer.validated_data["barcode_raw"])
        except BSTError as exc:
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
# Receiver side (current company == destination)
# ---------------------------------------------------------------------------

class BSTIncomingListView(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def get(self, request):
        svc = _service(request)
        qs = _apply_date_filter(svc.incoming_queryset(), request)
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
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def get(self, request):
        svc = _service(request)
        qs = _apply_date_filter(svc.gate_outwards_queryset(), request)
        return Response(BSTTransferListSerializer(qs, many=True).data)


class BSTGateInwardsListView(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def get(self, request):
        svc = _service(request)
        return Response(BSTTransferListSerializer(svc.gate_inwards_queryset(), many=True).data)


class BSTGateMarkOutView(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext]

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
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def post(self, request, transfer_id):
        svc = _service(request)
        try:
            transfer = svc.get_incoming_transfer(transfer_id)
            svc.mark_gate_in(transfer)
        except BSTError as exc:
            return _bst_error(exc)
        transfer = svc.get_incoming_transfer(transfer_id)
        return Response(BSTTransferDetailSerializer(transfer).data)
