"""Order-processing API.

Thin by design (Rule 6): every view resolves permissions, validates input and
delegates. The arithmetic lives in ``services/`` — nothing here decides anything.

Read endpoints need ``can_view_orders``; anything that triggers work needs the
matching action permission, because a stock check is a live SAP read and a sync
is a live OMS read.
"""
import logging

from django.db.models import Count, Q
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from company.permissions import HasCompanyContext

from .integrations.oms.reader import OmsUnavailable, ping
from .models import (
    MaterialRequirement,
    OmsOrder,
    OmsSyncRun,
    ProcessingEvent,
    ProcurementRequirement,
    ProcurementStatus,
    ProductionRequirement,
    RequirementStatus,
)
from .permissions import (
    CanPlanProcurement,
    CanPlanProduction,
    CanSyncOrders,
    CanViewOrders,
)
from .serializers import (
    MaterialRequirementSerializer,
    OmsOrderDetailSerializer,
    OmsOrderListSerializer,
    OmsSyncRunSerializer,
    ProcessingEventSerializer,
    ProcurementRequirementSerializer,
    ProductionRequirementSerializer,
    StockCheckSerializer,
)
from .services import availability, material_planning, order_sync, processing

logger = logging.getLogger(__name__)

PAGE_SIZE = 50


class BaseView(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewOrders]

    def _actor(self, request):
        return getattr(request.user, "email", "") or ""


class OrderListAPI(BaseView):
    """Orders, filterable. Counts are annotated so the list needs no per-row query."""

    def get(self, request):
        qs = (OmsOrder.objects
              .annotate(line_count=Count("lines", distinct=True),
                        issue_count=Count("lines", filter=~Q(lines__issues=[]), distinct=True)))

        state = request.query_params.get("state")
        oms_status = request.query_params.get("oms_status")
        search = request.query_params.get("search")
        if state:
            qs = qs.filter(state=state)
        if oms_status:
            qs = qs.filter(oms_status=oms_status)
        if search:
            qs = qs.filter(
                Q(order_number__icontains=search)
                | Q(customer_name__icontains=search)
                | Q(customer_code__icontains=search)
                | Q(lines__item_code__icontains=search)
            ).distinct()

        try:
            page = max(int(request.query_params.get("page", 1)), 1)
        except ValueError:
            page = 1
        total = qs.count()
        rows = qs[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]
        return Response({
            "count": total, "page": page, "page_size": PAGE_SIZE,
            "results": OmsOrderListSerializer(rows, many=True).data,
        })


class OrderDetailAPI(BaseView):
    def get(self, request, oms_order_id):
        order = OmsOrder.objects.filter(oms_order_id=oms_order_id).prefetch_related(
            "lines", "stock_checks__lines"
        ).first()
        if order is None:
            return Response({"detail": "Order not found.", "code": "NOT_FOUND"},
                            status=status.HTTP_404_NOT_FOUND)
        return Response(OmsOrderDetailSerializer(order).data)


class OrderTimelineAPI(BaseView):
    """Where an order is, and why — the audit trail for one order."""

    def get(self, request, oms_order_id):
        events = ProcessingEvent.objects.filter(
            entity_type="OmsOrder", entity_id=str(oms_order_id)
        )[:100]
        checks = StockCheckSerializer(
            OmsOrder.objects.filter(oms_order_id=oms_order_id).first().stock_checks.all()[:10]
            if OmsOrder.objects.filter(oms_order_id=oms_order_id).exists() else [],
            many=True,
        ).data
        return Response({
            "events": ProcessingEventSerializer(events, many=True).data,
            "checks": checks,
        })


