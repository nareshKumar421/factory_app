# quality_control/views_production_qc.py

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from django.shortcuts import get_object_or_404
from django.db import transaction
from django.utils import timezone

from company.permissions import HasCompanyContext
from production_execution.models import ProductionRun, RunStatus

from .models import (
    MaterialType,
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
    ProductionQCRunningRunSerializer,
    ProductionQCSessionCreateSerializer,
    ProductionQCResultBulkUpdateSerializer,
    ProductionQCSubmitSerializer,
    ProductionQCApprovalSerializer,
    ProductionQCRejectSerializer,
)
from .permissions import (
    CanViewProductionQC,
    CanCreateProductionQC,
    CanSubmitProductionQC,
    CanApproveProductionQC,
)
from .services import parameter_sets as parameter_set_services


def _get_company(request):
    return request.company.company


def _next_session_number(run):
    last_session = run.qc_sessions.filter(is_active=True).order_by(
        "-session_number"
    ).first()
    return (last_session.session_number + 1) if last_session else 1


def _populate_session_results(session, material_type, user):
    update_fields = {"is_active": False}
    if getattr(user, "pk", None):
        update_fields["updated_by_id"] = user.pk
    session.results.filter(is_active=True).update(**update_fields)

    # Production has no vendor, so it always runs against the material type's
    # default parameter set rather than any vendor-specific one.
    parameter_set = parameter_set_services.default_parameter_set(material_type)
    if parameter_set is None:
        return

    parameters = parameter_set.parameters.filter(is_active=True).order_by("sequence")

    rows = []
    for param in parameters:
        row = ProductionQCResult(
            session=session,
            parameter_master=param,
            created_by=user,
        )
        row.apply_parameter_snapshot(param)
        rows.append(row)

    ProductionQCResult.objects.bulk_create(rows)


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

        with transaction.atomic():
            # For FINAL sessions, only allow one per run. If production already
            # requested FG QC, QC can attach the parameter set to that draft.
            if session_type == ProductionQCSessionType.FINAL:
                existing = run.qc_sessions.select_for_update().filter(
                    session_type=ProductionQCSessionType.FINAL,
                    is_active=True,
                ).order_by("-created_at").first()
                if existing:
                    if (
                        existing.workflow_status == ProductionQCWorkflowStatus.DRAFT
                        and existing.material_type_id is None
                    ):
                        existing.material_type = material_type
                        existing.checked_at = data["checked_at"]
                        existing.checked_by = request.user
                        existing.remarks = data.get("remarks", existing.remarks)
                        existing.updated_by = request.user
                        existing.save(update_fields=[
                            "material_type", "checked_at", "checked_by",
                            "remarks", "updated_by", "updated_at",
                        ])
                        _populate_session_results(existing, material_type, request.user)
                        existing.refresh_from_db()
                        response_serializer = ProductionQCSessionSerializer(existing)
                        return Response(response_serializer.data)

                    return Response(
                        {"detail": "A Final QC session already exists for this run."},
                        status=status.HTTP_400_BAD_REQUEST
                    )

            session = ProductionQCSession.objects.create(
                production_run=run,
                material_type=material_type,
                session_number=_next_session_number(run),
                session_type=session_type,
                checked_at=data["checked_at"],
                checked_by=request.user,
                remarks=data.get("remarks", ""),
                created_by=request.user,
            )
            _populate_session_results(session, material_type, request.user)
            session.refresh_from_db()
        response_serializer = ProductionQCSessionSerializer(session)
        return Response(response_serializer.data, status=status.HTTP_201_CREATED)


