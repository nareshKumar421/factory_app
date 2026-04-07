"""
warehouse/views_wms.py

API views for Warehouse Management System:
- Stock overview, item detail, movements
- Dashboard summary with chart data
- Billing reconciliation
- Warehouse & item group lists
"""

import logging
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated

from company.permissions import HasCompanyContext
from .services.wms_hana_reader import WMSHanaReader

logger = logging.getLogger(__name__)


def _get_reader(request) -> WMSHanaReader:
    company_code = request.company.company.code
    return WMSHanaReader(company_code=company_code)


# ===========================================================================
# Dashboard Summary
# ===========================================================================

class WMSDashboardAPI(APIView):
    """WMS Dashboard - KPIs, charts, recent movements."""
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def get(self, request):
        try:
            reader = _get_reader(request)
            warehouse_code = request.query_params.get("warehouse_code")
            data = reader.get_dashboard_summary(warehouse_code=warehouse_code)
            return Response(data)
        except Exception as e:
            logger.error(f"WMS Dashboard error: {e}")
            return Response(
                {"error": str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


# ===========================================================================
# Stock Overview
# ===========================================================================

class WMSStockOverviewAPI(APIView):
    """Paginated stock overview across items and warehouses."""
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def get(self, request):
        try:
            reader = _get_reader(request)
            filters = {
                "warehouse_code": request.query_params.get("warehouse_code", ""),
                "item_group": request.query_params.get("item_group", ""),
                "search": request.query_params.get("search", ""),
                "stock_filter": request.query_params.get("stock_filter", "with_stock"),
                "page": request.query_params.get("page", 1),
                "page_size": request.query_params.get("page_size", 50),
            }
            data = reader.get_stock_overview(filters)
            return Response(data)
        except Exception as e:
            logger.error(f"WMS Stock Overview error: {e}")
            return Response(
                {"error": str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


# ===========================================================================
# Item Detail
# ===========================================================================

class WMSItemDetailAPI(APIView):
    """Detailed stock info for a single item across all warehouses."""
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def get(self, request, item_code):
        try:
            reader = _get_reader(request)
            data = reader.get_item_detail(item_code)
            if not data["item"]:
                return Response(
                    {"error": f"Item '{item_code}' not found"},
                    status=status.HTTP_404_NOT_FOUND,
                )
            return Response(data)
        except Exception as e:
            logger.error(f"WMS Item Detail error: {e}")
            return Response(
                {"error": str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


# ===========================================================================
# Stock Movements
# ===========================================================================

class WMSStockMovementsAPI(APIView):
    """Stock movement history from SAP OINM table."""
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def get(self, request):
        try:
            reader = _get_reader(request)
            filters = {
                "item_code": request.query_params.get("item_code", ""),
                "warehouse_code": request.query_params.get("warehouse_code", ""),
                "from_date": request.query_params.get("from_date", ""),
                "to_date": request.query_params.get("to_date", ""),
                "limit": request.query_params.get("limit", 100),
            }
            data = reader.get_stock_movements(filters)
            return Response({"movements": data})
        except Exception as e:
            logger.error(f"WMS Movements error: {e}")
            return Response(
                {"error": str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


# ===========================================================================
# Warehouse Summary
# ===========================================================================

class WMSWarehouseSummaryAPI(APIView):
    """Summary metrics for each warehouse."""
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def get(self, request):
        try:
            reader = _get_reader(request)
            data = reader.get_warehouse_summary()
            return Response({"warehouses": data})
        except Exception as e:
            logger.error(f"WMS Warehouse Summary error: {e}")
            return Response(
                {"error": str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


# ===========================================================================
# Billing Reconciliation
# ===========================================================================

class WMSBillingOverviewAPI(APIView):
    """Billing reconciliation: GRPO received vs AP invoiced."""
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def get(self, request):
        try:
            reader = _get_reader(request)
            filters = {
                "from_date": request.query_params.get("from_date", ""),
                "to_date": request.query_params.get("to_date", ""),
                "vendor": request.query_params.get("vendor", ""),
                "warehouse_code": request.query_params.get("warehouse_code", ""),
            }
            data = reader.get_billing_overview(filters)
            return Response(data)
        except Exception as e:
            logger.error(f"WMS Billing Overview error: {e}")
            return Response(
                {"error": str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


# ===========================================================================
# Dropdowns (Warehouses, Item Groups)
# ===========================================================================

class WMSWarehouseListAPI(APIView):
    """List of active warehouses for filter dropdowns."""
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def get(self, request):
        try:
            reader = _get_reader(request)
            data = reader.get_warehouses()
            return Response({"warehouses": data})
        except Exception as e:
            logger.error(f"WMS Warehouse List error: {e}")
            return Response(
                {"error": str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


class WMSItemGroupListAPI(APIView):
    """List of item groups for filter dropdowns."""
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def get(self, request):
        try:
            reader = _get_reader(request)
            data = reader.get_item_groups()
            return Response({"item_groups": data})
        except Exception as e:
            logger.error(f"WMS Item Groups error: {e}")
            return Response(
                {"error": str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
