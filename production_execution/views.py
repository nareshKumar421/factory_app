import logging
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated

from company.permissions import HasCompanyContext
from .services import ProductionExecutionService
from .serializers import (
    # Master Data
    ProductionLineSerializer, ProductionLineCreateSerializer,
    MachineSerializer, MachineCreateSerializer,
    ChecklistTemplateSerializer, ChecklistTemplateCreateSerializer,
    # Production Runs
    ProductionRunCreateSerializer, ProductionRunUpdateSerializer,
    ProductionRunListSerializer, ProductionRunDetailSerializer,
    # Logs
    ProductionLogSerializer, ProductionLogCreateSerializer,
    # Breakdowns
    MachineBreakdownSerializer, MachineBreakdownCreateSerializer,
    # Materials
    MaterialUsageSerializer, MaterialUsageCreateSerializer,
    # Machine Runtime
    MachineRuntimeSerializer, MachineRuntimeCreateSerializer,
    # Manpower
    ManpowerSerializer, ManpowerCreateSerializer,
    # Line Clearance
    LineClearanceListSerializer, LineClearanceDetailSerializer,
    LineClearanceCreateSerializer, LineClearanceUpdateSerializer,
    # Machine Checklists
    MachineChecklistEntrySerializer, MachineChecklistCreateSerializer,
    # Waste
    WasteLogSerializer, WasteLogCreateSerializer, WasteApprovalSerializer,
)
from .permissions import (
    CanManageProductionLines, CanManageMachines, CanManageChecklistTemplates,
    CanViewProductionRun, CanCreateProductionRun, CanEditProductionRun,
    CanCompleteProductionRun,
    CanViewProductionLog, CanEditProductionLog,
    CanViewBreakdown, CanCreateBreakdown, CanEditBreakdown,
    CanViewMaterialUsage, CanCreateMaterialUsage, CanEditMaterialUsage,
    CanViewMachineRuntime, CanCreateMachineRuntime,
    CanViewManpower, CanCreateManpower,
    CanViewLineClearance, CanCreateLineClearance, CanApproveLineClearanceQA,
    CanViewMachineChecklist, CanCreateMachineChecklist,
    CanViewWasteLog, CanCreateWasteLog,
    CanApproveWasteEngineer, CanApproveWasteAM,
    CanApproveWasteStore, CanApproveWasteHOD,
    CanViewReports,
)

logger = logging.getLogger(__name__)


def _get_service(request):
    return ProductionExecutionService(request.company.company.code)


# ===========================================================================
# MASTER DATA — Production Lines
# ===========================================================================

class LineListCreateAPI(APIView):
    def get_permissions(self):
        if self.request.method == 'GET':
            return [IsAuthenticated(), HasCompanyContext(), CanViewProductionRun()]
        return [IsAuthenticated(), HasCompanyContext(), CanManageProductionLines()]

    def get(self, request):
        service = _get_service(request)
        is_active = request.GET.get('is_active')
        if is_active is not None:
            is_active = is_active.lower() == 'true'
        lines = service.list_lines(is_active=is_active)
        return Response(ProductionLineSerializer(lines, many=True).data)

    def post(self, request):
        serializer = ProductionLineCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                {"detail": "Invalid data.", "errors": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST
            )
        service = _get_service(request)
        try:
            line = service.create_line(serializer.validated_data)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(ProductionLineSerializer(line).data, status=status.HTTP_201_CREATED)


class LineDetailAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanManageProductionLines]

    def patch(self, request, line_id):
        service = _get_service(request)
        try:
            line = service.update_line(line_id, request.data)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(ProductionLineSerializer(line).data)

    def delete(self, request, line_id):
        service = _get_service(request)
        try:
            service.delete_line(line_id)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(status=status.HTTP_204_NO_CONTENT)


# ===========================================================================
# MASTER DATA — Machines
# ===========================================================================

