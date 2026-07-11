"""Views for the Flipkart sheet-import + warehouse-issue flow.

Thin APIViews over the services; company-scoped and permission-gated exactly like
``views.py``. See ``MARKETPLACE_FLIPKART_SHEET_FLOW.md``.
"""
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response

from . import permissions as mp_perms
from .models import (
    MarketplaceChannel,
    MarketplaceIssueRequest,
    OrderImportBatch,
)
from .serializers_sheet import (
    MarketplaceIssueRequestDetailSerializer,
    MarketplaceIssueRequestListSerializer,
    OrderImportBatchSerializer,
    ReceiveSerializer,
    RejectSerializer,
    ReviewSerializer,
    SendIssueRequestSerializer,
    StockListSerializer,
)
from .services import (
    batch_resolve_service,
    issuance_export_service,
    issue_request_service,
    order_import_service,
    warehouse_insights_service,
)
from .services.errors import MarketplaceError
from .views import MpBaseView


def _read_sheet(request):
    """Extract (text, filename) from a multipart ``file`` or JSON ``{text, filename}``."""
    upload = request.FILES.get("file")
    if upload is not None:
        return upload.read().decode("utf-8-sig", errors="replace"), upload.name
    return (request.data.get("text") or ""), (request.data.get("filename") or "pasted.csv")


def _as_bool(value):
    return str(value).strip().lower() in ("1", "true", "yes", "on")


class OrderImportPreviewView(MpBaseView):
    """Dry-run analysis of a sheet: new vs duplicate orders + unmapped SKUs.

    Writes nothing — drives the pre-import review so the user can acknowledge
    duplicates before they are re-imported.
    """

    write_perms = [mp_perms.CanImportOrders]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def post(self, request):
        text, _filename = _read_sheet(request)
        if not text.strip():
            raise MarketplaceError("No sheet content received.", code="BAD_SHEET", status_code=400)
        report = order_import_service.analyze(
            self.company, text=text, channel=MarketplaceChannel.FLIPKART,
        )
        return Response(report)


class OrderImportView(MpBaseView):
    """POST a Flipkart order sheet (multipart ``file`` or JSON ``{text, filename}``).

    ``skip_duplicates`` (bool) imports only new orders, leaving already-present
    orders untouched (used when the user declines to re-import duplicates).
    """

    write_perms = [mp_perms.CanImportOrders]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def post(self, request):
        text, filename = _read_sheet(request)
        if not text.strip():
            raise MarketplaceError("No sheet content received.", code="BAD_SHEET", status_code=400)

        batch = order_import_service.ingest(
            self.company, text=text, filename=filename, user=request.user,
            channel=MarketplaceChannel.FLIPKART,
            skip_duplicates=_as_bool(request.data.get("skip_duplicates")),
        )
        stock = batch_resolve_service.build_stock_list(batch)
        data = OrderImportBatchSerializer(batch).data
        data["unmapped_skus"] = stock["unmapped_skus"]
        data["stock_line_count"] = len(stock["lines"])
        return Response(data, status=status.HTTP_201_CREATED)


class BatchListView(MpBaseView):
    read_perms = [mp_perms.CanViewBatch]

    def get(self, request):
        qs = OrderImportBatch.objects.filter(company=self.company)
        if self._channel():
            qs = qs.filter(channel=self._channel())
        return Response(OrderImportBatchSerializer(qs[:200], many=True).data)


class BatchDetailView(MpBaseView):
    read_perms = [mp_perms.CanViewBatch]

    def get(self, request, pk):
        batch = get_object_or_404(OrderImportBatch, pk=pk, company=self.company)
        return Response(OrderImportBatchSerializer(batch).data)


class BatchStockListView(MpBaseView):
    read_perms = [mp_perms.CanViewBatch]

    def get(self, request, pk):
        batch = get_object_or_404(OrderImportBatch, pk=pk, company=self.company)
        stock = batch_resolve_service.build_stock_list(batch)
        return Response(StockListSerializer(stock).data)


class BatchIssuanceExportView(MpBaseView):
    read_perms = [mp_perms.CanViewBatch]

    def get(self, request, pk):
        batch = get_object_or_404(OrderImportBatch, pk=pk, company=self.company)
        csv_text = issuance_export_service.build_csv(batch)
        response = HttpResponse(csv_text, content_type="text/csv")
        response["Content-Disposition"] = f'attachment; filename="issuance-batch-{batch.id}.csv"'
        return response


