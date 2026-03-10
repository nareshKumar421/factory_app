import logging
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated

from company.permissions import HasCompanyContext
from sap_client.exceptions import SAPConnectionError, SAPDataError, SAPValidationError

from .services import ProductionPlanningService
from .serializers import (
    # Dropdowns
    ItemDropdownSerializer,
    UoMDropdownSerializer,
    WarehouseDropdownSerializer,
    BOMResponseSerializer,
    # Plan CRUD
    ProductionPlanCreateSerializer,
    ProductionPlanUpdateSerializer,
    ProductionPlanListSerializer,
    ProductionPlanDetailSerializer,
    PostToSAPResponseSerializer,
    PlanCloseResponseSerializer,
    # Materials
    PlanMaterialSerializer,
    PlanMaterialCreateSerializer,
    # Weekly plan
    WeeklyPlanSerializer,
    WeeklyPlanCreateSerializer,
    WeeklyPlanUpdateSerializer,
    # Daily entry
    DailyProductionEntrySerializer,
    DailyProductionEntryCreateSerializer,
    DailyProductionEntryResponseSerializer,
)
from .permissions import (
    CanCreateProductionPlan,
    CanEditProductionPlan,
    CanDeleteProductionPlan,
    CanPostPlanToSAP,
    CanViewProductionPlan,
    CanManageWeeklyPlan,
    CanAddDailyProduction,
    CanViewDailyProduction,
    CanCloseProductionPlan,
)

logger = logging.getLogger(__name__)


# ===========================================================================
# DROPDOWN APIs — SAP HANA data for form fields
# ===========================================================================

