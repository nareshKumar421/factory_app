"""API for adding inventory-transfer drafts SAP approved but never posted.

The third step of the same flow the two views beside this one cover: SAP holds
the draft (``views_sap_approval``), somebody approves it, and then — for a
transfer keyed in the SAP client — the draft still has to be *added* before any
stock moves. That last step had no home in this app, so approved drafts simply
went quiet: 33 of them were waiting when this shipped.

Read with the transfer-request view permission, so the backlog is visible to
anyone who follows the flow; the add itself needs ``can_post_transfer_to_sap``
and the caller must manage every warehouse the stock leaves.
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
from .services.sap_transfer_draft_service import (
    SapTransferDraftError,
    SapTransferDraftService,
)

logger = logging.getLogger(__name__)


class _SapTransferDraftView(APIView):
    def handle_exception(self, exc):
        # warehouse_scope raises DRF PermissionDenied, which DRF renders itself.
        if isinstance(exc, (SapTransferDraftError, SAPValidationError)):
            # SAP refused the draft, or it is not in a state that can be added —
            # either way it is the operator's to act on, and SAP's own wording
            # (a notification-procedure rule, a batch shortfall) is the useful
            # part, so it is passed through unchanged.
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        if isinstance(exc, (SAPConnectionError, SAPDataError)):
            logger.error("SAP error adding a transfer draft: %s", exc)
            return Response({"error": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        return super().handle_exception(exc)

    def service(self) -> SapTransferDraftService:
        return SapTransferDraftService(
            self.request.company.company.code, self.request.user
        )


class SapTransferDraftListView(_SapTransferDraftView):
    """GET /api/v1/warehouse/sap-transfer-drafts/

    Approved inventory-transfer drafts whose stock has not moved yet, newest
    first, with each line's quantity and what the source warehouse holds today.
    ``can_post`` says per row whether this caller may add it; ``warnings`` says
    what SAP would refuse.
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewTransferRequest]

    def get(self, request):
        rows = self.service().list_awaiting_add(
            limit=int(request.query_params.get("limit") or 100)
        )
        return Response(rows)


class SapTransferDraftPostView(_SapTransferDraftView):
    """POST /api/v1/warehouse/sap-transfer-drafts/<draft_entry>/post/

    ``draft_entry`` is the SAP ``ODRF.DocEntry``. There is no body: the draft is
    added exactly as SAP holds it, which is the whole point — quantities,
    warehouses and batch allocations were settled when it was approved, and
    changing any of them belongs on the draft in SAP.
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanPostTransferToSAP]

    def post(self, request, draft_entry):
        result = self.service().post_draft(draft_entry)
        return Response(result, status=status.HTTP_201_CREATED)
