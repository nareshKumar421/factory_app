import logging

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated

from company.permissions import HasCompanyContext

from .services import BlowingService, BlowingReportService
from .services.sap_reader import BlowingItemReader, SAPReadError
from .serializers import (
    BlowingMachineSerializer, BlowingMachineCreateSerializer,
    PreformSpecSerializer, PreformSpecCreateSerializer,
    BlowingRateConfigSerializer, BlowingRateConfigCreateSerializer,
    BlowingRunListSerializer, BlowingRunDetailSerializer,
    BlowingRunCreateSerializer, BlowingRunUpdateSerializer,
    BlowingRunCostSerializer,
    BottleBuyPriceSerializer, BottleBuyPriceCreateSerializer,
    BlowingSegmentSerializer, BlowingBreakdownSerializer,
    BlowingBreakdownCategorySerializer, BlowingBreakdownCategoryCreateSerializer,
    StopProductionSerializer, AddBreakdownSerializer, ResolveBreakdownSerializer,
    AddManualSegmentSerializer, AddManualBreakdownSerializer,
    UpdateSegmentSerializer, CompleteRunSerializer,
    SubmitPreformRequestSerializer,
    BlowingAuditLogSerializer,
)
from .permissions import (
    CanViewBlowingRun, CanCreateBlowingRun, CanEditBlowingRun,
    CanCompleteBlowingRun, CanManageBlowingMachines, CanManagePreformSpecs,
    CanManageBlowingRateConfig, CanViewBlowingReports,
    CanCreateBlowingBreakdown, CanManageBlowingBreakdownCategories,
    CanRequestPreform,
)

logger = logging.getLogger(__name__)


def _get_service(request):
    return BlowingService(request.company.company.code)


def _bool_param(request, key):
    v = request.GET.get(key)
    if v is None:
        return None
    return v.lower() == 'true'


def _validation_error(serializer):
    return Response(
        {"detail": "Invalid data.", "errors": serializer.errors},
        status=status.HTTP_400_BAD_REQUEST,
    )


# ===========================================================================
# Machines
# ===========================================================================
class MachineListCreateAPI(APIView):
    def get_permissions(self):
        if self.request.method == 'GET':
            return [IsAuthenticated(), HasCompanyContext(), CanViewBlowingRun()]
        return [IsAuthenticated(), HasCompanyContext(), CanManageBlowingMachines()]

    def get(self, request):
        service = _get_service(request)
        machines = service.list_machines(is_active=_bool_param(request, 'is_active'))
        return Response(BlowingMachineSerializer(machines, many=True).data)

    def post(self, request):
        serializer = BlowingMachineCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return _validation_error(serializer)
        service = _get_service(request)
        try:
            machine = service.create_machine(serializer.validated_data, user=request.user)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(BlowingMachineSerializer(machine).data, status=status.HTTP_201_CREATED)


class MachineDetailAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanManageBlowingMachines]

    def patch(self, request, machine_id):
        service = _get_service(request)
        try:
            machine = service.update_machine(machine_id, request.data, user=request.user)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(BlowingMachineSerializer(machine).data)

    def delete(self, request, machine_id):
        service = _get_service(request)
        try:
            service.delete_machine(machine_id)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(status=status.HTTP_204_NO_CONTENT)


# ===========================================================================
# Preform specs
# ===========================================================================
class PreformSpecListCreateAPI(APIView):
    def get_permissions(self):
        if self.request.method == 'GET':
            return [IsAuthenticated(), HasCompanyContext(), CanViewBlowingRun()]
        return [IsAuthenticated(), HasCompanyContext(), CanManagePreformSpecs()]

    def get(self, request):
        service = _get_service(request)
        specs = service.list_preform_specs(is_active=_bool_param(request, 'is_active'))
        return Response(PreformSpecSerializer(specs, many=True).data)

    def post(self, request):
        serializer = PreformSpecCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return _validation_error(serializer)
        service = _get_service(request)
        try:
            spec = service.create_preform_spec(serializer.validated_data, user=request.user)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(PreformSpecSerializer(spec).data, status=status.HTTP_201_CREATED)


class PreformSpecDetailAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanManagePreformSpecs]

    def patch(self, request, spec_id):
        service = _get_service(request)
        try:
            spec = service.update_preform_spec(spec_id, request.data, user=request.user)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(PreformSpecSerializer(spec).data)

    def delete(self, request, spec_id):
        service = _get_service(request)
        try:
            service.delete_preform_spec(spec_id)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(status=status.HTTP_204_NO_CONTENT)


