"""
warehouse/views.py

API views for the Warehouse Management module.
Phase 1: Read-only inventory visibility from SAP HANA.
"""

import logging

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from company.permissions import HasCompanyContext
from sap_client.exceptions import SAPConnectionError, SAPDataError

from .permissions import CanViewInventory, CanViewNonMoving, CanViewWarehouseDashboard
from .serializers import (
    BatchExpiryFilterSerializer,
    BatchExpiryResponseSerializer,
    DashboardFilterSerializer,
    DashboardSummaryResponseSerializer,
    FilterOptionsResponseSerializer,
    InventoryFilterSerializer,
    InventoryResponseSerializer,
    ItemDetailSerializer,
    MovementHistoryFilterSerializer,
    MovementHistoryResponseSerializer,
)
from .services import WarehouseService

logger = logging.getLogger(__name__)


class WarehouseFilterOptionsAPI(APIView):
    """
    Returns distinct warehouses and item groups for filter dropdowns.

    GET /api/v1/warehouse/filter-options/
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewInventory]

    def get(self, request):
        service = WarehouseService(
            company_code=request.company.company.code
        )

        try:
            options = service.get_filter_options()
        except SAPConnectionError:
            return Response(
                {"detail": "SAP system is currently unavailable. Please try again later."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except SAPDataError as e:
            return Response(
                {"detail": f"SAP data error: {str(e)}"},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        return Response(FilterOptionsResponseSerializer(options).data)


class WarehouseInventoryListAPI(APIView):
    """
    Search and browse inventory across warehouses.

    GET /api/v1/warehouse/inventory/
        ?search=&warehouse=&item_group=
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewInventory]

    def get(self, request):
        filter_serializer = InventoryFilterSerializer(
            data=request.query_params
        )
        if not filter_serializer.is_valid():
            return Response(
                {
                    "detail": "Invalid query parameters.",
                    "errors": filter_serializer.errors,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        service = WarehouseService(
            company_code=request.company.company.code
        )

        try:
            result = service.get_inventory(filter_serializer.validated_data)
        except SAPConnectionError:
            return Response(
                {"detail": "SAP system is currently unavailable. Please try again later."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except SAPDataError as e:
            return Response(
                {"detail": f"SAP data error: {str(e)}"},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        return Response(InventoryResponseSerializer(result).data)


class WarehouseInventoryDetailAPI(APIView):
    """
    Item detail with per-warehouse stock breakdown and batches.

    GET /api/v1/warehouse/inventory/{item_code}/
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewInventory]

    def get(self, request, item_code):
        service = WarehouseService(
            company_code=request.company.company.code
        )

        try:
            item = service.get_item_detail(item_code)
        except SAPConnectionError:
            return Response(
                {"detail": "SAP system is currently unavailable. Please try again later."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except SAPDataError as e:
            return Response(
                {"detail": f"SAP data error: {str(e)}"},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        if item is None:
            return Response(
                {"detail": f"Item '{item_code}' not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        return Response(ItemDetailSerializer(item).data)


class WarehouseMovementHistoryAPI(APIView):
    """
    Movement history for an item.

    GET /api/v1/warehouse/inventory/{item_code}/movements/
        ?warehouse=&from_date=&to_date=
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewInventory]

    def get(self, request, item_code):
        filter_serializer = MovementHistoryFilterSerializer(
            data=request.query_params
        )
        if not filter_serializer.is_valid():
            return Response(
                {
                    "detail": "Invalid query parameters.",
                    "errors": filter_serializer.errors,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        service = WarehouseService(
            company_code=request.company.company.code
        )

        try:
            result = service.get_movement_history(
                item_code, filter_serializer.validated_data
            )
        except SAPConnectionError:
            return Response(
                {"detail": "SAP system is currently unavailable. Please try again later."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except SAPDataError as e:
            return Response(
                {"detail": f"SAP data error: {str(e)}"},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        return Response(MovementHistoryResponseSerializer(result).data)


class WarehouseDashboardSummaryAPI(APIView):
    """
    Dashboard summary with low-stock alerts.

    GET /api/v1/warehouse/dashboard/summary/
        ?warehouse_code=
    """

    permission_classes = [
        IsAuthenticated,
        HasCompanyContext,
        CanViewWarehouseDashboard,
    ]

    def get(self, request):
        filter_serializer = DashboardFilterSerializer(
            data=request.query_params
        )
        if not filter_serializer.is_valid():
            return Response(
                {
                    "detail": "Invalid query parameters.",
                    "errors": filter_serializer.errors,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        service = WarehouseService(
            company_code=request.company.company.code
        )

        try:
            result = service.get_dashboard_summary(
                warehouse_code=filter_serializer.validated_data.get(
                    "warehouse_code"
                )
            )
        except SAPConnectionError:
            return Response(
                {"detail": "SAP system is currently unavailable. Please try again later."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except SAPDataError as e:
            return Response(
                {"detail": f"SAP data error: {str(e)}"},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        return Response(DashboardSummaryResponseSerializer(result).data)


class WarehouseBatchExpiryAPI(APIView):
    """
    Batch expiry / non-moving FG report.

    GET /api/v1/warehouse/batch-expiry/
        ?warehouse=
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewNonMoving]

    def get(self, request):
        filter_serializer = BatchExpiryFilterSerializer(
            data=request.query_params
        )
        if not filter_serializer.is_valid():
            return Response(
                {
                    "detail": "Invalid query parameters.",
                    "errors": filter_serializer.errors,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        service = WarehouseService(
            company_code=request.company.company.code
        )

        try:
            result = service.get_batch_expiry_report(
                warehouse_code=filter_serializer.validated_data.get("warehouse")
            )
        except SAPConnectionError:
            return Response(
                {"detail": "SAP system is currently unavailable. Please try again later."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except SAPDataError as e:
            return Response(
                {"detail": f"SAP data error: {str(e)}"},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        return Response(BatchExpiryResponseSerializer(result).data)
