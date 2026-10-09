"""
production_dispatch/views.py

``GET /api/v1/dashboards/production-dispatch/report/?from=YYYY-MM-DD&to=YYYY-MM-DD``
    Oil's production and dispatch per day and item over the range, each item's
    factors, and its FAST / SLOW over the 90 days ending on ``to``.

``GET /api/v1/dashboards/production-dispatch/documents/?from=&to=[&item=]``
    The production and dispatch lines behind it, one row each.

Read-only, JWT, a company context header and the report's own right. The
report is always Oil's (see ``constants.COMPANY_CODE``), so the header only
proves the reader is staff of some company; it does not pick the schema.
"""

import logging
from datetime import date

from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from company.permissions import HasCompanyContext
from sap_client.exceptions import SAPConnectionError, SAPDataError

from .constants import MAX_RANGE_DAYS
from .permissions import CanViewProductionDispatch
from .services import ProductionDispatchService

logger = logging.getLogger(__name__)


def _day(raw, name):
    try:
        return date.fromisoformat((raw or "").strip())
    except ValueError:
        raise ValueError(f"{name} must be a date, YYYY-MM-DD.") from None


def _range(params):
    """``(from, to)`` from the query string, or a 400 response."""
    try:
        date_from = _day(params.get("from"), "from")
        date_to = _day(params.get("to"), "to")
    except ValueError as exc:
        return None, Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    if date_from > date_to:
        return None, Response(
            {"detail": "from must be on or before to."}, status=status.HTTP_400_BAD_REQUEST
        )
    if date_to > timezone.localdate():
        return None, Response(
            {"detail": "The report has no figures for a day that has not happened."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if (date_to - date_from).days + 1 > MAX_RANGE_DAYS:
        return None, Response(
            {"detail": f"Ask for at most {MAX_RANGE_DAYS} days at a time."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    return (date_from, date_to), None


def _failure(exc, what):
    """503 when SAP did not answer, 502 when it refused the read, 503 otherwise."""
    if isinstance(exc, SAPConnectionError):
        return Response({"detail": str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
    if isinstance(exc, SAPDataError):
        return Response({"detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
    return Response(
        {"detail": f"The {what} could not be read.", "error": str(exc)},
        status=status.HTTP_503_SERVICE_UNAVAILABLE,
    )


class ProductionDispatchReportAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewProductionDispatch]

    def get(self, request):
        span, error = _range(request.query_params)
        if error:
            return error
        try:
            report = ProductionDispatchService().build(*span)
        except Exception as exc:  # noqa: BLE001 - SAP errors and composition alike
            logger.exception("production_dispatch: %s to %s could not be read", *span)
            return _failure(exc, "report")
        return Response(report, status=status.HTTP_200_OK)


class ProductionDispatchDocumentsAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewProductionDispatch]

    def get(self, request):
        span, error = _range(request.query_params)
        if error:
            return error
        item = (request.query_params.get("item") or "").strip().upper() or None
        try:
            payload = ProductionDispatchService().documents(*span, item_code=item)
        except Exception as exc:  # noqa: BLE001
            logger.exception("production_dispatch: documents %s to %s could not be read", *span)
            return _failure(exc, "documents")
        return Response(payload, status=status.HTTP_200_OK)
