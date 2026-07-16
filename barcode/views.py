import logging
import csv
from django.db import IntegrityError
from django.db.models import Q
from django.http import HttpResponse
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated

from company.permissions import HasCompanyContext
from .permissions import HasAnyBarcodePermission, CanAccessBarcodePalletSync
from .services.barcode_service import BarcodeService
from .services.label_service import LabelService
from .services.scan_service import ScanService
from .services.dispatch_service import BarcodeDispatchService, DispatchValidationError
from .services.intercompany_transfer_service import (
    IntercompanyTransferError,
    IntercompanyTransferService,
)
from .services.production_integration_service import ProductionBarcodeIntegration
from .services.production_release_service import (
    ProductionReleaseOilService,
    ProductionReleaseReadError,
)
from .services.oitm_item_service import OitmItemReadError, OitmItemService
from .services.verify_request_service import PalletVerifyRequestService
from .serializers import (
    BoxGenerateSerializer, BoxListSerializer, BoxDetailSerializer,
    PalletCreateSerializer, PalletListSerializer, PalletDetailSerializer,
    VoidSerializer, PalletVoidSerializer, VoidedPalletSerializer, VoidedBoxSerializer,
    PrintRequestSerializer, PalletPrintWorkflowSerializer, BulkPrintRequestSerializer,
    LabelPrintLogSerializer,
    PalletMoveSerializer, PalletClearSerializer, PalletSplitSerializer,
    PalletAddBoxesSerializer, PalletRemoveBoxesSerializer, PalletReconcileSerializer,
    PalletVerifyRequestListSerializer, PalletVerifyRequestDetailSerializer,
    PalletVerifyRequestCreateSerializer, PalletVerifyRequestResolveSerializer,
    PalletVerifyRequestCancelSerializer,
    BoxTransferSerializer,
    DismantlePalletSerializer, DismantleBoxSerializer, RepackSerializer,
    LooseStockListSerializer, LooseStockDetailSerializer,
    ScanRequestSerializer, ScanLogSerializer,
    DispatchBillLookupSerializer, DispatchSessionCreateSerializer,
    DispatchScanSubmitSerializer, DispatchScannedBoxQtySerializer, DispatchCancelSerializer,
    DispatchSessionSerializer, DispatchScanLogSerializer,
    DispatchSapSyncLogSerializer, DispatchSettingsSerializer,
    PalletBoxHistorySerializer,
    BarcodeAuditLogSerializer, IntercompanyBarcodeScanSerializer,
    IntercompanyTransferCreateSerializer, IntercompanyTransferReverseSerializer,
    IntercompanyTransferSerializer,
    ProductionLabelsSerializer, ProductionPalletSerializer,
)
from .models import (
    BarcodeAuditLog, IntercompanyTransfer,
    PalletMovement, BoxMovement, PalletMovementType, BoxMovementType,
)

logger = logging.getLogger(__name__)

DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 100


def _get_service(request) -> BarcodeService:
    company_code = request.company.company.code
    return BarcodeService(company_code=company_code)


def _get_scan_service(request) -> ScanService:
    company_code = request.company.company.code
    return ScanService(company_code=company_code)


def _get_label_service(request) -> LabelService:
    company_code = request.company.company.code
    return LabelService(company_code=company_code)


def _get_dispatch_service(request) -> BarcodeDispatchService:
    company_code = request.company.company.code
    return BarcodeDispatchService(company_code=company_code)


def _get_verify_request_service(request) -> PalletVerifyRequestService:
    company_code = request.company.company.code
    return PalletVerifyRequestService(company_code=company_code)


def _is_barcode_team(request) -> bool:
    """Barcode team = users with access to the barcode module (view_pallet)."""
    return request.user.has_perm('barcode.view_pallet')


