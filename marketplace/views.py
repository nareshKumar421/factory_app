"""APIViews for the marketplace app (company-scoped, permission-gated).

Convention follows ``barcode/views.py`` / ``gate_core`` — explicit APIView classes
with a services layer doing the real work.
"""
from django.db.models import ProtectedError
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.permissions import SAFE_METHODS, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from company.permissions import HasCompanyContext

from . import permissions as mp_perms
from .models import (
    ComboDefinition,
    MarketplaceChannel,
    MarketplaceDispatch,
    MarketplaceDispatchStatus,
    MarketplaceOrder,
    MarketplaceReturn,
    MarketplaceReturnScan,
    MarketplaceReturnStatus,
    MarketplaceSapPostStatus,
    MarketplaceScan,
    MarketplaceWarehouse,
    SkuMapping,
)
from .serializers import (
    CancelSerializer,
    ComboDefinitionSerializer,
    ConfirmSerializer,
    DeliveryNoteCutSerializer,
    DispatchCreateSerializer,
    MarketplaceDispatchDetailSerializer,
    MarketplaceDispatchListSerializer,
    MarketplaceOrderSerializer,
    MarketplaceReturnDetailSerializer,
    MarketplaceReturnListSerializer,
    MarketplaceScanSerializer,
    MarketplaceReturnScanSerializer,
    MarketplaceSettingsSerializer,
    MarketplaceWarehouseSerializer,
    ResolvedOrderSerializer,
    ReturnCreateSerializer,
    ReturnScanConditionSerializer,
    ReturnSubmitSerializer,
    ScanCreateSerializer,
    SkuMappingImportSerializer,
    SkuMappingSerializer,
)
from .services import (
    delivery_note_service,
    dispatch_board_service,
    dispatch_gate,
    reconciliation_service,
    resolve_service,
    return_service,
    settings_service,
    variant_service,
)
from .services.confirm_service import confirm_dispatch, retry_delivery_note
from .services.errors import MarketplaceError
from .services.scan_service import (
    dispatch_progress,
    is_fully_scanned,
    record_dispatch_scan,
    record_return_scan,
    scan_dispatch_by_tracking,
    scan_return_by_tracking,
)

EDITABLE_DISPATCH = {
    MarketplaceDispatchStatus.DRAFT,
    MarketplaceDispatchStatus.SCANNING,
    MarketplaceDispatchStatus.READY,
}


from .pagination import positive_int as _positive_int, paginate as _paginate_core


def _paginate(request, qs, serializer_class, **kwargs):
    """Serializer-rendered pagination envelope (see ``pagination.paginate``)."""
    return _paginate_core(
        request, qs, lambda rows: serializer_class(rows, many=True).data, **kwargs
    )


class MpBaseView(APIView):
    read_perms = []
    write_perms = []

    def get_permissions(self):
        extra = self.read_perms if self.request.method in SAFE_METHODS else self.write_perms
        return [IsAuthenticated(), HasCompanyContext()] + [p() for p in extra]

    def handle_exception(self, exc):
        if isinstance(exc, MarketplaceError):
            return Response(exc.to_response(), status=exc.status_code)
        return super().handle_exception(exc)

    def initial(self, request, *args, **kwargs):
        # Auth + company context run in super().initial(); after that enforce that
        # the marketplace module is only usable under its configured company unit.
        super().initial(request, *args, **kwargs)
        from django.conf import settings
        allowed = getattr(settings, "MARKETPLACE_COMPANY_CODE", "")
        if allowed and self.company.code != allowed:
            raise MarketplaceError(
                "The marketplace module is not enabled for this company unit.",
                code="WRONG_COMPANY", status_code=403,
            )

    @property
    def company(self):
        return self.request.company.company

    def _channel(self):
        return self.request.query_params.get("channel") or None

    def _require_channel(self):
        channel = self._channel()
        if not channel:
            raise MarketplaceError("channel is required.", status_code=400)
        return channel