class MachineListCreateAPI(APIView):
    def get_permissions(self):
        if self.request.method == 'GET':
            return [IsAuthenticated(), HasCompanyContext(), CanViewProductionRun()]
        return [IsAuthenticated(), HasCompanyContext(), CanManageMachines()]

    def get(self, request):
        service = _get_service(request)
        machines = service.list_machines(
            line_id=request.GET.get('line_id'),
            machine_type=request.GET.get('machine_type'),
            is_active=request.GET.get('is_active'),
        )
        return Response(MachineSerializer(machines, many=True).data)

    def post(self, request):
        serializer = MachineCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                {"detail": "Invalid data.", "errors": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST
            )
        service = _get_service(request)
        try:
            machine = service.create_machine(serializer.validated_data)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(MachineSerializer(machine).data, status=status.HTTP_201_CREATED)


class MachineDetailAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanManageMachines]

    def patch(self, request, machine_id):
        service = _get_service(request)
        try:
            machine = service.update_machine(machine_id, request.data)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(MachineSerializer(machine).data)

    def delete(self, request, machine_id):
        service = _get_service(request)
        try:
            service.delete_machine(machine_id)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(status=status.HTTP_204_NO_CONTENT)


# ===========================================================================
# MASTER DATA — Checklist Templates
# ===========================================================================

class ChecklistTemplateListCreateAPI(APIView):
    def get_permissions(self):
        if self.request.method == 'GET':
            return [IsAuthenticated(), HasCompanyContext(), CanViewMachineChecklist()]
        return [IsAuthenticated(), HasCompanyContext(), CanManageChecklistTemplates()]

    def get(self, request):
        service = _get_service(request)
        templates = service.list_checklist_templates(
            machine_type=request.GET.get('machine_type'),
            frequency=request.GET.get('frequency'),
        )
        return Response(ChecklistTemplateSerializer(templates, many=True).data)

    def post(self, request):
        serializer = ChecklistTemplateCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                {"detail": "Invalid data.", "errors": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST
            )
        service = _get_service(request)
        try:
            template = service.create_checklist_template(serializer.validated_data)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(
            ChecklistTemplateSerializer(template).data,
            status=status.HTTP_201_CREATED
        )


class ChecklistTemplateDetailAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanManageChecklistTemplates]

    def patch(self, request, template_id):
        service = _get_service(request)
        try:
            template = service.update_checklist_template(template_id, request.data)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(ChecklistTemplateSerializer(template).data)

    def delete(self, request, template_id):
        service = _get_service(request)
        try:
            service.delete_checklist_template(template_id)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(status=status.HTTP_204_NO_CONTENT)


# ===========================================================================
# PRODUCTION RUNS
# ===========================================================================

class RunListCreateAPI(APIView):
    def get_permissions(self):
        if self.request.method == 'GET':
            return [IsAuthenticated(), HasCompanyContext(), CanViewProductionRun()]
        return [IsAuthenticated(), HasCompanyContext(), CanCreateProductionRun()]

    def get(self, request):
        service = _get_service(request)
        runs = service.list_runs(
            date=request.GET.get('date'),
            line_id=request.GET.get('line_id'),
            status=request.GET.get('status'),
            production_plan_id=request.GET.get('production_plan_id'),
        )
        return Response(ProductionRunListSerializer(runs, many=True).data)

    def post(self, request):
        serializer = ProductionRunCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                {"detail": "Invalid data.", "errors": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST
            )
        service = _get_service(request)
        try:
            run = service.create_run(serializer.validated_data, user=request.user)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(
            ProductionRunDetailSerializer(run).data,
            status=status.HTTP_201_CREATED
        )


class RunDetailAPI(APIView):
    def get_permissions(self):
        if self.request.method == 'GET':
            return [IsAuthenticated(), HasCompanyContext(), CanViewProductionRun()]
        return [IsAuthenticated(), HasCompanyContext(), CanEditProductionRun()]

    def get(self, request, run_id):
        service = _get_service(request)
        try:
            run = service.get_run(run_id)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_404_NOT_FOUND)
        return Response(ProductionRunDetailSerializer(run).data)

    def patch(self, request, run_id):
        serializer = ProductionRunUpdateSerializer(data=request.data, partial=True)
        if not serializer.is_valid():
            return Response(
                {"detail": "Invalid data.", "errors": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST
            )
        service = _get_service(request)
        try:
            run = service.update_run(run_id, serializer.validated_data)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(ProductionRunDetailSerializer(run).data)


class CompleteRunAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanCompleteProductionRun]

    def post(self, request, run_id):
        service = _get_service(request)
        try:
            run = service.complete_run(run_id)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(ProductionRunDetailSerializer(run).data)