def _parse_positive_int(value, default):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _paginated_response(request, qs, serializer_class):
    page = _parse_positive_int(request.query_params.get('page'), 1)
    page_size = min(
        _parse_positive_int(request.query_params.get('page_size'), DEFAULT_PAGE_SIZE),
        MAX_PAGE_SIZE,
    )
    total_count = qs.count()
    total_pages = max((total_count + page_size - 1) // page_size, 1)
    page = min(page, total_pages)
    start = (page - 1) * page_size
    end = start + page_size

    return Response(
        {
            'results': serializer_class(qs[start:end], many=True).data,
            'count': total_count,
            'page': page,
            'page_size': page_size,
            'total_pages': total_pages,
            'next': page < total_pages,
            'previous': page > 1,
        }
    )


def _list_response(request, qs, serializer_class):
    if 'page' in request.query_params or 'page_size' in request.query_params:
        return _paginated_response(request, qs, serializer_class)
    return Response(serializer_class(qs[:500], many=True).data)


def _is_unique_integrity_error(exc: IntegrityError, field_name: str) -> bool:
    message = str(exc).lower()
    return field_name.lower() in message and (
        'duplicate' in message or 'unique' in message
    )


def _dispatch_error_response(exc: DispatchValidationError):
    return Response(
        {'code': exc.code, 'error': exc.message},
        status=getattr(exc, 'status_code', status.HTTP_400_BAD_REQUEST),
    )


def _report_response(request, rows, filename):
    export_format = (request.query_params.get('export') or '').lower()
    if export_format not in {'csv', 'excel'}:
        return Response(rows)

    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename="{filename}.csv"'
    writer = csv.writer(response)
    if not rows:
        return response
    headers = list(rows[0].keys())
    writer.writerow(headers)
    for row in rows:
        writer.writerow([row.get(header, '') for header in headers])
    return response


# ===========================================================================
# Box — Generate
# ===========================================================================

class BoxGenerateAPI(APIView):
    """Bulk-generate box barcode records."""
    permission_classes = [IsAuthenticated, HasCompanyContext, HasAnyBarcodePermission]

    def post(self, request):
        serializer = BoxGenerateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            svc = _get_service(request)
            boxes = svc.generate_boxes(serializer.validated_data, request.user)
            return Response(
                BoxListSerializer(boxes, many=True).data,
                status=status.HTTP_201_CREATED,
            )
        except ValueError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except IntegrityError as e:
            logger.error(f"Barcode generation integrity error: {e}")
            if _is_unique_integrity_error(e, 'box_barcode'):
                return Response(
                    {'error': 'Duplicate barcode detected. Please try again.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            return Response(
                {'error': 'Unable to generate barcode labels because of a database constraint.'},
                status=status.HTTP_400_BAD_REQUEST,
            )


# ===========================================================================
# Box — List & Detail
# ===========================================================================

class BoxListAPI(APIView):
    """List boxes with optional filters."""
    permission_classes = [IsAuthenticated, HasCompanyContext, HasAnyBarcodePermission]

    def get(self, request):
        svc = _get_service(request)
        qs = svc.list_boxes(
            status=request.query_params.get('status'),
            item_code=request.query_params.get('item_code'),
            batch_number=request.query_params.get('batch_number'),
            warehouse=request.query_params.get('warehouse'),
            pallet_id=request.query_params.get('pallet_id'),
            unpalletized=request.query_params.get('unpalletized'),
            search=request.query_params.get('search'),
        )
        return _list_response(request, qs, BoxListSerializer)


class BoxDetailAPI(APIView):
    """Get box detail with movement history."""
    permission_classes = [IsAuthenticated, HasCompanyContext, HasAnyBarcodePermission]

    def get(self, request, box_id):
        try:
            svc = _get_service(request)
            box = svc.get_box(box_id)
            return Response(BoxDetailSerializer(box).data)
        except ValueError as e:
            return Response({'error': str(e)}, status=status.HTTP_404_NOT_FOUND)


# ===========================================================================
# Box — Void
# ===========================================================================

class BoxVoidAPI(APIView):
    """Void a box (damaged, lost)."""
    permission_classes = [IsAuthenticated, HasCompanyContext, HasAnyBarcodePermission]

    def post(self, request, box_id):
        serializer = VoidSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            svc = _get_service(request)
            box = svc.void_box(
                box_id, serializer.validated_data.get('reason', ''), request.user
            )
            return Response(BoxDetailSerializer(box).data)
        except ValueError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)


# ===========================================================================
# Pallet — Create
# ===========================================================================

class PalletCreateAPI(APIView):
    """Create a generic empty pallet."""
    permission_classes = [IsAuthenticated, HasCompanyContext, HasAnyBarcodePermission]

    def post(self, request):
        serializer = PalletCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            svc = _get_service(request)
            pallet = svc.create_pallet(serializer.validated_data, request.user)
            return Response(
                PalletDetailSerializer(pallet).data,
                status=status.HTTP_201_CREATED,
            )
        except ValueError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except IntegrityError as e:
            logger.error(f"Pallet creation integrity error: {e}")
            if _is_unique_integrity_error(e, 'pallet_id'):
                return Response(
                    {'error': 'Duplicate pallet ID detected. Please try again.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            return Response(
                {'error': 'Unable to create pallet because of a database constraint.'},
                status=status.HTTP_400_BAD_REQUEST,
            )


# ===========================================================================
# Pallet — List & Detail
# ===========================================================================

class PalletListAPI(APIView):
    """List pallets with optional filters."""
    # Also called by the Warehouse Ops pallet-move sync (useSyncPalletToBarcode).
    permission_classes = [IsAuthenticated, HasCompanyContext, CanAccessBarcodePalletSync]

    def get(self, request):
        svc = _get_service(request)
        qs = svc.list_pallets(
            status=request.query_params.get('status'),
            item_code=request.query_params.get('item_code'),
            batch_number=request.query_params.get('batch_number'),
            warehouse=request.query_params.get('warehouse'),
            search=request.query_params.get('search'),
        )
        return _list_response(request, qs, PalletListSerializer)


class PalletDetailAPI(APIView):
    """Get or delete a pallet. Delete is allowed only for empty pallets."""
    permission_classes = [IsAuthenticated, HasCompanyContext, HasAnyBarcodePermission]

    def get(self, request, pallet_id):
        try:
            svc = _get_service(request)
            pallet = svc.get_pallet(pallet_id)
            return Response(PalletDetailSerializer(pallet).data)
        except ValueError as e:
            return Response({'error': str(e)}, status=status.HTTP_404_NOT_FOUND)

    def delete(self, request, pallet_id):
        try:
            svc = _get_service(request)
            pallet_code = svc.delete_empty_pallet(pallet_id)
            return Response(
                {'message': f'Empty pallet {pallet_code} deleted.'},
                status=status.HTTP_200_OK,
            )
        except ValueError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)


# ===========================================================================
# Pallet — Void
# ===========================================================================

class PalletVoidAPI(APIView):
    """Void a pallet and disassociate its boxes."""
    permission_classes = [IsAuthenticated, HasCompanyContext, HasAnyBarcodePermission]

    def post(self, request, pallet_id):
        serializer = PalletVoidSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            svc = _get_service(request)
            pallet = svc.void_pallet(
                pallet_id,
                serializer.validated_data.get('reason', ''),
                request.user,
                box_ids=serializer.validated_data.get('box_ids'),
            )
            return Response(PalletDetailSerializer(pallet).data)
        except ValueError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)


# ===========================================================================
# Voided items — traceability list
# ===========================================================================

class VoidedPalletsAPI(APIView):
    """List voided pallets (who/when/reason), from VOID pallet movements."""
    permission_classes = [IsAuthenticated, HasCompanyContext, HasAnyBarcodePermission]

    def get(self, request):
        svc = _get_service(request)
        qs = PalletMovement.objects.filter(
            company=svc.company, movement_type=PalletMovementType.VOID,
        ).select_related('pallet', 'performed_by')
        search = (request.query_params.get('search') or '').strip()
        if search:
            qs = qs.filter(
                Q(pallet__pallet_id__icontains=search)
                | Q(pallet__item_code__icontains=search)
                | Q(pallet__item_name__icontains=search)
                | Q(pallet__batch_number__icontains=search)
            )
        return _list_response(request, qs, VoidedPalletSerializer)


