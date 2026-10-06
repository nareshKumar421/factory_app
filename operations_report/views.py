"""
operations_report/views.py

``GET /api/v1/dashboards/operations-report/days/?from=YYYY-MM-DD&to=YYYY-MM-DD``

Read-only. JWT, a company context header, and either the factory expense or
the production cost right -- or the board feed that mirrors one of them. The
report shows the factory's wage, salary and power bill beside what each litre
cost, which is exactly what those two rights already disclose; it mints no right of
its own, so nothing has to be created on the live database before it opens.

Per section the service withholds what the reader may not see, so a reader
with only one of the two rights gets that half and is told about the other.
Goods Return (GR) is shown only to a holder of the goods return right (or its
board feed) -- the same right the Customer Returns board reads it under.

Same contract as the other boards: a section that could not be read is
reported inside a 200 (``meta.degraded``); only a failure that leaves no report
at all gets a status code.
"""

import logging
from datetime import date

from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from company.permissions import HasCompanyContext
from control_boards.permissions import CanReadBoard

from .services import MAX_SPAN_DAYS, OperationsReportService

logger = logging.getLogger(__name__)


def _day(raw, name):
    try:
        return date.fromisoformat((raw or "").strip())
    except ValueError:
        raise ValueError(f"{name} must be a date, YYYY-MM-DD.") from None


class OperationsReportDaysAPI(APIView):
    """A company's report days, ``from`` to ``to`` inclusive."""

    permission_classes = [
        IsAuthenticated,
        HasCompanyContext,
        CanReadBoard("factory_expense", "production_cost", board="Operations Report"),
    ]

    def get(self, request):
        try:
            date_from = _day(request.query_params.get("from"), "from")
            date_to = _day(request.query_params.get("to"), "to")
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        if date_from > date_to:
            return Response(
                {"detail": "from must be on or before to."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if date_to > timezone.localdate():
            return Response(
                {"detail": "The report has no figures for a day that has not happened."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if (date_to - date_from).days + 1 > MAX_SPAN_DAYS:
            return Response(
                {"detail": f"Ask for at most {MAX_SPAN_DAYS} days at a time."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        company = request.company.company
        try:
            report = OperationsReportService(
                company, date_from, date_to, user=request.user
            ).build()
        except Exception as exc:  # noqa: BLE001
            # Only reached if the composition itself fails -- every section
            # already catches its own.
            logger.exception(
                "operations_report: %s to %s could not be composed for %s",
                date_from,
                date_to,
                company.code,
            )
            return Response(
                {"detail": "The Operations Report could not be read.", "error": str(exc)},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return Response(report, status=status.HTTP_200_OK)
