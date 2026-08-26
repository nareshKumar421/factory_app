"""planning_purchase/views.py

Two screens' worth of API: read the plan SAP holds, and raise the purchase orders
its bill of materials implies.

Every endpoint needs a JWT, a `Company-Code` header, and a module permission.
SAP failures map the way the rest of the platform maps them, so the frontend's
existing "SAP unavailable" handling works here without a special case.
"""

import logging

from django.db.models import Count, Q
from django.http import HttpResponse
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from company.permissions import HasCompanyContext
from sap_client.exceptions import SAPConnectionError, SAPDataError, SAPValidationError

from .models import PurchaseOrder, PurchaseOrderStatus
from .permissions import (
    CanApprovePurchaseOrder,
    CanCreatePurchaseOrder,
    CanPostPurchaseOrderToSAP,
    CanViewProductionPlan,
)
from .serializers import (
    CancelSerializer,
    CommitmentDocumentSerializer,
    CommitmentQuerySerializer,
    CommitmentSourceSerializer,
    PlanDetailQuerySerializer,
    PlanHeaderSerializer,
    PlanLineSerializer,
    PlanListQuerySerializer,
    ProducibleComponentSerializer,
    ProducibleQuerySerializer,
    ProducibleSkuSerializer,
    PurchaseOrderCreateSerializer,
    PurchaseOrderListQuerySerializer,
    PurchaseOrderSerializer,
    PurchaseOrderUpdateSerializer,
    RequirementQuerySerializer,
    RequirementResourceSerializer,
    RequirementRowSerializer,
    VendorQuerySerializer,
)
from .services import PlanService, PurchaseOrderService
from .services.errors import PlanningError

logger = logging.getLogger(__name__)


def _company_code(request) -> str:
    return request.company.company.code