class VoidedBoxesAPI(APIView):
    """List voided boxes (who/when/reason), from VOID box movements."""
    permission_classes = [IsAuthenticated, HasCompanyContext, HasAnyBarcodePermission]

    def get(self, request):
        svc = _get_service(request)
        qs = BoxMovement.objects.filter(
            company=svc.company, movement_type=BoxMovementType.VOID,
        ).select_related('box', 'from_pallet', 'performed_by')
        search = (request.query_params.get('search') or '').strip()
        if search:
            qs = qs.filter(
                Q(box__box_barcode__icontains=search)
                | Q(box__item_code__icontains=search)
                | Q(box__item_name__icontains=search)
                | Q(box__batch_number__icontains=search)
            )
        return _list_response(request, qs, VoidedBoxSerializer)


# ===========================================================================
# Pallet — Move
# ===========================================================================

class PalletMoveAPI(APIView):
    """Move pallet to a different warehouse."""
    # Also called by the Warehouse Ops pallet-move sync (useSyncPalletToBarcode).
    permission_classes = [IsAuthenticated, HasCompanyContext, CanAccessBarcodePalletSync]

    def post(self, request, pallet_id):
        serializer = PalletMoveSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            svc = _get_service(request)
            pallet = svc.move_pallet(
                pallet_id,
                to_warehouse=serializer.validated_data['to_warehouse'],
                to_bin=serializer.validated_data.get('to_bin', ''),
                notes=serializer.validated_data.get('notes', ''),
                user=request.user,
            )
            return Response(PalletDetailSerializer(pallet).data)
        except ValueError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)


# ===========================================================================
# Pallet — Clear
# ===========================================================================

class PalletClearAPI(APIView):
    """Clear pallet — remove all boxes."""
    permission_classes = [IsAuthenticated, HasCompanyContext, HasAnyBarcodePermission]

    def post(self, request, pallet_id):
        serializer = PalletClearSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            svc = _get_service(request)
            pallet = svc.clear_pallet(
                pallet_id,
                notes=serializer.validated_data.get('notes', ''),
                user=request.user,
            )
            return Response(PalletDetailSerializer(pallet).data)
        except ValueError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)


# ===========================================================================
# Pallet — Split
# ===========================================================================

class PalletSplitAPI(APIView):
    """Split selected boxes into an existing empty pallet."""
    permission_classes = [IsAuthenticated, HasCompanyContext, HasAnyBarcodePermission]

    def post(self, request, pallet_id):
        serializer = PalletSplitSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            svc = _get_service(request)
            target_pallet = svc.split_pallet(
                pallet_id,
                box_ids=serializer.validated_data['box_ids'],
                target_pallet_id=serializer.validated_data['target_pallet_id'],
                user=request.user,
            )
            return Response(PalletDetailSerializer(target_pallet).data, status=status.HTTP_200_OK)
        except ValueError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)


# ===========================================================================
# Pallet — Add / Remove Boxes
# ===========================================================================

class PalletAddBoxesAPI(APIView):
    """Add unpalletized boxes to an existing pallet."""
    permission_classes = [IsAuthenticated, HasCompanyContext, HasAnyBarcodePermission]

    def post(self, request, pallet_id):
        serializer = PalletAddBoxesSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            svc = _get_service(request)
            pallet = svc.add_boxes_to_pallet(
                pallet_id,
                box_ids=serializer.validated_data['box_ids'],
                user=request.user,
            )
            return Response(PalletDetailSerializer(pallet).data)
        except ValueError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)


class PalletRemoveBoxesAPI(APIView):
    """Remove specific boxes from a pallet."""
    permission_classes = [IsAuthenticated, HasCompanyContext, HasAnyBarcodePermission]

    def post(self, request, pallet_id):
        serializer = PalletRemoveBoxesSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            svc = _get_service(request)
            pallet = svc.remove_boxes_from_pallet(
                pallet_id,
                box_ids=serializer.validated_data['box_ids'],
                user=request.user,
            )
            return Response(PalletDetailSerializer(pallet).data)
        except ValueError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)


class PalletReconcileAPI(APIView):
    """Verify a pallet against physically-scanned boxes (read-only reconciliation)."""
    permission_classes = [IsAuthenticated, HasCompanyContext, HasAnyBarcodePermission]

    def post(self, request, pallet_id):
        serializer = PalletReconcileSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            svc = _get_service(request)
            result = svc.reconcile_pallet(
                pallet_id,
                scanned_barcodes=data['scanned_barcodes'],
                unlabeled_count=data['unlabeled_count'],
                apply=data['apply'],
                pull_foreign=data['pull_foreign'],
                drop_missing=data['drop_missing'],
                reason=data['reason'],
                user=request.user,
            )
            return Response(result)
        except ValueError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)


# ===========================================================================
# Pallet Verify Requests (ticket workflow)
# ===========================================================================

class PalletVerifyRequestListCreateAPI(APIView):
    """List verify requests (team sees all, requesters see own) and raise new ones."""
    permission_classes = [IsAuthenticated, HasCompanyContext, HasAnyBarcodePermission]

    def get(self, request):
        svc = _get_verify_request_service(request)
        qs = svc.list_requests(
            user=request.user,
            is_team=_is_barcode_team(request),
            status=request.query_params.get('status'),
            pallet_id=request.query_params.get('pallet_id'),
        )
        return _list_response(request, qs, PalletVerifyRequestListSerializer)

    def post(self, request):
        serializer = PalletVerifyRequestCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            svc = _get_verify_request_service(request)
            req = svc.create_request(
                data['pallet_id'],
                reason=data['reason'],
                source=data['source'],
                source_reference=data['source_reference'],
                scanned_barcodes=data['scanned_barcodes'],
                unlabeled_count=data['unlabeled_count'],
                user=request.user,
            )
            return Response(
                PalletVerifyRequestDetailSerializer(req).data,
                status=status.HTTP_201_CREATED,
            )
        except ValueError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)


class PalletVerifyRequestDetailAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, HasAnyBarcodePermission]

    def get(self, request, request_id):
        try:
            svc = _get_verify_request_service(request)
            req = svc.get_request(request_id)
        except ValueError as e:
            return Response({'error': str(e)}, status=status.HTTP_404_NOT_FOUND)
        if not _is_barcode_team(request) and req.requested_by_id != request.user.id:
            return Response({'error': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        return Response(PalletVerifyRequestDetailSerializer(req).data)


class PalletVerifyRequestStartAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, HasAnyBarcodePermission]

    def post(self, request, request_id):
        if not _is_barcode_team(request):
            return Response(
                {'error': 'Only the barcode team can update this request.'},
                status=status.HTTP_403_FORBIDDEN,
            )
        try:
            svc = _get_verify_request_service(request)
            req = svc.mark_in_progress(request_id, request.user)
            return Response(PalletVerifyRequestDetailSerializer(req).data)
        except ValueError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)


class PalletVerifyRequestResolveAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, HasAnyBarcodePermission]

    def post(self, request, request_id):
        if not _is_barcode_team(request):
            return Response(
                {'error': 'Only the barcode team can resolve requests.'},
                status=status.HTTP_403_FORBIDDEN,
            )
        serializer = PalletVerifyRequestResolveSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            svc = _get_verify_request_service(request)
            req = svc.resolve_request(
                request_id, serializer.validated_data['resolution_note'], request.user,
            )
            return Response(PalletVerifyRequestDetailSerializer(req).data)
        except ValueError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)


class PalletVerifyRequestCancelAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, HasAnyBarcodePermission]

    def post(self, request, request_id):
        serializer = PalletVerifyRequestCancelSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            svc = _get_verify_request_service(request)
            req = svc.get_request(request_id)
        except ValueError as e:
            return Response({'error': str(e)}, status=status.HTTP_404_NOT_FOUND)
        # Team can cancel anything; a requester can cancel only their own ticket.
        if not _is_barcode_team(request) and req.requested_by_id != request.user.id:
            return Response(
                {'error': 'You cannot cancel this request.'},
                status=status.HTTP_403_FORBIDDEN,
            )
        try:
            req = svc.cancel_request(
                request_id, serializer.validated_data['reason'], request.user,
            )
            return Response(PalletVerifyRequestDetailSerializer(req).data)
        except ValueError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)


# ===========================================================================
# Box — Transfer
# ===========================================================================

class BoxTransferAPI(APIView):
    """Transfer boxes between warehouses or to a pallet."""
    permission_classes = [IsAuthenticated, HasCompanyContext, HasAnyBarcodePermission]

    def post(self, request):
        serializer = BoxTransferSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            svc = _get_service(request)
            boxes = svc.transfer_boxes(
                box_ids=serializer.validated_data['box_ids'],
                to_warehouse=serializer.validated_data['to_warehouse'],
                to_bin=serializer.validated_data.get('to_bin', ''),
                to_pallet_id=serializer.validated_data.get('to_pallet_id'),
                user=request.user,
            )
            return Response(BoxListSerializer(boxes, many=True).data)
        except ValueError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)


# ===========================================================================
# Intercompany Barcode Transfer
# ===========================================================================

class IntercompanyTransferDashboardAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, HasAnyBarcodePermission]

    def get(self, request):
        svc = IntercompanyTransferService(request.user)
        data = svc.dashboard(request.company.company)
        recent = (
            IntercompanyTransfer.objects
            .filter(Q(source_company=request.company.company) | Q(destination_company=request.company.company))
            .select_related('source_company', 'destination_company', 'created_by', 'reversed_by')
            .prefetch_related('lines')[:10]
        )
        data['recent_transfers'] = IntercompanyTransferSerializer(recent, many=True).data
        return Response(data)


class IntercompanyTransferListCreateAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, HasAnyBarcodePermission]

    def get(self, request):
        search = request.query_params.get('search', '').strip()
        qs = (
            IntercompanyTransfer.objects
            .filter(Q(source_company=request.company.company) | Q(destination_company=request.company.company))
            .select_related('source_company', 'destination_company', 'created_by', 'reversed_by')
            .prefetch_related('lines')
        )
        if search:
            qs = qs.filter(
                Q(transfer_number__icontains=search)
                | Q(lines__barcode__icontains=search)
                | Q(lines__item_code__icontains=search)
                | Q(lines__batch_number__icontains=search)
            ).distinct()
        return _list_response(request, qs, IntercompanyTransferSerializer)

    def post(self, request):
        serializer = IntercompanyTransferCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            transfer = IntercompanyTransferService(request.user).create_transfer(
                **serializer.validated_data
            )
            return Response(
                IntercompanyTransferSerializer(transfer).data,
                status=status.HTTP_201_CREATED,
            )
        except IntercompanyTransferError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)


class IntercompanyTransferDetailAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, HasAnyBarcodePermission]

    def get(self, request, transfer_id):
        try:
            transfer = (
                IntercompanyTransfer.objects
                .filter(Q(source_company=request.company.company) | Q(destination_company=request.company.company))
                .select_related('source_company', 'destination_company', 'created_by', 'reversed_by')
                .prefetch_related('lines')
                .get(id=transfer_id)
            )
        except IntercompanyTransfer.DoesNotExist:
            return Response({'error': 'Transfer not found.'}, status=status.HTTP_404_NOT_FOUND)
        return Response(IntercompanyTransferSerializer(transfer).data)


class IntercompanyTransferScanAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, HasAnyBarcodePermission]

    def post(self, request):
        serializer = IntercompanyBarcodeScanSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            result = IntercompanyTransferService(request.user).scan_barcode(
                **serializer.validated_data
            )
            return Response(result)
        except IntercompanyTransferError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)


class IntercompanyTransferReverseAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, HasAnyBarcodePermission]

    def post(self, request, transfer_id):
        serializer = IntercompanyTransferReverseSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            transfer = IntercompanyTransferService(request.user).reverse_transfer(
                transfer_id,
                **serializer.validated_data,
            )
            return Response(IntercompanyTransferSerializer(transfer).data)
        except IntercompanyTransfer.DoesNotExist:
            return Response({'error': 'Transfer not found.'}, status=status.HTTP_404_NOT_FOUND)
        except IntercompanyTransferError as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)


class BarcodeTraceabilityAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, HasAnyBarcodePermission]

    def get(self, request):
        search = request.query_params.get('search', '').strip()
        if not search:
            return Response({'error': 'Search value is required.'}, status=status.HTTP_400_BAD_REQUEST)

        logs = list(
            BarcodeAuditLog.objects
            .filter(
                Q(barcode__icontains=search)
                | Q(box__box_barcode__icontains=search)
                | Q(box__batch_number__icontains=search)
                | Q(box__item_code__icontains=search)
                | Q(transfer__transfer_number__icontains=search)
            )
            .select_related('box', 'from_company', 'to_company', 'user', 'transfer')[:100]
        )
        box = logs[0].box if logs and logs[0].box else None
        first_manufacturing_log = logs[-1] if logs else None
        return Response({
            'barcode': box.box_barcode if box else search,
            'current_company': box.company.code if box else '',
            'current_company_name': box.company.name if box else '',
            'manufacturing_company': (
                first_manufacturing_log.to_company.code
                if first_manufacturing_log and first_manufacturing_log.to_company
                else ''
            ),
            'dispatch_status': 'DISPATCHED' if box and box.dispatched_at else 'NOT_DISPATCHED',
            'current_location': box.current_warehouse if box else '',
            'history': BarcodeAuditLogSerializer(logs, many=True).data,
        })


# ===========================================================================
# Print — Box Label
# ===========================================================================

class BoxPrintAPI(APIView):
    """Log print and return box label data for frontend rendering."""
    permission_classes = [IsAuthenticated, HasCompanyContext, HasAnyBarcodePermission]

    def post(self, request, box_id):
        serializer = PrintRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            label_svc = _get_label_service(request)
            label_data = label_svc.get_box_label_data(box_id)
            label_svc.log_print(
                label_type='BOX',
                reference_id=box_id,
                reference_code=label_data['barcode'],
                print_type=serializer.validated_data.get('print_type', 'ORIGINAL'),
                user=request.user,
                reprint_reason=serializer.validated_data.get('reprint_reason', ''),
                printer_name=serializer.validated_data.get('printer_name', ''),
            )
            return Response(label_data)
        except ValueError as e:
            return Response({'error': str(e)}, status=status.HTTP_404_NOT_FOUND)


# ===========================================================================
# Print — Pallet Label
# ===========================================================================

class PalletPrintAPI(APIView):
    """Log print and return pallet label data for frontend rendering."""
    permission_classes = [IsAuthenticated, HasCompanyContext, HasAnyBarcodePermission]

    def post(self, request, pallet_id):
        serializer = PrintRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            label_svc = _get_label_service(request)
            label_data = label_svc.get_pallet_label_data(pallet_id)
            label_svc.log_print(
                label_type='PALLET',
                reference_id=pallet_id,
                reference_code=label_data['barcode'],
                print_type=serializer.validated_data.get('print_type', 'ORIGINAL'),
                user=request.user,
                reprint_reason=serializer.validated_data.get('reprint_reason', ''),
                printer_name=serializer.validated_data.get('printer_name', ''),
            )
            return Response(label_data)
        except ValueError as e:
            return Response({'error': str(e)}, status=status.HTTP_404_NOT_FOUND)


# ===========================================================================
# Print — Bulk
# ===========================================================================

class PalletPrintWorkflowAPI(APIView):
    """Deprecated old pallet print workflow."""
    permission_classes = [IsAuthenticated, HasCompanyContext, HasAnyBarcodePermission]

    def post(self, request, pallet_id):
        return Response(
            {
                'error': (
                    'This print workflow is disabled. Use Pallet QR Print so '
                    'the SAP item is selected before box labels are attached.'
                )
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

class BulkPrintAPI(APIView):
    """Return label data for multiple items at once."""
    permission_classes = [IsAuthenticated, HasCompanyContext, HasAnyBarcodePermission]

    def post(self, request):
        serializer = BulkPrintRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        label_svc = _get_label_service(request)
        items = serializer.validated_data['items']

        results = label_svc.get_bulk_label_data(items)

        # Log each successful print
        for item, result in zip(items, results):
            if 'error' not in result:
                label_svc.log_print(
                    label_type=item.get('label_type', ''),
                    reference_id=item.get('id', 0),
                    reference_code=result.get('barcode', ''),
                    print_type=item.get('print_type', 'ORIGINAL'),
                    user=request.user,
                    reprint_reason=item.get('reprint_reason', ''),
                    printer_name=item.get('printer_name', ''),
                )

        return Response(results)


# ===========================================================================
# Print — History
# ===========================================================================

class PrintHistoryAPI(APIView):
    """Print/reprint audit log."""
    permission_classes = [IsAuthenticated, HasCompanyContext, HasAnyBarcodePermission]

    def get(self, request):
        label_svc = _get_label_service(request)
        qs = label_svc.get_print_history(
            label_type=request.query_params.get('label_type'),
            print_type=request.query_params.get('print_type'),
            reference_code=request.query_params.get('search'),
        )
        return _list_response(request, qs, LabelPrintLogSerializer)


# ===========================================================================
# Dismantle — Pallet
# ===========================================================================

class DismantlePalletAPI(APIView):
    """Dismantle a pallet — remove all or selected boxes."""
    permission_classes = [IsAuthenticated, HasCompanyContext, HasAnyBarcodePermission]

    def post(self, request, pallet_id):
        serializer = DismantlePalletSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            svc = _get_service(request)
            pallet = svc.dismantle_pallet(
                pallet_id,
                box_ids=serializer.validated_data.get('box_ids'),
                reason=serializer.validated_data['reason'],
                reason_notes=serializer.validated_data.get('reason_notes', ''),
                user=request.user,
            )
            return Response(PalletDetailSerializer(pallet).data)
        except ValueError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)


# ===========================================================================
# Dismantle — Box
# ===========================================================================

class DismantleBoxAPI(APIView):
    """Dismantle a box fully or partially into loose stock."""
    permission_classes = [IsAuthenticated, HasCompanyContext, HasAnyBarcodePermission]

    def post(self, request, box_id):
        serializer = DismantleBoxSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            svc = _get_service(request)
            loose = svc.dismantle_box(
                box_id,
                loose_qty=serializer.validated_data.get('qty'),
                reason=serializer.validated_data['reason'],
                reason_notes=serializer.validated_data.get('reason_notes', ''),
                user=request.user,
            )
            return Response(LooseStockDetailSerializer(loose).data, status=status.HTTP_201_CREATED)
        except ValueError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)


