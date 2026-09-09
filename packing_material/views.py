"""
packing_material/views.py

API for the packing-material board.

Read-only, and every endpoint requires:
  - JWT authentication (Authorization: Bearer <token>)
  - Company context header (Company-Code: <company_code>)
  - CanViewPackingMaterial permission

Three endpoints for the three panels of the stock board, rather than one for
the board. They answer questions on different clocks: stock is a snapshot of
now and does not change when somebody picks a different month, the two top
lists are a period, and only the dispatch list changes when the SAP/
FactoryFlow toggle is flipped. One endpoint would re-read all of it every
time any of it was asked something new, and a slow BOM explosion would hold up
the cards behind it.

The requirement board is the opposite case and is ONE endpoint. Every column
on it belongs to a single row of arithmetic -- `Req` needs the plan and the
movements and the stock at once -- so there is no useful partial answer, and
splitting it would only make the front end re-join what SQL already joined.
"""

import logging

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from company.permissions import HasCompanyContext
from sap_client.exceptions import SAPConnectionError, SAPDataError

from .errors import PlanNotFound
from .permissions import CanViewPackingMaterial
from .serializers import (
    DispatchFilterSerializer,
    DispatchResponseSerializer,
    PeriodFilterSerializer,
    PlanListFilterSerializer,
    PlanListResponseSerializer,
    ProductionResponseSerializer,
    RequirementFilterSerializer,
    RequirementResponseSerializer,
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


class PackingMaterialPlanListAPI(_PackingMaterialAPI):
    """The production plans the requirement board can be pointed at.

    GET /api/v1/packing-material/plans/?limit=36

    Read from SAP (``OFCT``/``FCT1``), newest first. ``meta.default_abs_id``
    is the plan the requirement endpoint would pick on its own, so the picker
    and the table agree on which month is open without the front end
    re-implementing the choice.
    """

    def get(self, request):
        filters = PlanListFilterSerializer(data=request.query_params)
        if not filters.is_valid():
            return Response(
                {"detail": "Invalid query parameters.", "errors": filters.errors},
                status=status.HTTP_400_BAD_REQUEST,
            )

        service = self.service(request)
        report, error = self.guarded(
            lambda: service.get_plans(limit=filters.validated_data["limit"])
        )
        if error:
            return error
        return Response(PlanListResponseSerializer(report).data)


class PackingMaterialRequirementAPI(_PackingMaterialAPI):
    """The month's plan against what is still to be bought.

    GET /api/v1/packing-material/requirement/?abs_id=44

    Nine columns per component: the BOM requirement for the plan, what has
    already reached the floor, what is left of the plan, what the feeding
    stores hold, the resulting requirement, what is on open purchase orders,
    and what is still required after those. Omit ``abs_id`` for the plan
    covering today.

    A missing plan is 404 and not an empty table: no rows and no error would
    read as "the plan needs no packing material", which is the one thing this
    board must never say by accident.
    """

    def get(self, request):
        filters = RequirementFilterSerializer(data=request.query_params)
        if not filters.is_valid():
            return Response(
                {"detail": "Invalid query parameters.", "errors": filters.errors},
                status=status.HTTP_400_BAD_REQUEST,
            )

        service = self.service(request)
        try:
            report, error = self.guarded(
                lambda: service.get_requirement(
                    abs_id=filters.validated_data.get("abs_id")
                )
            )
        except PlanNotFound as e:
            return Response({"detail": str(e)}, status=status.HTTP_404_NOT_FOUND)
        if error:
            return error
        return Response(RequirementResponseSerializer(report).data)
