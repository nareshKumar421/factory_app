import logging

from django.core.exceptions import PermissionDenied

from rest_framework import status
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from company.permissions import HasCompanyContext
from gate_core.services.user_scope import user_company_ids, wants_all_companies

from . import services
from .permissions import (
    CanApproveGoodsReturn,
    CanCreateGoodsReturn,
    CanEditGoodsReturn,
    CanGateInGoodsReturn,
    CanReceiveGoodsReturn,
    CanSubmitGoodsReturn,
    CanViewGoodsReturn,
)
from .serializers import (
    GoodsReturnApprovalDecisionSerializer,
    GoodsReturnAttachmentSerializer,
    GoodsReturnAttachmentUploadSerializer,
    GoodsReturnCreateSerializer,
    GoodsReturnDetailSerializer,
    GoodsReturnHeaderPatchSerializer,
    GoodsReturnItemsSaveSerializer,
    GoodsReturnListSerializer,
    GoodsReturnMarkInSerializer,
    GoodsReturnReceiveSerializer,
    GoodsReturnVehicleSerializer,
    InvoiceRefAddSerializer,
    ReturnWarehouseSerializer,
)
from .services import GoodsReturnService

logger = logging.getLogger(__name__)


def _service(request):
    return GoodsReturnService(company=request.company.company)


def _allowed_ids(request):
    return user_company_ids(request)


def _validation_error(serializer):
    return Response(
        {"detail": "Invalid data.", "errors": serializer.errors},
        status=status.HTTP_400_BAD_REQUEST,
    )


def _detail(gr):
    return Response(GoodsReturnDetailSerializer(gr).data)


class GoodsReturnListCreateAPI(APIView):
    def get_permissions(self):
        if self.request.method == "POST":
            return [IsAuthenticated(), HasCompanyContext(), CanCreateGoodsReturn()]
        return [IsAuthenticated(), HasCompanyContext(), CanViewGoodsReturn()]

    def get(self, request):
        if wants_all_companies(request):
            company_ids = user_company_ids(request)
        else:
            company_ids = [request.company.company_id]
        qs = _service(request).list_returns(
            company_ids,
            status=request.GET.get("status") or None,
            basis=request.GET.get("basis") or None,
            search=request.GET.get("search") or None,
            approval=request.GET.get("approval") or None,
        )
        return Response(GoodsReturnListSerializer(qs, many=True).data)

    def post(self, request):
        serializer = GoodsReturnCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return _validation_error(serializer)
        try:
            gr = _service(request).create_return(serializer.validated_data, request.user)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(GoodsReturnDetailSerializer(gr).data, status=status.HTTP_201_CREATED)


class GoodsReturnDetailAPI(APIView):
    def get_permissions(self):
        if self.request.method == "GET":
            return [IsAuthenticated(), HasCompanyContext(), CanViewGoodsReturn()]
        return [IsAuthenticated(), HasCompanyContext(), CanEditGoodsReturn()]

    def get(self, request, pk):
        try:
            gr = _service(request).get_return(pk, _allowed_ids(request))
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_404_NOT_FOUND)
        data = GoodsReturnDetailSerializer(gr).data
        if request.GET.get("with_invoice_preview") in ("1", "true", "True"):
            data["invoice_preview"] = _service(request).get_invoice_preview(gr)
        return Response(data)

    def patch(self, request, pk):
        serializer = GoodsReturnHeaderPatchSerializer(data=request.data, partial=True)
        if not serializer.is_valid():
            return _validation_error(serializer)
        try:
            gr = _service(request).update_header(
                pk, serializer.validated_data, request.user, _allowed_ids(request)
            )
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return _detail(gr)

    def delete(self, request, pk):
        try:
            gr = _service(request).cancel(pk, request.user, _allowed_ids(request))
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return _detail(gr)


class GoodsReturnInvoiceRefAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanEditGoodsReturn]

    def post(self, request, pk):
        serializer = InvoiceRefAddSerializer(data=request.data)
        if not serializer.is_valid():
            return _validation_error(serializer)
        try:
            gr = _service(request).add_invoice_ref(
                pk,
                serializer.validated_data["invoice_number"],
                request.user,
                _allowed_ids(request),
            )
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        data = GoodsReturnDetailSerializer(gr).data
        data["invoice_preview"] = _service(request).get_invoice_preview(gr)
        return Response(data, status=status.HTTP_201_CREATED)


class GoodsReturnInvoiceRefDetailAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanEditGoodsReturn]

    def delete(self, request, pk, ref_id):
        try:
            gr = _service(request).remove_invoice_ref(
                pk, ref_id, request.user, _allowed_ids(request)
            )
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return _detail(gr)


class GoodsReturnItemsAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanEditGoodsReturn]

    def put(self, request, pk):
        serializer = GoodsReturnItemsSaveSerializer(data=request.data)
        if not serializer.is_valid():
            return _validation_error(serializer)
        try:
            gr = _service(request).save_items(
                pk, serializer.validated_data["lines"], request.user, _allowed_ids(request)
            )
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return _detail(gr)


class GoodsReturnVehicleAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanEditGoodsReturn]

    def patch(self, request, pk):
        serializer = GoodsReturnVehicleSerializer(data=request.data)
        if not serializer.is_valid():
            return _validation_error(serializer)
        try:
            gr = _service(request).set_vehicle(
                pk, serializer.validated_data, request.user, _allowed_ids(request)
            )
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return _detail(gr)


