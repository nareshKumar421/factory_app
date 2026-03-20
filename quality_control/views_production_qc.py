# quality_control/views_production_qc.py

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from django.shortcuts import get_object_or_404

from company.permissions import HasCompanyContext
from production_execution.models import ProductionRun

from .models import (
    MaterialType,
    QCParameterMaster,
    ProductionQCSession,
    ProductionQCResult,
)
from .models.production_qc_session import (
    ProductionQCSessionType,
    ProductionQCWorkflowStatus,
)
from .serializers import (
    ProductionQCSessionSerializer,
    ProductionQCSessionListSerializer,
    ProductionQCSessionCreateSerializer,
    ProductionQCResultBulkUpdateSerializer,
    ProductionQCSubmitSerializer,
)
from .permissions import (
    CanViewProductionQC,
    CanCreateProductionQC,
    CanSubmitProductionQC,
)


def _get_company(request):
    return request.company.company


# ===========================================================================
# Production QC Sessions — List & Create
# ===========================================================================

class ProductionQCSessionListCreateAPI(APIView):
    """
    GET  - List all QC sessions for a production run.
    POST - Create a new QC session (auto-populates parameters from material type).
    """
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewProductionQC]

    def get(self, request, run_id):
        company = _get_company(request)
        run = get_object_or_404(ProductionRun, id=run_id, company=company)
        sessions = run.qc_sessions.filter(is_active=True).select_related(
            "material_type", "checked_by", "submitted_by", "production_run__line"
        ).prefetch_related("results__parameter_master")

        session_type = request.GET.get("session_type")
        if session_type:
            sessions = sessions.filter(session_type=session_type)

        serializer = ProductionQCSessionListSerializer(sessions, many=True)
        return Response(serializer.data)

    def post(self, request, run_id):
        if not request.user.has_perm("quality_control.can_create_production_qc"):
            return Response(
                {"detail": "You do not have permission to create QC sessions."},
                status=status.HTTP_403_FORBIDDEN
            )

        company = _get_company(request)
        run = get_object_or_404(ProductionRun, id=run_id, company=company)

        serializer = ProductionQCSessionCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                {"detail": "Invalid data.", "errors": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST
            )

        data = serializer.validated_data
        material_type_id = data["material_type_id"]
        session_type = data["session_type"]

        material_type = get_object_or_404(
            MaterialType, id=material_type_id, company=company, is_active=True
        )

        # For FINAL sessions, only allow one per run
        if session_type == ProductionQCSessionType.FINAL:
            existing = run.qc_sessions.filter(
                session_type=ProductionQCSessionType.FINAL, is_active=True
            )
            if existing.exists():
                return Response(
                    {"detail": "A Final QC session already exists for this run."},
                    status=status.HTTP_400_BAD_REQUEST
                )

        # Auto-calculate session number
        last_session = run.qc_sessions.filter(is_active=True).order_by(
            "-session_number"
        ).first()
        session_number = (last_session.session_number + 1) if last_session else 1

        # Create the session
        session = ProductionQCSession.objects.create(
            production_run=run,
            material_type=material_type,
            session_number=session_number,
            session_type=session_type,
            checked_at=data["checked_at"],
            checked_by=request.user,
            remarks=data.get("remarks", ""),
            created_by=request.user,
        )

        # Auto-populate parameter results from material type's QC parameters
        parameters = QCParameterMaster.objects.filter(
            material_type=material_type, is_active=True
        ).order_by("sequence")

        results_to_create = []
        for param in parameters:
            results_to_create.append(ProductionQCResult(
                session=session,
                parameter_master=param,
                parameter_name=param.parameter_name,
                standard_value=param.standard_value,
                created_by=request.user,
            ))
        ProductionQCResult.objects.bulk_create(results_to_create)

        # Return full session with results
        session.refresh_from_db()
        response_serializer = ProductionQCSessionSerializer(session)
        return Response(response_serializer.data, status=status.HTTP_201_CREATED)


# ===========================================================================
# Production QC Session Detail
# ===========================================================================

class ProductionQCSessionDetailAPI(APIView):
    """
    GET    - Get session detail with all parameter results.
    DELETE - Soft-delete a session (only if DRAFT).
    """
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewProductionQC]

    def get(self, request, session_id):
        company = _get_company(request)
        session = get_object_or_404(
            ProductionQCSession,
            id=session_id,
            production_run__company=company,
            is_active=True,
        )
        serializer = ProductionQCSessionSerializer(session)
        return Response(serializer.data)

    def delete(self, request, session_id):
        company = _get_company(request)
        session = get_object_or_404(
            ProductionQCSession,
            id=session_id,
            production_run__company=company,
            is_active=True,
        )
        if session.workflow_status != ProductionQCWorkflowStatus.DRAFT:
            return Response(
                {"detail": "Only DRAFT sessions can be deleted."},
                status=status.HTTP_400_BAD_REQUEST
            )
        session.is_active = False
        session.save()
        return Response(status=status.HTTP_204_NO_CONTENT)


# ===========================================================================
# Production QC Parameter Results — Bulk Update
# ===========================================================================

