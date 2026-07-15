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
    MarketplaceOrder,
    MarketplacePackBarcode,
    MarketplacePacking,
    OrderImportBatch,
)
from .serializers_sheet import (
    MarketplaceIssueRequestDetailSerializer,
    MarketplaceIssueRequestListSerializer,
    MarketplacePackingSerializer,
    OpenPackingSerializer,
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
    packing_service,
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


def _positive_int(value, default):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _paginate(request, qs, mapper, *, default_size=25, max_size=100):
    """Page a queryset into the ``{results, count, page, …}`` envelope used across
    the app (see ``barcode.views._paginated_response``). ``mapper`` turns one row
    into its response dict."""
    page = _positive_int(request.query_params.get("page"), 1)
    page_size = min(_positive_int(request.query_params.get("page_size"), default_size), max_size)
    total = qs.count()
    total_pages = max((total + page_size - 1) // page_size, 1)
    page = min(page, total_pages)
    start = (page - 1) * page_size
    return Response({
        "results": [mapper(o) for o in qs[start:start + page_size]],
        "count": total,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
        "next": page < total_pages,
        "previous": page > 1,
    })


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


class PackingQueueView(MpBaseView):
    """Packing screen orders — those still to pack plus those already packed
    (kept so their labels can be reprinted). Paginated (``?page=&page_size=``)."""

    read_perms = [mp_perms.CanViewPacking]

    def get(self, request):
        channel = self._channel() or MarketplaceChannel.FLIPKART
        qs = packing_service.packing_queue(self.company, channel)
        return _paginate(request, qs, lambda o: {
            "order_id": o.order_id,
            "buyer_name": o.buyer_name,
            "line_count": o.line_count,
            "packing_status": getattr(getattr(o, "packing", None), "status", None),
        })


class PackingScanView(MpBaseView):
    """Pack an order by scanning its Flipkart Tracking ID barcode.

    No manual order selection: the tracking ID (already on the shipping label)
    resolves the order, marks it PACKED, and it then appears in Outward.
    """

    write_perms = [mp_perms.CanPackOrder]

    def post(self, request):
        channel = self._channel() or MarketplaceChannel.FLIPKART
        barcode = str(request.data.get("barcode", "")).strip()
        packing, already_packed = packing_service.scan_pack(
            self.company, channel, barcode=barcode, user=request.user,
        )
        data = MarketplacePackingSerializer(packing).data
        data["already_packed"] = already_packed
        return Response(data)


class PackingOpenView(MpBaseView):
    """Open (or fetch) the packing session for an issued order."""

    write_perms = [mp_perms.CanPackOrder]

    def post(self, request):
        ser = OpenPackingSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        order = get_object_or_404(
            MarketplaceOrder, company=self.company,
            channel=MarketplaceChannel.FLIPKART, order_id=ser.validated_data["order_id"],
        )
        packing = packing_service.start_or_get(order, user=request.user)
        return Response(MarketplacePackingSerializer(packing).data)


class PackingDetailView(MpBaseView):
    read_perms = [mp_perms.CanViewPacking]

    def get(self, request, pk):
        packing = get_object_or_404(MarketplacePacking, pk=pk, company=self.company)
        return Response(MarketplacePackingSerializer(packing).data)


class PackingGenerateView(MpBaseView):
    write_perms = [mp_perms.CanPackOrder]

    def post(self, request, pk):
        packing = get_object_or_404(MarketplacePacking, pk=pk, company=self.company)
        packing_service.generate_barcodes(packing, user=request.user)
        packing.refresh_from_db()
        return Response(MarketplacePackingSerializer(packing).data)


class PackingCompleteView(MpBaseView):
    write_perms = [mp_perms.CanPackOrder]

    def post(self, request, pk):
        packing = get_object_or_404(MarketplacePacking, pk=pk, company=self.company)
        packing = packing_service.complete(packing, user=request.user)
        return Response(MarketplacePackingSerializer(packing).data)


class PackBarcodePrintView(MpBaseView):
    """Return the printable label data for one barcode (frontend renders it)."""

    write_perms = [mp_perms.CanPackOrder]

    def post(self, request, pk):
        barcode = get_object_or_404(MarketplacePackBarcode, pk=pk, company=self.company)
        return Response(packing_service.label_data(barcode))


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
