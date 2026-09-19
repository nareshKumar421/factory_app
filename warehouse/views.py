import logging
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated

from company.permissions import HasCompanyContext
from .permissions import (
    CanViewBOMRequest, CanCreateBOMRequest, CanApproveBOMRequest, CanIssueMaterials,
    CanViewFGReceipt, CanCreateFGReceipt, CanReceiveFG, CanPostFGToSAP,
)
from .models import BOMMaterialKind
from .services.warehouse_service import WarehouseService
from .serializers import (
    BOMRequestCreateSerializer, BOMRequestListSerializer,
    BOMRequestDetailSerializer, BOMRequestApproveSerializer,
    BOMRequestRejectSerializer, BOMRequestReRequestSerializer,
    MaterialIssueSerializer,
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
    permission_classes = [IsAuthenticated, HasCompanyContext, CanCreateBOMRequest]

    def post(self, request):
        serializer = BOMRequestCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            svc = _get_service(request)
            requests_raised = svc.create_bom_request(
                serializer.validated_data, request.user)
            if not requests_raised:
                # Nothing needed approving — no raw material on the bill and all
                # packing material already at BH-PC. The run is marked
                # NOT_REQUIRED and can start, so this is a success, not a 400.
                return Response(
                    {
                        'detail': 'No warehouse approval needed for this run — there '
                                  'is no raw material on the bill and all packing '
                                  'material is already at BH-PC.',
                        'approval_required': False,
                        'warehouse_approval_status': 'NOT_REQUIRED',
                        'requests': [],
                    },
                    status=status.HTTP_200_OK,
                )
            # A run raises one request per half of the bill — raw material and
            # packing material are settled separately — so the response is a
            # list. `bom_request` stays as the first one for older clients that
            # read a single object.
            payload = [
                BOMRequestDetailSerializer(req).data for req in requests_raised
            ]
            return Response(
                {
                    'approval_required': True,
                    'requests': payload,
                    **payload[0],
                },
                status=status.HTTP_201_CREATED,
            )
        except ValueError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)


# ===========================================================================
# BOM Request — List & Detail (Warehouse team views)
# ===========================================================================

class BOMRequestListAPI(APIView):
    """List BOM requests — filterable by status, production_run_id."""
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewBOMRequest]

    def get(self, request):
        svc = _get_service(request)
        qs = svc.list_bom_requests(
            status=request.query_params.get('status'),
            production_run_id=request.query_params.get('production_run_id'),
        )
        return Response(BOMRequestListSerializer(qs, many=True).data)


class BOMRequestDetailAPI(APIView):
    """Get BOM request detail with all lines and stock info."""
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewBOMRequest]

    def get(self, request, request_id):
        try:
            svc = _get_service(request)
            bom_request = svc.get_bom_request(request_id)

            # Enrich lines with current stock — from whichever register the
            # approval gate will check against (RM register for a raw-material
            # request, SAP for packing). Showing SAP for both used to offer a
            # quantity the approval then refused.
            stock_map = svc.get_stock_for_bom_request(bom_request)
            default_source = (
                'RM_REGISTER'
                if bom_request.material_kind == BOMMaterialKind.RAW
                else 'SAP'
            )

            # Where each line's quantity can actually be drawn from: every
            # godown holding the item bar the one production already consumes
            # it from, less what other live approvals hold. The screen shows
            # the same figure the approval gate enforces, so a quantity the
            # approver is offered is one the store can hand over.
            source_map = svc.source_options_for_request(bom_request)

            # Update available_stock on each line (in-memory, not saved)
            data = BOMRequestDetailSerializer(bom_request).data
            for line_data in data.get('lines', []):
                code = (line_data['item_code'] or '').strip().upper()
                stock_info = stock_map.get(code, {})
                line_sources = source_map.get(line_data['id'], {})
                line_data['available_stock'] = float(
                    line_sources.get('total_available', 0) or 0
                )
                line_data['available_qty'] = stock_info.get('total_available', 0)
                line_data['stock_warehouses'] = stock_info.get('warehouses', [])
                line_data['consumption_warehouse'] = line_sources.get(
                    'consumption_warehouse', ''
                )
                line_data['at_consumption'] = float(
                    line_sources.get('at_consumption', 0) or 0
                )
                line_data['source_options'] = [
                    {
                        'warehouse': opt['warehouse'],
                        'on_hand': float(opt['on_hand']),
                        'claimed': float(opt['claimed']),
                        'available': float(opt['available']),
                    }
                    for opt in line_sources.get('options', [])
                ]
                # An item with no stock at all is still told which register
                # was asked, so the screen never labels the column differently
                # row by row.
                line_data['stock_source'] = stock_info.get('source', default_source)

            return Response(data)
        except ValueError as e:
            return Response({'error': str(e)}, status=status.HTTP_404_NOT_FOUND)


