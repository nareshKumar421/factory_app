"""
budget_approvals/views.py

API views for the Budget Approvals Dashboard.

All endpoints are read-only and require:
  - JWT authentication (Authorization: Bearer <token>)
  - Company context header (Company-Code: <company_code>)
  - CanViewBudgetApprovals permission

The data itself always comes from the Oil HANA schema, where the
DRAFT_APPROVAL_Budget procedure unions the Oil and Beverages branches.
"""

import logging

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from company.permissions import HasCompanyContext
from sap_client.exceptions import SAPConnectionError, SAPDataError

from .permissions import CanViewBudgetApprovals
from .serializers import (
    BudgetApprovalFilterSerializer,
    BudgetApprovalReportResponseSerializer,
    ColumnValuesFilterSerializer,
    ColumnValuesResponseSerializer,
)
from .services import BudgetApprovalService

logger = logging.getLogger(__name__)


class BudgetApprovalReportAPI(APIView):
    """
    Budget approval drafts for the Factory budget head.

    GET /api/v1/dashboards/budget-approvals/report/
        ?status=pending&branch=OIL&effect_month=09-2026&search=diesel
        &page=1&page_size=50&refresh=false
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewBudgetApprovals]

    def get(self, request):
        filter_serializer = BudgetApprovalFilterSerializer(data=request.query_params)
        if not filter_serializer.is_valid():
            return Response(
                {"detail": "Invalid query parameters.", "errors": filter_serializer.errors},
                status=status.HTTP_400_BAD_REQUEST,
            )

        filters = filter_serializer.validated_data
        service = BudgetApprovalService()

        try:
            result = service.get_report(
                status=filters["status"],
                branch=filters["branch"],
                effect_month=filters["effect_month"],
                search=filters["search"],
                column_filters=filters["column_filters"],
                sort_by=filters["sort_by"],
                sort_dir=filters["sort_dir"],
                page=filters["page"],
                page_size=filters["page_size"],
                refresh=filters["refresh"],
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

        return Response(BudgetApprovalReportResponseSerializer(result).data)


class BudgetApprovalColumnValuesAPI(APIView):
    """
    Distinct values for one column, for the Excel-style header filter
    dropdowns. Values are computed over the dataset filtered by everything
    except the requested column.

    GET /api/v1/dashboards/budget-approvals/column-values/
        ?field=owner&status=pending&column_filters={"sub_budget":["DIESEL"]}
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewBudgetApprovals]

    def get(self, request):
        filter_serializer = ColumnValuesFilterSerializer(data=request.query_params)
        if not filter_serializer.is_valid():
            return Response(
                {"detail": "Invalid query parameters.", "errors": filter_serializer.errors},
                status=status.HTTP_400_BAD_REQUEST,
            )

        filters = filter_serializer.validated_data
        service = BudgetApprovalService()

        try:
            result = service.get_column_values(
                field=filters["field"],
                status=filters["status"],
                branch=filters["branch"],
                effect_month=filters["effect_month"],
                search=filters["search"],
                column_filters=filters["column_filters"],
                refresh=filters["refresh"],
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

        return Response(ColumnValuesResponseSerializer(result).data)
