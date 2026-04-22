import logging
from django.db import IntegrityError
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated

from company.permissions import HasCompanyContext
from .services.barcode_service import BarcodeService
from .services.label_service import LabelService
from .services.scan_service import ScanService
from .services.production_integration_service import ProductionBarcodeIntegration
from .serializers import (
    BoxGenerateSerializer, BoxListSerializer, BoxDetailSerializer,
    PalletCreateSerializer, PalletListSerializer, PalletDetailSerializer,
    VoidSerializer, PrintRequestSerializer, BulkPrintRequestSerializer,
    LabelPrintLogSerializer,
    PalletMoveSerializer, PalletClearSerializer, PalletSplitSerializer,
    PalletAddBoxesSerializer, PalletRemoveBoxesSerializer, BoxTransferSerializer,
    DismantlePalletSerializer, DismantleBoxSerializer, RepackSerializer,
    LooseStockListSerializer, LooseStockDetailSerializer,
    ScanRequestSerializer, ScanLogSerializer,
    ProductionLabelsSerializer, ProductionPalletSerializer,
)

logger = logging.getLogger(__name__)


def _get_service(request) -> BarcodeService:
    company_code = request.company.company.code
    return BarcodeService(company_code=company_code)


def _get_scan_service(request) -> ScanService:
    company_code = request.company.company.code
    return ScanService(company_code=company_code)


def _get_label_service(request) -> LabelService:
    company_code = request.company.company.code
    return LabelService(company_code=company_code)


# ===========================================================================
# Box — Generate
# ===========================================================================

class BoxGenerateAPI(APIView):
    """Bulk-generate box barcode records."""
    permission_classes = [IsAuthenticated, HasCompanyContext]

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
            return Response(
                {'error': 'Duplicate barcode detected. Please try again.'},
                status=status.HTTP_400_BAD_REQUEST,
            )


# ===========================================================================
# Box — List & Detail
# ===========================================================================

class BoxListAPI(APIView):
    """List boxes with optional filters."""
    permission_classes = [IsAuthenticated, HasCompanyContext]

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
        return Response(BoxListSerializer(qs[:500], many=True).data)


class BoxDetailAPI(APIView):
    """Get box detail with movement history."""
    permission_classes = [IsAuthenticated, HasCompanyContext]

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
    permission_classes = [IsAuthenticated, HasCompanyContext]

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
    """Create a pallet by linking existing boxes."""
    permission_classes = [IsAuthenticated, HasCompanyContext]

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


# ===========================================================================
# Pallet — List & Detail
# ===========================================================================

class PalletListAPI(APIView):
    """List pallets with optional filters."""
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def get(self, request):
        svc = _get_service(request)
        qs = svc.list_pallets(
            status=request.query_params.get('status'),
            item_code=request.query_params.get('item_code'),
            batch_number=request.query_params.get('batch_number'),
            warehouse=request.query_params.get('warehouse'),
            search=request.query_params.get('search'),
        )
        return Response(PalletListSerializer(qs[:500], many=True).data)


class PalletDetailAPI(APIView):
    """Get pallet detail with boxes and movement history."""
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def get(self, request, pallet_id):
        try:
            svc = _get_service(request)
            pallet = svc.get_pallet(pallet_id)
            return Response(PalletDetailSerializer(pallet).data)
        except ValueError as e:
            return Response({'error': str(e)}, status=status.HTTP_404_NOT_FOUND)


# ===========================================================================
# Pallet — Void
# ===========================================================================

class PalletVoidAPI(APIView):
    """Void a pallet and disassociate its boxes."""
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def post(self, request, pallet_id):
        serializer = VoidSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            svc = _get_service(request)
            pallet = svc.void_pallet(
                pallet_id, serializer.validated_data.get('reason', ''), request.user
            )
            return Response(PalletDetailSerializer(pallet).data)
        except ValueError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)


# ===========================================================================
# Pallet — Move
# ===========================================================================

