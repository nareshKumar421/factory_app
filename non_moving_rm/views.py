"""
non_moving_rm/views.py

API views for the Non-Moving Raw Material Dashboard.

All endpoints are read-only and require:
  - JWT authentication (Authorization: Bearer <token>)
  - Company context header (Company-Code: <company_code>)
  - CanViewNonMovingRM permission
"""

import logging

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from company.permissions import HasCompanyContext
from sap_client.exceptions import SAPConnectionError, SAPDataError

from .permissions import CanViewNonMovingRM
from .serializers import (
    NonMovingRMFilterSerializer,
    NonMovingRMReportResponseSerializer,
    ItemGroupResponseSerializer,
)
from .services import NonMovingRMService

logger = logging.getLogger(__name__)


class NonMovingRMReportAPI(APIView):
    """
    Non-moving raw material report.

    Calls the REPORT_BP_NON_MOVING_RM stored procedure with Age and ItemGroup
    parameters and returns detailed item-level data with summary aggregations.

    GET /api/v1/non-moving-rm/report/?age=45&item_group=105

    Query parameters:
        age         — (required) Number of days since last movement (e.g. 45, 90, 180)
        item_group  — (required) Item group code from OITB (e.g. 105, 106)
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewNonMovingRM]

    def get(self, request):
        filter_serializer = NonMovingRMFilterSerializer(data=request.query_params)
        if not filter_serializer.is_valid():
            return Response(
                {"detail": "Invalid query parameters.", "errors": filter_serializer.errors},
                status=status.HTTP_400_BAD_REQUEST,
            )

        filters = filter_serializer.validated_data
        service = NonMovingRMService(company_code=request.company.company.code)

        try:
            result = service.get_report(
                age=filters["age"],
                item_group=filters["item_group"],
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

        return Response(NonMovingRMReportResponseSerializer(result).data)


class ItemGroupDropdownAPI(APIView):
    """
    Item group dropdown list.

    Returns all item groups from the OITB table for use in the
    dashboard filter dropdown.

    GET /api/v1/non-moving-rm/item-groups/
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewNonMovingRM]

    def get(self, request):
        service = NonMovingRMService(company_code=request.company.company.code)

        try:
            result = service.get_item_groups()
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

        return Response(ItemGroupResponseSerializer(result).data)
