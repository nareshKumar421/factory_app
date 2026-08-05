"""
stock_dashboard/views.py

API views for the Stock Dashboard.

All endpoints are read-only and require:
  - JWT authentication (Authorization: Bearer <token>)
  - Company context header (Company-Code: <company_code>)
  - CanViewStockDashboard permission
"""

import logging

from django.http import HttpResponse
from django.utils import timezone
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
    StockDashboardAsOfFilterSerializer,
    StockDashboardExportFilterSerializer,
    StockDashboardFilterSerializer,
    StockDashboardResponseSerializer,
)
from .services import StockDashboardService

logger = logging.getLogger(__name__)


class StockDashboardAPI(APIView):
    """
    Stock level dashboard showing items against benchmark levels.

    Returns one row per item-warehouse or grouped item with current on-hand
    qty, health ratio, stock status, and movement status.

    GET /api/v1/dashboards/stock/

    Query parameters:
        warehouse - comma-separated warehouse codes
        item_group - SAP item group name
        status - comma-separated healthy, low, critical, unset
        movement_status - comma-separated recent, slow
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


class StockDashboardAsOfAPI(APIView):
    """
    Experimental SAP reconstruction endpoint for Stock Benchmark.

    GET /api/v1/dashboards/stock/as-of/?as_of_date=YYYY-MM-DD
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewStockDashboard]

    def get(self, request):
        filter_serializer = StockDashboardAsOfFilterSerializer(data=request.query_params)
        if not filter_serializer.is_valid():
            return Response(
                {"detail": "Invalid query parameters.", "errors": filter_serializer.errors},
                status=status.HTTP_400_BAD_REQUEST,
            )

        filters = filter_serializer.validated_data
        service = StockDashboardService(company_code=request.company.company.code)

        try:
            result = service.get_as_of_stock_levels(filters)
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


class StockDashboardExportAPI(APIView):
    """
    Excel export of the Stock Benchmark table.

    Accepts the same filters as the table (search, warehouse, item_group,
    status, movement_status, sort_by, sort_dir, optional as_of_date) and
    returns ALL matching rows as an .xlsx attachment (not just one page).

    GET /api/v1/dashboards/stock/export/
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewStockDashboard]

    MOVEMENT_LABELS = {"recent": "Recently Used", "slow": "Slow Moving"}
    STATUS_LABELS = {
        "healthy": "Healthy",
        "low": "Low",
        "critical": "Critical",
        "unset": "Unset",
        "none": "",
    }

    def get(self, request):
        filter_serializer = StockDashboardExportFilterSerializer(data=request.query_params)
        if not filter_serializer.is_valid():
            return Response(
                {"detail": "Invalid query parameters.", "errors": filter_serializer.errors},
                status=status.HTTP_400_BAD_REQUEST,
            )

        filters = filter_serializer.validated_data
        service = StockDashboardService(company_code=request.company.company.code)

        try:
            rows = service.get_stock_levels_for_export(filters)
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

        return self._build_workbook_response(rows, filters)

    def _build_workbook_response(self, rows, filters):
        import openpyxl
        from openpyxl.styles import Font

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Stock Benchmark"

        headers = [
            "Item Code",
            "Item Name",
            "Warehouse",
            "On Hand",
            "Benchmark",
            "Difference",
            "UOM",
            "Health %",
            "Status",
            "Movement",
            "Last Used",
            "Days Since Use",
        ]
        ws.append(headers)
        for cell in ws[1]:
            cell.font = Font(bold=True)

        for row in rows:
            on_hand = row.get("on_hand", 0) or 0
            min_stock = row.get("min_stock", 0) or 0
            ws.append([
                row.get("item_code", ""),
                row.get("item_name", ""),
                row.get("warehouse", ""),
                on_hand,
                min_stock,
                on_hand - min_stock,
                row.get("uom", ""),
                round(row.get("health_ratio", 0) * 100),
                self.STATUS_LABELS.get(row.get("stock_status", ""), row.get("stock_status", "")),
                self.MOVEMENT_LABELS.get(
                    row.get("movement_status", ""), row.get("movement_status", "")
                ),
                row.get("last_consumption_date") or "",
                row.get("days_since_last_consumption"),
            ])

        for column_cells in ws.columns:
            width = max((len(str(c.value)) for c in column_cells if c.value is not None), default=10)
            ws.column_dimensions[column_cells[0].column_letter].width = min(width + 2, 50)

        as_of_date = filters.get("as_of_date")
        stamp = as_of_date.isoformat() if as_of_date else timezone.localdate().isoformat()
        filename = f"stock_benchmark_{stamp}.xlsx"

        response = HttpResponse(
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        wb.save(response)
        return response


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
