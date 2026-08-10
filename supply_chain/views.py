"""Supply-chain API.

One dashboard endpoint (the brief's "single brain"), three reference-data
endpoints, a template upload, and the policy that settles the brief's open
questions.
"""
import logging

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from company.permissions import HasCompanyContext

from .models import (
    MachineCapacity,
    MaterialLeadTime,
    MaterialMachineMap,
    ReferenceImport,
    SupplyChainPolicy,
)
from .permissions import CanManageSupplyChainReference, CanViewSupplyChain
from .serializers import (
    DashboardQuerySerializer,
    MachineCapacitySerializer,
    MaterialLeadTimeSerializer,
    MaterialMachineMapSerializer,
    ReferenceImportSerializer,
    SupplyChainPolicySerializer,
)
from .services import SupplyChainError
from .services import planning as planning_service
from .services.template_import import import_reference_workbook

logger = logging.getLogger(__name__)


def _company_code(request):
    return request.company.company.code


class SupplyChainBaseView(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewSupplyChain]

    def handle_exception(self, exc):
        if isinstance(exc, SupplyChainError):
            return Response(
                {"detail": exc.message, "code": exc.code}, status=exc.status_code
            )
        return super().handle_exception(exc)


class SupplyChainDashboardAPI(SupplyChainBaseView):
    """Steps 6 + 7 in one payload: what to order today, and whether the plan runs."""

    def get(self, request):
        query = DashboardQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        return Response(planning_service.dashboard(
            _company_code(request),
            forecast_id=query.validated_data.get("forecast_id"),
        ))


class ProcurementAlarmsAPI(SupplyChainBaseView):
    """Step 6 alone — the procurement action list, most urgent first."""

    def get(self, request):
        query = DashboardQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        return Response(planning_service.material_alarms(
            _company_code(request),
            forecast_id=query.validated_data.get("forecast_id"),
        ))


class CapacityCheckAPI(SupplyChainBaseView):
    """Step 7 alone — per-line feasibility of the plan."""

    def get(self, request):
        query = DashboardQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        return Response(planning_service.capacity_check(
            _company_code(request),
            forecast_id=query.validated_data.get("forecast_id"),
        ))


class LeadTimeListAPI(SupplyChainBaseView):
    def get(self, request):
        qs = MaterialLeadTime.objects.filter(company_code=_company_code(request))
        return Response(MaterialLeadTimeSerializer(qs, many=True).data)


class MachineCapacityListAPI(SupplyChainBaseView):
    def get(self, request):
        qs = MachineCapacity.objects.filter(company_code=_company_code(request))
        return Response(MachineCapacitySerializer(qs, many=True).data)


class MaterialMachineMapListAPI(SupplyChainBaseView):
    def get(self, request):
        qs = MaterialMachineMap.objects.filter(company_code=_company_code(request))
        return Response(MaterialMachineMapSerializer(qs, many=True).data)


class SupplyChainPolicyAPI(SupplyChainBaseView):
    """Read the policy with GET; changing it needs the reference-data permission."""

    def get(self, request):
        policy = SupplyChainPolicy.for_company(_company_code(request))
        return Response(SupplyChainPolicySerializer(policy).data)

    def put(self, request):
        for permission in (CanManageSupplyChainReference(),):
            if not permission.has_permission(request, self):
                return Response(
                    {"detail": "You cannot change the supply chain policy.",
                     "code": "FORBIDDEN"},
                    status=status.HTTP_403_FORBIDDEN,
                )
        company_code = _company_code(request)
        policy, _ = SupplyChainPolicy.objects.get_or_create(company_code=company_code)
        serializer = SupplyChainPolicySerializer(policy, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


class ReferenceTemplateUploadAPI(APIView):
    """Upload the JIVO Supply Chain Reference Template workbook."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanManageSupplyChainReference]

    def handle_exception(self, exc):
        if isinstance(exc, SupplyChainError):
            return Response(
                {"detail": exc.message, "code": exc.code}, status=exc.status_code
            )
        return super().handle_exception(exc)

    def post(self, request):
        upload = request.FILES.get("file")
        if upload is None:
            return Response(
                {"detail": "Attach the reference template as 'file'.", "code": "NO_FILE"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        record = import_reference_workbook(
            _company_code(request), upload.read(),
            filename=getattr(upload, "name", ""), user=request.user,
        )
        return Response(
            ReferenceImportSerializer(record).data, status=status.HTTP_201_CREATED
        )


class ReferenceImportHistoryAPI(SupplyChainBaseView):
    def get(self, request):
        qs = ReferenceImport.objects.filter(company_code=_company_code(request))[:25]
        return Response(ReferenceImportSerializer(qs, many=True).data)
