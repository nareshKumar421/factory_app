"""
packing_material/views.py

API for the packing-material board.

Read-only, and every endpoint requires:
  - JWT authentication (Authorization: Bearer <token>)
  - Company context header (Company-Code: <company_code>)
  - CanViewPackingMaterial permission

Three endpoints for three panels, rather than one for the board. They answer
questions on different clocks: stock is a snapshot of now and does not change
when somebody picks a different month, the two top lists are a period, and
only the dispatch list changes when the SAP/FactoryFlow toggle is flipped.
One endpoint would re-read all of it every time any of it was asked something
new, and a slow BOM explosion would hold up the cards behind it.
"""

import logging

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from company.permissions import HasCompanyContext
from sap_client.exceptions import SAPConnectionError, SAPDataError

from .permissions import CanViewPackingMaterial
from .serializers import (
    DispatchFilterSerializer,
    DispatchResponseSerializer,
    PeriodFilterSerializer,
    ProductionResponseSerializer,
    StockResponseSerializer,
)
from .services import PackingMaterialService

logger = logging.getLogger(__name__)


class _PackingMaterialAPI(APIView):
    """Shared plumbing: the permissions, and how SAP failures are reported."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewPackingMaterial]

    def service(self, request) -> PackingMaterialService:
        return PackingMaterialService(company_code=request.company.company.code)

    def guarded(self, work):
        """Run a SAP read, turning its two failure modes into the right status.

        Unreachable is 503 and retryable; a rejected or unreadable query is
        502, because retrying it will fail the same way and the front end
        should say so rather than spin.
        """
        try:
            return work(), None
        except SAPConnectionError:
            return None, Response(
                {"detail": "SAP system is currently unavailable. Please try again later."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except SAPDataError as e:
            return None, Response(
                {"detail": f"SAP data error: {str(e)}"},
                status=status.HTTP_502_BAD_GATEWAY,
            )


class PackingMaterialStockAPI(_PackingMaterialAPI):
    """Packing-material stock in each store, and the total of them.

    GET /api/v1/packing-material/stock/

    No parameters: the warehouses are configuration, not a query, and the
    figure is stock right now. Every item in every one of those stores comes
    back with the totals, because each card opens onto its own item list and
    a second request per card would read the same table three more times.
    """

    def get(self, request):
        service = self.service(request)
        board, error = self.guarded(service.get_stock)
        if error:
            return error
        return Response(StockResponseSerializer(board).data)


class PackingMaterialProductionAPI(_PackingMaterialAPI):
    """Packing material the line issued over the period, top first.

    GET /api/v1/packing-material/production/
        ?date_from=2026-09-01&date_to=2026-09-30&top=10
    """

    def get(self, request):
        filters = PeriodFilterSerializer(data=request.query_params)
        if not filters.is_valid():
            return Response(
                {"detail": "Invalid query parameters.", "errors": filters.errors},
                status=status.HTTP_400_BAD_REQUEST,
            )

        validated = filters.validated_data
        service = self.service(request)
        report, error = self.guarded(
            lambda: service.get_production(
                date_from=validated["date_from"],
                date_to=validated["date_to"],
                top_n=validated["top"],
            )
        )
        if error:
            return error
        return Response(ProductionResponseSerializer(report).data)


class PackingMaterialDispatchAPI(_PackingMaterialAPI):
    """Packing material that left inside the finished goods dispatched.

    GET /api/v1/packing-material/dispatch/
        ?date_from=2026-09-01&date_to=2026-09-30&top=10&source=sap

    ``source=sap`` counts A/R invoices net of credit notes; ``source=app``
    counts the bills that went out through FactoryFlow docking. The two do not
    cover the same set of bills and the response says which one it answered.
    """

    def get(self, request):
        filters = DispatchFilterSerializer(data=request.query_params)
        if not filters.is_valid():
            return Response(
                {"detail": "Invalid query parameters.", "errors": filters.errors},
                status=status.HTTP_400_BAD_REQUEST,
            )

        validated = filters.validated_data
        service = self.service(request)
        report, error = self.guarded(
            lambda: service.get_dispatch(
                date_from=validated["date_from"],
                date_to=validated["date_to"],
                top_n=validated["top"],
                source=validated["source"],
            )
        )
        if error:
            return error
        return Response(DispatchResponseSerializer(report).data)