# ===========================================================================
# Repack — Loose → New Box
# ===========================================================================

class RepackAPI(APIView):
    """Repack loose stock items into a new box."""
    permission_classes = [IsAuthenticated, HasCompanyContext, HasAnyBarcodePermission]

    def post(self, request):
        serializer = RepackSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            svc = _get_service(request)
            new_box = svc.repack(
                loose_ids=serializer.validated_data['loose_ids'],
                qty_per_loose=serializer.validated_data.get('qty_per_loose'),
                warehouse=serializer.validated_data['warehouse'],
                user=request.user,
            )
            return Response(BoxDetailSerializer(new_box).data, status=status.HTTP_201_CREATED)
        except ValueError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)


# ===========================================================================
# Loose Stock — List & Detail
# ===========================================================================

class LooseStockListAPI(APIView):
    """List loose stock with filters."""
    permission_classes = [IsAuthenticated, HasCompanyContext, HasAnyBarcodePermission]

    def get(self, request):
        svc = _get_service(request)
        qs = svc.list_loose_stock(
            status=request.query_params.get('status'),
            item_code=request.query_params.get('item_code'),
            warehouse=request.query_params.get('warehouse'),
            reason=request.query_params.get('reason'),
            search=request.query_params.get('search'),
        )
        return _list_response(request, qs, LooseStockListSerializer)


class LooseStockDetailAPI(APIView):
    """Get loose stock detail."""
    permission_classes = [IsAuthenticated, HasCompanyContext, HasAnyBarcodePermission]

    def get(self, request, loose_id):
        try:
            svc = _get_service(request)
            loose = svc.get_loose_stock(loose_id)
            return Response(LooseStockDetailSerializer(loose).data)
        except ValueError as e:
            return Response({'error': str(e)}, status=status.HTTP_404_NOT_FOUND)


# ===========================================================================
# Scan
# ===========================================================================

class ScanAPI(APIView):
    """Process a barcode scan — parse, lookup, log."""
    permission_classes = [IsAuthenticated, HasCompanyContext, HasAnyBarcodePermission]

    def post(self, request):
        serializer = ScanRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        svc = _get_scan_service(request)
        result = svc.process_scan(
            barcode_raw=serializer.validated_data['barcode_raw'],
            scan_type=serializer.validated_data.get('scan_type', 'LOOKUP'),
            context_ref_type=serializer.validated_data.get('context_ref_type', ''),
            context_ref_id=serializer.validated_data.get('context_ref_id'),
            user=request.user,
            device_info=serializer.validated_data.get('device_info', ''),
        )
        return Response(result)


class BarcodeLookupAPI(APIView):
    """Universal barcode lookup (no scan logging)."""
    permission_classes = [IsAuthenticated, HasCompanyContext, HasAnyBarcodePermission]

    def get(self, request, barcode_string):
        svc = _get_scan_service(request)
        result = svc.lookup_barcode(barcode_string)
        return Response(result)


class ScanHistoryAPI(APIView):
    """Scan audit log."""
    permission_classes = [IsAuthenticated, HasCompanyContext, HasAnyBarcodePermission]

    def get(self, request):
        svc = _get_scan_service(request)
        qs = svc.get_scan_history(
            scan_type=request.query_params.get('scan_type'),
            scan_result=request.query_params.get('scan_result'),
            entity_type=request.query_params.get('entity_type'),
        )
        return _list_response(request, qs, ScanLogSerializer)


# ===========================================================================
# Barcode Dispatch
# ===========================================================================

class DispatchBillLookupAPI(APIView):
    """Lookup an SAP bill/invoice and normalize it for dispatch scanning."""
    permission_classes = [IsAuthenticated, HasCompanyContext, HasAnyBarcodePermission]

    def post(self, request):
        serializer = DispatchBillLookupSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            svc = _get_dispatch_service(request)
            bill = svc.lookup_bill(serializer.validated_data['bill_number'])
            return Response(bill)
        except DispatchValidationError as e:
            return _dispatch_error_response(e)


class DispatchSessionListCreateAPI(APIView):
    """List dispatch sessions or create/resume one from an SAP bill."""
    permission_classes = [IsAuthenticated, HasCompanyContext, HasAnyBarcodePermission]

    def get(self, request):
        svc = _get_dispatch_service(request)
        qs = svc.list_sessions(
            status_value=request.query_params.get('status', ''),
            status_group=request.query_params.get('status_group', ''),
            bill_number=request.query_params.get('bill_number', ''),
            customer=request.query_params.get('customer', ''),
            created_by=request.query_params.get('created_by', ''),
            date_from=request.query_params.get('from_date', ''),
            date_to=request.query_params.get('to_date', ''),
            sap_sync_status=request.query_params.get('sap_sync_status', ''),
        )
        return _list_response(request, qs, DispatchSessionSerializer)

    def post(self, request):
        serializer = DispatchSessionCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            svc = _get_dispatch_service(request)
            session = svc.create_session(
                serializer.validated_data['bill_number'],
                user=request.user,
            )
            return Response(
                DispatchSessionSerializer(session).data,
                status=status.HTTP_201_CREATED,
            )
        except DispatchValidationError as e:
            return _dispatch_error_response(e)


class DispatchSessionFromBillAPI(DispatchSessionListCreateAPI):
    """Create or resume a dispatch session from an SAP bill number."""


class DispatchSessionActiveAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, HasAnyBarcodePermission]

    def get(self, request):
        svc = _get_dispatch_service(request)
        qs = svc.list_sessions(status_group='active')
        return _list_response(request, qs, DispatchSessionSerializer)


class DispatchSessionCompletedAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, HasAnyBarcodePermission]

    def get(self, request):
        svc = _get_dispatch_service(request)
        qs = svc.list_sessions(status_group='completed')
        return _list_response(request, qs, DispatchSessionSerializer)


class DispatchSessionClosedAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, HasAnyBarcodePermission]

    def get(self, request):
        svc = _get_dispatch_service(request)
        qs = svc.list_sessions(status_group='closed')
        return _list_response(request, qs, DispatchSessionSerializer)


