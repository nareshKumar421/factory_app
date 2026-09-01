# quality_control/views_testing_procedure.py
"""APIs for the QC "Documents" screen -- controlled testing procedures."""

from django.db.models import Count, Q
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from company.permissions import HasCompanyContext

from .models import ProcedureType, TestingProcedure
from .serializers_testing_procedure import (
    TestingProcedureListSerializer,
    TestingProcedureSerializer,
)


class CanViewTestingProcedures(BasePermission):
    """Read a controlled testing procedure."""

    def has_permission(self, request, view):
        return request.user.has_perm("quality_control.can_view_testing_procedures")


class CanManageTestingProcedures(BasePermission):
    """Create, edit or retire a controlled testing procedure."""

    def has_permission(self, request, view):
        return request.user.has_perm("quality_control.can_manage_testing_procedures")


def _company(request):
    return request.company.company


def _base_queryset(company):
    return TestingProcedure.objects.filter(company=company, is_active=True)


class TestingProcedureListCreateAPI(APIView):
    """GET the procedure list (filterable) - POST a newly analysed procedure."""

    def get_permissions(self):
        if self.request.method == "POST":
            return [IsAuthenticated(), HasCompanyContext(), CanManageTestingProcedures()]
        return [IsAuthenticated(), HasCompanyContext(), CanViewTestingProcedures()]

    def get(self, request):
        queryset = _base_queryset(_company(request)).annotate(
            section_count=Count("sections", distinct=True),
            line_count=Count("sections__lines", distinct=True),
        )

        procedure_type = request.query_params.get("procedure_type")
        if procedure_type in ProcedureType.values:
            queryset = queryset.filter(procedure_type=procedure_type)

        status_filter = request.query_params.get("status")
        if status_filter:
            queryset = queryset.filter(status=status_filter)

        search = (request.query_params.get("search") or "").strip()
        if search:
            queryset = queryset.filter(
                Q(title__icontains=search) | Q(document_code__icontains=search)
            )

        return Response(TestingProcedureListSerializer(queryset, many=True).data)

    def post(self, request):
        company = _company(request)
        serializer = TestingProcedureSerializer(
            data=request.data, context={"company": company, "request": request}
        )
        serializer.is_valid(raise_exception=True)
        procedure = serializer.save(
            company=company, created_by=request.user, updated_by=request.user
        )
        return Response(
            TestingProcedureSerializer(procedure).data, status=status.HTTP_201_CREATED
        )


class TestingProcedureDetailAPI(APIView):
    """GET / PUT / DELETE one procedure, sections and lines included."""

    def get_permissions(self):
        if self.request.method == "GET":
            return [IsAuthenticated(), HasCompanyContext(), CanViewTestingProcedures()]
        return [IsAuthenticated(), HasCompanyContext(), CanManageTestingProcedures()]

    def _get_object(self, request, procedure_id):
        return get_object_or_404(
            _base_queryset(_company(request)).prefetch_related("sections__lines"),
            id=procedure_id,
        )

    def get(self, request, procedure_id):
        procedure = self._get_object(request, procedure_id)
        return Response(TestingProcedureSerializer(procedure).data)

    def put(self, request, procedure_id):
        procedure = self._get_object(request, procedure_id)
        company = _company(request)
        serializer = TestingProcedureSerializer(
            procedure,
            data=request.data,
            partial=True,
            context={"company": company, "request": request},
        )
        serializer.is_valid(raise_exception=True)
        procedure = serializer.save(updated_by=request.user)
        return Response(TestingProcedureSerializer(procedure).data)

    def delete(self, request, procedure_id):
        """Retire a procedure. Soft delete -- a controlled document is never
        erased, so the record stays for traceability and drops out of lists."""
        procedure = self._get_object(request, procedure_id)
        procedure.is_active = False
        procedure.updated_by = request.user
        procedure.save(update_fields=["is_active", "updated_by", "updated_at"])
        return Response(status=status.HTTP_204_NO_CONTENT)


class TestingProcedureCountsAPI(APIView):
    """Counts per procedure type, for the tab badges on the Documents page."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewTestingProcedures]

    def get(self, request):
        queryset = _base_queryset(_company(request))
        return Response(
            {
                "total": queryset.count(),
                "inhouse": queryset.filter(
                    procedure_type=ProcedureType.INHOUSE
                ).count(),
                "standard": queryset.filter(
                    procedure_type=ProcedureType.STANDARD
                ).count(),
            }
        )