class GoodsReturnAttachmentsAPI(APIView):
    parser_classes = [MultiPartParser, FormParser]

    def get_permissions(self):
        if self.request.method == "GET":
            return [IsAuthenticated(), HasCompanyContext(), CanViewGoodsReturn()]
        return [IsAuthenticated(), HasCompanyContext(), CanEditGoodsReturn()]

    def get(self, request, pk):
        try:
            attachments = _service(request).list_attachments(pk, _allowed_ids(request))
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_404_NOT_FOUND)
        return Response(GoodsReturnAttachmentSerializer(attachments, many=True).data)

    def post(self, request, pk):
        serializer = GoodsReturnAttachmentUploadSerializer(data=request.data)
        if not serializer.is_valid():
            return _validation_error(serializer)
        try:
            attachment = _service(request).upload_attachment(
                pk,
                serializer.validated_data["file"],
                serializer.validated_data.get("attachment_type"),
                serializer.validated_data.get("notes"),
                request.user,
                _allowed_ids(request),
            )
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(
            GoodsReturnAttachmentSerializer(attachment).data, status=status.HTTP_201_CREATED
        )


class GoodsReturnAttachmentDetailAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanEditGoodsReturn]

    def delete(self, request, pk, attachment_id):
        try:
            _service(request).delete_attachment(pk, attachment_id, _allowed_ids(request))
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(status=status.HTTP_204_NO_CONTENT)


class GoodsReturnSubmitAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanSubmitGoodsReturn]

    def post(self, request, pk):
        try:
            gr = _service(request).submit(pk, request.user, _allowed_ids(request))
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return _detail(gr)


class GoodsReturnReturnableItemsAPI(APIView):
    """What this return's customer has been invoiced — the item picker's list."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewGoodsReturn]

    def get(self, request, pk):
        try:
            limit = int(request.query_params.get("limit") or 100)
        except (TypeError, ValueError):
            return Response({"detail": "limit must be a number."},
                            status=status.HTTP_400_BAD_REQUEST)
        try:
            items = _service(request).returnable_items(
                pk,
                _allowed_ids(request),
                search=(request.query_params.get("search") or "").strip(),
                limit=limit,
            )
        except (ValueError, PermissionDenied) as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as exc:
            logger.error("Failed to list returnable items: %s", exc)
            return Response(
                {"detail": "Could not load this customer's items from SAP."},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        return Response(items)


class GoodsReturnWarehousesAPI(APIView):
    """Goods-return warehouses for the active company (destination picker at receipt)."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewGoodsReturn]

    def get(self, request):
        try:
            warehouses = _service(request).list_return_warehouses(request.company.company.code)
        except Exception as exc:
            logger.error("Failed to list return warehouses: %s", exc)
            return Response(
                {"detail": "Could not load return warehouses from SAP."},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        return Response(ReturnWarehouseSerializer(warehouses, many=True).data)


class GoodsReturnReceiveAPI(APIView):
    """The GR creator confirms receipt -> posts the SAP A/R Returns (invoice basis)."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanReceiveGoodsReturn]

    def post(self, request, pk):
        serializer = GoodsReturnReceiveSerializer(data=request.data)
        if not serializer.is_valid():
            return _validation_error(serializer)
        try:
            gr = _service(request).receive(
                pk,
                request.user,
                serializer.validated_data.get("warehouse_code"),
                _allowed_ids(request),
            )
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return _detail(gr)


class GoodsReturnApproveAPI(APIView):
    """Admin approves a return flagged 'coming on approval' so it can be received."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanApproveGoodsReturn]

    def post(self, request, pk):
        serializer = GoodsReturnApprovalDecisionSerializer(data=request.data)
        if not serializer.is_valid():
            return _validation_error(serializer)
        try:
            gr = _service(request).approve(
                pk, request.user, serializer.validated_data.get("remarks"), _allowed_ids(request)
            )
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return _detail(gr)


class GoodsReturnRejectAPI(APIView):
    """Admin rejects a return flagged 'coming on approval'."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanApproveGoodsReturn]

    def post(self, request, pk):
        serializer = GoodsReturnApprovalDecisionSerializer(data=request.data)
        if not serializer.is_valid():
            return _validation_error(serializer)
        try:
            gr = _service(request).reject(
                pk, request.user, serializer.validated_data.get("remarks"), _allowed_ids(request)
            )
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return _detail(gr)


# ---------------------------------------------------------------------------
# Gate side -- cross-company
# ---------------------------------------------------------------------------
class GoodsReturnExpectedAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanGateInGoodsReturn]

    def get(self, request):
        qs = services.list_expected_returns(user_company_ids(request))
        return Response(GoodsReturnListSerializer(qs, many=True).data)


class GoodsReturnMarkInAPI(APIView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanGateInGoodsReturn]

    def post(self, request, pk):
        serializer = GoodsReturnMarkInSerializer(data=request.data)
        if not serializer.is_valid():
            return _validation_error(serializer)
        try:
            gr = services.mark_return_in(
                pk, request.user, serializer.validated_data, user_company_ids(request)
            )
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return _detail(gr)