class OrderCheckStockAPI(BaseView):
    """Run the availability check now. A live SAP read, hence the permission."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanPlanProduction]

    def post(self, request, oms_order_id):
        order = OmsOrder.objects.filter(oms_order_id=oms_order_id).first()
        if order is None:
            return Response({"detail": "Order not found.", "code": "NOT_FOUND"},
                            status=status.HTTP_404_NOT_FOUND)
        order, check, result = processing.process_order(order, actor=self._actor(request))
        return Response({
            "order": OmsOrderDetailSerializer(order).data,
            "check": StockCheckSerializer(check).data if check else None,
            "verdict": result.verdict if result else "SKIPPED",
        })


class ProductionRequirementListAPI(BaseView):
    def get(self, request):
        qs = (ProductionRequirement.objects
              .prefetch_related("sources__order", "materials")
              .order_by("needed_by", "-quantity"))
        state = request.query_params.get("status", "open")
        if state == "open":
            qs = qs.filter(status__in=[RequirementStatus.REQUIRED, RequirementStatus.PLANNED])
        elif state != "all":
            qs = qs.filter(status=state)
        return Response(ProductionRequirementSerializer(qs[:200], many=True).data)


class ProductionRequirementDetailAPI(BaseView):
    def get(self, request, pk):
        req = ProductionRequirement.objects.filter(pk=pk).prefetch_related(
            "sources__order", "materials").first()
        if req is None:
            return Response({"detail": "Not found.", "code": "NOT_FOUND"},
                            status=status.HTTP_404_NOT_FOUND)
        return Response(ProductionRequirementSerializer(req).data)


class PlanMaterialsAPI(BaseView):
    """Explode BOMs and roll up procurement. Live SAP reads; creates no SAP documents."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanPlanProcurement]

    def post(self, request):
        depth = int(request.data.get("bom_depth", 1) or 1)
        return Response(material_planning.plan_all(bom_depth=depth))


class MaterialRequirementListAPI(BaseView):
    def get(self, request):
        qs = MaterialRequirement.objects.select_related("requirement")
        if request.query_params.get("short_only") == "true":
            qs = qs.filter(net_required__gt=0, stock_known=True)
        return Response(MaterialRequirementSerializer(qs[:200], many=True).data)


class ProcurementRequirementListAPI(BaseView):
    def get(self, request):
        qs = ProcurementRequirement.objects.all()
        state = request.query_params.get("status", "open")
        if state == "open":
            qs = qs.filter(status__in=[ProcurementStatus.REQUIRED, ProcurementStatus.REQUESTED])
        elif state != "all":
            qs = qs.filter(status=state)
        return Response(ProcurementRequirementSerializer(qs[:200], many=True).data)


class SyncAPI(BaseView):
    """Trigger an OMS pull. A live read of another system, hence the permission."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanSyncOrders]

    def get(self, request):
        ok, detail = ping()
        return Response({
            "oms_reachable": ok, "detail": detail,
            "runs": OmsSyncRunSerializer(OmsSyncRun.objects.all()[:10], many=True).data,
        })

    def post(self, request):
        try:
            run = order_sync.sync_orders(
                full=bool(request.data.get("full")),
                limit=request.data.get("limit"),
                actor=self._actor(request),
            )
        except OmsUnavailable as exc:
            return Response({"detail": str(exc), "code": "OMS_UNAVAILABLE"},
                            status=status.HTTP_502_BAD_GATEWAY)
        return Response(OmsSyncRunSerializer(run).data, status=status.HTTP_201_CREATED)


class DashboardAPI(BaseView):
    """The counts the specification asks the system to surface."""

    def get(self, request):
        orders = OmsOrder.objects.all()
        by_state = dict(orders.values_list("state").annotate(n=Count("id")))
        short_materials = MaterialRequirement.objects.filter(
            net_required__gt=0, stock_known=True
        ).count()
        return Response({
            "orders": {
                "total": orders.count(),
                "by_state": by_state,
                "waiting_for_stock": by_state.get("PRODUCTION_REQUIRED", 0),
                "ready": by_state.get("READY_FOR_FULFILLMENT", 0),
                "unresolved": by_state.get("STOCK_CHECKED", 0),
            },
            "production": {
                "open": ProductionRequirement.objects.filter(
                    status__in=[RequirementStatus.REQUIRED, RequirementStatus.PLANNED]).count(),
            },
            "materials": {"short": short_materials},
            "procurement": {
                "open": ProcurementRequirement.objects.filter(
                    status__in=[ProcurementStatus.REQUIRED,
                                ProcurementStatus.REQUESTED]).count(),
            },
            "data_quality": {
                # Surfaced on the dashboard because a third of live lines carry one
                # of these, and an unexplained UNKNOWN order is worse than a
                # visible gap.
                "lines_with_issues": OmsOrder.objects.filter(~Q(lines__issues=[])).distinct().count(),
            },
            "last_sync": OmsSyncRunSerializer(OmsSyncRun.objects.first()).data
                         if OmsSyncRun.objects.exists() else None,
        })