class PlanningBaseView(APIView):
    """Shared auth, company scoping and error mapping."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewProductionPlan]

    def handle_exception(self, exc):
        if isinstance(exc, PlanningError):
            return Response(
                {"detail": exc.message, "code": exc.code}, status=exc.status_code
            )
        if isinstance(exc, SAPConnectionError):
            return Response(
                {"detail": "SAP system is currently unavailable. Please try again later."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        if isinstance(exc, SAPValidationError):
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        if isinstance(exc, SAPDataError):
            # Generic on purpose. `inventory_age` leaked raw HANA text into API
            # responses; the other dashboards do not, and neither does this.
            return Response(
                {"detail": "Failed to read planning data from SAP."},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        return super().handle_exception(exc)


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------


class PlanListAPI(PlanningBaseView):
    """Production plans authored in SAP.

    GET /api/v1/production-planning/plans/
    """

    def get(self, request):
        query = PlanListQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)

        result = PlanService(_company_code(request)).list_plans(
            limit=query.validated_data["limit"]
        )
        return Response({
            "data": PlanHeaderSerializer(result["data"], many=True).data,
            "meta": result["meta"],
        })


class PlanDetailAPI(PlanningBaseView):
    """One plan, phased into day, week or month buckets, against actual production.

    GET /api/v1/production-planning/plans/<abs_id>/?bucket_type=WEEK
    """

    def get(self, request, abs_id: int):
        query = PlanDetailQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        filters = query.validated_data

        result = PlanService(_company_code(request)).get_plan(
            abs_id,
            bucket_type=filters["bucket_type"],
            spread_policy=filters["spread_policy"],
            include_actuals=filters["include_actuals"],
        )
        return Response({
            "plan": PlanHeaderSerializer(result["plan"]).data,
            "lines": PlanLineSerializer(result["lines"], many=True).data,
            "buckets": result["buckets"],
            "meta": result["meta"],
        })


class PlanRequirementAPI(PlanningBaseView):
    """What the plan consumes, what is available, and what must be bought.

    GET /api/v1/production-planning/plans/<abs_id>/requirement/
    """

    def get(self, request, abs_id: int):
        query = RequirementQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        filters = query.validated_data

        result = PlanService(_company_code(request)).get_requirement(
            abs_id,
            material_type=filters.get("material_type"),
            warehouses=filters.get("warehouse") or None,
            include_covered=filters["include_covered"],
        )
        return Response({
            "plan": PlanHeaderSerializer(result["plan"]).data,
            "data": RequirementRowSerializer(result["data"], many=True).data,
            "resources": RequirementResourceSerializer(
                result["resources"], many=True
            ).data,
            "meta": result["meta"],
        })


class PlanProducibleAPI(PlanningBaseView):
    """What the floor can actually build from the stock on hand.

    GET /api/v1/planning-purchase/plans/<abs_id>/producible/

    Two answers in one payload, deliberately kept apart. `skus` is the standalone
    maximum for each product if it had the warehouse to itself — figures that are
    alternatives to one another and must never be totalled. `components` is the
    additive answer: what the whole day's planned mix consumes against stock.
    """

    def get(self, request, abs_id: int):
        query = ProducibleQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        filters = query.validated_data

        result = PlanService(_company_code(request)).get_producible(
            abs_id,
            target_date=filters.get("target_date"),
            warehouses=filters.get("warehouse") or None,
            spread_policy=filters["spread_policy"],
            stock_basis=filters["stock_basis"],
        )
        return Response({
            "plan": PlanHeaderSerializer(result["plan"]).data,
            "target_date": result["target_date"],
            "skus": ProducibleSkuSerializer(result["skus"], many=True).data,
            "components": ProducibleComponentSerializer(
                result["components"], many=True
            ).data,
            "meta": result["meta"],
        })


class PlanRequirementExportAPI(PlanningBaseView):
    """The requirement table as an .xlsx, so procurement can circulate it.

    GET /api/v1/production-planning/plans/<abs_id>/requirement/export/
    """

    HEADERS = [
        "Component", "Description", "Type", "UoM",
        "Required", "On Hand", "Committed", "Net Available",
        "On Open PO", "Shortage", "Suggested Order", "MOQ",
        "Lead Days", "Need By", "Order By", "Urgency",
        "Supplier", "Unit Price", "Estimated Value", "Used By SKUs",
    ]

    def get(self, request, abs_id: int):
        query = RequirementQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        filters = query.validated_data

        result = PlanService(_company_code(request)).get_requirement(
            abs_id,
            material_type=filters.get("material_type"),
            warehouses=filters.get("warehouse") or None,
            include_covered=filters["include_covered"],
        )
        return self._workbook(result)

    def _workbook(self, result):
        import openpyxl
        from openpyxl.styles import Font

        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.title = "Material Requirement"
        sheet.append(self.HEADERS)
        for cell in sheet[1]:
            cell.font = Font(bold=True)

        for row in result["data"]:
            sheet.append([
                row["component_code"],
                row["component_name"],
                row["material_type"],
                row["uom"],
                float(row["required_qty"]),
                float(row["on_hand_qty"]),
                float(row["committed_qty"]),
                float(row["net_available_qty"]),
                float(row["on_order_qty"]),
                float(row["shortage_qty"]),
                float(row["suggested_order_qty"]),
                float(row["moq"]) if row.get("moq") else "",
                row["lead_time_days"] if row["lead_time_days"] is not None else "",
                row["need_by_date"] or "",
                row["order_by_date"] or "",
                row["urgency"],
                row["vendor_name"],
                float(row["unit_price"]),
                float(row["estimated_value"]),
                ", ".join(use["item_code"] for use in row["used_by"]),
            ])

        for column in sheet.columns:
            width = max(
                (len(str(cell.value)) for cell in column if cell.value is not None),
                default=10,
            )
            sheet.column_dimensions[column[0].column_letter].width = min(width + 2, 45)

        plan = result["plan"]
        name = f"requirement_{plan['code'] or plan['abs_id']}_{timezone.localdate()}.xlsx"
        safe_name = "".join(c if c.isalnum() or c in "._-" else "_" for c in name)

        response = HttpResponse(
            content_type=(
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )
        )
        response["Content-Disposition"] = f'attachment; filename="{safe_name}"'
        workbook.save(response)
        return response


# ---------------------------------------------------------------------------
# Dropdowns
# ---------------------------------------------------------------------------


class CommitmentBreakdownAPI(PlanningBaseView):
    """The documents behind a committed-stock figure.

    GET /api/v1/planning-purchase/commitments/?item_code=RM0000003&warehouse=BH-LO

    SAP publishes `IsCommited` as one number with no explanation, and it is the
    figure that decides whether a component reads as available. The response
    always states whether the documents add up to it, because a confident
    explanation missing a document type would be worse than none.
    """

    def get(self, request):
        query = CommitmentQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)

        result = PlanService(_company_code(request)).get_commitments(
            query.validated_data["item_code"], query.validated_data["warehouse"]
        )
        return Response({
            **{
                key: result[key]
                for key in (
                    "item_code", "item_name", "warehouse", "uom",
                    "on_hand_qty", "committed_qty", "on_order_qty", "free_qty",
                )
            },
            "documents": CommitmentDocumentSerializer(
                result["documents"], many=True
            ).data,
            "by_source": CommitmentSourceSerializer(
                result["by_source"], many=True
            ).data,
            "meta": result["meta"],
        })


class VendorListAPI(PlanningBaseView):
    """GET /api/v1/production-planning/vendors/?search=cap"""

    def get(self, request):
        query = VendorQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)

        service = PlanService(_company_code(request))
        rows = service.reader.get_vendors(
            search=query.validated_data["search"],
            limit=query.validated_data["limit"],
        )
        return Response({
            "data": [
                {
                    "vendor_code": row["CardCode"],
                    "vendor_name": row["CardName"],
                    "currency": row.get("Currency") or "",
                }
                for row in rows
            ]
        })


class WarehouseListAPI(PlanningBaseView):
    """GET /api/v1/production-planning/warehouses/"""

    def get(self, request):
        service = PlanService(_company_code(request))
        return Response({
            "data": [
                {"warehouse_code": row["WhsCode"], "warehouse_name": row["WhsName"]}
                for row in service.reader.get_warehouses()
            ]
        })


# ---------------------------------------------------------------------------
# Purchase orders
# ---------------------------------------------------------------------------


class PurchaseOrderListCreateAPI(PlanningBaseView):
    """List purchase orders raised from plans, or raise new ones.

    GET  /api/v1/production-planning/purchase-orders/
    POST /api/v1/production-planning/purchase-orders/
    """

    def get_permissions(self):
        if self.request.method == "POST":
            return [
                IsAuthenticated(), HasCompanyContext(), CanCreatePurchaseOrder(),
            ]
        return super().get_permissions()

    def get(self, request):
        query = PurchaseOrderListQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        filters = query.validated_data

        queryset = (
            PurchaseOrder.objects
            .filter(company_code=_company_code(request))
            .prefetch_related("lines")
        )
        if filters.get("status"):
            queryset = queryset.filter(status=filters["status"])
        if filters.get("plan_abs_id"):
            queryset = queryset.filter(plan_abs_id=filters["plan_abs_id"])
        if filters.get("search"):
            token = filters["search"]
            queryset = queryset.filter(
                Q(vendor_name__icontains=token)
                | Q(vendor_code__icontains=token)
                | Q(plan_code__icontains=token)
                | Q(lines__item_code__icontains=token)
            ).distinct()

        page = filters["page"]
        page_size = filters["page_size"]
        total = queryset.count()
        rows = queryset[(page - 1) * page_size: page * page_size]

        return Response({
            "data": PurchaseOrderSerializer(rows, many=True).data,
            "meta": {
                "total": total,
                "page": page,
                "page_size": page_size,
                "total_pages": max(1, (total + page_size - 1) // page_size),
                "status_counts": self._status_counts(_company_code(request)),
            },
        })

    def post(self, request):
        payload = PurchaseOrderCreateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)

        service = PurchaseOrderService(_company_code(request), user=request.user)
        orders = service.create_from_requirement(payload.validated_data)

        return Response(
            {
                "data": PurchaseOrderSerializer(orders, many=True).data,
                "meta": {
                    "created": len(orders),
                    "note": (
                        "One draft per supplier — a SAP purchase order belongs to "
                        "exactly one business partner."
                    ),
                },
            },
            status=status.HTTP_201_CREATED,
        )

    @staticmethod
    def _status_counts(company_code):
        """Every status keyed, including the ones at zero.

        A missing key reads as "no data" in the UI; an explicit zero reads as
        "nothing waiting", which is the answer the buyer actually wants.
        """
        counts = dict.fromkeys(PurchaseOrderStatus.values, 0)
        rows = (
            PurchaseOrder.objects
            .filter(company_code=company_code)
            .values("status")
            .annotate(count=Count("id"))
        )
        for row in rows:
            counts[row["status"]] = row["count"]
        return counts


class PurchaseOrderDetailAPI(PlanningBaseView):
    """GET / PATCH / DELETE one purchase order."""

    def get_permissions(self):
        if self.request.method in ("PATCH", "PUT", "DELETE"):
            return [IsAuthenticated(), HasCompanyContext(), CanCreatePurchaseOrder()]
        return super().get_permissions()

    def _get_object(self, request, order_id):
        try:
            return (
                PurchaseOrder.objects
                .prefetch_related("lines")
                .get(pk=order_id, company_code=_company_code(request))
            )
        except PurchaseOrder.DoesNotExist:
            raise PlanningError("Purchase order not found.", "not_found", 404)

    def get(self, request, order_id: int):
        order = self._get_object(request, order_id)
        return Response(PurchaseOrderSerializer(order).data)

    def patch(self, request, order_id: int):
        order = self._get_object(request, order_id)
        payload = PurchaseOrderUpdateSerializer(data=request.data, partial=True)
        payload.is_valid(raise_exception=True)

        service = PurchaseOrderService(_company_code(request), user=request.user)
        order = service.update_draft(order, payload.validated_data)
        return Response(PurchaseOrderSerializer(order).data)

    def delete(self, request, order_id: int):
        order = self._get_object(request, order_id)
        payload = CancelSerializer(data=request.data or {})
        payload.is_valid(raise_exception=True)

        service = PurchaseOrderService(_company_code(request), user=request.user)
        order = service.cancel(order, payload.validated_data["reason"])
        return Response(PurchaseOrderSerializer(order).data)


class PurchaseOrderApproveAPI(PlanningBaseView):
    """POST /api/v1/production-planning/purchase-orders/<id>/approve/"""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanApprovePurchaseOrder]

    def post(self, request, order_id: int):
        try:
            order = PurchaseOrder.objects.get(
                pk=order_id, company_code=_company_code(request)
            )
        except PurchaseOrder.DoesNotExist:
            raise PlanningError("Purchase order not found.", "not_found", 404)

        service = PurchaseOrderService(_company_code(request), user=request.user)
        order = service.approve(order)
        return Response(PurchaseOrderSerializer(order).data)


class PurchaseOrderPostAPI(PlanningBaseView):
    """POST /api/v1/production-planning/purchase-orders/<id>/post-to-sap/

    Creates the real document in SAP. Refuses anything that is not approved, and
    cannot post the same order twice.
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanPostPurchaseOrderToSAP]

    def post(self, request, order_id: int):
        from .services.purchase_service import simulate_sap

        if not PurchaseOrder.objects.filter(
            pk=order_id, company_code=_company_code(request)
        ).exists():
            raise PlanningError("Purchase order not found.", "not_found", 404)

        service = PurchaseOrderService(_company_code(request), user=request.user)
        order = service.post_to_sap(order_id)

        return Response({
            "data": PurchaseOrderSerializer(order).data,
            "meta": {
                "simulated": order.simulated,
                "note": (
                    "Simulate mode is on — no document was created in SAP."
                    if simulate_sap()
                    else "Created in SAP."
                ),
            },
        })
