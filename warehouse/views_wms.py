"""
warehouse/views_wms.py

Lookup APIs backed by the SAP HANA reader:
- Warehouse & item group lists (used as filter dropdowns by the barcode pallet
  pages and the stock-level dashboard)

The WMS dashboard/stock/transfer/batch/backlog/billing views were removed; only
the shared dropdown lookups remain. The underlying ``WMSHanaReader`` is still
used here and by the marketplace stock check.
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