# ── Settings ─────────────────────────────────────────────────────────────────
class MarketplaceSettingsView(MpBaseView):
    """GET/PUT the per company + channel flow settings (e.g. skip_packing).

    ``channel`` is required. GET returns the settings (creating defaults on first
    read); PUT updates the writable toggles.
    """

    read_perms = [mp_perms.CanViewMaster]
    write_perms = [mp_perms.CanChangeMaster]

    def get(self, request):
        row = settings_service.get_settings(self.company, self._require_channel())
        return Response(MarketplaceSettingsSerializer(row).data)

    def put(self, request):
        channel = self._require_channel()
        ser = MarketplaceSettingsSerializer(data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        row = settings_service.get_settings(self.company, channel)
        if "skip_packing" in ser.validated_data:
            row = settings_service.set_skip_packing(
                self.company, channel, ser.validated_data["skip_packing"], user=request.user
            )
        if "defer_delivery_note" in ser.validated_data:
            row = settings_service.set_defer_delivery_note(
                self.company, channel, ser.validated_data["defer_delivery_note"], user=request.user
            )
        return Response(MarketplaceSettingsSerializer(row).data)


# ── SAP Delivery Notes (bulk) ────────────────────────────────────────────────
class DeliveryNoteSheetsView(MpBaseView):
    """Sheets (import batches) with dispatches awaiting a delivery note, each with
    its awaiting/posted counts, so the operator can post a delivery note per sheet."""

    read_perms = [mp_perms.CanViewDispatch]

    def get(self, request):
        channel = self._channel()
        if not channel:
            raise MarketplaceError("channel is required.", status_code=400)
        return Response(delivery_note_service.list_dn_sheets(self.company, channel))


class DeliveryNoteSummaryView(MpBaseView):
    """Preview the combined SAP Delivery Note for confirmed dispatches awaiting one.

    ``batch_id`` scopes the preview (and the eventual cut) to one sheet."""

    read_perms = [mp_perms.CanViewDispatch]

    def get(self, request):
        channel = self._channel()
        if not channel:
            raise MarketplaceError("channel is required.", status_code=400)
        warehouse_id = _positive_int(request.query_params.get("warehouse_id"), None)
        batch_id = _positive_int(request.query_params.get("batch_id"), None)
        return Response(delivery_note_service.build_bulk_summary(
            self.company, channel, warehouse_id=warehouse_id, batch_id=batch_id,
        ))


class DeliveryNoteCutView(MpBaseView):
    """Cut the awaiting dispatches' delivery note(s). ``batch_id`` restricts the cut
    to one sheet; omit it to cut every awaiting dispatch across all sheets."""

    write_perms = [mp_perms.CanConfirmDispatch]

    def post(self, request):
        ser = DeliveryNoteCutSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        channel = self._channel() or data.get("channel")
        if not channel:
            raise MarketplaceError("channel is required.", status_code=400)
        result = delivery_note_service.cut_bulk_delivery_note(
            self.company, channel, dispatch_ids=data.get("dispatch_ids") or None,
            warehouse_id=data.get("warehouse_id"), user=request.user,
            batch_id=data.get("batch_id"),
        )
        return Response(result)


class DeliveryNotePostedView(MpBaseView):
    """Delivery notes this module has already posted, with their SAP metadata."""

    read_perms = [mp_perms.CanViewDispatch]

    def get(self, request):
        limit = _positive_int(request.query_params.get("limit"), 50)
        return Response(delivery_note_service.posted_delivery_notes(
            self.company, self._channel(), limit=limit,
        ))


class DeliveryNoteReconcileView(MpBaseView):
    """Finalize delivery notes that were AWAITING SAP approval — once approved in
    SAP, record the real document + billing and mark them POSTED (or FAILED if the
    approval was rejected). Safe to call repeatedly."""

    read_perms = [mp_perms.CanViewDispatch]
    write_perms = [mp_perms.CanConfirmDispatch]

    def post(self, request):
        channel = self._channel() or request.data.get("channel")
        result = delivery_note_service.reconcile_approved_delivery_notes(
            self.company, channel=channel, user=request.user
        )
        return Response(result)

    def get(self, request):
        """Count of dispatches still awaiting SAP approval (for the UI)."""
        channel = self._channel()
        qs = MarketplaceDispatch.objects.filter(
            company=self.company,
            sap_post_status=MarketplaceSapPostStatus.AWAITING_APPROVAL,
        )
        if channel:
            qs = qs.filter(channel=channel)
        return Response({"awaiting_approval": qs.count()})


class DeliveryNoteExportView(MpBaseView):
    """Download a posted delivery note's items as CSV: one row per SAP item with its
    quantity plus DN number/date, warehouse, orders, HSN, UOM, customer and amount."""

    read_perms = [mp_perms.CanViewDispatch]

    def get(self, request, doc_entry):
        filename, csv_text = delivery_note_service.export_posted_delivery_note_csv(
            self.company, doc_entry, channel=self._channel(),
        )
        resp = HttpResponse(csv_text, content_type="text/csv")
        resp["Content-Disposition"] = f'attachment; filename="{filename}"'
        return resp


# ── Warehouses ───────────────────────────────────────────────────────────────
def _enforce_single_default(warehouse):
    """Only one default warehouse per company + channel."""
    if warehouse.is_default:
        MarketplaceWarehouse.objects.filter(
            company=warehouse.company, channel=warehouse.channel, is_default=True,
        ).exclude(pk=warehouse.pk).update(is_default=False)


class WarehouseListCreateView(MpBaseView):
    """List active warehouse masters (SAP posting config) / create one."""

    read_perms = [mp_perms.CanViewMaster]
    write_perms = [mp_perms.CanChangeMaster]

    def get(self, request):
        qs = MarketplaceWarehouse.objects.filter(company=self.company)
        if self._channel():
            qs = qs.filter(channel=self._channel())
        return Response(MarketplaceWarehouseSerializer(qs, many=True).data)

    def post(self, request):
        ser = MarketplaceWarehouseSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        obj = ser.save(company=self.company, created_by=request.user)
        _enforce_single_default(obj)
        return Response(MarketplaceWarehouseSerializer(obj).data, status=status.HTTP_201_CREATED)


class WarehouseDetailView(MpBaseView):
    read_perms = [mp_perms.CanViewMaster]
    write_perms = [mp_perms.CanChangeMaster]

    def _get(self, pk):
        return get_object_or_404(MarketplaceWarehouse, pk=pk, company=self.company)

    def patch(self, request, pk):
        obj = self._get(pk)
        ser = MarketplaceWarehouseSerializer(obj, data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        obj = ser.save(updated_by=request.user)
        _enforce_single_default(obj)
        return Response(ser.data)

    def delete(self, request, pk):
        self._get(pk).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# ── SKU mappings ─────────────────────────────────────────────────────────────
class SkuMappingListCreateView(MpBaseView):
    """List / create marketplace SKU→SAP item (or combo) mappings."""

    read_perms = [mp_perms.CanViewMaster]
    write_perms = [mp_perms.CanChangeMaster]

    def get(self, request):
        qs = (
            SkuMapping.objects.filter(company=self.company)
            .select_related("combo")
            .prefetch_related("options", "options__combo")
        )
        if self._channel():
            qs = qs.filter(channel=self._channel())
        search = request.query_params.get("search")
        if search:
            # Match anything the operator is likely to type: SKU, FSN, item code or name.
            from django.db.models import Q
            qs = qs.filter(
                Q(marketplace_sku__icontains=search)
                | Q(fsn__icontains=search)
                | Q(sku_name__icontains=search)
                | Q(fg_item_code__icontains=search)
                | Q(fg_item_name__icontains=search)
            )
        return Response(SkuMappingSerializer(qs, many=True).data)

    def post(self, request):
        ser = SkuMappingSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        obj = ser.save(company=self.company, created_by=request.user)
        return Response(SkuMappingSerializer(obj).data, status=status.HTTP_201_CREATED)


class SkuMappingDetailView(MpBaseView):
    read_perms = [mp_perms.CanViewMaster]
    write_perms = [mp_perms.CanChangeMaster]

    def _get(self, pk):
        return get_object_or_404(SkuMapping, pk=pk, company=self.company)

    def patch(self, request, pk):
        ser = SkuMappingSerializer(self._get(pk), data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        ser.save(updated_by=request.user)
        return Response(ser.data)

    def delete(self, request, pk):
        self._get(pk).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class SkuMappingImportView(MpBaseView):
    """Bulk create/update SKU mappings from a list of rows (upsert per SKU)."""

    write_perms = [mp_perms.CanChangeMaster]

    def post(self, request):
        ser = SkuMappingImportSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        imported, errors = 0, []
        for idx, row in enumerate(ser.validated_data["rows"]):
            sku = row.get("marketplace_sku")
            channel = row.get("channel")
            existing = SkuMapping.objects.filter(
                company=self.company, channel=channel, marketplace_sku=sku
            ).first()
            row_ser = SkuMappingSerializer(existing, data=row, partial=bool(existing))
            if not row_ser.is_valid():
                errors.append({"row": idx, "sku": sku, "errors": row_ser.errors})
                continue
            row_ser.save(company=self.company, updated_by=request.user,
                         **({} if existing else {"created_by": request.user}))
            imported += 1
        return Response({"imported": imported, "skipped": len(errors), "errors": errors})


# ── Combos ───────────────────────────────────────────────────────────────────
class ComboListCreateView(MpBaseView):
    """List / create combo definitions (a SKU that explodes into components)."""

    read_perms = [mp_perms.CanViewMaster]
    write_perms = [mp_perms.CanChangeMaster]

    def get(self, request):
        qs = ComboDefinition.objects.filter(company=self.company).prefetch_related("components")
        if self._channel():
            qs = qs.filter(channel=self._channel())
        return Response(ComboDefinitionSerializer(qs, many=True).data)

    def post(self, request):
        ser = ComboDefinitionSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        obj = ser.save(company=self.company, created_by=request.user)
        return Response(ComboDefinitionSerializer(obj).data, status=status.HTTP_201_CREATED)


class ComboDetailView(MpBaseView):
    read_perms = [mp_perms.CanViewMaster]
    write_perms = [mp_perms.CanChangeMaster]

    def _get(self, pk):
        return get_object_or_404(ComboDefinition, pk=pk, company=self.company)

    def patch(self, request, pk):
        ser = ComboDefinitionSerializer(self._get(pk), data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        ser.save(updated_by=request.user)
        return Response(ser.data)

    def delete(self, request, pk):
        try:
            self._get(pk).delete()
        except ProtectedError:
            raise MarketplaceError(
                "Combo is referenced by a SKU mapping; unlink it first.",
                code="PROTECTED", status_code=409,
            )
        return Response(status=status.HTTP_204_NO_CONTENT)


# ── Orders ───────────────────────────────────────────────────────────────────
class OrderListView(MpBaseView):
    """List marketplace orders (paginated), filterable by status/search/ready."""

    read_perms = [mp_perms.CanViewDispatch]

    def get(self, request):
        skip_packing = settings_service.is_skip_packing(self.company, self._channel())
        qs = (
            MarketplaceOrder.objects.filter(company=self.company)
            .prefetch_related("lines")
            .annotate(dispatch_ready=dispatch_gate.dispatch_ready_subquery(skip_packing))
        )
        if self._channel():
            qs = qs.filter(channel=self._channel())
        status_f = request.query_params.get("status")
        if status_f:
            qs = qs.filter(status=status_f)
        search = request.query_params.get("search")
        if search:
            qs = qs.filter(order_id__icontains=search)
        # Outward passes ready=1 → only orders whose materials were issued show.
        if request.query_params.get("ready") in ("1", "true", "yes"):
            qs = qs.filter(dispatch_ready=True)
        return _paginate(request, qs, MarketplaceOrderSerializer)


class OrderResolveView(MpBaseView):
    read_perms = [mp_perms.CanViewDispatch]

    def get(self, request):
        channel = self._channel()
        order_id = request.query_params.get("order_id")
        if not channel or not order_id:
            raise MarketplaceError("channel and order_id are required.", status_code=400)
        order = get_object_or_404(
            MarketplaceOrder, company=self.company, channel=channel, order_id=order_id
        )
        resolved = resolve_service.resolve_order(order)
        payload = {"order": order, **resolved}
        return Response(ResolvedOrderSerializer(payload).data)


# ── Variant choice (one FSN → several SAP items) ──────────────────────────────
class BatchVariantsView(MpBaseView):
    """Orders in a sheet whose FSN maps to more than one SAP item, with each line's
    options + current pick — powers the sheet-processing variant picker."""

    read_perms = [mp_perms.CanViewDispatch]

    def get(self, request, pk):
        from .models import OrderImportBatch
        channel = self._channel()
        batch = get_object_or_404(OrderImportBatch, pk=pk, company=self.company)
        mappings = resolve_service.load_mappings(self.company, batch.channel)
        orders = (
            MarketplaceOrder.objects.filter(company=self.company, import_batch=batch, is_cancelled=False)
            .prefetch_related("lines", "lines__chosen_option")
            .order_by("order_id")
        )
        out = []
        for o in orders:
            variants = variant_service.order_variants(o, mappings, choosable_only=True)
            if variants:
                out.append({"order_id": o.order_id, "buyer_name": o.buyer_name, "lines": variants})
        return Response({"orders": out})


class OrderChooseVariantView(MpBaseView):
    """Record (or clear) the SAP item to ship for one order line.

    Pass ``component_id`` as well to pick the item for a single combo component
    instead of the whole line.
    """

    read_perms = [mp_perms.CanViewDispatch]
    write_perms = [mp_perms.CanAddDispatch]

    def post(self, request):
        line_id = request.data.get("line_id")
        if not line_id:
            raise MarketplaceError("line_id is required.", status_code=400)
        component_id = request.data.get("component_id")
        if component_id:
            line = variant_service.set_component_option(
                self.company, line_id=line_id, component_id=component_id,
                option_id=request.data.get("option_id"), user=request.user,
            )
        else:
            line = variant_service.set_line_option(
                self.company, line_id=line_id,
                option_id=request.data.get("option_id"), user=request.user,
            )
        mappings = resolve_service.load_mappings(self.company, line.order.channel)
        return Response(variant_service.line_variants(
            line, variant_service.mapping_for_line(line, mappings)
        ))


# ── Dispatches ───────────────────────────────────────────────────────────────
class DispatchListCreateView(MpBaseView):
    """List outward dispatches (paginated) / create one for an order."""

    read_perms = [mp_perms.CanViewDispatch]
    write_perms = [mp_perms.CanAddDispatch]

    def get(self, request):
        from django.db.models import CharField, Count, F, Func, Q, Value
        qs = (
            MarketplaceDispatch.objects.filter(company=self.company)
            .select_related("order", "internal_billing")
            # Distinct tracking IDs scanned = distinct barcode prefixes before '#'.
            .annotate(scanned_count_ann=Count(
                Func(F("scans__barcode_raw"), Value("#"), Value(1),
                     function="split_part", output_field=CharField()),
                filter=Q(scans__is_active=True), distinct=True,
            ))
        )
        if self._channel():
            qs = qs.filter(channel=self._channel())
        status_f = request.query_params.get("status")
        if status_f:
            qs = qs.filter(status=status_f)
        return _paginate(request, qs, MarketplaceDispatchListSerializer)

    def post(self, request):
        ser = DispatchCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        channel = ser.validated_data["channel"]
        order_id = ser.validated_data["order_id"]
        order = get_object_or_404(
            MarketplaceOrder, company=self.company, channel=channel, order_id=order_id
        )
        if not dispatch_gate.order_dispatch_ready(order):
            skip_packing = settings_service.is_skip_packing(self.company, channel)
            raise MarketplaceError(
                "This order's materials have not been issued yet." if skip_packing
                else "This order has not been packed yet.",
                code="NOT_ISSUED" if skip_packing else "NOT_PACKED", status_code=409,
            )
        existing = (
            MarketplaceDispatch.objects.filter(company=self.company, order=order)
            .exclude(status=MarketplaceDispatchStatus.CANCELLED)
            .order_by("-created_at")
            .first()
        )
        if existing is not None:
            return Response(MarketplaceDispatchDetailSerializer(existing).data)
        dispatch = MarketplaceDispatch.objects.create(
            company=self.company,
            channel=channel,
            order=order,
            sap_warehouse_code=order.sap_warehouse_code,
            status=MarketplaceDispatchStatus.DRAFT,
            created_by=request.user,
        )
        return Response(MarketplaceDispatchDetailSerializer(dispatch).data, status=status.HTTP_201_CREATED)


class DispatchSheetListView(MpBaseView):
    """Sheets (import batches) with dispatchable orders + live scan insights, so the
    operator can pick a sheet and scan it. See ``dispatch_board_service``."""

    read_perms = [mp_perms.CanViewDispatch]

    def get(self, request):
        channel = self._require_channel()
        return Response(dispatch_board_service.list_sheets(self.company, channel))


class DispatchBoardView(MpBaseView):
    """One sheet's dispatch board: insights + every order with per-item tracking
    IDs and each item's scanned state."""

    read_perms = [mp_perms.CanViewDispatch]

    def get(self, request, pk):
        channel = self._require_channel()
        return Response(dispatch_board_service.sheet_board(self.company, channel, pk))


class DispatchOrdersInRangeView(MpBaseView):
    """Orders across ALL sheets within an order-date range — powers the date-range
    CSV export in Outward (the per-sheet board keeps its own current-sheet export)."""

    read_perms = [mp_perms.CanViewDispatch]

    def get(self, request):
        channel = self._require_channel()
        date_from = request.query_params.get("from") or None
        date_to = request.query_params.get("to") or None
        return Response(dispatch_board_service.orders_in_range(
            self.company, channel, date_from, date_to))


class ReportExportView(MpBaseView):
    """Download a marketplace report as CSV, filtered by channel + a date range.

    ``report_type`` ∈ orders | invoices | delivery-notes | returns | reconciliation.
    Query: channel, from, to (YYYY-MM-DD), plus date_field & status for the orders
    report. An empty range exports everything for that report/channel."""

    read_perms = [mp_perms.CanViewDispatch]

    def get(self, request, report_type):
        from datetime import date as _date

        from .services import reports_service

        channel = self._require_channel()

        def _pdate(value):
            value = (value or "").strip()
            if not value:
                return None
            try:
                return _date.fromisoformat(value)
            except ValueError:
                raise MarketplaceError("Dates must be YYYY-MM-DD.", status_code=400)

        params = {
            "date_from": _pdate(request.query_params.get("from")),
            "date_to": _pdate(request.query_params.get("to")),
            "date_field": request.query_params.get("date_field") or "order",
            "status": request.query_params.get("status") or None,
        }
        filename, csv_text = reports_service.build_report_csv(
            report_type, self.company, channel, params)
        resp = HttpResponse(csv_text, content_type="text/csv")
        resp["Content-Disposition"] = f'attachment; filename="{filename}"'
        return resp


class DispatchDetailView(MpBaseView):
    read_perms = [mp_perms.CanViewDispatch]

    def get(self, request, pk):
        dispatch = get_object_or_404(
            MarketplaceDispatch.objects.select_related("order", "internal_billing"),
            pk=pk, company=self.company,
        )
        return Response(MarketplaceDispatchDetailSerializer(dispatch).data)


class DispatchScanView(MpBaseView):
    read_perms = [mp_perms.CanViewDispatch]
    write_perms = [mp_perms.CanScanDispatch]

    def _get(self, pk):
        return get_object_or_404(
            MarketplaceDispatch.objects.select_related("order"), pk=pk, company=self.company
        )

    def get(self, request, pk):
        dispatch = self._get(pk)
        return Response(MarketplaceScanSerializer(dispatch.scans.filter(is_active=True), many=True).data)

    def post(self, request, pk):
        dispatch = self._get(pk)
        if dispatch.status not in EDITABLE_DISPATCH:
            raise MarketplaceError(
                f"Dispatch is {dispatch.status}; scanning is closed.",
                code="INVALID_STATE", status_code=409,
            )
        ser = ScanCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        scan, created, duplicate = record_dispatch_scan(
            dispatch,
            barcode_raw=ser.validated_data["barcode_raw"],
            item_code=ser.validated_data.get("item_code") or None,
            quantity=ser.validated_data.get("quantity"),
            user=request.user,
        )
        data = MarketplaceScanSerializer(scan).data
        data["duplicate"] = duplicate
        return Response(data, status=status.HTTP_200_OK if (duplicate or not created) else status.HTTP_201_CREATED)


class DispatchScanByTrackingView(MpBaseView):
    """Scan a whole order into Outward by its Flipkart Tracking ID — no need to
    open the order first. One scan completes every FG line and marks it READY.

    Body: ``{channel?, barcode}``. Returns the dispatch detail + ``{created, duplicate}``.
    """

    write_perms = [mp_perms.CanScanDispatch]

    def post(self, request):
        channel = self._channel() or request.data.get("channel") or MarketplaceChannel.FLIPKART
        dispatch, created, duplicate = scan_dispatch_by_tracking(
            self.company, channel, barcode=request.data.get("barcode", ""), user=request.user,
        )
        data = MarketplaceDispatchDetailSerializer(dispatch).data
        data["created"] = created
        data["duplicate"] = duplicate
        return Response(data, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


class DispatchScanDetailView(MpBaseView):
    read_perms = [mp_perms.CanScanDispatch]
    write_perms = [mp_perms.CanScanDispatch]

    def delete(self, request, pk, scan_id):
        dispatch = get_object_or_404(MarketplaceDispatch, pk=pk, company=self.company)
        scan = get_object_or_404(MarketplaceScan, pk=scan_id, dispatch=dispatch)
        scan.delete()
        # Recompute status after removal (tracking-based readiness).
        from .services.scan_service import dispatch_is_fully_scanned
        if dispatch.status in EDITABLE_DISPATCH:
            dispatch.status = (
                MarketplaceDispatchStatus.READY if dispatch_is_fully_scanned(dispatch)
                else (MarketplaceDispatchStatus.SCANNING if dispatch.scans.exists()
                      else MarketplaceDispatchStatus.DRAFT)
            )
            dispatch.updated_by = request.user
            dispatch.save(update_fields=["status", "updated_by", "updated_at"])
        return Response(status=status.HTTP_204_NO_CONTENT)


class DispatchConfirmView(MpBaseView):
    """Confirm (dispatch) an order. The SAP Delivery Note post is best-effort: the
    order is dispatched (HTTP 200) even if SAP is down — the client MUST read
    ``sap_post_status`` (POSTED / PENDING / FAILED / AWAITING_APPROVAL) on the
    returned dispatch to know the SAP outcome, and use retry-delivery-note on FAILED."""

    write_perms = [mp_perms.CanConfirmDispatch]

    def post(self, request, pk):
        dispatch = get_object_or_404(
            MarketplaceDispatch.objects.select_related("order"), pk=pk, company=self.company
        )
        ser = ConfirmSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        dispatch = confirm_dispatch(
            dispatch,
            user=request.user,
            override_deviation=ser.validated_data.get("override_deviation", False),
            remarks=ser.validated_data.get("remarks", ""),
        )
        return Response(MarketplaceDispatchDetailSerializer(dispatch).data)


class DispatchRetryDeliveryNoteView(MpBaseView):
    """Retry posting the SAP delivery note for a confirmed dispatch (post failed)."""

    write_perms = [mp_perms.CanConfirmDispatch]

    def post(self, request, pk):
        dispatch = get_object_or_404(
            MarketplaceDispatch.objects.select_related("order"), pk=pk, company=self.company
        )
        dispatch = retry_delivery_note(dispatch, user=request.user)
        return Response(MarketplaceDispatchDetailSerializer(dispatch).data)


class DispatchCancelView(MpBaseView):
    """Cancel a non-confirmed dispatch (records a reason)."""

    write_perms = [mp_perms.CanCancelDispatch]

    def post(self, request, pk):
        dispatch = get_object_or_404(MarketplaceDispatch, pk=pk, company=self.company)
        if dispatch.status == MarketplaceDispatchStatus.CONFIRMED:
            raise MarketplaceError("Confirmed dispatch cannot be cancelled.",
                                   code="INVALID_STATE", status_code=409)
        ser = CancelSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        dispatch.status = MarketplaceDispatchStatus.CANCELLED
        dispatch.cancel_reason = ser.validated_data.get("reason", "")
        dispatch.updated_by = request.user
        dispatch.save(update_fields=["status", "cancel_reason", "updated_by", "updated_at"])
        return Response(MarketplaceDispatchDetailSerializer(dispatch).data)


# ── Returns ──────────────────────────────────────────────────────────────────
class ReturnListCreateView(MpBaseView):
    read_perms = [mp_perms.CanViewReturn]
    write_perms = [mp_perms.CanAddReturn]

    def get(self, request):
        qs = MarketplaceReturn.objects.filter(company=self.company).select_related("order")
        if self._channel():
            qs = qs.filter(channel=self._channel())
        status_f = request.query_params.get("status")
        if status_f:
            qs = qs.filter(status=status_f)
        return _paginate(request, qs, MarketplaceReturnListSerializer)

    def post(self, request):
        ser = ReturnCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        order = get_object_or_404(
            MarketplaceOrder, company=self.company,
            channel=ser.validated_data["channel"], order_id=ser.validated_data["order_id"],
        )
        existing = (
            MarketplaceReturn.objects.filter(company=self.company, order=order)
            .exclude(status=MarketplaceReturnStatus.CANCELLED)
            .order_by("-created_at")
            .first()
        )
        if existing is not None:
            return Response(MarketplaceReturnDetailSerializer(existing).data)
        mp_return = MarketplaceReturn.objects.create(
            company=self.company, channel=order.channel, order=order,
            status=MarketplaceReturnStatus.DRAFT, created_by=request.user,
        )
        return Response(MarketplaceReturnDetailSerializer(mp_return).data, status=status.HTTP_201_CREATED)


class ReturnScanByTrackingView(MpBaseView):
    """Scan a whole order into Inward by its Flipkart Tracking ID — the returns
    mirror of Outward's scan-first flow. One scan records every FG line.

    Body: ``{channel?, barcode}``. Returns the return detail + ``{created, duplicate}``.
    """

    write_perms = [mp_perms.CanAddReturn]

    def post(self, request):
        channel = self._channel() or request.data.get("channel") or MarketplaceChannel.FLIPKART
        mp_return, created, duplicate = scan_return_by_tracking(
            self.company, channel, barcode=request.data.get("barcode", ""), user=request.user,
        )
        data = MarketplaceReturnDetailSerializer(mp_return).data
        data["created"] = created
        data["duplicate"] = duplicate
        return Response(data, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


class ReturnDetailView(MpBaseView):
    read_perms = [mp_perms.CanViewReturn]

    def get(self, request, pk):
        mp_return = get_object_or_404(
            MarketplaceReturn.objects.select_related("order"), pk=pk, company=self.company
        )
        return Response(MarketplaceReturnDetailSerializer(mp_return).data)


class ReturnScanView(MpBaseView):
    read_perms = [mp_perms.CanViewReturn]
    write_perms = [mp_perms.CanAddReturn]

    def post(self, request, pk):
        mp_return = get_object_or_404(
            MarketplaceReturn.objects.select_related("order"), pk=pk, company=self.company
        )
        if mp_return.status not in (MarketplaceReturnStatus.DRAFT, MarketplaceReturnStatus.SCANNING):
            raise MarketplaceError(f"Return is {mp_return.status}; scanning is closed.",
                                   code="INVALID_STATE", status_code=409)
        ser = ScanCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        scan, created, duplicate = record_return_scan(
            mp_return,
            barcode_raw=ser.validated_data["barcode_raw"],
            item_code=ser.validated_data.get("item_code") or None,
            quantity=ser.validated_data.get("quantity"),
            user=request.user,
        )
        data = MarketplaceReturnScanSerializer(scan).data
        data["duplicate"] = duplicate
        return Response(data, status=status.HTTP_200_OK if (duplicate or not created) else status.HTTP_201_CREATED)


class ReturnScanConditionView(MpBaseView):
    """Set the condition (+ optional remarks) on one returned item (scan)."""

    read_perms = [mp_perms.CanViewReturn]
    write_perms = [mp_perms.CanAddReturn]

    def post(self, request, pk, scan_pk):
        mp_return = get_object_or_404(MarketplaceReturn, pk=pk, company=self.company)
        scan = get_object_or_404(
            MarketplaceReturnScan, pk=scan_pk, mp_return=mp_return, company=self.company
        )
        ser = ReturnScanConditionSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        scan.condition = ser.validated_data.get("condition", "")
        scan.condition_remarks = ser.validated_data.get("condition_remarks", "")
        scan.updated_by = request.user
        scan.save(update_fields=["condition", "condition_remarks", "updated_by", "updated_at"])
        return Response(MarketplaceReturnScanSerializer(scan).data)


class ReturnSubmitView(MpBaseView):
    write_perms = [mp_perms.CanSubmitReturn]

    def post(self, request, pk):
        mp_return = get_object_or_404(MarketplaceReturn, pk=pk, company=self.company)
        if mp_return.status == MarketplaceReturnStatus.SUBMITTED:
            return Response(MarketplaceReturnDetailSerializer(mp_return).data)
        ser = ReturnSubmitSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        mp_return = return_service.submit_return(mp_return, user=request.user)
        return Response(MarketplaceReturnDetailSerializer(mp_return).data)


# ── Reconciliation ───────────────────────────────────────────────────────────
class ReconciliationView(MpBaseView):
    read_perms = [mp_perms.CanViewReconciliation]

    def get(self, request):
        report = reconciliation_service.build_report(
            self.company,
            channel=self._channel(),
            from_date=request.query_params.get("from_date") or None,
            to_date=request.query_params.get("to_date") or None,
            order_id=request.query_params.get("order_id") or None,
        )
        return Response(report)
