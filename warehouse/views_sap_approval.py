"""API views for the SAP transfer-approval queue.

These sit beside the app's own transfer requests on the Transfer Requests page:
the app's queue is what warehouses ask of each other here, this one is what
SAP's own approval procedure is holding on stock-transfer and transfer-request
drafts — including moves raised directly in the SAP client, which the app would
otherwise never surface.

The decision is signed as the SAP user SAP itself names as the current stage's
authorizer, read fresh from HANA rather than taken from the request body: SAP
accepts a decision from that one user only (``-6006`` for anyone else), and a
page left open can easily be a stage behind.

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

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from company.permissions import HasCompanyContext
from sap_client.client import SAPClient
from sap_client.exceptions import (
    SAPConnectionError,
    SAPDataError,
    SAPValidationError,
)
from sap_client.models import SapApproverIdentity

from .models_sap_approval import SapApprovalAudit
from .permissions import CanApproveTransferRequest, CanViewTransferRequest
from .serializers_sap_approval import SapApprovalDecisionSerializer

logger = logging.getLogger(__name__)


class _SapApprovalView(APIView):
    """Shared company/SAP plumbing for the two endpoints below."""

    def handle_exception(self, exc):
        if isinstance(exc, SAPValidationError):
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        if isinstance(exc, (SAPConnectionError, SAPDataError)):
            logger.error("SAP error in the transfer-approval queue: %s", exc)
            return Response({"error": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        return super().handle_exception(exc)

    @property
    def company(self):
        # HasCompanyContext attaches request.company as a UserCompany.
        return self.request.company.company

    def client(self) -> SAPClient:
        return SAPClient(company_code=self.company.code)

    def configured_approvers(self) -> set:
        """SAP user codes this company holds a password for, upper-cased."""
        from django.conf import settings

        credentials = settings.SAP_APPROVER_CREDENTIALS.get(self.company.code) or {}
        return set(credentials)

    def my_sap_code(self) -> str | None:
        """The SAP account the caller acts as in this company, if mapped."""
        return SapApproverIdentity.code_for(self.request.user, self.company)

    def acting_name(self) -> str:
        """Display name recorded in the SAP remarks and the local audit row."""
        user = self.request.user
        return (getattr(user, "full_name", "") or user.get_username() or "").strip()


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
            limit=int(request.query_params.get("limit") or 100),
        )
        available = self.configured_approvers()
        mine = (self.my_sap_code() or "").upper()
        can_approve = CanApproveTransferRequest().has_permission(request, self)
        for row in rows:
            code = (row.get("approver_code") or "").strip().upper()
            row["credentials_configured"] = bool(code) and code in available
            row["is_mine"] = bool(code) and bool(mine) and code == mine
            row["can_decide"] = bool(
                can_approve
                and row["is_mine"]
                and row["credentials_configured"]
                and row.get("status") == "PENDING"
            )
        return Response(rows)


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

        client = self.client()
        # Re-read the stage from SAP: the authorizer is whoever SAP says it is
        # right now, not whoever the page was rendered with.
        stage = client.transfer_approval_stage(wdd_code)
        if stage["status"] != "PENDING":
            return Response(
                {
                    "error": (
                        f"This transfer approval is already "
                        f"{stage['status'].lower()} in SAP."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        approver = (stage.get("approver_code") or "").strip()
        if not approver:
            return Response(
                {
                    "error": (
                        "SAP does not name an authorizer on this request's current "
                        "stage, so it cannot be decided from the app."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        name = stage.get("approver_name")
        who = f"{approver} ({name})" if name else approver

        # Only the person who IS this authorizer may decide it. Anyone else
        # would be acting under someone else's SAP account.
        mine = self.my_sap_code()
        if not mine:
            return Response(
                {
                    "error": (
                        f"Your account is not linked to a SAP user in "
                        f"{self.company.code}, so the app cannot tell whether you "
                        f"are {who}. Ask an administrator to map you on the SAP "
                        "Identities page."
                    )
                },
                status=status.HTTP_403_FORBIDDEN,
            )
        if mine.upper() != approver.upper():
            return Response(
                {
                    "error": (
                        f"This approval is waiting on {who}. You act as {mine}, and "
                        "SAP accepts a decision only from the authorizer it named — "
                        f"so only {approver} can decide this one."
                    )
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        if approver.upper() not in self.configured_approvers():
            return Response(
                {
                    "error": (
                        f"Your SAP password for {who} is not configured, so the app "
                        "cannot sign in as you to record this. Ask an administrator "
                        "to add it, or decide this one in SAP."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # SAP stamps the authorizer; carry the real actor in the remarks.
        if decision == SapApprovalAudit.DECISION_REJECTED:
            remarks = f"{reason} — {self.acting_name()} (Factory app)"
        else:
            remarks = f"Approved by {self.acting_name()} (Factory app)"

        result = client.decide_transfer_approval(
            wdd_code,
            approve=(decision == SapApprovalAudit.DECISION_APPROVED),
            remarks=remarks,
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
