"""
dispatch_plans/dashboard_views.py

Read-only Dispatch Fulfilment dashboard endpoint.

GET /api/v1/dispatch-plans/dashboard/summary/?from=YYYY-MM-DD&to=YYYY-MM-DD

Requires:
  - JWT auth (Authorization: Bearer <token>)
  - Company context (Company-Code: <code> header)
  - dispatch_plans.can_view_dispatch_plans permission (reused)

All data comes from Postgres — see DispatchDashboardService.
"""
from datetime import timedelta

from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from company.permissions import HasCompanyContext

from .dashboard_service import DispatchDashboardService
from .models import DispatchPlanStatus
from .permissions import CanViewDispatchPlans

MAX_BILL_PAGE = 100

# Widest window we will aggregate in one request (guards against a huge range).
MAX_RANGE_DAYS = 366
# Default look-back when the client sends no dates.
DEFAULT_RANGE_DAYS = 90


def _parse_range(request):
    """Return (date_from, date_to). Raises ValueError with a client message."""
    today = timezone.localdate()

    to_raw = request.query_params.get("to")
    from_raw = request.query_params.get("from")

    date_to = today
    if to_raw:
        date_to = parse_date(to_raw)
        if date_to is None:
            raise ValueError("`to` must be a valid date (YYYY-MM-DD).")

    date_from = date_to - timedelta(days=DEFAULT_RANGE_DAYS - 1)
    if from_raw:
        date_from = parse_date(from_raw)
        if date_from is None:
            raise ValueError("`from` must be a valid date (YYYY-MM-DD).")

    if date_from > date_to:
        raise ValueError("`from` cannot be after `to`.")
    if (date_to - date_from).days > MAX_RANGE_DAYS:
        raise ValueError(f"Date range cannot exceed {MAX_RANGE_DAYS} days.")

    return date_from, date_to


class DispatchDashboardSummaryAPI(APIView):
    """Billed vs Planned vs Dispatched for the active company + date window."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewDispatchPlans]

    def get(self, request):
        try:
            date_from, date_to = _parse_range(request)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        company = request.company.company  # UserCompany -> Company
        service = DispatchDashboardService(company, date_from, date_to)
        return Response(service.build())


class DispatchDashboardBillsAPI(APIView):
    """Bill-wise (invoice-wise) drill-down for the active company + window.

    Query params: from, to, status, search, limit, offset.
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewDispatchPlans]

    def get(self, request):
        try:
            date_from, date_to = _parse_range(request)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        status_filter = request.query_params.get("status") or None
        if status_filter and status_filter not in DispatchPlanStatus.values:
            return Response(
                {"detail": f"Unknown status `{status_filter}`."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        search = request.query_params.get("search") or None

        try:
            limit = int(request.query_params.get("limit", 50))
            offset = int(request.query_params.get("offset", 0))
        except (TypeError, ValueError):
            return Response(
                {"detail": "`limit`/`offset` must be integers."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        limit = max(1, min(limit, MAX_BILL_PAGE))
        offset = max(0, offset)

        company = request.company.company
        service = DispatchDashboardService(company, date_from, date_to)
        return Response(
            service.bills(
                status=status_filter, search=search, limit=limit, offset=offset
            )
        )