# ===========================================================================
# BOM Request — Approve / Reject (Warehouse action)
# ===========================================================================

class BOMRequestApproveAPI(APIView):
    """Warehouse approves BOM request with line-level decisions."""
    permission_classes = [IsAuthenticated, HasCompanyContext, CanApproveBOMRequest]

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
    permission_classes = [IsAuthenticated, HasCompanyContext, CanApproveBOMRequest]

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


class BOMRequestReRequestAPI(APIView):
    """Production re-requests the un-approved remainder of a partial/rejected request."""
    permission_classes = [IsAuthenticated, HasCompanyContext, CanCreateBOMRequest]

    def post(self, request, request_id):
        serializer = BOMRequestReRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            svc = _get_service(request)
            bom_request = svc.re_request_bom_shortfall(
                request_id, request.user,
                remarks=serializer.validated_data.get('remarks', ''),
            )
            return Response(
                BOMRequestDetailSerializer(bom_request).data,
                status=status.HTTP_201_CREATED,
            )
        except ValueError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)


# ===========================================================================
# Material Issue — Issue approved materials to SAP
# ===========================================================================

class MaterialIssueAPI(APIView):
    """Issue approved materials to SAP (creates InventoryGenExits)."""
    permission_classes = [IsAuthenticated, HasCompanyContext, CanIssueMaterials]

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
    permission_classes = [IsAuthenticated, HasCompanyContext, CanCreateFGReceipt]

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
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewFGReceipt]

    def get(self, request):
        svc = _get_service(request)
        qs = svc.list_fg_receipts(
            status=request.query_params.get('status'),
            production_run_id=request.query_params.get('production_run_id'),
        )
        return Response(FGReceiptListSerializer(qs, many=True).data)


class FGReceiptDetailAPI(APIView):
    """Get FG receipt detail."""
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewFGReceipt]

    def get(self, request, receipt_id):
        try:
            svc = _get_service(request)
            receipt = svc.get_fg_receipt(receipt_id)
            return Response(FGReceiptDetailSerializer(receipt).data)
        except ValueError as e:
            return Response({'error': str(e)}, status=status.HTTP_404_NOT_FOUND)


class FGReceiptReceiveAPI(APIView):
    """Warehouse confirms receipt of finished goods."""
    permission_classes = [IsAuthenticated, HasCompanyContext, CanReceiveFG]

    def post(self, request, receipt_id):
        try:
            svc = _get_service(request)
            receipt = svc.receive_finished_goods(receipt_id, request.user)
            return Response(FGReceiptDetailSerializer(receipt).data)
        except ValueError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)


class FGReceiptPostToSAPAPI(APIView):
    """Post received FG to SAP (creates InventoryGenEntries)."""
    permission_classes = [IsAuthenticated, HasCompanyContext, CanPostFGToSAP]

    def post(self, request, receipt_id):
        try:
            svc = _get_service(request)
            receipt = svc.post_fg_receipt_to_sap(receipt_id)
            return Response(FGReceiptDetailSerializer(receipt).data)
        except ValueError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
