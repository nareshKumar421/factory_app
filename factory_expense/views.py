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

from accounts.models import Department
from company.permissions import HasCompanyContext

from .models import (
    DepartmentSalaryConfig,
    LabourRateConfig,
    MonthlyBudget,
    month_start,
)
from .permissions import (
    CanReadOrConfigureFactoryExpense,
    CanViewFactoryExpense,
)
from .serializers import (
    DepartmentOptionSerializer,
    DepartmentSalaryConfigSerializer,
    FactoryExpenseSettingsSerializer,
    LabourRateConfigSerializer,
    MonthlyBudgetSerializer,
)
from .services import build_board, get_settings

logger = logging.getLogger(__name__)


def _requested_date(request):
    """``?date=YYYY-MM-DD``, defaulting to today. Returns (date, error)."""
    raw = (request.query_params.get("date") or "").strip()
    if not raw:
        return timezone.localdate(), None
    parsed = parse_date(raw)
    if not parsed:
        return None, "date must be YYYY-MM-DD."
    return parsed, None


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
    """The wall board for one day.

    GET /api/v1/dashboards/factory-expense/board/?date=YYYY-MM-DD
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewFactoryExpense]

    def get(self, request):
        on_date, error = _requested_date(request)
        if error:
            return Response({"detail": error}, status=status.HTTP_400_BAD_REQUEST)

        company = request.company.company
        try:
            board = build_board(company, on_date)
        except Exception:
            logger.exception("[FactoryExpense] board failed for %s on %s", company.code, on_date)
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


class DepartmentOptionsAPI(APIView):
    """The department list both configuration tabs pick from."""

    permission_classes = [
        IsAuthenticated,
        HasCompanyContext,
        CanReadOrConfigureFactoryExpense,
    ]

    def get(self, request):
        departments = Department.objects.all().order_by("name")
        return Response(DepartmentOptionSerializer(departments, many=True).data)


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


class LabourRateListCreateAPI(_CompanyScopedListCreateAPI):
    model = LabourRateConfig
    serializer_class = LabourRateConfigSerializer

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .select_related("department")
            .order_by("-effective_from", "department__name", "shift")
        )


class LabourRateDetailAPI(_CompanyScopedDetailAPI):
    model = LabourRateConfig
    serializer_class = LabourRateConfigSerializer


class DepartmentSalaryListCreateAPI(_CompanyScopedListCreateAPI):
    model = DepartmentSalaryConfig
    serializer_class = DepartmentSalaryConfigSerializer

    def get_queryset(self, request):
        rows = super().get_queryset(request).select_related("department")
        month, error = _requested_month(request)
        if error is None and request.query_params.get("month"):
            rows = rows.filter(month=month)
        return rows.order_by("-month", "department__name")


class DepartmentSalaryDetailAPI(_CompanyScopedDetailAPI):
    model = DepartmentSalaryConfig
    serializer_class = DepartmentSalaryConfigSerializer


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
