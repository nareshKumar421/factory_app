import logging
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated

from company.permissions import HasCompanyContext
from .services import ProductionExecutionService
from .models import (
    ProductionRun, MachineBreakdown, WasteLog, BreakdownCategory,
    ResourceElectricity, ResourceWater, ResourceGas, ResourceCompressedAir,
    ResourceLabour, ResourceMachineCost, ResourceOverhead,
    ProductionRunCost, InProcessQCCheck, FinalQCCheck,
)
from .serializers import (
    # Master Data
    ProductionLineSerializer, ProductionLineCreateSerializer,
    MachineSerializer, MachineCreateSerializer,
    ChecklistTemplateSerializer, ChecklistTemplateCreateSerializer,
    # Breakdown Categories
    BreakdownCategorySerializer, BreakdownCategoryCreateSerializer,
    # Production Runs
    ProductionRunCreateSerializer, ProductionRunUpdateSerializer,
    ProductionRunListSerializer, ProductionRunDetailSerializer,
    # Timeline Actions
    ProductionSegmentSerializer, AddBreakdownSerializer,
    ResolveBreakdownSerializer, CompleteRunSerializer, StopProductionSerializer,
    SegmentUpdateSerializer, BreakdownUpdateSerializer,
    # Breakdowns
    MachineBreakdownSerializer,
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
    # Resource Tracking
    ResourceElectricitySerializer, ResourceElectricityCreateSerializer,
    ResourceWaterSerializer, ResourceWaterCreateSerializer,
    ResourceGasSerializer, ResourceGasCreateSerializer,
    ResourceCompressedAirSerializer, ResourceCompressedAirCreateSerializer,
    ResourceLabourSerializer, ResourceLabourCreateSerializer,
    ResourceMachineCostSerializer, ResourceMachineCostCreateSerializer,
    ResourceOverheadSerializer, ResourceOverheadCreateSerializer,
    # Cost
    ProductionRunCostSerializer,
    # QC
    InProcessQCCheckSerializer, InProcessQCCheckCreateSerializer,
    FinalQCCheckSerializer, FinalQCCheckCreateSerializer,
)
from .permissions import (
    CanManageProductionLines, CanManageMachines, CanManageChecklistTemplates,
    CanViewProductionRun, CanCreateProductionRun, CanEditProductionRun,
    CanCompleteProductionRun,
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
# MASTER DATA — Breakdown Categories
# ===========================================================================

class BreakdownCategoryListCreateAPI(APIView):
    def get_permissions(self):
        if self.request.method == 'GET':
            return [IsAuthenticated(), HasCompanyContext(), CanViewProductionRun()]
        return [IsAuthenticated(), HasCompanyContext(), CanManageProductionLines()]

    def get(self, request):
        service = _get_service(request)
        categories = BreakdownCategory.objects.filter(
            company=service.company, is_active=True
        ).order_by('name')
        return Response(BreakdownCategorySerializer(categories, many=True).data)

    def post(self, request):
        serializer = BreakdownCategoryCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                {"detail": "Invalid data.", "errors": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST
            )
        service = _get_service(request)
        try:
            category = BreakdownCategory.objects.create(
                company=service.company,
                **serializer.validated_data
            )
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(
            BreakdownCategorySerializer(category).data,
            status=status.HTTP_201_CREATED
        )


class BreakdownCategoryDetailAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanManageProductionLines]

    def patch(self, request, category_id):
        service = _get_service(request)
        try:
            category = BreakdownCategory.objects.get(
                id=category_id, company=service.company
            )
        except BreakdownCategory.DoesNotExist:
            return Response(
                {"detail": "Breakdown category not found."},
                status=status.HTTP_404_NOT_FOUND
            )
        for field in ['name', 'description']:
            if field in request.data:
                setattr(category, field, request.data[field])
        category.save()
        return Response(BreakdownCategorySerializer(category).data)

    def delete(self, request, category_id):
        service = _get_service(request)
        try:
            category = BreakdownCategory.objects.get(
                id=category_id, company=service.company
            )
        except BreakdownCategory.DoesNotExist:
            return Response(
                {"detail": "Breakdown category not found."},
                status=status.HTTP_404_NOT_FOUND
            )
        category.is_active = False
        category.save()
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
            date_from=request.GET.get('date_from'),
            date_to=request.GET.get('date_to'),
            line_id=request.GET.get('line_id'),
            status=request.GET.get('status'),
            sap_doc_entry=request.GET.get('sap_doc_entry'),
            search=request.GET.get('search'),
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


# ===========================================================================
# TIMELINE ACTIONS
# ===========================================================================

class StartProductionAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanEditProductionRun]

    def post(self, request, run_id):
        service = _get_service(request)
        try:
            segment = service.start_production(run_id)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(ProductionSegmentSerializer(segment).data, status=status.HTTP_201_CREATED)


class StopProductionAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanEditProductionRun]

    def post(self, request, run_id):
        serializer = StopProductionSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                {"detail": "Invalid data.", "errors": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST
            )
        service = _get_service(request)
        try:
            segment = service.stop_production(
                run_id,
                serializer.validated_data['produced_cases'],
                serializer.validated_data.get('remarks', ''),
            )
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(ProductionSegmentSerializer(segment).data)


class AddBreakdownAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanCreateBreakdown]

    def post(self, request, run_id):
        serializer = AddBreakdownSerializer(data=request.data)
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


class ResolveBreakdownAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanEditBreakdown]

    def post(self, request, run_id, breakdown_id):
        serializer = ResolveBreakdownSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                {"detail": "Invalid data.", "errors": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST
            )
        service = _get_service(request)
        try:
            breakdown = service.resolve_breakdown(
                run_id, breakdown_id, serializer.validated_data['action']
            )
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(MachineBreakdownSerializer(breakdown).data)


class SegmentUpdateAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanEditProductionRun]

    def patch(self, request, run_id, segment_id):
        serializer = SegmentUpdateSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                {"detail": "Invalid data.", "errors": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST
            )
        service = _get_service(request)
        try:
            segment = service.update_segment(run_id, segment_id, serializer.validated_data)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(ProductionSegmentSerializer(segment).data)


class BreakdownUpdateAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanEditBreakdown]

    def patch(self, request, run_id, breakdown_id):
        serializer = BreakdownUpdateSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                {"detail": "Invalid data.", "errors": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST
            )
        service = _get_service(request)
        try:
            breakdown = service.update_breakdown_remarks(
                run_id, breakdown_id, serializer.validated_data.get('remarks', '')
            )
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(MachineBreakdownSerializer(breakdown).data)


class CompleteRunAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanCompleteProductionRun]

    def post(self, request, run_id):
        serializer = CompleteRunSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                {"detail": "Invalid data.", "errors": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST
            )
        service = _get_service(request)
        try:
            run = service.complete_run(
                run_id, total_production=serializer.validated_data['total_production']
            )
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(ProductionRunDetailSerializer(run).data)


# ===========================================================================
# MACHINE BREAKDOWNS
# ===========================================================================

class BreakdownListCreateAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewBreakdown]

    def get(self, request, run_id):
        service = _get_service(request)
        try:
            breakdowns = service.get_run_breakdowns(run_id)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_404_NOT_FOUND)
        return Response(MachineBreakdownSerializer(breakdowns, many=True).data)


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


# ===========================================================================
# SAP Orders Proxy Views
# ===========================================================================

class SAPItemSearchAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewProductionRun]

    def get(self, request):
        from .services.sap_reader import ProductionOrderReader, SAPReadError
        search = request.GET.get('search', '')
        if len(search) < 2:
            return Response([])
        try:
            reader = ProductionOrderReader(request.company.company.code)
            items = reader.search_items(search=search, limit=50)
        except SAPReadError as e:
            return Response({'detail': str(e)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        return Response(items)


class SAPProductionOrderListAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewProductionRun]

    def get(self, request):
        from .services.sap_reader import ProductionOrderReader, SAPReadError
        try:
            reader = ProductionOrderReader(request.company.company.code)
            orders = reader.get_released_production_orders()
        except SAPReadError as e:
            return Response({'detail': str(e)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        return Response(orders)


class SAPProductionOrderDetailAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewProductionRun]

    def get(self, request, doc_entry):
        from .services.sap_reader import ProductionOrderReader, SAPReadError
        try:
            reader = ProductionOrderReader(request.company.company.code)
            detail = reader.get_production_order_detail(doc_entry)
        except SAPReadError as e:
            return Response({'detail': str(e)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        return Response(detail)


# ===========================================================================
# Resource Tracking — Electricity
# ===========================================================================

class ResourceElectricityListCreateAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanCreateMaterialUsage]

    def get(self, request, run_id):
        service = _get_service(request)
        try:
            run = service._get_run_or_raise(run_id)
        except ValueError as e:
            return Response({'detail': str(e)}, status=status.HTTP_404_NOT_FOUND)
        entries = run.electricity_usage.all()
        return Response(ResourceElectricitySerializer(entries, many=True).data)

    def post(self, request, run_id):
        serializer = ResourceElectricityCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return Response({'detail': 'Invalid data.', 'errors': serializer.errors}, status=status.HTTP_400_BAD_REQUEST)
        service = _get_service(request)
        try:
            run = service._get_run_or_raise(run_id)
            entry = ResourceElectricity.objects.create(
                production_run=run,
                created_by=request.user,
                **serializer.validated_data
            )
            from .services.cost_calculator import recalculate_run_cost
            recalculate_run_cost(run)
        except ValueError as e:
            return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(ResourceElectricitySerializer(entry).data, status=status.HTTP_201_CREATED)


class ResourceElectricityDetailAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanCreateMaterialUsage]

    def patch(self, request, run_id, entry_id):
        service = _get_service(request)
        try:
            run = service._get_run_or_raise(run_id)
            entry = ResourceElectricity.objects.get(id=entry_id, production_run=run)
        except (ValueError, ResourceElectricity.DoesNotExist) as e:
            return Response({'detail': str(e)}, status=status.HTTP_404_NOT_FOUND)
        for field in ['description', 'units_consumed', 'rate_per_unit']:
            if field in request.data:
                setattr(entry, field, request.data[field])
        entry.save()
        from .services.cost_calculator import recalculate_run_cost
        recalculate_run_cost(run)
        return Response(ResourceElectricitySerializer(entry).data)

    def delete(self, request, run_id, entry_id):
        service = _get_service(request)
        try:
            run = service._get_run_or_raise(run_id)
            entry = ResourceElectricity.objects.get(id=entry_id, production_run=run)
        except (ValueError, ResourceElectricity.DoesNotExist) as e:
            return Response({'detail': str(e)}, status=status.HTTP_404_NOT_FOUND)
        entry.delete()
        from .services.cost_calculator import recalculate_run_cost
        recalculate_run_cost(run)
        return Response(status=status.HTTP_204_NO_CONTENT)


# ===========================================================================
# Resource Tracking — Water
# ===========================================================================

class ResourceWaterListCreateAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanCreateMaterialUsage]

    def get(self, request, run_id):
        service = _get_service(request)
        try:
            run = service._get_run_or_raise(run_id)
        except ValueError as e:
            return Response({'detail': str(e)}, status=status.HTTP_404_NOT_FOUND)
        entries = run.water_usage.all()
        return Response(ResourceWaterSerializer(entries, many=True).data)

    def post(self, request, run_id):
        serializer = ResourceWaterCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return Response({'detail': 'Invalid data.', 'errors': serializer.errors}, status=status.HTTP_400_BAD_REQUEST)
        service = _get_service(request)
        try:
            run = service._get_run_or_raise(run_id)
            entry = ResourceWater.objects.create(
                production_run=run,
                created_by=request.user,
                **serializer.validated_data
            )
            from .services.cost_calculator import recalculate_run_cost
            recalculate_run_cost(run)
        except ValueError as e:
            return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(ResourceWaterSerializer(entry).data, status=status.HTTP_201_CREATED)


class ResourceWaterDetailAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanCreateMaterialUsage]

    def patch(self, request, run_id, entry_id):
        service = _get_service(request)
        try:
            run = service._get_run_or_raise(run_id)
            entry = ResourceWater.objects.get(id=entry_id, production_run=run)
        except (ValueError, ResourceWater.DoesNotExist) as e:
            return Response({'detail': str(e)}, status=status.HTTP_404_NOT_FOUND)
        for field in ['description', 'volume_consumed', 'rate_per_unit']:
            if field in request.data:
                setattr(entry, field, request.data[field])
        entry.save()
        from .services.cost_calculator import recalculate_run_cost
        recalculate_run_cost(run)
        return Response(ResourceWaterSerializer(entry).data)

    def delete(self, request, run_id, entry_id):
        service = _get_service(request)
        try:
            run = service._get_run_or_raise(run_id)
            entry = ResourceWater.objects.get(id=entry_id, production_run=run)
        except (ValueError, ResourceWater.DoesNotExist) as e:
            return Response({'detail': str(e)}, status=status.HTTP_404_NOT_FOUND)
        entry.delete()
        from .services.cost_calculator import recalculate_run_cost
        recalculate_run_cost(run)
        return Response(status=status.HTTP_204_NO_CONTENT)


# ===========================================================================
# Resource Tracking — Gas
# ===========================================================================

class ResourceGasListCreateAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanCreateMaterialUsage]

    def get(self, request, run_id):
        service = _get_service(request)
        try:
            run = service._get_run_or_raise(run_id)
        except ValueError as e:
            return Response({'detail': str(e)}, status=status.HTTP_404_NOT_FOUND)
        entries = run.gas_usage.all()
        return Response(ResourceGasSerializer(entries, many=True).data)

    def post(self, request, run_id):
        serializer = ResourceGasCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return Response({'detail': 'Invalid data.', 'errors': serializer.errors}, status=status.HTTP_400_BAD_REQUEST)
        service = _get_service(request)
        try:
            run = service._get_run_or_raise(run_id)
            entry = ResourceGas.objects.create(
                production_run=run,
                created_by=request.user,
                **serializer.validated_data
            )
            from .services.cost_calculator import recalculate_run_cost
            recalculate_run_cost(run)
        except ValueError as e:
            return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(ResourceGasSerializer(entry).data, status=status.HTTP_201_CREATED)


class ResourceGasDetailAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanCreateMaterialUsage]

    def patch(self, request, run_id, entry_id):
        service = _get_service(request)
        try:
            run = service._get_run_or_raise(run_id)
            entry = ResourceGas.objects.get(id=entry_id, production_run=run)
        except (ValueError, ResourceGas.DoesNotExist) as e:
            return Response({'detail': str(e)}, status=status.HTTP_404_NOT_FOUND)
        for field in ['description', 'qty_consumed', 'rate_per_unit']:
            if field in request.data:
                setattr(entry, field, request.data[field])
        entry.save()
        from .services.cost_calculator import recalculate_run_cost
        recalculate_run_cost(run)
        return Response(ResourceGasSerializer(entry).data)

    def delete(self, request, run_id, entry_id):
        service = _get_service(request)
        try:
            run = service._get_run_or_raise(run_id)
            entry = ResourceGas.objects.get(id=entry_id, production_run=run)
        except (ValueError, ResourceGas.DoesNotExist) as e:
            return Response({'detail': str(e)}, status=status.HTTP_404_NOT_FOUND)
        entry.delete()
        from .services.cost_calculator import recalculate_run_cost
        recalculate_run_cost(run)
        return Response(status=status.HTTP_204_NO_CONTENT)


# ===========================================================================
# Resource Tracking — Compressed Air
# ===========================================================================

class ResourceCompressedAirListCreateAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanCreateMaterialUsage]

    def get(self, request, run_id):
        service = _get_service(request)
        try:
            run = service._get_run_or_raise(run_id)
        except ValueError as e:
            return Response({'detail': str(e)}, status=status.HTTP_404_NOT_FOUND)
        entries = run.compressed_air_usage.all()
        return Response(ResourceCompressedAirSerializer(entries, many=True).data)

    def post(self, request, run_id):
        serializer = ResourceCompressedAirCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return Response({'detail': 'Invalid data.', 'errors': serializer.errors}, status=status.HTTP_400_BAD_REQUEST)
        service = _get_service(request)
        try:
            run = service._get_run_or_raise(run_id)
            entry = ResourceCompressedAir.objects.create(
                production_run=run,
                created_by=request.user,
                **serializer.validated_data
            )
            from .services.cost_calculator import recalculate_run_cost
            recalculate_run_cost(run)
        except ValueError as e:
            return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(ResourceCompressedAirSerializer(entry).data, status=status.HTTP_201_CREATED)


class ResourceCompressedAirDetailAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanCreateMaterialUsage]

    def patch(self, request, run_id, entry_id):
        service = _get_service(request)
        try:
            run = service._get_run_or_raise(run_id)
            entry = ResourceCompressedAir.objects.get(id=entry_id, production_run=run)
        except (ValueError, ResourceCompressedAir.DoesNotExist) as e:
            return Response({'detail': str(e)}, status=status.HTTP_404_NOT_FOUND)
        for field in ['description', 'units_consumed', 'rate_per_unit']:
            if field in request.data:
                setattr(entry, field, request.data[field])
        entry.save()
        from .services.cost_calculator import recalculate_run_cost
        recalculate_run_cost(run)
        return Response(ResourceCompressedAirSerializer(entry).data)

    def delete(self, request, run_id, entry_id):
        service = _get_service(request)
        try:
            run = service._get_run_or_raise(run_id)
            entry = ResourceCompressedAir.objects.get(id=entry_id, production_run=run)
        except (ValueError, ResourceCompressedAir.DoesNotExist) as e:
            return Response({'detail': str(e)}, status=status.HTTP_404_NOT_FOUND)
        entry.delete()
        from .services.cost_calculator import recalculate_run_cost
        recalculate_run_cost(run)
        return Response(status=status.HTTP_204_NO_CONTENT)


# ===========================================================================
# Resource Tracking — Labour
# ===========================================================================

class ResourceLabourListCreateAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanCreateMaterialUsage]

    def get(self, request, run_id):
        service = _get_service(request)
        try:
            run = service._get_run_or_raise(run_id)
        except ValueError as e:
            return Response({'detail': str(e)}, status=status.HTTP_404_NOT_FOUND)
        entries = run.labour_entries.all()
        return Response(ResourceLabourSerializer(entries, many=True).data)

    def post(self, request, run_id):
        serializer = ResourceLabourCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return Response({'detail': 'Invalid data.', 'errors': serializer.errors}, status=status.HTTP_400_BAD_REQUEST)
        service = _get_service(request)
        try:
            run = service._get_run_or_raise(run_id)
            entry = ResourceLabour.objects.create(
                production_run=run,
                created_by=request.user,
                **serializer.validated_data
            )
            from .services.cost_calculator import recalculate_run_cost
            recalculate_run_cost(run)
        except ValueError as e:
            return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(ResourceLabourSerializer(entry).data, status=status.HTTP_201_CREATED)


class ResourceLabourDetailAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanCreateMaterialUsage]

    def patch(self, request, run_id, entry_id):
        service = _get_service(request)
        try:
            run = service._get_run_or_raise(run_id)
            entry = ResourceLabour.objects.get(id=entry_id, production_run=run)
        except (ValueError, ResourceLabour.DoesNotExist) as e:
            return Response({'detail': str(e)}, status=status.HTTP_404_NOT_FOUND)
        for field in ['worker_name', 'hours_worked', 'rate_per_hour']:
            if field in request.data:
                setattr(entry, field, request.data[field])
        entry.save()
        from .services.cost_calculator import recalculate_run_cost
        recalculate_run_cost(run)
        return Response(ResourceLabourSerializer(entry).data)

    def delete(self, request, run_id, entry_id):
        service = _get_service(request)
        try:
            run = service._get_run_or_raise(run_id)
            entry = ResourceLabour.objects.get(id=entry_id, production_run=run)
        except (ValueError, ResourceLabour.DoesNotExist) as e:
            return Response({'detail': str(e)}, status=status.HTTP_404_NOT_FOUND)
        entry.delete()
        from .services.cost_calculator import recalculate_run_cost
        recalculate_run_cost(run)
        return Response(status=status.HTTP_204_NO_CONTENT)


# ===========================================================================
# Resource Tracking — Machine Costs
# ===========================================================================

class ResourceMachineCostListCreateAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanCreateMaterialUsage]

    def get(self, request, run_id):
        service = _get_service(request)
        try:
            run = service._get_run_or_raise(run_id)
        except ValueError as e:
            return Response({'detail': str(e)}, status=status.HTTP_404_NOT_FOUND)
        entries = run.machine_cost_entries.all()
        return Response(ResourceMachineCostSerializer(entries, many=True).data)

    def post(self, request, run_id):
        serializer = ResourceMachineCostCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return Response({'detail': 'Invalid data.', 'errors': serializer.errors}, status=status.HTTP_400_BAD_REQUEST)
        service = _get_service(request)
        try:
            run = service._get_run_or_raise(run_id)
            entry = ResourceMachineCost.objects.create(
                production_run=run,
                created_by=request.user,
                **serializer.validated_data
            )
            from .services.cost_calculator import recalculate_run_cost
            recalculate_run_cost(run)
        except ValueError as e:
            return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(ResourceMachineCostSerializer(entry).data, status=status.HTTP_201_CREATED)


class ResourceMachineCostDetailAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanCreateMaterialUsage]

    def patch(self, request, run_id, entry_id):
        service = _get_service(request)
        try:
            run = service._get_run_or_raise(run_id)
            entry = ResourceMachineCost.objects.get(id=entry_id, production_run=run)
        except (ValueError, ResourceMachineCost.DoesNotExist) as e:
            return Response({'detail': str(e)}, status=status.HTTP_404_NOT_FOUND)
        for field in ['machine_name', 'hours_used', 'rate_per_hour']:
            if field in request.data:
                setattr(entry, field, request.data[field])
        entry.save()
        from .services.cost_calculator import recalculate_run_cost
        recalculate_run_cost(run)
        return Response(ResourceMachineCostSerializer(entry).data)

    def delete(self, request, run_id, entry_id):
        service = _get_service(request)
        try:
            run = service._get_run_or_raise(run_id)
            entry = ResourceMachineCost.objects.get(id=entry_id, production_run=run)
        except (ValueError, ResourceMachineCost.DoesNotExist) as e:
            return Response({'detail': str(e)}, status=status.HTTP_404_NOT_FOUND)
        entry.delete()
        from .services.cost_calculator import recalculate_run_cost
        recalculate_run_cost(run)
        return Response(status=status.HTTP_204_NO_CONTENT)


# ===========================================================================
# Resource Tracking — Overhead
# ===========================================================================

class ResourceOverheadListCreateAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanCreateMaterialUsage]

    def get(self, request, run_id):
        service = _get_service(request)
        try:
            run = service._get_run_or_raise(run_id)
        except ValueError as e:
            return Response({'detail': str(e)}, status=status.HTTP_404_NOT_FOUND)
        entries = run.overhead_entries.all()
        return Response(ResourceOverheadSerializer(entries, many=True).data)

    def post(self, request, run_id):
        serializer = ResourceOverheadCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return Response({'detail': 'Invalid data.', 'errors': serializer.errors}, status=status.HTTP_400_BAD_REQUEST)
        service = _get_service(request)
        try:
            run = service._get_run_or_raise(run_id)
            entry = ResourceOverhead.objects.create(
                production_run=run,
                created_by=request.user,
                **serializer.validated_data
            )
            from .services.cost_calculator import recalculate_run_cost
            recalculate_run_cost(run)
        except ValueError as e:
            return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(ResourceOverheadSerializer(entry).data, status=status.HTTP_201_CREATED)


class ResourceOverheadDetailAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanCreateMaterialUsage]

    def patch(self, request, run_id, entry_id):
        service = _get_service(request)
        try:
            run = service._get_run_or_raise(run_id)
            entry = ResourceOverhead.objects.get(id=entry_id, production_run=run)
        except (ValueError, ResourceOverhead.DoesNotExist) as e:
            return Response({'detail': str(e)}, status=status.HTTP_404_NOT_FOUND)
        for field in ['expense_name', 'amount']:
            if field in request.data:
                setattr(entry, field, request.data[field])
        entry.save()
        from .services.cost_calculator import recalculate_run_cost
        recalculate_run_cost(run)
        return Response(ResourceOverheadSerializer(entry).data)

    def delete(self, request, run_id, entry_id):
        service = _get_service(request)
        try:
            run = service._get_run_or_raise(run_id)
            entry = ResourceOverhead.objects.get(id=entry_id, production_run=run)
        except (ValueError, ResourceOverhead.DoesNotExist) as e:
            return Response({'detail': str(e)}, status=status.HTTP_404_NOT_FOUND)
        entry.delete()
        from .services.cost_calculator import recalculate_run_cost
        recalculate_run_cost(run)
        return Response(status=status.HTTP_204_NO_CONTENT)


# ===========================================================================
# Cost Summary
# ===========================================================================

class RunCostSummaryAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewProductionRun]

    def get(self, request, run_id):
        service = _get_service(request)
        try:
            run = service._get_run_or_raise(run_id)
        except ValueError as e:
            return Response({'detail': str(e)}, status=status.HTTP_404_NOT_FOUND)
        try:
            cost = run.cost_summary
            return Response(ProductionRunCostSerializer(cost).data)
        except Exception:
            return Response({'detail': 'No cost data yet.'}, status=status.HTTP_404_NOT_FOUND)


# ===========================================================================
# Cost Analytics
# ===========================================================================

class CostAnalyticsAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewReports]

    def get(self, request):
        from company.models import Company
        try:
            company = Company.objects.get(code=request.company.company.code)
        except Company.DoesNotExist:
            return Response({'detail': 'Company not found.'}, status=status.HTTP_404_NOT_FOUND)

        date_from = request.GET.get('date_from')
        date_to = request.GET.get('date_to')
        line_id = request.GET.get('line')

        runs_qs = ProductionRun.objects.filter(company=company)
        if date_from:
            runs_qs = runs_qs.filter(date__gte=date_from)
        if date_to:
            runs_qs = runs_qs.filter(date__lte=date_to)
        if line_id:
            runs_qs = runs_qs.filter(line_id=line_id)

        costs = ProductionRunCost.objects.filter(
            production_run__in=runs_qs
        ).select_related('production_run', 'production_run__line')

        return Response(ProductionRunCostSerializer(costs, many=True).data)


# ===========================================================================
# QC Checks — In-Process
# ===========================================================================

class InProcessQCListCreateAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewProductionRun]

    def get(self, request, run_id):
        service = _get_service(request)
        try:
            run = service._get_run_or_raise(run_id)
        except ValueError as e:
            return Response({'detail': str(e)}, status=status.HTTP_404_NOT_FOUND)
        checks = run.inprocess_qc_checks.all()
        return Response(InProcessQCCheckSerializer(checks, many=True).data)

    def post(self, request, run_id):
        serializer = InProcessQCCheckCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return Response({'detail': 'Invalid data.', 'errors': serializer.errors}, status=status.HTTP_400_BAD_REQUEST)
        service = _get_service(request)
        try:
            run = service._get_run_or_raise(run_id)
        except ValueError as e:
            return Response({'detail': str(e)}, status=status.HTTP_404_NOT_FOUND)
        check = InProcessQCCheck.objects.create(
            production_run=run,
            checked_by=request.user,
            **serializer.validated_data
        )
        return Response(InProcessQCCheckSerializer(check).data, status=status.HTTP_201_CREATED)


class InProcessQCDetailAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewProductionRun]

    def patch(self, request, run_id, check_id):
        service = _get_service(request)
        try:
            run = service._get_run_or_raise(run_id)
            check = InProcessQCCheck.objects.get(id=check_id, production_run=run)
        except (ValueError, InProcessQCCheck.DoesNotExist):
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        for field in ['checked_at', 'parameter', 'acceptable_min', 'acceptable_max', 'actual_value', 'result', 'remarks']:
            if field in request.data:
                setattr(check, field, request.data[field])
        check.save()
        return Response(InProcessQCCheckSerializer(check).data)

    def delete(self, request, run_id, check_id):
        service = _get_service(request)
        try:
            run = service._get_run_or_raise(run_id)
            check = InProcessQCCheck.objects.get(id=check_id, production_run=run)
        except (ValueError, InProcessQCCheck.DoesNotExist):
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        check.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# ===========================================================================
# QC Checks — Final
# ===========================================================================

class FinalQCCheckAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewProductionRun]

    def get(self, request, run_id):
        service = _get_service(request)
        try:
            run = service._get_run_or_raise(run_id)
            check = run.final_qc
        except (ValueError, FinalQCCheck.DoesNotExist):
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        return Response(FinalQCCheckSerializer(check).data)

    def post(self, request, run_id):
        serializer = FinalQCCheckCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return Response({'detail': 'Invalid data.', 'errors': serializer.errors}, status=status.HTTP_400_BAD_REQUEST)
        service = _get_service(request)
        try:
            run = service._get_run_or_raise(run_id)
        except ValueError as e:
            return Response({'detail': str(e)}, status=status.HTTP_404_NOT_FOUND)
        if FinalQCCheck.objects.filter(production_run=run).exists():
            return Response({'detail': 'Final QC already exists. Use PATCH to update.'}, status=status.HTTP_400_BAD_REQUEST)
        check = FinalQCCheck.objects.create(
            production_run=run,
            checked_by=request.user,
            **serializer.validated_data
        )
        return Response(FinalQCCheckSerializer(check).data, status=status.HTTP_201_CREATED)

    def patch(self, request, run_id):
        service = _get_service(request)
        try:
            run = service._get_run_or_raise(run_id)
            check = run.final_qc
        except (ValueError, FinalQCCheck.DoesNotExist):
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        for field in ['checked_at', 'overall_result', 'parameters', 'remarks']:
            if field in request.data:
                setattr(check, field, request.data[field])
        check.save()
        return Response(FinalQCCheckSerializer(check).data)


# ===========================================================================
# Extended Analytics
# ===========================================================================

class OEEAnalyticsAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewReports]

    def get(self, request):
        service = _get_service(request)
        date_from = request.GET.get('date_from')
        date_to = request.GET.get('date_to')
        line_id = request.GET.get('line')
        data = service.get_analytics(
            date_from=date_from, date_to=date_to, line_id=line_id
        )
        # Enhance with OEE calculation
        runs_qs = ProductionRun.objects.filter(company=service.company, status='COMPLETED')
        if date_from:
            runs_qs = runs_qs.filter(date__gte=date_from)
        if date_to:
            runs_qs = runs_qs.filter(date__lte=date_to)
        if line_id:
            runs_qs = runs_qs.filter(line_id=line_id)

        # Per run OEE
        run_oees = []
        for run in runs_qs.select_related('line'):
            available = 720
            breakdown = run.total_breakdown_time or 0
            operating = max(0, available - breakdown)
            availability = (operating / available * 100) if available else 0

            rated = float(run.rated_speed or 0)
            total_prod_f = float(run.total_production or 0)
            actual_speed = (total_prod_f / operating) if (operating > 0 and total_prod_f) else 0
            performance = (actual_speed / rated * 100) if rated else 0
            performance = min(performance, 100)

            rejected = float(run.rejected_qty or 0)
            total_prod = float(run.total_production or 0)
            quality = ((total_prod - rejected) / total_prod * 100) if total_prod > 0 else 100
            oee = (availability * performance * quality) / 10000

            run_oees.append({
                'run_id': run.id,
                'run_number': run.run_number,
                'date': run.date,
                'line': run.line.name,
                'availability': round(availability, 1),
                'performance': round(performance, 1),
                'quality': round(quality, 1),
                'oee': round(oee, 2),
            })

        data['per_run_oee'] = run_oees
        return Response(data)


class DowntimeAnalyticsAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewReports]

    def get(self, request):
        service = _get_service(request)
        date_from = request.GET.get('date_from')
        date_to = request.GET.get('date_to')
        machine_id = request.GET.get('machine')

        qs = MachineBreakdown.objects.filter(
            production_run__company=service.company
        ).select_related('machine', 'production_run')

        if date_from:
            qs = qs.filter(production_run__date__gte=date_from)
        if date_to:
            qs = qs.filter(production_run__date__lte=date_to)
        if machine_id:
            qs = qs.filter(machine_id=machine_id)

        from django.db.models import Sum, Count
        agg = qs.values('reason').annotate(
            count=Count('id'),
            total_minutes=Sum('breakdown_minutes')
        ).order_by('-total_minutes')

        return Response({
            'breakdowns': list(agg),
            'total_count': qs.count(),
            'total_minutes': qs.aggregate(t=Sum('breakdown_minutes'))['t'] or 0,
        })


class WasteAnalyticsAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewReports]

    def get(self, request):
        service = _get_service(request)
        date_from = request.GET.get('date_from')
        date_to = request.GET.get('date_to')

        qs = WasteLog.objects.filter(
            production_run__company=service.company
        )
        if date_from:
            qs = qs.filter(created_at__date__gte=date_from)
        if date_to:
            qs = qs.filter(created_at__date__lte=date_to)

        from django.db.models import Sum, Count
        by_material = qs.values('material_name', 'uom').annotate(
            total_waste=Sum('wastage_qty'),
            count=Count('id')
        ).order_by('-total_waste')

        by_status = qs.values('wastage_approval_status').annotate(count=Count('id'))

        return Response({
            'by_material': list(by_material),
            'by_approval_status': list(by_status),
            'total_waste_logs': qs.count(),
        })


# ===========================================================================
# Phase 1 Reports
# ===========================================================================

class ResourceConsumptionReportAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewReports]

    def get(self, request):
        from .services.report_service import ReportService
        service = _get_service(request)
        report = ReportService(service.company)
        data = report.get_daywise_resource_consumption(
            date_from=request.GET.get('date_from'),
            date_to=request.GET.get('date_to'),
            line_id=request.GET.get('line'),
        )
        return Response(data)


class MonthlySummaryReportAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewReports]

    def get(self, request):
        from .services.report_service import ReportService
        service = _get_service(request)
        year = request.GET.get('year')
        if not year:
            from django.utils import timezone
            year = timezone.now().year
        report = ReportService(service.company)
        data = report.get_monthly_summary(
            year=int(year),
            line_id=request.GET.get('line'),
        )
        return Response(data)


class PlanVsProductionReportAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewReports]

    def get(self, request):
        from .services.report_service import ReportService
        service = _get_service(request)
        company_code = request.company.company.code
        report = ReportService(service.company)
        data = report.get_plan_vs_production(
            company_code=company_code,
            date_from=request.GET.get('date_from'),
            date_to=request.GET.get('date_to'),
        )
        return Response(data)


class ProcurementVsPlannedReportAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewReports]

    def get(self, request):
        from .services.report_service import ReportService
        sap_doc_entry = request.GET.get('sap_doc_entry')
        if not sap_doc_entry:
            return Response(
                {'error': 'sap_doc_entry is required'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        service = _get_service(request)
        company_code = request.company.company.code
        report = ReportService(service.company)
        data = report.get_procurement_vs_planned(
            company_code=company_code,
            sap_doc_entry=int(sap_doc_entry),
        )
        return Response(data)


# ===========================================================================
# Phase 2 Reports
# ===========================================================================

class OEETrendReportAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewReports]

    def get(self, request):
        from .services.report_service import ReportService
        service = _get_service(request)
        report = ReportService(service.company)
        data = report.get_oee_trend(
            date_from=request.GET.get('date_from'),
            date_to=request.GET.get('date_to'),
            line_id=request.GET.get('line'),
            group_by=request.GET.get('group_by', 'daily'),
        )
        return Response(data)


class DowntimeParetoReportAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewReports]

    def get(self, request):
        from .services.report_service import ReportService
        service = _get_service(request)
        report = ReportService(service.company)
        data = report.get_downtime_pareto(
            date_from=request.GET.get('date_from'),
            date_to=request.GET.get('date_to'),
            line_id=request.GET.get('line'),
        )
        return Response(data)


class CostAnalysisReportAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewReports]

    def get(self, request):
        from .services.report_service import ReportService
        service = _get_service(request)
        report = ReportService(service.company)
        data = report.get_cost_analysis(
            date_from=request.GET.get('date_from'),
            date_to=request.GET.get('date_to'),
            line_id=request.GET.get('line'),
        )
        return Response(data)


class WasteTrendReportAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewReports]

    def get(self, request):
        from .services.report_service import ReportService
        service = _get_service(request)
        report = ReportService(service.company)
        data = report.get_waste_trend(
            date_from=request.GET.get('date_from'),
            date_to=request.GET.get('date_to'),
            line_id=request.GET.get('line'),
        )
        return Response(data)
