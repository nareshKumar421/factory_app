"""APIViews for the marketplace app (company-scoped, permission-gated).

Convention follows ``barcode/views.py`` / ``gate_core`` — explicit APIView classes
with a services layer doing the real work.
"""
from django.db.models import ProtectedError
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import SAFE_METHODS, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from company.permissions import HasCompanyContext

from . import permissions as mp_perms
from .models import (
    ComboDefinition,
    MarketplaceDispatch,
    MarketplaceDispatchStatus,
    MarketplaceOrder,
    MarketplaceReturn,
    MarketplaceReturnStatus,
    MarketplaceScan,
    MarketplaceWarehouse,
    SkuMapping,
)
from .serializers import (
    CancelSerializer,
    ComboDefinitionSerializer,
    ConfirmSerializer,
    DispatchCreateSerializer,
    MarketplaceDispatchDetailSerializer,
    MarketplaceDispatchListSerializer,
    MarketplaceOrderSerializer,
    MarketplaceReturnDetailSerializer,
    MarketplaceReturnListSerializer,
    MarketplaceScanSerializer,
    MarketplaceReturnScanSerializer,
    MarketplaceWarehouseSerializer,
    ResolvedOrderSerializer,
    ReturnCreateSerializer,
    ReturnSubmitSerializer,
    ScanCreateSerializer,
    SkuMappingImportSerializer,
    SkuMappingSerializer,
)
from .services import dispatch_gate, reconciliation_service, resolve_service
from .services.confirm_service import confirm_dispatch
from .services.errors import MarketplaceError
from .services.scan_service import (
    dispatch_progress,
    is_fully_scanned,
    record_dispatch_scan,
    record_return_scan,
)

EDITABLE_DISPATCH = {
    MarketplaceDispatchStatus.DRAFT,
    MarketplaceDispatchStatus.SCANNING,
    MarketplaceDispatchStatus.READY,
}


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

    @property
    def company(self):
        return self.request.company.company

    def _channel(self):
        return self.request.query_params.get("channel") or None


# ── Warehouses ───────────────────────────────────────────────────────────────
class WarehouseListCreateView(MpBaseView):
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
        return Response(MarketplaceWarehouseSerializer(obj).data, status=201)