# ===========================================================================
# HOURLY PRODUCTION LOGS
# ===========================================================================

class RunLogListCreateAPI(APIView):
    def get_permissions(self):
        if self.request.method == 'GET':
            return [IsAuthenticated(), HasCompanyContext(), CanViewProductionLog()]
        return [IsAuthenticated(), HasCompanyContext(), CanEditProductionLog()]

    def get(self, request, run_id):
        service = _get_service(request)
        try:
            logs = service.get_run_logs(run_id)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_404_NOT_FOUND)
        return Response(ProductionLogSerializer(logs, many=True).data)

    def post(self, request, run_id):
        data = request.data
        if not isinstance(data, list):
            data = [data]

        serializer = ProductionLogCreateSerializer(data=data, many=True)
        if not serializer.is_valid():
            return Response(
                {"detail": "Invalid data.", "errors": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST
            )

        service = _get_service(request)
        try:
            logs = service.save_hourly_logs(run_id, serializer.validated_data)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(
            ProductionLogSerializer(logs, many=True).data,
            status=status.HTTP_201_CREATED
        )


class RunLogDetailAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanEditProductionLog]

    def patch(self, request, run_id, log_id):
        service = _get_service(request)
        try:
            log = service.update_log(run_id, log_id, request.data)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(ProductionLogSerializer(log).data)


# ===========================================================================
# MACHINE BREAKDOWNS
# ===========================================================================

class BreakdownListCreateAPI(APIView):
    def get_permissions(self):
        if self.request.method == 'GET':
            return [IsAuthenticated(), HasCompanyContext(), CanViewBreakdown()]
        return [IsAuthenticated(), HasCompanyContext(), CanCreateBreakdown()]

    def get(self, request, run_id):
        service = _get_service(request)
        try:
            breakdowns = service.get_run_breakdowns(run_id)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_404_NOT_FOUND)
        return Response(MachineBreakdownSerializer(breakdowns, many=True).data)

    def post(self, request, run_id):
        serializer = MachineBreakdownCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                {"detail": "Invalid data.", "errors": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST
            )
        service = _get_service(request)
        try:
            breakdown = service.add_breakdown(run_id, serializer.validated_data)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(
            MachineBreakdownSerializer(breakdown).data,
            status=status.HTTP_201_CREATED
        )


class BreakdownDetailAPI(APIView):
    def get_permissions(self):
        if self.request.method == 'DELETE':
            return [IsAuthenticated(), HasCompanyContext(), CanEditBreakdown()]
        return [IsAuthenticated(), HasCompanyContext(), CanEditBreakdown()]

    def patch(self, request, run_id, breakdown_id):
        service = _get_service(request)
        try:
            breakdown = service.update_breakdown(run_id, breakdown_id, request.data)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(MachineBreakdownSerializer(breakdown).data)

    def delete(self, request, run_id, breakdown_id):
        service = _get_service(request)
        try:
            service.delete_breakdown(run_id, breakdown_id)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(status=status.HTTP_204_NO_CONTENT)


# ===========================================================================
# MATERIAL USAGE
# ===========================================================================

