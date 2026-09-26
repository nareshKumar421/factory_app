"""SAP production-order screens, ported from SAP Portal.

The portal's Production, Issue, Receipt and Close pages worked on SAP
production orders directly: list every order with how much has been issued and
received, create one (optionally released straight away), release, close, issue
components and receive the finished product. These endpoints do the same for
the company in the ``Company-Code`` header.

They sit beside, and do not change, the run screens' ``sap/orders/`` reads
(released/open orders for starting a run and the procurement report).

Errors: a request refused before SAP → 400/404/409 with the reason; SAP's own
refusal → 400 with SAP's words; SAP unreachable → 503; SAP broken → 502.
"""

import logging

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from company.permissions import HasCompanyContext
from sap_client.client import SAPClient
from sap_client.exceptions import SAPConnectionError, SAPDataError, SAPValidationError

from .models_sap_orders import SapProductionOrderAction
from .permissions import (
    CanCreateSapProductionOrders,
    CanIssueForSapProductionOrders,
    CanReceiveFromSapProductionOrders,
    CanReleaseCloseSapProductionOrders,
    CanViewSapProductionOrders,
)
from .serializers_sap_orders import (
    CreateOrderSerializer,
    IssueSerializer,
    ReceiptSerializer,
    SapProductionOrderActionSerializer,
)
from .services import sap_order_service
from .services.sap_order_service import SapOrderError

logger = logging.getLogger(__name__)


class _SapOrderView(APIView):
    def handle_exception(self, exc):
        if isinstance(exc, SapOrderError):
            return Response({"detail": str(exc), **exc.extra}, status=exc.status)
        if isinstance(exc, SAPValidationError):
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        if isinstance(exc, SAPConnectionError):
            logger.error("SAP unreachable in %s: %s", type(self).__name__, exc)
            return Response(
                {"detail": "SAP system is currently unavailable. Please try again later."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        if isinstance(exc, SAPDataError):
            logger.error("SAP data error in %s: %s", type(self).__name__, exc)
            return Response({"detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        return super().handle_exception(exc)

    @property
    def company(self):
        return self.request.company.company


def _rights(right):
    return [IsAuthenticated(), HasCompanyContext(), right()]


class SapOrderListCreateAPI(_SapOrderView):
    """GET — orders of every status, newest first (?status=P|R|L|C ?search ?limit ?offset).
    POST — create one (and release it if ``release``)."""

    def get_permissions(self):
        return _rights(CanViewSapProductionOrders if self.request.method == "GET" else CanCreateSapProductionOrders)

    def get(self, request):
        return Response(
            SAPClient(company_code=self.company.code).list_sap_production_orders(
                status=(request.query_params.get("status") or "").upper() or None,
                search=request.query_params.get("search", ""),
                limit=request.query_params.get("limit", 50),
                offset=request.query_params.get("offset", 0),
            )
        )

    def post(self, request):
        serializer = CreateOrderSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        confirm = data.pop("confirm_repeat", False)
        created = sap_order_service.create_order(self.company, request.user, data, confirm_repeat=confirm)
        return Response(created, status=status.HTTP_201_CREATED)


class SapOrderDetailAPI(_SapOrderView):
    """GET — one order: header, component lines, issues and receipts, and what
    was done to it from this app."""

    def get_permissions(self):
        return _rights(CanViewSapProductionOrders)

    def get(self, request, doc_entry):
        order = SAPClient(company_code=self.company.code).sap_production_order(doc_entry)
        if order is None:
            return Response({"detail": f"Production order {doc_entry} was not found in SAP."}, status=404)
        actions = SapProductionOrderAction.objects.filter(
            company=self.company, order_doc_entry=doc_entry
        ).select_related("created_by")[:100]
        order["actions"] = SapProductionOrderActionSerializer(actions, many=True).data
        return Response(order)


class SapOrderReleaseAPI(_SapOrderView):
    def get_permissions(self):
        return _rights(CanReleaseCloseSapProductionOrders)

    def post(self, request, doc_entry):
        sap_order_service.release_order(self.company, request.user, doc_entry)
        return Response({"detail": f"Production order {doc_entry} released in SAP."})


class SapOrderCloseAPI(_SapOrderView):
    def get_permissions(self):
        return _rights(CanReleaseCloseSapProductionOrders)

    def post(self, request, doc_entry):
        sap_order_service.close_order(self.company, request.user, doc_entry)
        return Response({"detail": f"Production order {doc_entry} closed in SAP."})


class SapOrderIssueAPI(_SapOrderView):
    """POST {lines: [{line_num, quantity, warehouse?, batches?}], posting_date?, remarks?}"""

    def get_permissions(self):
        return _rights(CanIssueForSapProductionOrders)

    def post(self, request, doc_entry):
        serializer = IssueSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        confirm = data.pop("confirm_repeat", False)
        result = sap_order_service.issue(self.company, request.user, doc_entry, data, confirm_repeat=confirm)
        return Response(_document(result), status=status.HTTP_201_CREATED)


class SapOrderReceiptAPI(_SapOrderView):
    """POST {quantity, warehouse?, batch_number?, posting_date?, remarks?}"""

    def get_permissions(self):
        return _rights(CanReceiveFromSapProductionOrders)

    def post(self, request, doc_entry):
        serializer = ReceiptSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        confirm = data.pop("confirm_repeat", False)
        result = sap_order_service.receipt(self.company, request.user, doc_entry, data, confirm_repeat=confirm)
        return Response(_document(result), status=status.HTTP_201_CREATED)


def _document(result: dict) -> dict:
    """What SAP created: a posted document, or the draft an approval holds."""
    if result.get("pending_approval"):
        return {"pending_approval": True, "draft_entry": result.get("draft_entry")}
    return {"pending_approval": False, "doc_entry": result.get("DocEntry"), "doc_num": result.get("DocNum")}
