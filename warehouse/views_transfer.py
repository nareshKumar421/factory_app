"""API views for warehouse transfer requests.

Follows the conventions in `views_bst.py`: a `_service()` helper bound to the
request's company, and SAP failures reported as 502 rather than 400 so the
frontend can tell "SAP said no" from "you asked for something impossible".
"""

import logging
from decimal import Decimal

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from company.permissions import HasCompanyContext
from sap_client.exceptions import (
    SAPConnectionError,
    SAPDataError,
    SAPValidationError,
)

from .permissions import (
    CanApproveTransferRequest,
    CanCreateTransferRequest,
    CanPostTransferToSAP,
    CanViewTransferRequest,
)
from .serializers_transfer import (
    TransferApproveSerializer,
    TransferPostSerializer,
    TransferRejectSerializer,
    TransferRequestCreateSerializer,
    TransferRequestDetailSerializer,
    TransferRequestListSerializer,
    TransferSecondLegSerializer,
)
from .services.transfer_guards import TransferGuardError
from .services.transfer_request_service import (
    TransferRequestError,
    TransferRequestService,
)

logger = logging.getLogger(__name__)


def _service(request) -> TransferRequestService:
    return TransferRequestService(request.company.company.code, request.user)


def _bad_request(exc) -> Response:
    return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)


def _sap_error(exc) -> Response:
    logger.error("SAP error in transfer request flow: %s", exc)
    return Response({"error": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)


class _TransferView(APIView):
    """Shared exception handling — every action can fail the same three ways."""

    def dispatch_action(self, fn):
        try:
            return fn()
        except (TransferRequestError, TransferGuardError) as exc:
            return _bad_request(exc)
        except SAPValidationError as exc:
            # SAP refused the document itself; the operator can act on this.
            return _bad_request(exc)
        except (SAPConnectionError, SAPDataError) as exc:
            return _sap_error(exc)


# ---------------------------------------------------------------------------
# List / create
# ---------------------------------------------------------------------------

class TransferRequestListCreateView(_TransferView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewTransferRequest]

    def get(self, request):
        service = _service(request)
        qs = service.list_requests(
            status=request.query_params.get("status"),
            posting_status=request.query_params.get("posting_status"),
            from_warehouse=request.query_params.get("from_warehouse"),
            to_warehouse=request.query_params.get("to_warehouse"),
        )
        return Response(TransferRequestListSerializer(qs, many=True).data)

    def post(self, request):
        for permission in (CanCreateTransferRequest(),):
            if not permission.has_permission(request, self):
                return Response(
                    {"error": "You do not have permission to raise a transfer request."},
                    status=status.HTTP_403_FORBIDDEN,
                )

        serializer = TransferRequestCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        def action():
            created = _service(request).create_request(serializer.validated_data)
            return Response(
                TransferRequestDetailSerializer(created).data,
                status=status.HTTP_201_CREATED,
            )

        return self.dispatch_action(action)


class TransferRequestDetailView(_TransferView):
    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewTransferRequest]

    def get(self, request, request_id: int):
        def action():
            found = _service(request).get_request(request_id, link_bst=True)
            return Response(TransferRequestDetailSerializer(found).data)

        return self.dispatch_action(action)


# ---------------------------------------------------------------------------
# Approve / reject — the receiving warehouse decides
# ---------------------------------------------------------------------------

class TransferRequestApproveView(_TransferView):
    permission_classes = [
        IsAuthenticated, HasCompanyContext, CanApproveTransferRequest,
    ]

    def post(self, request, request_id: int):
        serializer = TransferApproveSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        def action():
            updated = _service(request).approve(request_id, serializer.validated_data)
            return Response(TransferRequestDetailSerializer(updated).data)

        return self.dispatch_action(action)


class TransferRequestRejectView(_TransferView):
    permission_classes = [
        IsAuthenticated, HasCompanyContext, CanApproveTransferRequest,
    ]

    def post(self, request, request_id: int):
        serializer = TransferRejectSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        def action():
            updated = _service(request).reject(
                request_id, serializer.validated_data["reason"]
            )
            return Response(TransferRequestDetailSerializer(updated).data)

        return self.dispatch_action(action)


# ---------------------------------------------------------------------------
# Post to SAP
# ---------------------------------------------------------------------------