class MaterialUsageListCreateAPI(APIView):
    def get_permissions(self):
        if self.request.method == 'GET':
            return [IsAuthenticated(), HasCompanyContext(), CanViewMaterialUsage()]
        return [IsAuthenticated(), HasCompanyContext(), CanCreateMaterialUsage()]

    def get(self, request, run_id):
        service = _get_service(request)
        try:
            materials = service.get_run_materials(
                run_id, batch_number=request.GET.get('batch_number')
            )
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_404_NOT_FOUND)
        return Response(MaterialUsageSerializer(materials, many=True).data)

    def post(self, request, run_id):
        data = request.data
        if not isinstance(data, list):
            data = [data]

        serializer = MaterialUsageCreateSerializer(data=data, many=True)
        if not serializer.is_valid():
            return Response(
                {"detail": "Invalid data.", "errors": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST
            )
        service = _get_service(request)
        try:
            materials = service.save_material_usage(run_id, serializer.validated_data)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(
            MaterialUsageSerializer(materials, many=True).data,
            status=status.HTTP_201_CREATED
        )


class MaterialUsageDetailAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanEditMaterialUsage]

    def patch(self, request, run_id, material_id):
        service = _get_service(request)
        try:
            material = service.update_material(run_id, material_id, request.data)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(MaterialUsageSerializer(material).data)


# ===========================================================================
# MACHINE RUNTIME
# ===========================================================================

class MachineRuntimeListCreateAPI(APIView):
    def get_permissions(self):
        if self.request.method == 'GET':
            return [IsAuthenticated(), HasCompanyContext(), CanViewMachineRuntime()]
        return [IsAuthenticated(), HasCompanyContext(), CanCreateMachineRuntime()]

    def get(self, request, run_id):
        service = _get_service(request)
        try:
            runtimes = service.get_run_machine_runtime(run_id)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_404_NOT_FOUND)
        return Response(MachineRuntimeSerializer(runtimes, many=True).data)

    def post(self, request, run_id):
        data = request.data
        if not isinstance(data, list):
            data = [data]

        serializer = MachineRuntimeCreateSerializer(data=data, many=True)
        if not serializer.is_valid():
            return Response(
                {"detail": "Invalid data.", "errors": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST
            )
        service = _get_service(request)
        try:
            runtimes = service.save_machine_runtime(run_id, serializer.validated_data)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(
            MachineRuntimeSerializer(runtimes, many=True).data,
            status=status.HTTP_201_CREATED
        )


class MachineRuntimeDetailAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanCreateMachineRuntime]

    def patch(self, request, run_id, runtime_id):
        service = _get_service(request)
        try:
            runtime = service.update_machine_runtime(run_id, runtime_id, request.data)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(MachineRuntimeSerializer(runtime).data)


# ===========================================================================
# MANPOWER
# ===========================================================================

class ManpowerListCreateAPI(APIView):
    def get_permissions(self):
        if self.request.method == 'GET':
            return [IsAuthenticated(), HasCompanyContext(), CanViewManpower()]
        return [IsAuthenticated(), HasCompanyContext(), CanCreateManpower()]

    def get(self, request, run_id):
        service = _get_service(request)
        try:
            manpower = service.get_run_manpower(run_id)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_404_NOT_FOUND)
        return Response(ManpowerSerializer(manpower, many=True).data)

    def post(self, request, run_id):
        serializer = ManpowerCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                {"detail": "Invalid data.", "errors": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST
            )
        service = _get_service(request)
        try:
            manpower = service.save_manpower(run_id, serializer.validated_data)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(ManpowerSerializer(manpower).data, status=status.HTTP_201_CREATED)


class ManpowerDetailAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanCreateManpower]

    def patch(self, request, run_id, manpower_id):
        service = _get_service(request)
        try:
            manpower = service.update_manpower(run_id, manpower_id, request.data)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(ManpowerSerializer(manpower).data)


# ===========================================================================
# LINE CLEARANCE
# ===========================================================================

class LineClearanceListCreateAPI(APIView):
    def get_permissions(self):
        if self.request.method == 'GET':
            return [IsAuthenticated(), HasCompanyContext(), CanViewLineClearance()]
        return [IsAuthenticated(), HasCompanyContext(), CanCreateLineClearance()]

    def get(self, request):
        service = _get_service(request)
        clearances = service.list_clearances(
            date=request.GET.get('date'),
            line_id=request.GET.get('line_id'),
            status=request.GET.get('status'),
        )
        return Response(LineClearanceListSerializer(clearances, many=True).data)

    def post(self, request):
        serializer = LineClearanceCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                {"detail": "Invalid data.", "errors": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST
            )
        service = _get_service(request)
        try:
            clearance = service.create_clearance(
                serializer.validated_data, user=request.user
            )
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(
            LineClearanceDetailSerializer(clearance).data,
            status=status.HTTP_201_CREATED
        )


