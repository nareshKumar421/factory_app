"""
inventory_age/views.py

API views for the Inventory Age & Value dashboard.

GET /api/v1/dashboards/inventory-age/filter-options/  — dropdown values
GET /api/v1/dashboards/inventory-age/report/          — filtered report (requires item_group)
"""

import logging

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from company.permissions import HasCompanyContext
from sap_client.exceptions import SAPConnectionError, SAPDataError

from .permissions import CanViewInventoryAge
from .serializers import (
    InventoryAgeFilterSerializer,
    InventoryAgeResponseSerializer,
    FilterOptionsSerializer,
)
from .services import InventoryAgeService

logger = logging.getLogger(__name__)


class InventoryAgeFilterOptionsAPI(APIView):
    """
    Returns distinct values for item groups, sub-groups, warehouses,
    and varieties.  Fast SQL query — no stored procedure call.

    GET /api/v1/dashboards/inventory-age/filter-options/
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewInventoryAge]

    def get(self, request):
        service = InventoryAgeService(company_code=request.company.company.code)

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

        return Response(FilterOptionsSerializer(options).data)


class InventoryAgeDashboardAPI(APIView):
    """
    Inventory age & value report.

    Requires ``item_group`` so the 73k-row SP result is filtered to a
    manageable size before being sent to the browser.

    GET /api/v1/dashboards/inventory-age/report/

    Query parameters:
        item_group  — (required) item group name
        search      — item code or name (optional)
        warehouse   — warehouse code (optional)
        sub_group   — sub-group name (optional)
        variety     — variety (optional)
        min_age     — minimum age in days (optional)
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewInventoryAge]

    def get(self, request):
        filter_serializer = InventoryAgeFilterSerializer(data=request.query_params)
        if not filter_serializer.is_valid():
            return Response(
                {"detail": "Invalid query parameters.", "errors": filter_serializer.errors},
                status=status.HTTP_400_BAD_REQUEST,
            )

        filters = filter_serializer.validated_data

        if not filters.get("item_group"):
            return Response(
                {"detail": "item_group is required. Please select an item group."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        service = InventoryAgeService(company_code=request.company.company.code)

        try:
            result = service.get_inventory_age(filters)
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

        return Response(InventoryAgeResponseSerializer(result).data)
