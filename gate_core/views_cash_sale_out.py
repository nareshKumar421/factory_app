"""Cash sale out — counter bills leaving with the buyer, on the Sales Dispatch Out board.

See ``gate_core.models.cash_sale_gate_out`` for why this is not a docking.
Like the rest of that board, it spans every company the user belongs to.
"""
from datetime import datetime

from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from company.models import Company
from company.permissions import HasCompanyContext
from gate_core.models import CashSaleGateOut
from gate_core.permissions import HasRequiredDjangoPermission
from gate_core.serializers_cash_sale_out import (
    CashSaleGateOutCancelSerializer,
    CashSaleGateOutCreateSerializer,
    CashSaleGateOutSerializer,
)
from gate_core.services import cash_sale_gate_out as cash_sale_out
from gate_core.services.user_scope import assert_company_in_scope, user_company_ids
from sap_client.exceptions import SAPConnectionError, SAPDataError

VIEW_PERMISSION = "gate_core.can_view_sales_dispatch_out"
MARK_OUT_PERMISSION = "gate_core.can_dispatch_sales_dispatch_out"
CANCEL_PERMISSION = "gate_core.can_cancel_sales_dispatch_out"


def _parse_date(value):
    try:
        return datetime.strptime(value, "%Y-%m-%d").date() if value else None
    except ValueError:
        return None


def _sap_unavailable_response(exc):
    if isinstance(exc, SAPConnectionError):
        return Response(
            {"detail": "SAP system is currently unavailable. Please try again later."},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    return Response(
        {"detail": "Failed to read the cash-sale bills from SAP."},
        status=status.HTTP_502_BAD_GATEWAY,
    )


class CashSaleGateOutListCreateView(APIView):
    """GET the cash sale outs in a date range; POST marks bills out."""

    permission_classes = [IsAuthenticated, HasCompanyContext, HasRequiredDjangoPermission]
    required_permissions = {"GET": VIEW_PERMISSION, "POST": MARK_OUT_PERMISSION}

    def get(self, request):
        qs = (
            CashSaleGateOut.objects.filter(
                company_id__in=user_company_ids(request), is_active=True
            )
            .select_related("company", "created_by", "cancelled_by")
            .prefetch_related("bills")
        )
        from_date = _parse_date(request.query_params.get("from_date"))
        to_date = _parse_date(request.query_params.get("to_date"))
        if from_date:
            qs = qs.filter(out_at__date__gte=from_date)
        if to_date:
            qs = qs.filter(out_at__date__lte=to_date)
        return Response(CashSaleGateOutSerializer(qs[:500], many=True).data)

    def post(self, request):
        serializer = CashSaleGateOutCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        assert_company_in_scope(request, data["company_id"])
        company = get_object_or_404(Company, id=data["company_id"])
        try:
            entry = cash_sale_out.mark_out(
                user=request.user,
                company=company,
                doc_entries=data["doc_entries"],
                taken_by_name=data["taken_by_name"],
                taken_by_mobile=data.get("taken_by_mobile", ""),
                vehicle_no=data.get("vehicle_no", ""),
                remarks=data.get("remarks", ""),
            )
        except cash_sale_out.CashSaleGateOutError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except (SAPConnectionError, SAPDataError) as exc:
            return _sap_unavailable_response(exc)
        return Response(CashSaleGateOutSerializer(entry).data, status=status.HTTP_201_CREATED)


class CashSaleGateOutPendingBillsView(APIView):
    """The cash-sale bills still inside the factory, for the guard to pick from."""

    permission_classes = [IsAuthenticated, HasCompanyContext, HasRequiredDjangoPermission]
    required_permissions = MARK_OUT_PERMISSION

    def get(self, request):
        companies = Company.objects.filter(id__in=user_company_ids(request)).order_by("code")
        return Response(
            cash_sale_out.pending_bills(companies, search=request.query_params.get("search"))
        )


class CashSaleGateOutCancelView(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, HasRequiredDjangoPermission]
    required_permissions = CANCEL_PERMISSION

    def post(self, request, entry_id):
        entry = get_object_or_404(
            CashSaleGateOut.objects.filter(
                company_id__in=user_company_ids(request), is_active=True
            ),
            id=entry_id,
        )
        serializer = CashSaleGateOutCancelSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            cash_sale_out.cancel(
                user=request.user, entry=entry, reason=serializer.validated_data["reason"]
            )
        except cash_sale_out.CashSaleGateOutError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(CashSaleGateOutSerializer(entry).data)