class TransferRequestAllocationPreviewView(_TransferView):
    """What batches posting would take, plus what else is on the shelf.

    Read-only: nothing is reserved or moved by looking.
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanPostTransferToSAP]

    def get(self, request, request_id: int):
        def action():
            return Response(_service(request).allocation_preview(request_id))

        return self.dispatch_action(action)


class TransferRequestPostView(_TransferView):
    """Post the approved quantities — the whole move, or leg 1 if cross-branch.

    Accepts an optional hand-picked batch split per line; anything omitted is
    allocated oldest-first.
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanPostTransferToSAP]

    def post(self, request, request_id: int):
        serializer = TransferPostSerializer(data=request.data or {})
        serializer.is_valid(raise_exception=True)
        allocations = {
            int(line["line_num"]): [
                {"BatchNumber": b["batch_number"], "Quantity": b["quantity"]}
                for b in line["batches"]
            ]
            for line in serializer.validated_data.get("lines") or []
        }

        def action():
            updated = _service(request).post_transfer(request_id, allocations or None)
            return Response(TransferRequestDetailSerializer(updated).data)

        return self.dispatch_action(action)


class TransferRequestCreateBSTView(_TransferView):
    """Seed a BST from the posted transfer, so the floor can start scanning."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanPostTransferToSAP]

    def post(self, request, request_id: int):
        from .serializers_bst import BSTTransferDetailSerializer

        def action():
            bst = _service(request).create_bst(request_id, request.data or {})
            return Response(
                BSTTransferDetailSerializer(bst).data,
                status=status.HTTP_201_CREATED,
            )

        return self.dispatch_action(action)


class TransferRequestSecondLegView(_TransferView):
    """Move cross-branch stock out of in-transit into its real destination."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanPostTransferToSAP]

    def post(self, request, request_id: int):
        serializer = TransferSecondLegSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        received = {
            int(line["line_num"]): Decimal(str(line["received_qty"]))
            for line in serializer.validated_data.get("lines") or []
        }

        def action():
            updated = _service(request).post_second_leg(request_id, received or None)
            return Response(TransferRequestDetailSerializer(updated).data)

        return self.dispatch_action(action)


# ---------------------------------------------------------------------------
# Queues and verification
# ---------------------------------------------------------------------------

class TransferRequestPendingView(_TransferView):
    """What the receiving warehouse has waiting on it."""

    permission_classes = [
        IsAuthenticated, HasCompanyContext, CanApproveTransferRequest,
    ]

    def get(self, request):
        qs = _service(request).pending_approvals()
        return Response(TransferRequestListSerializer(qs, many=True).data)


class TransferRequestInTransitView(_TransferView):
    """Cross-branch moves whose second leg is still outstanding."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewTransferRequest]

    def get(self, request):
        qs = _service(request).awaiting_second_leg()
        return Response(TransferRequestListSerializer(qs, many=True).data)


class TransferRequestStockView(_TransferView):
    """Items a warehouse holds, for the request form's item picker.

    Returns `available` alongside `on_hand` — see the service for why raw
    on-hand is the wrong number to offer.
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewTransferRequest]

    def get(self, request):
        warehouse = (request.query_params.get("warehouse") or "").strip()
        search = (request.query_params.get("search") or "").strip()
        try:
            limit = int(request.query_params.get("limit") or 50)
        except (TypeError, ValueError):
            return _bad_request("limit must be a number.")

        def action():
            return Response(
                _service(request).available_items(warehouse, search=search, limit=limit)
            )

        return self.dispatch_action(action)


class TransferRequestReconcileView(_TransferView):
    """Where the app and SAP disagree — drift reported rather than discovered."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewTransferRequest]

    def get(self, request):
        include_settled = request.query_params.get("all") in ("1", "true", "True")
        try:
            limit = int(request.query_params.get("limit") or 500)
        except (TypeError, ValueError):
            return _bad_request("limit must be a number.")

        def action():
            return Response(
                _service(request).reconcile(
                    include_settled=include_settled, limit=limit
                )
            )

        return self.dispatch_action(action)


class TransferRequestVerifyBatchesView(_TransferView):
    """Reconcile the batch split we sent against what SAP recorded in IBT1."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewTransferRequest]

    def get(self, request, request_id: int):
        def action():
            problems = _service(request).verify_batches(request_id)
            return Response({"matches": not problems, "discrepancies": problems})

        return self.dispatch_action(action)


class WarehousePrintInfoView(_TransferView):
    """Letterhead data for the Branch Stock Transfer print.

    Company legal name plus, per warehouse, the postal address (OWHS) and the
    GST registration/state of its SAP branch (OBPL) — the same sources SAP's
    own Crystal print reads. Read-only master data, so it needs no permission
    beyond being in a company: both the transfer-request and BST detail pages
    print, and their view permissions differ.
    """

    permission_classes = [IsAuthenticated, HasCompanyContext]

    def get(self, request):
        raw = request.query_params.get("warehouses") or ""
        codes = [code.strip() for code in raw.split(",") if code.strip()]
        if not codes:
            return _bad_request("warehouses query parameter is required.")
        if len(codes) > 10:
            return _bad_request("At most 10 warehouses per request.")

        def action():
            from sap_client.client import SAPClient

            client = SAPClient(request.company.company.code)
            return Response(client.get_warehouse_print_info(codes))

        return self.dispatch_action(action)
