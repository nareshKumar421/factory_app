import logging
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated

from company.permissions import HasCompanyContext
from .services.warehouse_service import WarehouseService
from .serializers import (
    BOMRequestCreateSerializer, BOMRequestListSerializer,
    BOMRequestDetailSerializer, BOMRequestApproveSerializer,
    BOMRequestRejectSerializer, MaterialIssueSerializer,
    FGReceiptCreateSerializer, FGReceiptListSerializer,
    FGReceiptDetailSerializer, StockCheckSerializer,
)

logger = logging.getLogger(__name__)


def _get_service(request) -> WarehouseService:
    company_code = request.company.company.code
    return WarehouseService(company_code=company_code)


# ===========================================================================
# BOM Request — Create (Production team calls this)
# ===========================================================================

class BOMRequestCreateAPI(APIView):
    """Production team submits a BOM request to warehouse."""
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def post(self, request):
        serializer = BOMRequestCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            svc = _get_service(request)
            bom_request = svc.create_bom_request(serializer.validated_data, request.user)
            return Response(
                BOMRequestDetailSerializer(bom_request).data,
                status=status.HTTP_201_CREATED,
            )
        except ValueError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)


# ===========================================================================
# BOM Request — List & Detail (Warehouse team views)
# ===========================================================================

class BOMRequestListAPI(APIView):
    """List BOM requests — filterable by status, production_run_id."""
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def get(self, request):
        svc = _get_service(request)
        qs = svc.list_bom_requests(
            status=request.query_params.get('status'),
            production_run_id=request.query_params.get('production_run_id'),
        )
        return Response(BOMRequestListSerializer(qs, many=True).data)


class BOMRequestDetailAPI(APIView):
    """Get BOM request detail with all lines and stock info."""
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def get(self, request, request_id):
        try:
            svc = _get_service(request)
            bom_request = svc.get_bom_request(request_id)

            # Enrich lines with current stock
            item_codes = list(bom_request.lines.values_list('item_code', flat=True))
            stock_map = svc.get_stock_for_items(item_codes)

            # Update available_stock on each line (in-memory, not saved)
            data = BOMRequestDetailSerializer(bom_request).data
            for line_data in data.get('lines', []):
                code = line_data['item_code']
                stock_info = stock_map.get(code, {})
                line_data['available_stock'] = stock_info.get('total_on_hand', 0)
                line_data['available_qty'] = stock_info.get('total_available', 0)
                line_data['stock_warehouses'] = stock_info.get('warehouses', [])

            return Response(data)
        except ValueError as e:
            return Response({'error': str(e)}, status=status.HTTP_404_NOT_FOUND)


# ===========================================================================
# BOM Request — Approve / Reject (Warehouse action)
# ===========================================================================

class BOMRequestApproveAPI(APIView):
    """Warehouse approves BOM request with line-level decisions."""
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def post(self, request, request_id):
        serializer = BOMRequestApproveSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            svc = _get_service(request)
            bom_request = svc.approve_bom_request(
                request_id, serializer.validated_data, request.user
            )
            return Response(BOMRequestDetailSerializer(bom_request).data)
        except ValueError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)


class BOMRequestRejectAPI(APIView):
    """Warehouse rejects entire BOM request."""
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def post(self, request, request_id):
        serializer = BOMRequestRejectSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            svc = _get_service(request)
            bom_request = svc.reject_bom_request(
                request_id, serializer.validated_data['reason'], request.user
            )
            return Response(BOMRequestDetailSerializer(bom_request).data)
        except ValueError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)


# ===========================================================================
# Material Issue — Issue approved materials to SAP
# ===========================================================================

class MaterialIssueAPI(APIView):
    """Issue approved materials to SAP (creates InventoryGenExits)."""
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def post(self, request, request_id):
        serializer = MaterialIssueSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            svc = _get_service(request)
            bom_request = svc.issue_materials_to_sap(
                request_id, serializer.validated_data, request.user
            )
            return Response(BOMRequestDetailSerializer(bom_request).data)
        except ValueError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)


# ===========================================================================
# Stock Check — Query available stock for items
# ===========================================================================

class StockCheckAPI(APIView):
    """Check available stock for a list of item codes."""
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def post(self, request):
        serializer = StockCheckSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        svc = _get_service(request)
        stock = svc.get_stock_for_items(serializer.validated_data['item_codes'])
        return Response(stock)


# ===========================================================================
# Finished Goods Receipt — Create, List, Detail, Receive, Post to SAP
# ===========================================================================

class FGReceiptCreateAPI(APIView):
    """Create a finished goods receipt for a completed production run."""
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def post(self, request):
        serializer = FGReceiptCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            svc = _get_service(request)
            receipt = svc.create_fg_receipt(serializer.validated_data, request.user)
            return Response(
                FGReceiptDetailSerializer(receipt).data,
                status=status.HTTP_201_CREATED,
            )
        except ValueError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)


class FGReceiptListAPI(APIView):
    """List finished goods receipts — filterable by status, production_run_id."""
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def get(self, request):
        svc = _get_service(request)
        qs = svc.list_fg_receipts(
            status=request.query_params.get('status'),
            production_run_id=request.query_params.get('production_run_id'),
        )
        return Response(FGReceiptListSerializer(qs, many=True).data)


class FGReceiptDetailAPI(APIView):
    """Get FG receipt detail."""
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def get(self, request, receipt_id):
        try:
            svc = _get_service(request)
            receipt = svc.get_fg_receipt(receipt_id)
            return Response(FGReceiptDetailSerializer(receipt).data)
        except ValueError as e:
            return Response({'error': str(e)}, status=status.HTTP_404_NOT_FOUND)


class FGReceiptReceiveAPI(APIView):
    """Warehouse confirms receipt of finished goods."""
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def post(self, request, receipt_id):
        try:
            svc = _get_service(request)
            receipt = svc.receive_finished_goods(receipt_id, request.user)
            return Response(FGReceiptDetailSerializer(receipt).data)
        except ValueError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)


class FGReceiptPostToSAPAPI(APIView):
    """Post received FG to SAP (creates InventoryGenEntries)."""
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def post(self, request, receipt_id):
        try:
            svc = _get_service(request)
            receipt = svc.post_fg_receipt_to_sap(receipt_id)
            return Response(FGReceiptDetailSerializer(receipt).data)
        except ValueError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