# ===========================================================================
# Rate config
# ===========================================================================
class RateConfigListCreateAPI(APIView):
    def get_permissions(self):
        if self.request.method == 'GET':
            return [IsAuthenticated(), HasCompanyContext(), CanViewBlowingRun()]
        return [IsAuthenticated(), HasCompanyContext(), CanManageBlowingRateConfig()]

    def get(self, request):
        service = _get_service(request)
        configs = service.list_rate_configs(is_active=_bool_param(request, 'is_active'))
        return Response(BlowingRateConfigSerializer(configs, many=True).data)

    def post(self, request):
        serializer = BlowingRateConfigCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return _validation_error(serializer)
        service = _get_service(request)
        try:
            rc = service.create_rate_config(serializer.validated_data, user=request.user)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(BlowingRateConfigSerializer(rc).data, status=status.HTTP_201_CREATED)


class RateConfigDetailAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanManageBlowingRateConfig]

    def patch(self, request, config_id):
        service = _get_service(request)
        try:
            rc = service.update_rate_config(config_id, request.data, user=request.user)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(BlowingRateConfigSerializer(rc).data)


# ===========================================================================
# Runs
# ===========================================================================
class RunListCreateAPI(APIView):
    def get_permissions(self):
        if self.request.method == 'GET':
            return [IsAuthenticated(), HasCompanyContext(), CanViewBlowingRun()]
        return [IsAuthenticated(), HasCompanyContext(), CanCreateBlowingRun()]

    def get(self, request):
        service = _get_service(request)
        runs = service.list_runs(
            date_from=request.GET.get('date_from') or None,
            date_to=request.GET.get('date_to') or None,
            machine_id=request.GET.get('machine_id') or None,
            status=request.GET.get('status') or None,
        )
        return Response(BlowingRunListSerializer(runs, many=True).data)

    def post(self, request):
        serializer = BlowingRunCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return _validation_error(serializer)
        service = _get_service(request)
        try:
            run = service.create_run(serializer.validated_data, user=request.user)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(BlowingRunDetailSerializer(run).data, status=status.HTTP_201_CREATED)


class RunDetailAPI(APIView):
    def get_permissions(self):
        if self.request.method == 'GET':
            return [IsAuthenticated(), HasCompanyContext(), CanViewBlowingRun()]
        return [IsAuthenticated(), HasCompanyContext(), CanEditBlowingRun()]

    def get(self, request, run_id):
        service = _get_service(request)
        try:
            run = service.get_run(run_id)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_404_NOT_FOUND)
        return Response(BlowingRunDetailSerializer(run).data)

    def patch(self, request, run_id):
        serializer = BlowingRunUpdateSerializer(data=request.data)
        if not serializer.is_valid():
            return _validation_error(serializer)
        service = _get_service(request)
        try:
            run = service.update_run(run_id, serializer.validated_data, user=request.user)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(BlowingRunDetailSerializer(run).data)


class CompleteRunAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanCompleteBlowingRun]

    def post(self, request, run_id):
        serializer = CompleteRunSerializer(data=request.data)
        if not serializer.is_valid():
            return _validation_error(serializer)
        service = _get_service(request)
        try:
            run = service.complete_run(run_id, serializer.validated_data, user=request.user)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(BlowingRunDetailSerializer(run).data)


# ===========================================================================
# Run lifecycle — segments / breakdowns
# ===========================================================================
class StartProductionAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanEditBlowingRun]

    def post(self, request, run_id):
        service = _get_service(request)
        try:
            seg = service.start_production(run_id, user=request.user)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(BlowingSegmentSerializer(seg).data, status=status.HTTP_201_CREATED)


class StopProductionAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanEditBlowingRun]

    def post(self, request, run_id):
        serializer = StopProductionSerializer(data=request.data)
        if not serializer.is_valid():
            return _validation_error(serializer)
        service = _get_service(request)
        try:
            seg = service.stop_production(
                run_id, serializer.validated_data['produced_pcs'],
                remarks=serializer.validated_data.get('remarks', ''), user=request.user)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(BlowingSegmentSerializer(seg).data)


class AddBreakdownAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanCreateBlowingBreakdown]

    def post(self, request, run_id):
        serializer = AddBreakdownSerializer(data=request.data)
        if not serializer.is_valid():
            return _validation_error(serializer)
        service = _get_service(request)
        try:
            bd = service.add_breakdown(run_id, serializer.validated_data, user=request.user)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(BlowingBreakdownSerializer(bd).data, status=status.HTTP_201_CREATED)


class ResolveBreakdownAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanCreateBlowingBreakdown]

    def post(self, request, run_id, breakdown_id):
        serializer = ResolveBreakdownSerializer(data=request.data)
        if not serializer.is_valid():
            return _validation_error(serializer)
        service = _get_service(request)
        try:
            bd = service.resolve_breakdown(
                run_id, breakdown_id, serializer.validated_data['action'], user=request.user)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(BlowingBreakdownSerializer(bd).data)


class AddManualSegmentAPI(APIView):
    """Backfill a completed running segment with explicit start/end times."""
    permission_classes = [IsAuthenticated, HasCompanyContext, CanEditBlowingRun]

    def post(self, request, run_id):
        serializer = AddManualSegmentSerializer(data=request.data)
        if not serializer.is_valid():
            return _validation_error(serializer)
        service = _get_service(request)
        try:
            seg = service.add_manual_segment(run_id, serializer.validated_data, user=request.user)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(BlowingSegmentSerializer(seg).data, status=status.HTTP_201_CREATED)


class AddManualBreakdownAPI(APIView):
    """Backfill a completed breakdown with explicit start/end times."""
    permission_classes = [IsAuthenticated, HasCompanyContext, CanCreateBlowingBreakdown]

    def post(self, request, run_id):
        serializer = AddManualBreakdownSerializer(data=request.data)
        if not serializer.is_valid():
            return _validation_error(serializer)
        service = _get_service(request)
        try:
            bd = service.add_manual_breakdown(run_id, serializer.validated_data, user=request.user)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(BlowingBreakdownSerializer(bd).data, status=status.HTTP_201_CREATED)


class SegmentUpdateAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanEditBlowingRun]

    def patch(self, request, run_id, segment_id):
        serializer = UpdateSegmentSerializer(data=request.data)
        if not serializer.is_valid():
            return _validation_error(serializer)
        service = _get_service(request)
        try:
            seg = service.update_segment(run_id, segment_id, serializer.validated_data)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(BlowingSegmentSerializer(seg).data)


# ===========================================================================
# Breakdown categories (master data)
# ===========================================================================
class BreakdownCategoryListCreateAPI(APIView):
    def get_permissions(self):
        if self.request.method == 'GET':
            return [IsAuthenticated(), HasCompanyContext(), CanViewBlowingRun()]
        return [IsAuthenticated(), HasCompanyContext(), CanManageBlowingBreakdownCategories()]

    def get(self, request):
        service = _get_service(request)
        cats = service.list_breakdown_categories(is_active=_bool_param(request, 'is_active'))
        return Response(BlowingBreakdownCategorySerializer(cats, many=True).data)

    def post(self, request):
        serializer = BlowingBreakdownCategoryCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return _validation_error(serializer)
        service = _get_service(request)
        try:
            cat = service.create_breakdown_category(serializer.validated_data, user=request.user)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(BlowingBreakdownCategorySerializer(cat).data, status=status.HTTP_201_CREATED)


class BreakdownCategoryDetailAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanManageBlowingBreakdownCategories]

    def patch(self, request, category_id):
        service = _get_service(request)
        try:
            cat = service.update_breakdown_category(category_id, request.data, user=request.user)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(BlowingBreakdownCategorySerializer(cat).data)


# ===========================================================================
# Warehouse-approval gate — preform request
# ===========================================================================
class SubmitPreformRequestAPI(APIView):
    """Raise a preform request into the Warehouse → BOM Requests queue."""
    permission_classes = [IsAuthenticated, HasCompanyContext, CanRequestPreform]

    def post(self, request, run_id):
        serializer = SubmitPreformRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return _validation_error(serializer)
        service = _get_service(request)
        try:
            service.submit_preform_request(
                run_id, remarks=serializer.validated_data.get('remarks', ''), user=request.user)
            run = service.get_run(run_id)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(BlowingRunDetailSerializer(run).data, status=status.HTTP_201_CREATED)


class RunCostAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewBlowingRun]

    def get(self, request, run_id):
        service = _get_service(request)
        try:
            run = service.get_run(run_id)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_404_NOT_FOUND)
        cost = getattr(run, 'cost_summary', None)
        if cost is None:
            return Response({"detail": "No cost computed for this run."},
                            status=status.HTTP_404_NOT_FOUND)
        return Response(BlowingRunCostSerializer(cost).data)


