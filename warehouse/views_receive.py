"""Receiving printed barcodes into a warehouse.

This is the godown gate: a receiver picks a warehouse they manage and scans each
pallet/box as it comes in, which is what turns a printed label into stock (see
``barcode.services.activation_service`` for why labels start inactive).

The endpoints live here rather than in ``barcode`` because receiving is a
warehouse job and the real restriction is the ``UserWarehouse`` assignment this
app owns — the same split ``gate_core`` uses when its docking views drive
``barcode.services.vehicle_load``. The activation logic itself stays in
``barcode``, which owns Box and Pallet.
"""
import logging

from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework import status

from barcode.serializers import BarcodeReceiveScanSerializer, BoxListSerializer
from barcode.services.activation_service import ActivationService, normalize_warehouse
from company.permissions import HasCompanyContext

from .permissions import CanReceiveBarcodes
from .services import warehouse_scope

logger = logging.getLogger(__name__)


def _assert_can_receive_into(request, warehouse: str):
    """The receiver must manage this warehouse. Raises DRF PermissionDenied.

    ``warehouse_scope`` names the warehouses the user *does* manage in the error,
    because a refusal with no second half reads as a bug rather than a setting.
    """
    warehouse_scope.assert_manages(
        request.user,
        request.company.company.code,
        [warehouse],
        action=f"receive into {warehouse}",
    )


class BarcodeReceiveScanAPI(APIView):
    """Scan one pallet/box into the selected warehouse.

    Business refusals come back as 200 with ``status: REJECTED`` rather than 4xx:
    a receiver working through a trolley needs the next scan to keep working, and
    the screen shows the reason inline. Only permission and validation failures
    are error codes.
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanReceiveBarcodes]

    def post(self, request):
        serializer = BarcodeReceiveScanSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        warehouse = normalize_warehouse(data['warehouse'])
        _assert_can_receive_into(request, warehouse)

        outcome = ActivationService(request.company.company.code).scan_for_activation(
            data['barcode'],
            warehouse=warehouse,
            user=request.user,
            confirmed_box_count=data.get('confirmed_box_count'),
            device_info=data.get('device_info', ''),
        )

        pallet = outcome.pallet
        return Response({
            'status': outcome.status,
            'code': outcome.code,
            'detail': outcome.detail,
            'entity_type': outcome.entity_type,
            'barcode': outcome.barcode,
            'warehouse': warehouse,
            'activated_count': outcome.activated_count,
            'pending_box_count': outcome.pending_box_count,
            'verify_request_id': outcome.verify_request_id,
            'pallet': {
                'id': pallet.id,
                'pallet_id': pallet.pallet_id,
                'item_code': pallet.item_code,
                'item_name': pallet.item_name,
                'batch_number': pallet.batch_number,
                'status': pallet.status,
                'box_count': pallet.box_count,
            } if pallet is not None else None,
            'activated_boxes': BoxListSerializer(outcome.activated_boxes, many=True).data,
        })


class BarcodeReceiveSessionAPI(APIView):
    """Today's running tally at one warehouse, for the receive screen header."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanReceiveBarcodes]

    def get(self, request):
        warehouse = normalize_warehouse(request.query_params.get('warehouse') or '')
        if not warehouse:
            return Response(
                {'error': 'A warehouse is required.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        _assert_can_receive_into(request, warehouse)
        return Response(
            ActivationService(request.company.company.code).receive_activity(
                warehouse=warehouse
            )
        )