class WarehouseDetailView(MpBaseView):
    read_perms = [mp_perms.CanViewMaster]
    write_perms = [mp_perms.CanChangeMaster]

    def _get(self, pk):
        return get_object_or_404(MarketplaceWarehouse, pk=pk, company=self.company)

    def patch(self, request, pk):
        obj = self._get(pk)
        ser = MarketplaceWarehouseSerializer(obj, data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        ser.save(updated_by=request.user)
        return Response(ser.data)

    def delete(self, request, pk):
        self._get(pk).delete()
        return Response(status=204)


# ── SKU mappings ─────────────────────────────────────────────────────────────
class SkuMappingListCreateView(MpBaseView):
    read_perms = [mp_perms.CanViewMaster]
    write_perms = [mp_perms.CanChangeMaster]

    def get(self, request):
        qs = SkuMapping.objects.filter(company=self.company).select_related("combo")
        if self._channel():
            qs = qs.filter(channel=self._channel())
        search = request.query_params.get("search")
        if search:
            qs = qs.filter(marketplace_sku__icontains=search)
        return Response(SkuMappingSerializer(qs, many=True).data)

    def post(self, request):
        ser = SkuMappingSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        obj = ser.save(company=self.company, created_by=request.user)
        return Response(SkuMappingSerializer(obj).data, status=201)


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
        return Response(status=204)


class SkuMappingImportView(MpBaseView):
    read_perms = [mp_perms.CanChangeMaster]
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
        return Response(ComboDefinitionSerializer(obj).data, status=201)


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
        return Response(status=204)


# ── Orders ───────────────────────────────────────────────────────────────────
class OrderListView(MpBaseView):
    read_perms = [mp_perms.CanViewDispatch]

    def get(self, request):
        qs = (
            MarketplaceOrder.objects.filter(company=self.company)
            .prefetch_related("lines")
            .annotate(dispatch_ready=dispatch_gate.dispatch_ready_subquery())
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
        return Response(MarketplaceOrderSerializer(qs[:200], many=True).data)


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


# ── Dispatches ───────────────────────────────────────────────────────────────
class DispatchListCreateView(MpBaseView):
    read_perms = [mp_perms.CanViewDispatch]
    write_perms = [mp_perms.CanAddDispatch]

    def get(self, request):
        qs = MarketplaceDispatch.objects.filter(company=self.company).select_related(
            "order", "internal_billing"
        )
        if self._channel():
            qs = qs.filter(channel=self._channel())
        status_f = request.query_params.get("status")
        if status_f:
            qs = qs.filter(status=status_f)
        return Response(MarketplaceDispatchListSerializer(qs[:200], many=True).data)

    def post(self, request):
        ser = DispatchCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        channel = ser.validated_data["channel"]
        order_id = ser.validated_data["order_id"]
        order = get_object_or_404(
            MarketplaceOrder, company=self.company, channel=channel, order_id=order_id
        )
        if not dispatch_gate.order_is_issued(order):
            raise MarketplaceError(
                "This order's materials have not been issued from the warehouse yet.",
                code="NOT_ISSUED", status_code=409,
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
        return Response(MarketplaceDispatchDetailSerializer(dispatch).data, status=201)


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
        return Response(data, status=200 if (duplicate or not created) else 201)


class DispatchScanDetailView(MpBaseView):
    read_perms = [mp_perms.CanScanDispatch]
    write_perms = [mp_perms.CanScanDispatch]

    def delete(self, request, pk, scan_id):
        dispatch = get_object_or_404(MarketplaceDispatch, pk=pk, company=self.company)
        scan = get_object_or_404(MarketplaceScan, pk=scan_id, dispatch=dispatch)
        scan.delete()
        # Recompute status after removal.
        progress = dispatch_progress(dispatch)
        if dispatch.status in EDITABLE_DISPATCH:
            dispatch.status = (
                MarketplaceDispatchStatus.READY if is_fully_scanned(progress)
                else (MarketplaceDispatchStatus.SCANNING if dispatch.scans.exists()
                      else MarketplaceDispatchStatus.DRAFT)
            )
            dispatch.updated_by = request.user
            dispatch.save(update_fields=["status", "updated_by", "updated_at"])
        return Response(status=204)


class DispatchConfirmView(MpBaseView):
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


class DispatchCancelView(MpBaseView):
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
        return Response(MarketplaceReturnListSerializer(qs[:200], many=True).data)

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
        return Response(MarketplaceReturnDetailSerializer(mp_return).data, status=201)


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
        return Response(data, status=200 if (duplicate or not created) else 201)


class ReturnSubmitView(MpBaseView):
    write_perms = [mp_perms.CanSubmitReturn]

    def post(self, request, pk):
        mp_return = get_object_or_404(MarketplaceReturn, pk=pk, company=self.company)
        if mp_return.status == MarketplaceReturnStatus.SUBMITTED:
            return Response(MarketplaceReturnDetailSerializer(mp_return).data)
        ser = ReturnSubmitSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        # Internal credit doc number (non-SAP), same scheme as billing.
        today = timezone.localdate()
        prefix = f"CRD-{today:%Y%m%d}-"
        seq = MarketplaceReturn.objects.filter(
            company=self.company, internal_credit_doc_num__startswith=prefix
        ).count() + 1
        mp_return.internal_credit_doc_num = f"{prefix}{seq:05d}"
        mp_return.status = MarketplaceReturnStatus.SUBMITTED
        mp_return.submitted_by = request.user
        mp_return.submitted_at = timezone.now()
        mp_return.updated_by = request.user
        mp_return.save()
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
