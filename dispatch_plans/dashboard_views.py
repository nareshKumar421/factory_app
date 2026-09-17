"""
dispatch_plans/dashboard_views.py

Read-only Dispatch Fulfilment dashboard endpoint.

GET /api/v1/dispatch-plans/dashboard/summary/?from=YYYY-MM-DD&to=YYYY-MM-DD
    Optional: &companies=JIVO_OIL,JIVO_MART -- narrows the aggregation to those
    companies. Intersected with the caller's own memberships, so it can only
    remove companies, never grant one. Omitted means every company the caller
    belongs to, which is the historic behaviour.

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

from company.models import UserCompany
from company.permissions import HasCompanyContext

from .dashboard_service import DispatchDashboardService
from .models import DispatchPlanStatus
from .permissions import CanViewDispatchPlans

MAX_BILL_PAGE = 100


def _user_companies(request):
    """The companies this request aggregates over.

    Every company the signed-in user can access, optionally narrowed by a
    ``companies=CODE,CODE`` parameter.

    The parameter can only ever REMOVE companies: the requested codes are
    intersected with the user's own memberships, so it is a scope control and
    never a way to read a company the user does not belong to. Unknown codes are
    ignored rather than rejected, and a parameter that matches nothing falls back
    to the full list -- a board asking for a company the viewer cannot see should
    show what it is allowed to, not an empty screen it cannot explain.

    Exists because "all the companies you belong to" is the wrong default for a
    board that names the companies it adds up. A wall reading
    "Oil + Mart" must not quietly fold in Beverages for whoever happens to hold
    all three.
    """
    rows = (
        UserCompany.objects.filter(user=request.user, is_active=True)
        .select_related("company")
    )

    requested = {
        code.strip().upper()
        for code in (request.query_params.get("companies") or "").split(",")
        if code.strip()
    }

    ids, codes = [], []
    for uc in rows:
        if requested and uc.company.code.upper() not in requested:
            continue
        ids.append(uc.company_id)
        codes.append(uc.company.code)

    if requested and not ids:
        return _user_companies_unscoped(rows)

    return ids, codes


def _user_companies_unscoped(rows):
    """Every membership in `rows`, ignoring any requested narrowing."""
    ids, codes = [], []
    for uc in rows:
        ids.append(uc.company_id)
        codes.append(uc.company.code)
    return ids, codes

# Widest window we will AGGREGATE in one request. The summary builds a row per
# day across the whole range, so this one has to stay tight.
MAX_RANGE_DAYS = 366
# Widest window for the bill LIST. Far looser on purpose: that endpoint is
# paginated, and its one unbounded step -- collecting the plan ids that had a
# gate-out in the window -- is bounded by the size of the gate-out table, not by
# the width of the range. A backlog picker that greys out whole years because of
# the aggregation guard is a filter people cannot use.
MAX_BILL_RANGE_DAYS = 3660
# Default look-back when the client sends no dates.
DEFAULT_RANGE_DAYS = 90


def _parse_range(request, max_days=MAX_RANGE_DAYS):
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
    if (date_to - date_from).days > max_days:
        raise ValueError(f"Date range cannot exceed {max_days} days.")

    return date_from, date_to


class DispatchDashboardSummaryAPI(APIView):
    """Billed vs Planned vs Dispatched for the active company + date window."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewDispatchPlans]

    def get(self, request):
        try:
            date_from, date_to = _parse_range(request)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        company_ids, company_codes = _user_companies(request)
        service = DispatchDashboardService(
            company_ids, date_from, date_to, company_codes=company_codes
        )
        return Response(service.build())


class DispatchDashboardBillsAPI(APIView):
    """Bill-wise (invoice-wise) drill-down for the active company + window.

    Query params: from, to, status, search, limit, offset, order, filled.
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewDispatchPlans]

    def get(self, request):
        try:
            date_from, date_to = _parse_range(request, MAX_BILL_RANGE_DAYS)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        status_filter = request.query_params.get("status") or None
        if status_filter and status_filter not in DispatchPlanStatus.values:
            return Response(
                {"detail": f"Unknown status `{status_filter}`."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        search = request.query_params.get("search") or None

        order = request.query_params.get("order") or "newest"
        if order not in ("newest", "oldest"):
            return Response(
                {"detail": "`order` must be `newest` or `oldest`."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        filled_only = (request.query_params.get("filled") or "").lower() in (
            "1",
            "true",
            "yes",
        )

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

        company_ids, company_codes = _user_companies(request)
        service = DispatchDashboardService(
            company_ids, date_from, date_to, company_codes=company_codes
        )
        return Response(
            service.bills(
                status=status_filter,
                search=search,
                limit=limit,
                offset=offset,
                order=order,
                filled_only=filled_only,
            )
        )