class ProductionQCFinalRequestAPI(APIView):
    """
    Production requests final FG QC approval.

    This only creates a draft Final QC shell. QC owns parameter selection,
    result entry, submission, and approval.
    """
    permission_classes = [IsAuthenticated, HasCompanyContext]

    def post(self, request, run_id):
        if not (
            request.user.has_perm("production_execution.can_edit_production_run")
            or request.user.has_perm("production_execution.can_complete_production_run")
        ):
            return Response(
                {"detail": "You do not have permission to request FG QC approval."},
                status=status.HTTP_403_FORBIDDEN
            )

        company = _get_company(request)
        with transaction.atomic():
            run = get_object_or_404(
                ProductionRun.objects.select_for_update(),
                id=run_id,
                company=company,
            )

            if run.status != RunStatus.COMPLETED:
                return Response(
                    {"detail": "FG QC approval can only be requested for completed runs."},
                    status=status.HTTP_400_BAD_REQUEST
                )

            existing = ProductionQCSession.objects.select_for_update().filter(
                production_run=run,
                session_type=ProductionQCSessionType.FINAL,
                is_active=True,
            ).order_by("-created_at").first()
            if existing:
                serializer = ProductionQCSessionSerializer(existing)
                return Response(serializer.data)

            session = ProductionQCSession.objects.create(
                production_run=run,
                material_type=None,
                session_number=_next_session_number(run),
                session_type=ProductionQCSessionType.FINAL,
                checked_at=timezone.now(),
                checked_by=None,
                remarks="FG QC approval requested by production.",
                created_by=request.user,
            )

        serializer = ProductionQCSessionSerializer(session)
        return Response(serializer.data, status=status.HTTP_201_CREATED)


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
        if not request.user.has_perm("quality_control.can_create_production_qc"):
            return Response(
                {"detail": "You do not have permission to delete QC sessions."},
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

        if not session.material_type_id or not session.results.filter(is_active=True).exists():
            return Response(
                {"detail": "Select QC parameters before submitting this session."},
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
# Production QC Approval
# ===========================================================================

class ProductionQCPendingAPI(APIView):
    """List submitted production QC sessions awaiting approval."""
    permission_classes = [IsAuthenticated, HasCompanyContext, CanApproveProductionQC]

    def get(self, request):
        company = _get_company(request)
        sessions = ProductionQCSession.objects.filter(
            production_run__company=company,
            workflow_status=ProductionQCWorkflowStatus.SUBMITTED,
            is_active=True,
        ).select_related(
            "material_type", "checked_by", "production_run__line"
        ).prefetch_related("results")

        serializer = ProductionQCSessionListSerializer(sessions, many=True)
        return Response(serializer.data)


class ProductionQCApproveAPI(APIView):
    """Approve a submitted production QC session."""
    permission_classes = [IsAuthenticated, HasCompanyContext, CanApproveProductionQC]

    def post(self, request, session_id):
        ser = ProductionQCApprovalSerializer(data=request.data)
        if not ser.is_valid():
            return Response(
                {"detail": "Invalid data.", "errors": ser.errors},
                status=status.HTTP_400_BAD_REQUEST
            )

        company = _get_company(request)
        with transaction.atomic():
            session = get_object_or_404(
                ProductionQCSession.objects.select_for_update(),
                id=session_id,
                production_run__company=company,
                is_active=True,
            )
            try:
                session.approve(
                    user=request.user,
                    overall_result=ser.validated_data.get("overall_result"),
                    remarks=ser.validated_data.get("remarks", ""),
                )
            except ValueError as e:
                return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        serializer = ProductionQCSessionSerializer(session)
        return Response(serializer.data)


class ProductionQCRejectAPI(APIView):
    """Reject a submitted production QC session."""
    permission_classes = [IsAuthenticated, HasCompanyContext, CanApproveProductionQC]

    def post(self, request, session_id):
        ser = ProductionQCRejectSerializer(data=request.data)
        if not ser.is_valid():
            return Response(
                {"detail": "Invalid data.", "errors": ser.errors},
                status=status.HTTP_400_BAD_REQUEST
            )

        company = _get_company(request)
        with transaction.atomic():
            session = get_object_or_404(
                ProductionQCSession.objects.select_for_update(),
                id=session_id,
                production_run__company=company,
                is_active=True,
            )
            try:
                session.reject(
                    user=request.user,
                    remarks=ser.validated_data.get("remarks", ""),
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

        sessions = sessions.order_by("-created_at", "-id")

        serializer = ProductionQCSessionListSerializer(sessions, many=True)
        return Response(serializer.data)


class ProductionQCRunningRunsAPI(APIView):
    """List currently-running production runs a QC user can select to do QC on.

    GET production-qc/running-runs/?line=<line_id>  (line filter optional)
    Returns IN_PROGRESS runs for the active company, each with its line, product
    and a hint of QC progress so the operator can pick a running line and go
    straight to its QC page.
    """
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewProductionQC]

    def get(self, request):
        company = _get_company(request)
        runs = (
            ProductionRun.objects.filter(
                company=company, status=RunStatus.IN_PROGRESS,
            )
            .select_related("line")
            .prefetch_related("qc_sessions")
        )
        line_id = request.GET.get("line")
        if line_id:
            runs = runs.filter(line_id=line_id)
        runs = runs.order_by("line__name", "-date", "-run_number")

        serializer = ProductionQCRunningRunSerializer(runs, many=True)
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
            "approved": qs.filter(workflow_status=ProductionQCWorkflowStatus.APPROVED).count(),
            "rejected": qs.filter(workflow_status=ProductionQCWorkflowStatus.REJECTED).count(),
        })
