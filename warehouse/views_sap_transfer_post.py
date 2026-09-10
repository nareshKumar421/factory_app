"""API for posting the actual transfer against a SAP-raised transfer request.

Sits beside the SAP approval queue on the Transfer Requests page. Approving an
inventory transfer *request* clears the request; this is what then moves the
stock, and until it runs the request keeps reserving stock nobody has shipped.
"""

import logging

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

from .permissions import CanPostTransferToSAP, CanViewTransferRequest
from .serializers_sap_transfer_post import SapTransferPostSerializer
from .services.sap_transfer_post_service import (
    SapTransferPostError,
    SapTransferPostService,
)

logger = logging.getLogger(__name__)


class _SapTransferPostView(APIView):
    def handle_exception(self, exc):
        # warehouse_scope raises DRF PermissionDenied, which DRF renders as 403
        # on its own — nothing to translate here.
        if isinstance(exc, (SapTransferPostError, SAPValidationError)):
            # SAP refused the document, or the quantities cannot be served —
            # either way the operator can act on it.
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        if isinstance(exc, (SAPConnectionError, SAPDataError)):
            logger.error("SAP error posting a transfer request: %s", exc)
            return Response({"error": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        return super().handle_exception(exc)

    def service(self) -> SapTransferPostService:
        return SapTransferPostService(
            self.request.company.company.code, self.request.user
        )


class SapTransferAwaitingListView(_SapTransferPostView):
    """GET /api/v1/warehouse/sap-transfer-requests/awaiting/

    Approved SAP transfer requests that still owe stock, with each open line's
    remaining quantity. Read with the transfer-request view permission so the
    backlog is visible to anyone who follows the flow; ``can_post`` says per row
    whether this caller may actually move it.
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewTransferRequest]

    def get(self, request):
        rows = self.service().list_awaiting_transfer(
            limit=int(request.query_params.get("limit") or 200)
        )
        return Response(rows)


class SapTransferPostView(_SapTransferPostView):
    """POST /api/v1/warehouse/sap-transfer-requests/<doc_entry>/post/

    ``doc_entry`` is the SAP ``OWTQ.DocEntry``. Body carries a quantity per
    ``WTQ1.LineNum``; a line omitted or zeroed is left open.
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanPostTransferToSAP]

    def post(self, request, doc_entry):
        serializer = SapTransferPostSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = self.service().post_transfer(
            doc_entry, serializer.validated_data["quantities"]
        )
        return Response(result, status=status.HTTP_201_CREATED)
