"""API views for the SAP transfer-approval queue.

These sit beside the app's own transfer requests on the Transfer Requests page:
the app's queue is what warehouses ask of each other here, this one is what
SAP's own approval procedure is holding on stock-transfer and transfer-request
drafts — including moves raised directly in the SAP client, which the app would
otherwise never surface.

The decision is signed as the SAP user SAP itself names as the current stage's
authorizer, read fresh from HANA rather than taken from the request body: SAP
accepts a decision from that one user only (``-6006`` for anyone else), and a
page left open can easily be a stage behind. The guards that enforce that —
plus the caller having to BE that authorizer — live in
:class:`warehouse.views_sap_approval_base.SapApprovalViewBase`, which the
credit-note queue shares.

Three things must hold before a row is actionable:

1. the caller holds ``warehouse.can_approve_transfer_request``;
2. the caller's own SAP account **is** that authorizer
   (:class:`sap_client.models.SapApproverIdentity`) — so a decision SAP records
   against ``USER37`` was genuinely taken by the person who is ``USER37``,
   rather than by anyone who happened to reach a page holding her credentials;
3. that account's password is configured (``SAP_APPROVER_CREDENTIALS``), or the
   app cannot authenticate as them at all.
"""

import logging

from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from company.permissions import HasCompanyContext
from sap_client.client import SAPClient

from .models_sap_approval import SapApprovalAudit
from .permissions import CanApproveTransferRequest, CanViewTransferRequest
from .serializers_sap_approval import SapApprovalDecisionSerializer
from .views_sap_approval_base import SapApprovalViewBase

logger = logging.getLogger(__name__)


class _SapApprovalView(SapApprovalViewBase):
    """The shared plumbing, with this queue's own SAPClient symbol."""

    queue_name = "transfer-approval"

    def client(self) -> SAPClient:
        return SAPClient(company_code=self.company.code)


class SapTransferApprovalListView(_SapApprovalView):
    """GET /api/v1/warehouse/sap-transfer-approvals/?status=PENDING

    Company-wide, newest first. Each row carries ``approver_code`` (the SAP user
    the request is waiting on) plus the flags the page needs to explain itself:
    ``is_mine`` (the caller is that authorizer), ``credentials_configured``, and
    ``can_decide``. Rows the caller cannot act on are still listed — seeing that
    a transfer is stuck, and on whom, is the point.
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewTransferRequest]

    def get(self, request):
        requested = (request.query_params.get("status") or "PENDING").upper()
        rows = self.client().list_transfer_approvals(
            status=None if requested == "ALL" else requested,
            # Clamped: the history views ask for more than the live queue, and
            # each row costs a HANA read of the draft's lines.
            limit=max(1, min(int(request.query_params.get("limit") or 100), 500)),
        )
        can_approve = CanApproveTransferRequest().has_permission(request, self)
        return Response(self.annotate_rows(rows, can_approve))


class SapTransferApprovalDecisionView(_SapApprovalView):
    """PATCH /api/v1/warehouse/sap-transfer-approvals/<wdd_code>/status/

    ``wdd_code`` is the SAP approval-request code (``OWDD.WddCode``).
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanApproveTransferRequest]

    def patch(self, request, wdd_code):
        serializer = SapApprovalDecisionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        decision = serializer.validated_data["status"]
        reason = serializer.validated_data.get("rejection_reason", "")
        approved = decision == SapApprovalAudit.DECISION_APPROVED

        client = self.client()
        # Re-read the stage from SAP: the authorizer is whoever SAP says it is
        # right now, not whoever the page was rendered with.
        stage = client.transfer_approval_stage(wdd_code)
        refusal = self.refuse_decision(stage, "transfer approval")
        if refusal is not None:
            return refusal
        approver = stage["approver_code"].strip()

        result = client.decide_transfer_approval(
            wdd_code,
            approve=approved,
            remarks=self.decision_remarks(approved, reason),
            approver=approver,
        )

        self._write_audit(stage, decision, reason, approver, result)
        return Response({**result, "signed_as": approver})

    def _write_audit(self, stage, decision, reason, approver, result):
        """Never let a bookkeeping failure undo a decision SAP has accepted."""
        try:
            SapApprovalAudit.objects.create(
                approval_code=stage["id"],
                draft_entry=stage.get("draft_entry"),
                obj_type=stage.get("obj_type") or "",
                doc_num=stage.get("doc_num"),
                from_warehouse=stage.get("from_warehouse") or "",
                to_warehouse=stage.get("to_warehouse") or "",
                sap_approver=approver,
                stage_code=stage.get("current_step"),
                decision=decision,
                rejection_reason=reason,
                sap_message=(result.get("message") or "")[:255],
                company=self.company,
                created_by=self.request.user,
            )
        except Exception:
            logger.exception(
                "Transfer approval %s was %s in SAP but the local audit row failed",
                stage["id"], decision,
            )