class ItemDropdownAPI(APIView):
    """
    Fetch items from SAP HANA for dropdown selection.

    GET /api/v1/production-planning/dropdown/items/
        ?type=finished    → finished goods (MakeItem=Y)
        ?type=raw         → raw materials (PrchseItem=Y)
        ?search=seeds     → search by code or name
    """
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewProductionPlan]

    def get(self, request):
        company_code = request.company.company.code
        service = ProductionPlanningService(company_code)

        item_type = request.GET.get('type')   # 'finished' | 'raw' | None
        search = request.GET.get('search')

        try:
            items = service.get_items_dropdown(item_type=item_type, search=search)
        except SAPConnectionError:
            return Response(
                {"detail": "SAP system unavailable. Cannot load items."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE
            )
        except SAPDataError as e:
            return Response({"detail": f"SAP error: {e}"}, status=status.HTTP_502_BAD_GATEWAY)

        serializer = ItemDropdownSerializer(
            [i.__dict__ for i in items], many=True
        )
        return Response(serializer.data)


class UoMDropdownAPI(APIView):
    """
    Fetch units of measure from SAP HANA.

    GET /api/v1/production-planning/dropdown/uom/
    """
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewProductionPlan]

    def get(self, request):
        company_code = request.company.company.code
        service = ProductionPlanningService(company_code)

        try:
            uom_list = service.get_uom_dropdown()
        except SAPConnectionError:
            return Response(
                {"detail": "SAP system unavailable. Cannot load UoM list."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE
            )
        except SAPDataError as e:
            return Response({"detail": f"SAP error: {e}"}, status=status.HTTP_502_BAD_GATEWAY)

        serializer = UoMDropdownSerializer(
            [u.__dict__ for u in uom_list], many=True
        )
        return Response(serializer.data)


class WarehouseDropdownAPI(APIView):
    """
    Fetch active warehouses from SAP HANA.

    GET /api/v1/production-planning/dropdown/warehouses/
    """
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewProductionPlan]

    def get(self, request):
        company_code = request.company.company.code
        service = ProductionPlanningService(company_code)

        try:
            warehouses = service.get_warehouses_dropdown()
        except SAPConnectionError:
            return Response(
                {"detail": "SAP system unavailable. Cannot load warehouses."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE
            )
        except SAPDataError as e:
            return Response({"detail": f"SAP error: {e}"}, status=status.HTTP_502_BAD_GATEWAY)

        serializer = WarehouseDropdownSerializer(
            [w.__dict__ for w in warehouses], many=True
        )
        return Response(serializer.data)


class BOMDropdownAPI(APIView):
    """
    Fetch production BOM for a finished-good item, scaled to planned_qty.
    Returns components with required qty, available stock, and shortage.

    GET /api/v1/production-planning/dropdown/bom/
        ?item_code=FG-OIL-1L        (required)
        &planned_qty=500            (optional, default 1)
    """
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewProductionPlan]

    def get(self, request):
        item_code = request.GET.get('item_code', '').strip()
        if not item_code:
            return Response(
                {"detail": "item_code query parameter is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            planned_qty = float(request.GET.get('planned_qty', 1))
            if planned_qty <= 0:
                raise ValueError
        except (ValueError, TypeError):
            return Response(
                {"detail": "planned_qty must be a positive number."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        company_code = request.company.company.code
        service = ProductionPlanningService(company_code)

        try:
            bom_data = service.get_bom_with_requirements(
                item_code=item_code, planned_qty=planned_qty
            )
        except SAPConnectionError:
            return Response(
                {"detail": "SAP system unavailable. Cannot load BOM."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except SAPDataError as e:
            return Response({"detail": f"SAP error: {e}"}, status=status.HTTP_502_BAD_GATEWAY)

        return Response(BOMResponseSerializer(bom_data).data)


# ===========================================================================
# PRODUCTION PLAN — CRUD
# ===========================================================================

class ProductionPlanListCreateAPI(APIView):
    """
    List all plans or create a new plan (DRAFT).

    GET  /api/v1/production-planning/
         ?status=OPEN&month=2026-03
    POST /api/v1/production-planning/
    """

    def get_permissions(self):
        if self.request.method == 'GET':
            return [IsAuthenticated(), HasCompanyContext(), CanViewProductionPlan()]
        return [IsAuthenticated(), HasCompanyContext(), CanCreateProductionPlan()]

    def get(self, request):
        company_code = request.company.company.code
        service = ProductionPlanningService(company_code)

        plans = service.get_plans(
            status=request.GET.get('status'),
            month=request.GET.get('month'),
        )
        return Response(ProductionPlanListSerializer(plans, many=True).data)

    def post(self, request):
        serializer = ProductionPlanCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                {"detail": "Invalid data.", "errors": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST
            )

        company_code = request.company.company.code
        service = ProductionPlanningService(company_code)

        try:
            plan = service.create_plan(serializer.validated_data, user=request.user)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(
            ProductionPlanDetailSerializer(plan).data,
            status=status.HTTP_201_CREATED
        )


class ProductionPlanDetailAPI(APIView):
    """
    Retrieve, update, or delete a production plan.

    GET    /api/v1/production-planning/<plan_id>/
    PATCH  /api/v1/production-planning/<plan_id>/   (DRAFT only)
    DELETE /api/v1/production-planning/<plan_id>/   (DRAFT only)
    """

    def get_permissions(self):
        if self.request.method == 'GET':
            return [IsAuthenticated(), HasCompanyContext(), CanViewProductionPlan()]
        if self.request.method == 'DELETE':
            return [IsAuthenticated(), HasCompanyContext(), CanDeleteProductionPlan()]
        return [IsAuthenticated(), HasCompanyContext(), CanEditProductionPlan()]

    def get(self, request, plan_id):
        company_code = request.company.company.code
        service = ProductionPlanningService(company_code)

        try:
            plan = service.get_plan(plan_id)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_404_NOT_FOUND)

        return Response(ProductionPlanDetailSerializer(plan).data)

    def patch(self, request, plan_id):
        company_code = request.company.company.code
        service = ProductionPlanningService(company_code)

        serializer = ProductionPlanUpdateSerializer(data=request.data, partial=True)
        if not serializer.is_valid():
            return Response(
                {"detail": "Invalid data.", "errors": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            plan = service.update_plan(plan_id, serializer.validated_data)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(ProductionPlanDetailSerializer(plan).data)

    def delete(self, request, plan_id):
        company_code = request.company.company.code
        service = ProductionPlanningService(company_code)

        try:
            service.delete_plan(plan_id)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(status=status.HTTP_204_NO_CONTENT)


class PostPlanToSAPAPI(APIView):
    """
    Post a DRAFT plan to SAP B1 as a Production Order.

    POST /api/v1/production-planning/<plan_id>/post-to-sap/
    """
    permission_classes = [IsAuthenticated, HasCompanyContext, CanPostPlanToSAP]

    def post(self, request, plan_id):
        company_code = request.company.company.code
        service = ProductionPlanningService(company_code)

        try:
            plan = service.post_to_sap(plan_id, user=request.user)

            response_data = {
                'success': True,
                'plan_id': plan.id,
                'sap_doc_entry': plan.sap_doc_entry,
                'sap_doc_num': plan.sap_doc_num,
                'sap_status': plan.sap_status,
                'message': f"Plan posted to SAP successfully. Production Order: {plan.sap_doc_num}",
            }
            return Response(PostToSAPResponseSerializer(response_data).data)

        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except SAPValidationError as e:
            return Response(
                {"detail": f"SAP validation error: {e}"},
                status=status.HTTP_400_BAD_REQUEST
            )
        except SAPConnectionError:
            return Response(
                {"detail": "SAP system is currently unavailable. Please try again later."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE
            )
        except SAPDataError as e:
            return Response(
                {"detail": f"SAP error: {e}"},
                status=status.HTTP_502_BAD_GATEWAY
            )


class ClosePlanAPI(APIView):
    """
    Close/complete a production plan.

    POST /api/v1/production-planning/<plan_id>/close/
    """
    permission_classes = [IsAuthenticated, HasCompanyContext, CanCloseProductionPlan]

    def post(self, request, plan_id):
        company_code = request.company.company.code
        service = ProductionPlanningService(company_code)

        try:
            plan = service.close_plan(plan_id, user=request.user)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        response_data = {
            'success': True,
            'plan_id': plan.id,
            'status': plan.status,
            'total_produced': plan.completed_qty,
            'planned_qty': plan.planned_qty,
            'message': f"Production plan closed. Total produced: {plan.completed_qty} units.",
        }
        return Response(PlanCloseResponseSerializer(response_data).data)


class PlanSummaryAPI(APIView):
    """
    Monthly summary — total plans, produced vs target, status breakdown.

    GET /api/v1/production-planning/summary/?month=2026-03
    """
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewProductionPlan]

    def get(self, request):
        company_code = request.company.company.code
        service = ProductionPlanningService(company_code)
        summary = service.get_summary(month=request.GET.get('month'))
        return Response(summary)


# ===========================================================================
# MATERIAL REQUIREMENTS
# ===========================================================================

class PlanMaterialListCreateAPI(APIView):
    """
    List or add BOM components for a plan.

    GET  /api/v1/production-planning/<plan_id>/materials/
    POST /api/v1/production-planning/<plan_id>/materials/
    """

    def get_permissions(self):
        if self.request.method == 'GET':
            return [IsAuthenticated(), HasCompanyContext(), CanViewProductionPlan()]
        return [IsAuthenticated(), HasCompanyContext(), CanEditProductionPlan()]

    def get(self, request, plan_id):
        from .models import PlanMaterialRequirement, ProductionPlan

        company_code = request.company.company.code
        if not ProductionPlan.objects.filter(
            id=plan_id, company__code=company_code
        ).exists():
            return Response({"detail": "Plan not found."}, status=status.HTTP_404_NOT_FOUND)

        materials = PlanMaterialRequirement.objects.filter(production_plan_id=plan_id)
        return Response(PlanMaterialSerializer(materials, many=True).data)

    def post(self, request, plan_id):
        serializer = PlanMaterialCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                {"detail": "Invalid data.", "errors": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST
            )

        company_code = request.company.company.code
        service = ProductionPlanningService(company_code)

        try:
            material = service.add_material(plan_id, serializer.validated_data)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(PlanMaterialSerializer(material).data, status=status.HTTP_201_CREATED)


class PlanMaterialDeleteAPI(APIView):
    """
    Remove a BOM component (DRAFT plans only).

    DELETE /api/v1/production-planning/<plan_id>/materials/<material_id>/
    """
    permission_classes = [IsAuthenticated, HasCompanyContext, CanEditProductionPlan]

    def delete(self, request, plan_id, material_id):
        company_code = request.company.company.code
        service = ProductionPlanningService(company_code)

        try:
            service.delete_material(plan_id, material_id)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(status=status.HTTP_204_NO_CONTENT)


# ===========================================================================
# WEEKLY PLANS
# ===========================================================================

class WeeklyPlanListCreateAPI(APIView):
    """
    GET  /api/v1/production-planning/<plan_id>/weekly-plans/
    POST /api/v1/production-planning/<plan_id>/weekly-plans/
    """

    def get_permissions(self):
        if self.request.method == 'GET':
            return [IsAuthenticated(), HasCompanyContext(), CanViewProductionPlan()]
        return [IsAuthenticated(), HasCompanyContext(), CanManageWeeklyPlan()]

    def get(self, request, plan_id):
        from .models import ProductionPlan, WeeklyPlan

        company_code = request.company.company.code
        if not ProductionPlan.objects.filter(id=plan_id, company__code=company_code).exists():
            return Response({"detail": "Plan not found."}, status=status.HTTP_404_NOT_FOUND)

        weekly_plans = WeeklyPlan.objects.filter(production_plan_id=plan_id)
        return Response(WeeklyPlanSerializer(weekly_plans, many=True).data)

    def post(self, request, plan_id):
        company_code = request.company.company.code
        service = ProductionPlanningService(company_code)

        try:
            plan = service.get_plan(plan_id)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_404_NOT_FOUND)

        serializer = WeeklyPlanCreateSerializer(
            data=request.data, context={'production_plan': plan}
        )
        if not serializer.is_valid():
            return Response(
                {"detail": "Invalid data.", "errors": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            weekly_plan = service.create_weekly_plan(
                plan_id=plan_id, data=serializer.validated_data, user=request.user
            )
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(WeeklyPlanSerializer(weekly_plan).data, status=status.HTTP_201_CREATED)


class WeeklyPlanDetailAPI(APIView):
    """
    PATCH  /api/v1/production-planning/<plan_id>/weekly-plans/<week_id>/
    DELETE /api/v1/production-planning/<plan_id>/weekly-plans/<week_id>/
    """
    permission_classes = [IsAuthenticated, HasCompanyContext, CanManageWeeklyPlan]

    def patch(self, request, plan_id, week_id):
        from .models import WeeklyPlan

        company_code = request.company.company.code
        service = ProductionPlanningService(company_code)

        try:
            plan = service.get_plan(plan_id)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_404_NOT_FOUND)

        try:
            instance = WeeklyPlan.objects.get(id=week_id, production_plan=plan)
        except WeeklyPlan.DoesNotExist:
            return Response({"detail": "Weekly plan not found."}, status=status.HTTP_404_NOT_FOUND)

        serializer = WeeklyPlanUpdateSerializer(
            instance, data=request.data, partial=True,
            context={'production_plan': plan}
        )
        if not serializer.is_valid():
            return Response(
                {"detail": "Invalid data.", "errors": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            weekly_plan = service.update_weekly_plan(
                plan_id, week_id, serializer.validated_data
            )
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(WeeklyPlanSerializer(weekly_plan).data)

    def delete(self, request, plan_id, week_id):
        company_code = request.company.company.code
        service = ProductionPlanningService(company_code)

        try:
            service.delete_weekly_plan(plan_id, week_id)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(status=status.HTTP_204_NO_CONTENT)


# ===========================================================================
# DAILY PRODUCTION ENTRIES
# ===========================================================================

class DailyEntryListCreateAPI(APIView):
    """
    GET  /api/v1/production-planning/weekly-plans/<week_id>/daily-entries/
    POST /api/v1/production-planning/weekly-plans/<week_id>/daily-entries/
    """

    def get_permissions(self):
        if self.request.method == 'GET':
            return [IsAuthenticated(), HasCompanyContext(), CanViewDailyProduction()]
        return [IsAuthenticated(), HasCompanyContext(), CanAddDailyProduction()]

    def get(self, request, week_id):
        from .models import WeeklyPlan

        company_code = request.company.company.code
        if not WeeklyPlan.objects.filter(
            id=week_id, production_plan__company__code=company_code
        ).exists():
            return Response({"detail": "Weekly plan not found."}, status=status.HTTP_404_NOT_FOUND)

        service = ProductionPlanningService(company_code)
        entries = service.get_daily_entries(week_id=week_id)
        return Response(DailyProductionEntrySerializer(entries, many=True).data)

    def post(self, request, week_id):
        from .models import WeeklyPlan

        company_code = request.company.company.code
        try:
            weekly_plan = WeeklyPlan.objects.select_related(
                'production_plan'
            ).get(id=week_id, production_plan__company__code=company_code)
        except WeeklyPlan.DoesNotExist:
            return Response({"detail": "Weekly plan not found."}, status=status.HTTP_404_NOT_FOUND)

        serializer = DailyProductionEntryCreateSerializer(
            data=request.data, context={'weekly_plan': weekly_plan}
        )
        if not serializer.is_valid():
            return Response(
                {"detail": "Invalid data.", "errors": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST
            )

        service = ProductionPlanningService(company_code)
        try:
            entry = service.add_daily_entry(week_id, serializer.validated_data, user=request.user)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(
            DailyProductionEntryResponseSerializer(entry).data,
            status=status.HTTP_201_CREATED
        )


class DailyEntryDetailAPI(APIView):
    """
    PATCH /api/v1/production-planning/weekly-plans/<week_id>/daily-entries/<entry_id>/
    """
    permission_classes = [IsAuthenticated, HasCompanyContext, CanAddDailyProduction]

    def patch(self, request, week_id, entry_id):
        company_code = request.company.company.code
        service = ProductionPlanningService(company_code)

        try:
            entry = service.update_daily_entry(week_id, entry_id, request.data)
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(DailyProductionEntrySerializer(entry).data)


class DailyEntryAllListAPI(APIView):
    """
    GET /api/v1/production-planning/daily-entries/
        ?plan_id=1&date_from=2026-03-01&date_to=2026-03-31
    """
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewDailyProduction]

    def get(self, request):
        company_code = request.company.company.code
        service = ProductionPlanningService(company_code)

        entries = service.get_daily_entries(
            plan_id=request.GET.get('plan_id'),
            date_from=request.GET.get('date_from'),
            date_to=request.GET.get('date_to'),
        )
        return Response(DailyProductionEntrySerializer(entries, many=True).data)