class DispatchSessionDetailAPI(APIView):
    """Get dispatch session progress and lines."""
    permission_classes = [IsAuthenticated, HasCompanyContext, HasAnyBarcodePermission]

    def get(self, request, session_id):
        try:
            svc = _get_dispatch_service(request)
            session = svc.get_session(session_id)
            return Response(DispatchSessionSerializer(session).data)
        except DispatchValidationError as e:
            return _dispatch_error_response(e)


class DispatchSessionScanAPI(APIView):
    """Submit one barcode scan against the current required dispatch line."""
    permission_classes = [IsAuthenticated, HasCompanyContext, HasAnyBarcodePermission]

    def post(self, request, session_id):
        serializer = DispatchScanSubmitSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            svc = _get_dispatch_service(request)
            scan_log = svc.submit_scan(
                session_id=session_id,
                raw_barcode=serializer.validated_data['barcode'],
                user=request.user,
                device_id=serializer.validated_data.get('device_id', ''),
                request_id=serializer.validated_data.get('request_id'),
                ip_address=request.META.get('REMOTE_ADDR'),
                line_id=serializer.validated_data.get('line_id'),
            )
            session = svc.get_session(session_id)
            return Response(
                {
                    'scan': DispatchScanLogSerializer(scan_log).data,
                    'session': DispatchSessionSerializer(session).data,
                },
                status=(
                    status.HTTP_200_OK
                    if scan_log.result == 'ACCEPTED'
                    else status.HTTP_400_BAD_REQUEST
                ),
            )
        except DispatchValidationError as e:
            return _dispatch_error_response(e)


class DispatchScannedBoxQtyAPI(APIView):
    """Update the staged dispatch quantity for one scanned box."""
    permission_classes = [IsAuthenticated, HasCompanyContext, HasAnyBarcodePermission]

    def patch(self, request, session_id, unit_id):
        serializer = DispatchScannedBoxQtySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            svc = _get_dispatch_service(request)
            session = svc.update_scanned_box_qty(
                session_id=session_id,
                unit_id=unit_id,
                dispatch_qty=serializer.validated_data['dispatch_qty'],
                user=request.user,
            )
            return Response(DispatchSessionSerializer(session).data)
        except DispatchValidationError as e:
            return _dispatch_error_response(e)


class DispatchScannedBoxRemoveAPI(APIView):
    """Remove one scanned box from the current dispatch list."""
    permission_classes = [IsAuthenticated, HasCompanyContext, HasAnyBarcodePermission]

    def post(self, request, session_id, unit_id):
        try:
            svc = _get_dispatch_service(request)
            session = svc.remove_scanned_box(session_id=session_id, unit_id=unit_id, user=request.user)
            return Response(DispatchSessionSerializer(session).data)
        except DispatchValidationError as e:
            return _dispatch_error_response(e)


class DispatchSessionDispatchAPI(APIView):
    """Mark a fully scanned bill as dispatched and attempt SAP update."""
    permission_classes = [IsAuthenticated, HasCompanyContext, HasAnyBarcodePermission]

    def post(self, request, session_id):
        try:
            svc = _get_dispatch_service(request)
            session = svc.mark_dispatched(session_id, user=request.user)
            return Response(DispatchSessionSerializer(session).data)
        except DispatchValidationError as e:
            return _dispatch_error_response(e)


class DispatchSessionCompleteAPI(DispatchSessionDispatchAPI):
    """Alias for final dispatch completion."""


class DispatchSessionCloseAPI(APIView):
    """Close a dispatch session with an audit reason."""
    permission_classes = [IsAuthenticated, HasCompanyContext, HasAnyBarcodePermission]

    def post(self, request, session_id):
        serializer = DispatchCancelSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            svc = _get_dispatch_service(request)
            session = svc.close_session(
                session_id,
                reason=serializer.validated_data['reason'],
                user=request.user,
            )
            return Response(DispatchSessionSerializer(session).data)
        except DispatchValidationError as e:
            return _dispatch_error_response(e)


class DispatchSessionCancelAPI(APIView):
    """Cancel a dispatch session before final dispatch."""
    permission_classes = [IsAuthenticated, HasCompanyContext, HasAnyBarcodePermission]

    def post(self, request, session_id):
        serializer = DispatchCancelSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            svc = _get_dispatch_service(request)
            session = svc.cancel_session(
                session_id,
                reason=serializer.validated_data['reason'],
                user=request.user,
            )
            return Response(DispatchSessionSerializer(session).data)
        except DispatchValidationError as e:
            return _dispatch_error_response(e)


class DispatchSessionRetrySapSyncAPI(APIView):
    """Retry SAP status update after local dispatch completed."""
    permission_classes = [IsAuthenticated, HasCompanyContext, HasAnyBarcodePermission]

    def post(self, request, session_id):
        try:
            svc = _get_dispatch_service(request)
            session = svc.retry_sap_sync(session_id, user=request.user)
            return Response(DispatchSessionSerializer(session).data)
        except DispatchValidationError as e:
            return _dispatch_error_response(e)


class DispatchSessionScanLogsAPI(APIView):
    """Accepted and rejected scan audit for a session."""
    permission_classes = [IsAuthenticated, HasCompanyContext, HasAnyBarcodePermission]

    def get(self, request, session_id):
        try:
            svc = _get_dispatch_service(request)
            session = svc.get_session(session_id)
            qs = session.scan_logs.select_related('line', 'scanned_by')
            return _list_response(request, qs, DispatchScanLogSerializer)
        except DispatchValidationError as e:
            return _dispatch_error_response(e)


class DispatchSessionSapSyncLogsAPI(APIView):
    """SAP sync attempt audit for a session."""
    permission_classes = [IsAuthenticated, HasCompanyContext, HasAnyBarcodePermission]

    def get(self, request, session_id):
        try:
            svc = _get_dispatch_service(request)
            session = svc.get_session(session_id)
            qs = session.sap_sync_logs.all()
            return _list_response(request, qs, DispatchSapSyncLogSerializer)
        except DispatchValidationError as e:
            return _dispatch_error_response(e)


