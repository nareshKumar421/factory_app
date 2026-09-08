"""
pm_demand/views.py

API for the Packing Material Demand dashboard.

Read-only, and requires:
  - JWT authentication (Authorization: Bearer <token>)
  - Company context header (Company-Code: <company_code>)
  - CanViewPmDemand permission
"""

import logging

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from company.permissions import HasCompanyContext
from sap_client.exceptions import SAPConnectionError, SAPDataError

from .permissions import CanViewPmDemand
from .serializers import PmDemandFilterSerializer, PmDemandReportResponseSerializer
from .services import PmDemandService

logger = logging.getLogger(__name__)


class PmDemandReportAPI(APIView):
    """The whole PM Demand board in one read.

    GET /api/v1/pm-demand/report/
        ?date_from=2026-08-01&date_to=2026-08-31&top=10&include_intercompany=true

    One endpoint rather than one per panel, because every panel is a different
    roll-up of the same five SAP reads over the same period. Splitting them
    would multiply the HANA round trips and let two panels on one screen
    disagree while a slower one was still loading.

    Query parameters:
        date_from            (required) first document date counted, inclusive
        date_to              (required) last document date counted, inclusive
        top                  (optional) length of each top list, default 10
        include_intercompany (optional) count group-company invoices as
                             dispatch; default true, because the packing
                             material physically left the factory
        source               (optional) 'sap' (default) reads goods issues and
                             invoices; 'app' reads FactoryFlow's own runs,
                             approved BOMs and gate-outs. The recipe, the
                             stock and the open purchase orders come from SAP
                             either way -- see ``app_reader``.
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewPmDemand]

    def get(self, request):
        filters = PmDemandFilterSerializer(data=request.query_params)
        if not filters.is_valid():
            return Response(
                {"detail": "Invalid query parameters.", "errors": filters.errors},
                status=status.HTTP_400_BAD_REQUEST,
            )

        validated = filters.validated_data
        service = PmDemandService(company_code=request.company.company.code)

        try:
            report = service.get_report(
                date_from=validated["date_from"],
                date_to=validated["date_to"],
                top_n=validated["top"],
                include_intercompany=validated["include_intercompany"],
                source=validated["source"],
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

        return Response(PmDemandReportResponseSerializer(report).data)