class PalletMoveAPI(APIView):
    """Move pallet to a different warehouse."""
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def post(self, request, pallet_id):
        serializer = PalletMoveSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            svc = _get_service(request)
            pallet = svc.move_pallet(
                pallet_id,
                to_warehouse=serializer.validated_data['to_warehouse'],
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
    permission_classes = [IsAuthenticated, HasCompanyContext]

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
    """Split selected boxes into a new pallet."""
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def post(self, request, pallet_id):
        serializer = PalletSplitSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            svc = _get_service(request)
            new_pallet = svc.split_pallet(
                pallet_id,
                box_ids=serializer.validated_data['box_ids'],
                warehouse=serializer.validated_data['warehouse'],
                user=request.user,
            )
            return Response(PalletDetailSerializer(new_pallet).data, status=status.HTTP_201_CREATED)
        except ValueError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)


# ===========================================================================
# Pallet — Add / Remove Boxes
# ===========================================================================

class PalletAddBoxesAPI(APIView):
    """Add unpalletized boxes to an existing pallet."""
    permission_classes = [IsAuthenticated, HasCompanyContext]

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
    permission_classes = [IsAuthenticated, HasCompanyContext]

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


# ===========================================================================
# Box — Transfer
# ===========================================================================

class BoxTransferAPI(APIView):
    """Transfer boxes between warehouses or to a pallet."""
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def post(self, request):
        serializer = BoxTransferSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            svc = _get_service(request)
            boxes = svc.transfer_boxes(
                box_ids=serializer.validated_data['box_ids'],
                to_warehouse=serializer.validated_data['to_warehouse'],
                to_pallet_id=serializer.validated_data.get('to_pallet_id'),
                user=request.user,
            )
            return Response(BoxListSerializer(boxes, many=True).data)
        except ValueError as e:
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)


# ===========================================================================
# Print — Box Label
# ===========================================================================

class BoxPrintAPI(APIView):
    """Log print and return box label data for frontend rendering."""
    permission_classes = [IsAuthenticated, HasCompanyContext]

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
    permission_classes = [IsAuthenticated, HasCompanyContext]

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

class BulkPrintAPI(APIView):
    """Return label data for multiple items at once."""
    permission_classes = [IsAuthenticated, HasCompanyContext]

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
                )

        return Response(results)


# ===========================================================================
# Print — History
# ===========================================================================

class PrintHistoryAPI(APIView):
    """Print/reprint audit log."""
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def get(self, request):
        label_svc = _get_label_service(request)
        qs = label_svc.get_print_history(
            label_type=request.query_params.get('label_type'),
            print_type=request.query_params.get('print_type'),
            reference_code=request.query_params.get('search'),
        )
        return Response(LabelPrintLogSerializer(qs[:500], many=True).data)


# ===========================================================================
# Dismantle — Pallet
# ===========================================================================

class DismantlePalletAPI(APIView):
    """Dismantle a pallet — remove all or selected boxes."""
    permission_classes = [IsAuthenticated, HasCompanyContext]

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
    permission_classes = [IsAuthenticated, HasCompanyContext]

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
    permission_classes = [IsAuthenticated, HasCompanyContext]

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
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def get(self, request):
        svc = _get_service(request)
        qs = svc.list_loose_stock(
            status=request.query_params.get('status'),
            item_code=request.query_params.get('item_code'),
            warehouse=request.query_params.get('warehouse'),
            reason=request.query_params.get('reason'),
            search=request.query_params.get('search'),
        )
        return Response(LooseStockListSerializer(qs[:500], many=True).data)


class LooseStockDetailAPI(APIView):
    """Get loose stock detail."""
    permission_classes = [IsAuthenticated, HasCompanyContext]

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
    permission_classes = [IsAuthenticated, HasCompanyContext]

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
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def get(self, request, barcode_string):
        svc = _get_scan_service(request)
        result = svc.lookup_barcode(barcode_string)
        return Response(result)


class ScanHistoryAPI(APIView):
    """Scan audit log."""
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def get(self, request):
        svc = _get_scan_service(request)
        qs = svc.get_scan_history(
            scan_type=request.query_params.get('scan_type'),
            scan_result=request.query_params.get('scan_result'),
            entity_type=request.query_params.get('entity_type'),
        )
        return Response(ScanLogSerializer(qs[:500], many=True).data)


# ===========================================================================
# Production Integration
# ===========================================================================

class ProductionRunLabelsAPI(APIView):
    """Generate box labels for a production run."""
    permission_classes = [IsAuthenticated, HasCompanyContext]

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


class ProductionRunPalletAPI(APIView):
    """Create a pallet linked to a production run."""
    permission_classes = [IsAuthenticated, HasCompanyContext]

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