class LineClearanceDetailAPI(APIView):
    def get_permissions(self):
        if self.request.method == 'GET':
            return [IsAuthenticated(), HasCompanyContext(), CanViewLineClearance()]
        return [IsAuthenticated(), HasCompanyContext(), CanCreateLineClearance()]

    def get(self, request, clearance_id):
        service = _get_service(request)
        try:
            clearance = service.get_clearance(clearance_id)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_404_NOT_FOUND)
        return Response(LineClearanceDetailSerializer(clearance).data)

    def patch(self, request, clearance_id):
        serializer = LineClearanceUpdateSerializer(data=request.data, partial=True)
        if not serializer.is_valid():
            return Response(
                {"detail": "Invalid data.", "errors": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST
            )
        service = _get_service(request)
        try:
            clearance = service.update_clearance(
                clearance_id, serializer.validated_data
            )
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(LineClearanceDetailSerializer(clearance).data)


class SubmitClearanceAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanCreateLineClearance]

    def post(self, request, clearance_id):
        service = _get_service(request)
        try:
            clearance = service.submit_clearance(clearance_id)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(LineClearanceDetailSerializer(clearance).data)


class ApproveClearanceAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanApproveLineClearanceQA]

    def post(self, request, clearance_id):
        approved = request.data.get('approved', False)
        service = _get_service(request)
        try:
            clearance = service.approve_clearance(
                clearance_id, user=request.user, approved=approved
            )
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(LineClearanceDetailSerializer(clearance).data)


# ===========================================================================
# MACHINE CHECKLISTS
# ===========================================================================

class MachineChecklistListCreateAPI(APIView):
    def get_permissions(self):
        if self.request.method == 'GET':
            return [IsAuthenticated(), HasCompanyContext(), CanViewMachineChecklist()]
        return [IsAuthenticated(), HasCompanyContext(), CanCreateMachineChecklist()]

    def get(self, request):
        service = _get_service(request)
        entries = service.list_checklist_entries(
            machine_id=request.GET.get('machine_id'),
            month=request.GET.get('month'),
            year=request.GET.get('year'),
            frequency=request.GET.get('frequency'),
        )
        return Response(MachineChecklistEntrySerializer(entries, many=True).data)

    def post(self, request):
        serializer = MachineChecklistCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                {"detail": "Invalid data.", "errors": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST
            )
        service = _get_service(request)
        try:
            entry = service.create_checklist_entry(serializer.validated_data)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(
            MachineChecklistEntrySerializer(entry).data,
            status=status.HTTP_201_CREATED
        )


class MachineChecklistBulkAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanCreateMachineChecklist]

    def post(self, request):
        serializer = MachineChecklistCreateSerializer(data=request.data, many=True)
        if not serializer.is_valid():
            return Response(
                {"detail": "Invalid data.", "errors": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST
            )
        service = _get_service(request)
        try:
            entries = service.bulk_upsert_checklist_entries(serializer.validated_data)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(
            MachineChecklistEntrySerializer(entries, many=True).data,
            status=status.HTTP_201_CREATED
        )


class MachineChecklistDetailAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanCreateMachineChecklist]

    def patch(self, request, entry_id):
        service = _get_service(request)
        try:
            entry = service.update_checklist_entry(entry_id, request.data)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(MachineChecklistEntrySerializer(entry).data)


# ===========================================================================
# WASTE MANAGEMENT
# ===========================================================================