class ProductionQCResultsAPI(APIView):
    """
    GET  - Get parameter results for a session.
    POST - Bulk update parameter results for a session.
    """
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewProductionQC]

    def get(self, request, session_id):
        company = _get_company(request)
        session = get_object_or_404(
            ProductionQCSession,
            id=session_id,
            production_run__company=company,
            is_active=True,
        )
        from .serializers import ProductionQCResultSerializer
        results = session.results.filter(is_active=True).select_related("parameter_master")
        serializer = ProductionQCResultSerializer(results, many=True)
        return Response(serializer.data)

    def post(self, request, session_id):
        if not request.user.has_perm("quality_control.can_create_production_qc"):
            return Response(
                {"detail": "You do not have permission to update QC results."},
                status=status.HTTP_403_FORBIDDEN
            )

        company = _get_company(request)
        session = get_object_or_404(
            ProductionQCSession,
            id=session_id,
            production_run__company=company,
            is_active=True,
        )

        if session.workflow_status != ProductionQCWorkflowStatus.DRAFT:
            return Response(
                {"detail": "Cannot update results for submitted sessions."},
                status=status.HTTP_400_BAD_REQUEST
            )

        serializer = ProductionQCResultBulkUpdateSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                {"detail": "Invalid data.", "errors": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST
            )

        for result_data in serializer.validated_data["results"]:
            param_id = result_data["parameter_master_id"]
            try:
                result = session.results.get(
                    parameter_master_id=param_id, is_active=True
                )
            except ProductionQCResult.DoesNotExist:
                continue

            result.result_value = result_data.get("result_value", result.result_value)
            result.result_numeric = result_data.get("result_numeric", result.result_numeric)
            if "is_within_spec" in result_data and result_data["is_within_spec"] is not None:
                result.is_within_spec = result_data["is_within_spec"]
            result.remarks = result_data.get("remarks", result.remarks)
            result.updated_by = request.user
            result.save()

        response_serializer = ProductionQCSessionSerializer(session)
        return Response(response_serializer.data)


# ===========================================================================
# Production QC Submit (finalize with PASS/FAIL)
# ===========================================================================

class ProductionQCSubmitAPI(APIView):
    """Submit and finalize a QC session with PASS/FAIL result."""
    permission_classes = [IsAuthenticated, HasCompanyContext, CanSubmitProductionQC]

    def post(self, request, session_id):
        company = _get_company(request)
        session = get_object_or_404(
            ProductionQCSession,
            id=session_id,
            production_run__company=company,
            is_active=True,
        )

        ser = ProductionQCSubmitSerializer(data=request.data)
        if not ser.is_valid():
            return Response(
                {"detail": "Invalid data.", "errors": ser.errors},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Validate all mandatory parameters have values
        mandatory_empty = session.results.filter(
            parameter_master__is_mandatory=True,
            is_active=True,
            result_value="",
            result_numeric__isnull=True,
        )
        if mandatory_empty.exists():
            params = list(mandatory_empty.values_list("parameter_name", flat=True))
            return Response(
                {"detail": f"Mandatory parameters missing values: {', '.join(params)}"},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            session.submit(
                user=request.user,
                overall_result=ser.validated_data["overall_result"],
            )
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        serializer = ProductionQCSessionSerializer(session)
        return Response(serializer.data)


# ===========================================================================
# Production QC Dashboard / Listing Endpoints
# ===========================================================================

class ProductionQCAllListAPI(APIView):
    """List all production QC sessions with filters."""
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewProductionQC]

    def get(self, request):
        company = _get_company(request)
        sessions = ProductionQCSession.objects.filter(
            production_run__company=company,
            is_active=True,
        ).select_related(
            "material_type", "checked_by", "production_run__line"
        ).prefetch_related("results")

        # Filters
        workflow_status = request.GET.get("workflow_status")
        if workflow_status:
            sessions = sessions.filter(workflow_status=workflow_status)

        session_type = request.GET.get("session_type")
        if session_type:
            sessions = sessions.filter(session_type=session_type)

        run_id = request.GET.get("run_id")
        if run_id:
            sessions = sessions.filter(production_run_id=run_id)

        line_id = request.GET.get("line")
        if line_id:
            sessions = sessions.filter(production_run__line_id=line_id)

        date_from = request.GET.get("date_from")
        if date_from:
            sessions = sessions.filter(production_run__date__gte=date_from)

        date_to = request.GET.get("date_to")
        if date_to:
            sessions = sessions.filter(production_run__date__lte=date_to)

        serializer = ProductionQCSessionListSerializer(sessions, many=True)
        return Response(serializer.data)


class ProductionQCCountsAPI(APIView):
    """Get counts for production QC dashboard."""
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewProductionQC]

    def get(self, request):
        company = _get_company(request)
        qs = ProductionQCSession.objects.filter(
            production_run__company=company,
            is_active=True,
        )

        return Response({
            "draft": qs.filter(workflow_status=ProductionQCWorkflowStatus.DRAFT).count(),
            "submitted": qs.filter(workflow_status=ProductionQCWorkflowStatus.SUBMITTED).count(),
        })
