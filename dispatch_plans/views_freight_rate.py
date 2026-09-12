"""
dispatch_plans/views_freight_rate.py

Freight per litre for one company, with the money read from SAP.

Defaults to month-to-date on the DISPATCH date, which is the window the control
board asks for: what the litres that went out this month cost to move.
"""

import logging
from datetime import date, datetime

from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from company.permissions import HasCompanyContext
from sap_client.context import CompanyContext
from sap_client.exceptions import SAPConnectionError, SAPDataError

from .freight_rate_service import FreightRateService
from .permissions import CanViewOpenBiltiesOrPostTransporterAPInvoice

logger = logging.getLogger(__name__)


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


class FreightRateAPI(APIView):
    """
    GET /api/v1/dispatch/freight-rate/?date_from=&date_to=

    Both dates optional; the default window is the 1st of the current month to
    today. Read-only.
    """

    permission_classes = [
        IsAuthenticated,
        HasCompanyContext,
        CanViewOpenBiltiesOrPostTransporterAPInvoice,
    ]

    def get(self, request):
        company_code = request.company.company.code

        today = timezone.localdate()
        date_to = _parse_date(request.GET.get("date_to")) or today
        date_from = _parse_date(request.GET.get("date_from")) or today.replace(day=1)
        # A window that runs backwards returns nothing and looks like a plant
        # that moved nothing. Swap it instead.
        if date_from > date_to:
            date_from, date_to = date_to, date_from

        service = FreightRateService(CompanyContext(company_code), company_code)

        try:
            rate = service.get_rate(date_from, date_to)
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

        return Response(
            {
                "company_code": company_code,
                "date_from": date_from.isoformat(),
                "date_to": date_to.isoformat(),
                **rate,
            }
        )