class WasteLogListCreateAPI(APIView):
    def get_permissions(self):
        if self.request.method == 'GET':
            return [IsAuthenticated(), HasCompanyContext(), CanViewWasteLog()]
        return [IsAuthenticated(), HasCompanyContext(), CanCreateWasteLog()]

    def get(self, request):
        service = _get_service(request)
        waste_logs = service.list_waste_logs(
            run_id=request.GET.get('run_id'),
            approval_status=request.GET.get('approval_status'),
        )
        return Response(WasteLogSerializer(waste_logs, many=True).data)

    def post(self, request):
        serializer = WasteLogCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                {"detail": "Invalid data.", "errors": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST
            )
        service = _get_service(request)
        try:
            waste = service.create_waste_log(serializer.validated_data)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(WasteLogSerializer(waste).data, status=status.HTTP_201_CREATED)


class WasteLogDetailAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewWasteLog]

    def get(self, request, waste_id):
        service = _get_service(request)
        try:
            waste = service.get_waste_log(waste_id)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_404_NOT_FOUND)
        return Response(WasteLogSerializer(waste).data)


class WasteApproveEngineerAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanApproveWasteEngineer]

    def post(self, request, waste_id):
        serializer = WasteApprovalSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                {"detail": "Invalid data.", "errors": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST
            )
        service = _get_service(request)
        try:
            waste = service.approve_waste(
                waste_id, 'engineer', request.user,
                serializer.validated_data['sign']
            )
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(WasteLogSerializer(waste).data)


class WasteApproveAMAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanApproveWasteAM]

    def post(self, request, waste_id):
        serializer = WasteApprovalSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                {"detail": "Invalid data.", "errors": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST
            )
        service = _get_service(request)
        try:
            waste = service.approve_waste(
                waste_id, 'am', request.user,
                serializer.validated_data['sign']
            )
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(WasteLogSerializer(waste).data)


class WasteApproveStoreAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanApproveWasteStore]

    def post(self, request, waste_id):
        serializer = WasteApprovalSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                {"detail": "Invalid data.", "errors": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST
            )
        service = _get_service(request)
        try:
            waste = service.approve_waste(
                waste_id, 'store', request.user,
                serializer.validated_data['sign']
            )
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(WasteLogSerializer(waste).data)


class WasteApproveHODAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanApproveWasteHOD]

    def post(self, request, waste_id):
        serializer = WasteApprovalSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                {"detail": "Invalid data.", "errors": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST
            )
        service = _get_service(request)
        try:
            waste = service.approve_waste(
                waste_id, 'hod', request.user,
                serializer.validated_data['sign']
            )
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(WasteLogSerializer(waste).data)


# ===========================================================================
# REPORTS
# ===========================================================================

class DailyProductionReportAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewReports]

    def get(self, request):
        date = request.GET.get('date')
        if not date:
            return Response(
                {"detail": "date query parameter is required."},
                status=status.HTTP_400_BAD_REQUEST
            )
        service = _get_service(request)
        runs = service.get_daily_production_report(
            date=date, line_id=request.GET.get('line_id')
        )
        return Response(ProductionRunDetailSerializer(runs, many=True).data)


class YieldReportAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewReports]

    def get(self, request, run_id):
        service = _get_service(request)
        try:
            report = service.get_yield_report(run_id)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_404_NOT_FOUND)
        return Response({
            'run': ProductionRunListSerializer(report['run']).data,
            'materials': MaterialUsageSerializer(report['materials'], many=True).data,
            'machine_runtimes': MachineRuntimeSerializer(
                report['machine_runtimes'], many=True
            ).data,
            'manpower': ManpowerSerializer(report['manpower'], many=True).data,
        })


class LineClearanceReportAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewReports]

    def get(self, request):
        service = _get_service(request)
        clearances = service.get_line_clearance_report(
            date_from=request.GET.get('date_from'),
            date_to=request.GET.get('date_to'),
        )
        return Response(LineClearanceListSerializer(clearances, many=True).data)


class AnalyticsAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewReports]

    def get(self, request):
        service = _get_service(request)
        analytics = service.get_analytics(
            date_from=request.GET.get('date_from'),
            date_to=request.GET.get('date_to'),
            line_id=request.GET.get('line_id'),
        )
        return Response(analytics)