# ===========================================================================
# Reports
# ===========================================================================
class DailyReportAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewBlowingReports]

    def get(self, request):
        date = request.GET.get('date')
        if not date:
            return Response({"detail": "Query param 'date' is required (YYYY-MM-DD)."},
                            status=status.HTTP_400_BAD_REQUEST)
        service = _get_service(request)
        report = BlowingReportService(service.company).get_daily_report(date)
        return Response(report)


class MonthlyReportAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewBlowingReports]

    def get(self, request):
        try:
            year = int(request.GET.get('year'))
            month = int(request.GET.get('month'))
        except (TypeError, ValueError):
            return Response({"detail": "Query params 'year' and 'month' are required."},
                            status=status.HTTP_400_BAD_REQUEST)
        service = _get_service(request)
        report = BlowingReportService(service.company).get_monthly_summary(year, month)
        return Response(report)


# ===========================================================================
# Buy prices (Phase 2)
# ===========================================================================
class BuyPriceListCreateAPI(APIView):
    def get_permissions(self):
        if self.request.method == 'GET':
            return [IsAuthenticated(), HasCompanyContext(), CanViewBlowingRun()]
        return [IsAuthenticated(), HasCompanyContext(), CanManageBlowingRateConfig()]

    def get(self, request):
        service = _get_service(request)
        prices = service.list_buy_prices(
            is_active=_bool_param(request, 'is_active'),
            preform_spec_id=request.GET.get('preform_spec_id') or None,
        )
        return Response(BottleBuyPriceSerializer(prices, many=True).data)

    def post(self, request):
        serializer = BottleBuyPriceCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return _validation_error(serializer)
        service = _get_service(request)
        try:
            bp = service.create_buy_price(serializer.validated_data, user=request.user)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(BottleBuyPriceSerializer(bp).data, status=status.HTTP_201_CREATED)


class BuyPriceDetailAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanManageBlowingRateConfig]

    def patch(self, request, buy_id):
        service = _get_service(request)
        try:
            bp = service.update_buy_price(buy_id, request.data, user=request.user)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(BottleBuyPriceSerializer(bp).data)


class MakeVsBuyReportAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewBlowingReports]

    def get(self, request):
        date_from = request.GET.get('date_from')
        date_to = request.GET.get('date_to')
        if not date_from or not date_to:
            return Response(
                {"detail": "Query params 'date_from' and 'date_to' are required."},
                status=status.HTTP_400_BAD_REQUEST)
        service = _get_service(request)
        report = BlowingReportService(service.company).get_make_vs_buy(
            date_from, date_to,
            preform_spec_id=request.GET.get('preform_spec_id') or None,
        )
        return Response(report)


class VarianceReportAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewBlowingReports]

    def get(self, request):
        date_from = request.GET.get('date_from')
        date_to = request.GET.get('date_to')
        if not date_from or not date_to:
            return Response(
                {"detail": "Query params 'date_from' and 'date_to' are required."},
                status=status.HTTP_400_BAD_REQUEST)
        service = _get_service(request)
        report = BlowingReportService(service.company).get_variances(date_from, date_to)
        return Response(report)


# ===========================================================================
# Audit log
# ===========================================================================
class AuditLogListAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewBlowingRun]

    def get(self, request):
        entity_type = request.GET.get('entity_type')
        entity_id = request.GET.get('entity_id')
        if not entity_type or not entity_id:
            return Response(
                {"detail": "Query params 'entity_type' and 'entity_id' are required."},
                status=status.HTTP_400_BAD_REQUEST)
        service = _get_service(request)
        logs = service.list_audit_logs(entity_type, entity_id)
        return Response(BlowingAuditLogSerializer(logs, many=True).data)


# ===========================================================================
# SAP item pickers (read-only)
# ===========================================================================
class SAPItemSearchAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewBlowingRun]

    def get(self, request):
        kind = request.GET.get('kind', 'preform')
        search = request.GET.get('search', '')
        company_code = request.company.company.code
        try:
            reader = BlowingItemReader(company_code)
            if kind == 'bottle':
                items = reader.search_bottle_items(search=search)
            else:
                items = reader.search_preform_items(search=search)
        except SAPReadError as e:
            return Response({"detail": str(e)}, status=status.HTTP_502_BAD_GATEWAY)
        return Response(items)