class IssueRequestListCreateView(MpBaseView):
    read_perms = [mp_perms.CanViewBatch]
    write_perms = [mp_perms.CanSendIssueRequest]

    def get(self, request):
        qs = MarketplaceIssueRequest.objects.filter(company=self.company).select_related("batch")
        if self._channel():
            qs = qs.filter(channel=self._channel())
        return Response(MarketplaceIssueRequestListSerializer(qs[:200], many=True).data)

    def post(self, request):
        ser = SendIssueRequestSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        batch = get_object_or_404(
            OrderImportBatch, pk=ser.validated_data["batch_id"], company=self.company
        )
        req = issue_request_service.create_from_batch(
            batch, warehouse_code=ser.validated_data.get("warehouse_code", ""), user=request.user,
        )
        return Response(
            MarketplaceIssueRequestDetailSerializer(req).data, status=status.HTTP_201_CREATED
        )


class IssueRequestDetailView(MpBaseView):
    read_perms = [mp_perms.CanViewBatch]

    def get(self, request, pk):
        req = get_object_or_404(MarketplaceIssueRequest, pk=pk, company=self.company)
        return Response(MarketplaceIssueRequestDetailSerializer(req).data)


class IssueRequestReviewView(MpBaseView):
    write_perms = [mp_perms.CanReviewIssueRequest]

    def post(self, request, pk):
        req = get_object_or_404(MarketplaceIssueRequest, pk=pk, company=self.company)
        ser = ReviewSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        req = issue_request_service.review(
            req, decisions=ser.validated_data["lines"], user=request.user
        )
        return Response(MarketplaceIssueRequestDetailSerializer(req).data)


class IssueRequestRejectView(MpBaseView):
    write_perms = [mp_perms.CanReviewIssueRequest]

    def post(self, request, pk):
        req = get_object_or_404(MarketplaceIssueRequest, pk=pk, company=self.company)
        ser = RejectSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        req = issue_request_service.reject(
            req, reason=ser.validated_data.get("reason", ""), user=request.user
        )
        return Response(MarketplaceIssueRequestDetailSerializer(req).data)


class IssueRequestIssueView(MpBaseView):
    write_perms = [mp_perms.CanIssueMaterials]

    def post(self, request, pk):
        req = get_object_or_404(MarketplaceIssueRequest, pk=pk, company=self.company)
        req = issue_request_service.issue(req, user=request.user)
        return Response(MarketplaceIssueRequestDetailSerializer(req).data)


class IssueRequestReceiveView(MpBaseView):
    write_perms = [mp_perms.CanReceiveIssue]

    def post(self, request, pk):
        req = get_object_or_404(MarketplaceIssueRequest, pk=pk, company=self.company)
        ser = ReceiveSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        req = issue_request_service.receive(
            req, receipts=ser.validated_data.get("lines"), user=request.user
        )
        return Response(MarketplaceIssueRequestDetailSerializer(req).data)


class WarehouseInsightsView(MpBaseView):
    """Aggregate warehouse insights: pipeline, issued-vs-dispatched, shortfalls."""

    read_perms = [mp_perms.CanViewBatch]

    def get(self, request):
        channel = self._channel() or MarketplaceChannel.FLIPKART
        return Response(warehouse_insights_service.build(self.company, channel))


class SapItemSearchView(MpBaseView):
    """Search the SAP item master so mappings/combos pick a real ItemCode.

    ``?search=`` (min 2 chars). Degrades to an empty list when SAP is unreachable
    so the caller can fall back to a free-text input.
    """

    read_perms = [mp_perms.CanViewMaster]

    def get(self, request):
        search = (request.query_params.get("search") or "").strip()
        if len(search) < 2:
            return Response([])
        try:
            limit = max(1, min(int(request.query_params.get("limit", 30)), 100))
        except (TypeError, ValueError):
            limit = 30
        try:
            from production_execution.services.sap_reader import ProductionOrderReader
            rows = ProductionOrderReader(self.company.code).search_items(search=search, limit=limit)
        except Exception:  # noqa: BLE001 — SAP optional; never hard-fail the masters UI
            return Response([])
        return Response([
            {
                "item_code": r.get("ItemCode") or "",
                "item_name": r.get("ItemName") or "",
                "uom": r.get("UomCode") or "",
            }
            for r in rows
        ])