class DispatchSettingsAPI(APIView):
    """Company-level dispatch configuration."""
    permission_classes = [IsAuthenticated, HasCompanyContext, HasAnyBarcodePermission]

    def get(self, request):
        svc = _get_dispatch_service(request)
        return Response(DispatchSettingsSerializer(svc.get_settings()).data)

    def put(self, request):
        return self._update(request)

    def patch(self, request):
        return self._update(request)

    def _update(self, request):
        serializer = DispatchSettingsSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        svc = _get_dispatch_service(request)
        settings_obj = svc.update_settings(serializer.validated_data, user=request.user)
        return Response(DispatchSettingsSerializer(settings_obj).data)


class PalletHistoryAPI(APIView):
    """Pallet box assignment/removal/dispatch history."""
    permission_classes = [IsAuthenticated, HasCompanyContext, HasAnyBarcodePermission]

    def get(self, request, pallet_id):
        qs = (
            PalletBoxHistorySerializer.Meta.model.objects
            .filter(company=request.company.company, pallet_id=pallet_id)
            .select_related('pallet', 'box', 'dispatch_session', 'created_by')
        )
        return _list_response(request, qs, PalletBoxHistorySerializer)


class BoxHistoryAPI(APIView):
    """Box pallet and dispatch history."""
    permission_classes = [IsAuthenticated, HasCompanyContext, HasAnyBarcodePermission]

    def get(self, request, box_id):
        qs = (
            PalletBoxHistorySerializer.Meta.model.objects
            .filter(company=request.company.company, box_id=box_id)
            .select_related('pallet', 'box', 'dispatch_session', 'created_by')
        )
        return _list_response(request, qs, PalletBoxHistorySerializer)


class DispatchReportAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, HasAnyBarcodePermission]

    def get(self, request):
        svc = _get_dispatch_service(request)
        rows = svc.dispatch_report(request.query_params)
        return _report_response(request, rows, 'dispatch-summary-report')


class DispatchReportDetailAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, HasAnyBarcodePermission]

    def get(self, request, session_id):
        try:
            svc = _get_dispatch_service(request)
            return Response(svc.dispatch_detail_report(session_id))
        except DispatchValidationError as e:
            return _dispatch_error_response(e)


class DispatchPalletReportAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, HasAnyBarcodePermission]

    def get(self, request):
        svc = _get_dispatch_service(request)
        rows = svc.pallet_report(request.query_params)
        return _report_response(request, rows, 'dispatch-pallet-report')


class DispatchBoxReportAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, HasAnyBarcodePermission]

    def get(self, request):
        svc = _get_dispatch_service(request)
        rows = svc.box_report(request.query_params)
        return _report_response(request, rows, 'dispatch-box-report')


class DispatchRejectedScanReportAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, HasAnyBarcodePermission]

    def get(self, request):
        svc = _get_dispatch_service(request)
        rows = svc.rejected_scan_report(request.query_params)
        return _report_response(request, rows, 'dispatch-rejected-scan-report')


# ===========================================================================
# Production Integration
# ===========================================================================

class ProductionRunLabelsAPI(APIView):
    """Generate box labels for a production run."""
    permission_classes = [IsAuthenticated, HasCompanyContext, HasAnyBarcodePermission]

    def post(self, request, run_id):
        serializer = ProductionLabelsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            company_code = request.company.company.code
            svc = ProductionBarcodeIntegration(company_code=company_code)
            boxes = svc.generate_labels_for_run(
                run_id=run_id,
                qty_per_box=serializer.validated_data['qty_per_box'],
                box_count=serializer.validated_data['box_count'],
                batch_number=serializer.validated_data['batch_number'],
                warehouse=serializer.validated_data['warehouse'],
                user=request.user,
            )
            return Response(
                BoxListSerializer(boxes, many=True).data,
                status=status.HTTP_201_CREATED,
            )
        except ValueError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)


class ProductionReleaseOilListAPI(APIView):
    """List released rows from SAP HANA PRODUCTION_RELEASE_OIL for label generation."""
    permission_classes = [IsAuthenticated, HasCompanyContext, HasAnyBarcodePermission]

    def get(self, request):
        try:
            service = ProductionReleaseOilService(
                company_code=request.company.company.code,
            )
            rows = service.list_releases(
                search=request.query_params.get('search', '').strip(),
                limit=request.query_params.get('limit', 100),
            )
        except ProductionReleaseReadError as e:
            return Response(
                {'error': str(e)},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return Response(rows)


class OitmItemListAPI(APIView):
    """List active inventory items from SAP HANA OITM for label generation."""
    permission_classes = [IsAuthenticated, HasCompanyContext, HasAnyBarcodePermission]

    def get(self, request):
        try:
            service = OitmItemService(
                company_code=request.company.company.code,
            )
            rows = service.list_items(
                search=request.query_params.get('search', '').strip(),
                limit=request.query_params.get('limit', 100),
            )
        except OitmItemReadError as e:
            return Response(
                {'error': str(e)},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return Response(rows)


class OitmItemDetailAPI(APIView):
    """Look up one item's group (code + name) from SAP HANA OITM/OITB by code."""
    permission_classes = [IsAuthenticated, HasCompanyContext, HasAnyBarcodePermission]

    def get(self, request):
        item_code = request.query_params.get('item_code', '').strip()
        if not item_code:
            return Response(
                {'error': 'item_code is required.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            service = OitmItemService(company_code=request.company.company.code)
            item = service.get_item(item_code)
        except OitmItemReadError as e:
            return Response(
                {'error': str(e)},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        if item is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        return Response(item)


class ProductionRunPalletAPI(APIView):
    """Create a pallet linked to a production run."""
    permission_classes = [IsAuthenticated, HasCompanyContext, HasAnyBarcodePermission]

    def post(self, request, run_id):
        serializer = ProductionPalletSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            company_code = request.company.company.code
            svc = ProductionBarcodeIntegration(company_code=company_code)
            pallet = svc.create_pallet_for_run(
                run_id=run_id,
                box_ids=serializer.validated_data['box_ids'],
                warehouse=serializer.validated_data['warehouse'],
                user=request.user,
            )
            return Response(
                PalletDetailSerializer(pallet).data,
                status=status.HTTP_201_CREATED,
            )
        except ValueError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
