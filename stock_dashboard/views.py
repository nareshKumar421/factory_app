"""
stock_dashboard/views.py

API views for the Stock Dashboard.

All endpoints are read-only and require:
  - JWT authentication (Authorization: Bearer <token>)
  - Company context header (Company-Code: <company_code>)
  - CanViewStockDashboard permission
"""

import logging

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from company.permissions import HasCompanyContext
from sap_client.exceptions import SAPConnectionError, SAPDataError

from .permissions import CanViewStockDashboard
from .serializers import (
    ItemDetailFilterSerializer,
    ItemDetailResponseSerializer,
    StockDashboardFilterSerializer,
    StockDashboardResponseSerializer,
)
from .services import StockDashboardService

logger = logging.getLogger(__name__)


class StockDashboardAPI(APIView):
    """
    Stock level dashboard showing items with minimum stock thresholds.

    Returns one row per item-warehouse where MinStock is configured,
    with current on-hand qty, health ratio, and stock status.

    GET /api/v1/dashboards/stock/

    Query parameters:
        warehouse — warehouse code (optional)
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewStockDashboard]

    def get(self, request):
        filter_serializer = StockDashboardFilterSerializer(data=request.query_params)
        if not filter_serializer.is_valid():
            return Response(
                {"detail": "Invalid query parameters.", "errors": filter_serializer.errors},
                status=status.HTTP_400_BAD_REQUEST,
            )

        filters = filter_serializer.validated_data
        service = StockDashboardService(company_code=request.company.company.code)

        try:
            result = service.get_stock_levels(filters)
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

        return Response(StockDashboardResponseSerializer(result).data)


class StockItemDetailAPI(APIView):
    """
    Per-warehouse breakdown for a single item (used by row expand).

    GET /api/v1/dashboards/stock/<item_code>/warehouses/?warehouse=WH-01,WH-02
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewStockDashboard]

    def get(self, request, item_code: str):
        filter_serializer = ItemDetailFilterSerializer(data=request.query_params)
        if not filter_serializer.is_valid():
            return Response(
                {"detail": "Invalid query parameters.", "errors": filter_serializer.errors},
                status=status.HTTP_400_BAD_REQUEST,
            )

        warehouses = filter_serializer.validated_data["warehouse"]
        service = StockDashboardService(company_code=request.company.company.code)

        try:
            result = service.get_item_detail(item_code, warehouses)
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

        return Response(ItemDetailResponseSerializer(result).data)
