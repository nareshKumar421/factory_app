"""
production_movements/views.py

P0 read APIs:
  - warehouse-role config (list) for the active company
  - role-tagged production stock board (live SAP on-hand)
  - per-warehouse item drill-down

All require JWT + Company-Code header. Company is resolved from the header
(request.company.company.code), consistent with the rest of the app.
"""

import logging

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from company.permissions import HasCompanyContext
from sap_client.exceptions import SAPConnectionError, SAPDataError, SAPValidationError

from .models import WarehouseMovement
from .permissions import (
    CanCreateMovement,
    CanViewMovements,
    CanViewProductionStock,
    CanViewWarehouseRoles,
)
from .serializers import (
    StockBoardResponseSerializer,
    TransferRequestSerializer,
    WarehouseMovementSerializer,
    WarehouseRoleSerializer,
    WarehouseStockFilterSerializer,
)
from .services import (
    ProductionStockService,
    TransferError,
    TransferService,
    get_roles_for_company,
    get_transfer_options,
)

logger = logging.getLogger(__name__)


def _sap_error_response(exc):
    if isinstance(exc, SAPConnectionError):
        return Response(
            {"detail": "SAP system is currently unavailable. Please try again later."},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    return Response(
        {"detail": f"SAP data error: {exc}"}, status=status.HTTP_502_BAD_GATEWAY
    )


class WarehouseRoleListAPI(APIView):
    """GET the warehouse-role config for the active company."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewWarehouseRoles]

    def get(self, request):
        company_code = request.company.company.code
        roles = get_roles_for_company(company_code, active_only=False)
        return Response(WarehouseRoleSerializer(roles, many=True).data)


class ProductionStockBoardAPI(APIView):
    """GET role-tagged live-SAP stock for the active company's production warehouses."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewProductionStock]

    def get(self, request):
        company_code = request.company.company.code
        service = ProductionStockService(company_code)
        try:
            board = service.get_stock_board()
        except (SAPConnectionError, SAPDataError, SAPValidationError) as exc:
            return _sap_error_response(exc)
        return Response(StockBoardResponseSerializer(board).data)


class ProductionWarehouseStockAPI(APIView):
    """GET item-level stock for one warehouse (optionally PM-only)."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewProductionStock]

    def get(self, request, whs_code):
        filt = WarehouseStockFilterSerializer(data=request.query_params)
        if not filt.is_valid():
            return Response(
                {"detail": "Invalid query parameters.", "errors": filt.errors},
                status=status.HTTP_400_BAD_REQUEST,
            )
        company_code = request.company.company.code
        service = ProductionStockService(company_code)
        try:
            result = service.get_warehouse_stock(whs_code, filt.validated_data)
        except (SAPConnectionError, SAPDataError, SAPValidationError) as exc:
            return _sap_error_response(exc)
        return Response(result)


class TransferOptionsAPI(APIView):
    """GET the issue point + eligible source stores for the transfer form."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewMovements]

    def get(self, request):
        company_code = request.company.company.code
        return Response(get_transfer_options(company_code))


class TransferCreateAPI(APIView):
    """
    POST a PM transfer from a store into the company's BOM issue point.

    Body: {from_whs, lines:[{item_code, quantity, ...}], posting_date?, dry_run?}.
    When SAP writes are disabled (or dry_run=true) the movement is recorded as
    DRY_RUN and nothing is posted. Handles the ITR two-step automatically.
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanCreateMovement]

    def post(self, request):
        ser = TransferRequestSerializer(data=request.data)
        if not ser.is_valid():
            return Response(
                {"detail": "Invalid request.", "errors": ser.errors},
                status=status.HTTP_400_BAD_REQUEST,
            )
        data = ser.validated_data
        company_code = request.company.company.code
        service = TransferService(company_code, user=request.user)
        try:
            result = service.transfer_to_issue_point(
                from_whs=data["from_whs"],
                lines=data["lines"],
                posting_date=(
                    data["posting_date"].isoformat() if data.get("posting_date") else None
                ),
                dry_run=data.get("dry_run"),
                reference=data.get("reference", ""),
            )
        except TransferError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except (SAPConnectionError, SAPDataError, SAPValidationError) as exc:
            # The ledger row is saved FAILED; surface SAP's message to the client.
            return Response(
                {"detail": f"SAP rejected the movement: {exc}"},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        return Response(result, status=status.HTTP_201_CREATED)


class MovementListAPI(APIView):
    """GET the movement ledger for the active company (most recent first)."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewMovements]

    def get(self, request):
        company_code = request.company.company.code
        qs = (
            WarehouseMovement.objects.filter(company__code=company_code)
            .prefetch_related("lines")
        )
        mtype = request.query_params.get("movement_type")
        if mtype:
            qs = qs.filter(movement_type=mtype)
        status_f = request.query_params.get("status")
        if status_f:
            qs = qs.filter(status=status_f)
        qs = qs[:200]
        return Response(WarehouseMovementSerializer(qs, many=True).data)
