"""
factory_expense/views.py

One read endpoint for the wall, and CRUD for the four things that feed it.

Every endpoint needs JWT auth plus the ``Company-Code`` header, the same
contract the rest of the dashboards use. Reads accept the view right; writes
need the configure right.
"""

import logging

from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from company.permissions import HasCompanyContext

from .constants import LABOUR_COST_TYPE_CODE, SALARY_COST_TYPE_CODE
from .models import MonthlyBudget, month_start
from .permissions import (
    CanReadOrConfigureFactoryExpense,
    CanViewFactoryExpense,
)
from .rates import load_rates
from .serializers import FactoryExpenseSettingsSerializer, MonthlyBudgetSerializer
from .services import build_board, get_settings

logger = logging.getLogger(__name__)


def _requested_date(request, param="date"):
    """One ``YYYY-MM-DD`` query parameter, defaulting to today."""
    raw = (request.query_params.get(param) or "").strip()
    if not raw:
        return timezone.localdate(), None
    parsed = parse_date(raw)
    if not parsed:
        return None, f"{param} must be YYYY-MM-DD."
    return parsed, None


def _requested_range(request):
    """``?from=…&to=…``, defaulting to a single day: today.

    ``?date=`` is still accepted as a shorthand for both ends, so a client
    deployed before the range filter existed keeps working rather than 400ing
    on a parameter it has never heard of.
    """
    if request.query_params.get("date") and not request.query_params.get("from"):
        day, error = _requested_date(request)
        return (day, day, error)

    date_from, error = _requested_date(request, "from")
    if error:
        return None, None, error
    date_to, error = _requested_date(request, "to")
    if error:
        return None, None, error
    # A backwards range is a slip, not an error worth a red banner — the board
    # swaps the ends and shows what was obviously meant.
    if date_to < date_from:
        date_from, date_to = date_to, date_from
    return date_from, date_to, None


def _requested_month(request):
    """``?month=YYYY-MM-DD`` (any day in it), defaulting to this month."""
    raw = (request.query_params.get("month") or "").strip()
    if not raw:
        return month_start(timezone.localdate()), None
    parsed = parse_date(raw if len(raw) > 7 else f"{raw}-01")
    if not parsed:
        return None, "month must be YYYY-MM or YYYY-MM-DD."
    return month_start(parsed), None


class FactoryExpenseBoardAPI(APIView):
    """The wall board for a span of days.

    GET /api/v1/dashboards/factory-expense/board/?from=YYYY-MM-DD&to=YYYY-MM-DD

    Both default to today, so the wall's normal state is a single day.
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewFactoryExpense]

    def get(self, request):
        date_from, date_to, error = _requested_range(request)
        if error:
            return Response({"detail": error}, status=status.HTTP_400_BAD_REQUEST)

        company = request.company.company
        try:
            board = build_board(company, date_from, date_to)
        except Exception:
            logger.exception(
                "[FactoryExpense] board failed for %s over %s..%s",
                company.code, date_from, date_to,
            )
            return Response(
                {"detail": "The expense board could not be built. Please try again."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        return Response(board)


class FactoryExpenseSettingsAPI(APIView):
    """GET / PATCH the company's board settings."""

    permission_classes = [
        IsAuthenticated,
        HasCompanyContext,
        CanReadOrConfigureFactoryExpense,
    ]

    def get(self, request):
        row = get_settings(request.company.company)
        return Response(FactoryExpenseSettingsSerializer(row).data)

    def patch(self, request):
        row = get_settings(request.company.company)
        serializer = FactoryExpenseSettingsSerializer(row, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save(updated_by=request.user)
        return Response(serializer.data)


class ResolvedRatesAPI(APIView):
    """What the board would price today with, and where each rate came from.

    A read-back over the Cost Master rather than an editor: rates are owned by
    ``cost_master`` and changed in Admin › Cost Master. This exists so somebody
    looking at an unexpected figure on the wall can see the exact row behind it
    without leaving the board's own configuration screen.

    GET /api/v1/dashboards/factory-expense/rates/?date=YYYY-MM-DD
    """

    permission_classes = [
        IsAuthenticated,
        HasCompanyContext,
        CanReadOrConfigureFactoryExpense,
    ]

    def get(self, request):
        on_date, error = _requested_date(request)
        if error:
            return Response({"detail": error}, status=status.HTTP_400_BAD_REQUEST)

        company = request.company.company
        payload = {}
        for key, code in (
            ("labour", LABOUR_COST_TYPE_CODE),
            ("salary", SALARY_COST_TYPE_CODE),
        ):
            rows = load_rates(code, company, on_date)
            payload[key] = {
                "cost_type_code": code,
                "rates": [
                    {
                        "id": row.id,
                        "scope": row.scope,
                        "scope_display": row.get_scope_display(),
                        "company_code": row.company.code if row.company else None,
                        "department": row.department.name if row.department else None,
                        "basis": row.basis,
                        "basis_display": row.get_basis_display(),
                        "rate": str(row.rate),
                        "effective_from": row.effective_from.isoformat(),
                        "notes": row.notes,
                    }
                    for row in sorted(
                        rows, key=lambda r: (r.scope, r.effective_from), reverse=True
                    )
                ],
            }
        payload["date"] = on_date.isoformat()
        return Response(payload)


class _CompanyScopedListCreateAPI(APIView):
    """List and create rows of one configuration model, scoped to the company."""

    permission_classes = [
        IsAuthenticated,
        HasCompanyContext,
        CanReadOrConfigureFactoryExpense,
    ]
    model = None
    serializer_class = None

    def get_queryset(self, request):
        return self.model.objects.filter(company=request.company.company)

    def get(self, request):
        rows = self.get_queryset(request)
        return Response(self.serializer_class(rows, many=True).data)

    def post(self, request):
        serializer = self.serializer_class(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save(
            company=request.company.company,
            created_by=request.user,
            updated_by=request.user,
        )
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class _CompanyScopedDetailAPI(APIView):
    """Update or retire one configuration row."""

    permission_classes = [
        IsAuthenticated,
        HasCompanyContext,
        CanReadOrConfigureFactoryExpense,
    ]
    model = None
    serializer_class = None

    def get_object(self, request, pk):
        return self.model.objects.filter(company=request.company.company, pk=pk).first()

    def patch(self, request, pk):
        row = self.get_object(request, pk)
        if row is None:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        serializer = self.serializer_class(row, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save(updated_by=request.user)
        return Response(serializer.data)

    def delete(self, request, pk):
        """Retire rather than erase — a deleted rate would silently reprice history."""
        row = self.get_object(request, pk)
        if row is None:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        row.is_active = False
        row.updated_by = request.user
        row.save(update_fields=["is_active", "updated_by", "updated_at"])
        return Response(status=status.HTTP_204_NO_CONTENT)


class MonthlyBudgetListCreateAPI(_CompanyScopedListCreateAPI):
    model = MonthlyBudget
    serializer_class = MonthlyBudgetSerializer

    def get_queryset(self, request):
        rows = super().get_queryset(request)
        month, error = _requested_month(request)
        if error is None and request.query_params.get("month"):
            rows = rows.filter(month=month)
        return rows.order_by("-month", "bucket")


class MonthlyBudgetDetailAPI(_CompanyScopedDetailAPI):
    model = MonthlyBudget
    serializer_class = MonthlyBudgetSerializer
