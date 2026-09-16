"""API views for the SAP credit-note approval queue.

Credit notes are raised in the SAP client and, where a company's approval
procedure catches them, sit as drafts nobody outside SAP can see. This page is
where they surface: A/R credit notes (a customer is credited) and A/P ones (a
vendor is debited), company-wide, each naming the one authorizer SAP will take
a decision from.

The rules are SAP's, not this page's, and are enforced in
:class:`warehouse.views_sap_approval_base.SapApprovalViewBase` exactly as they
are for the transfer queue: the authorizer is re-read from HANA at decision
time, the caller must BE that authorizer, and that account's password must be
configured. See that module for why each of those exists.
"""

import logging
from decimal import Decimal, InvalidOperation

from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from company.permissions import HasCompanyContext
from sap_client.client import SAPClient

from .models_credit_note_approval import CreditNoteApprovalAudit
from .permissions import CanApproveCreditNote, CanViewCreditNoteApproval
from .serializers_sap_approval import SapApprovalDecisionSerializer
from .views_sap_approval_base import SapApprovalViewBase

logger = logging.getLogger(__name__)


class _CreditNoteApprovalView(SapApprovalViewBase):
    """The shared plumbing, with this queue's own SAPClient symbol."""

    queue_name = "credit-note-approval"

    def client(self) -> SAPClient:
        return SAPClient(company_code=self.company.code)


class CreditNoteApprovalListView(_CreditNoteApprovalView):
    """GET /api/v1/warehouse/credit-note-approvals/?status=PENDING&family=ALL

    ``status`` is PENDING (default), APPROVED, REJECTED or ALL; ``family`` is
    AR, AP or ALL (default). Each row carries ``approver_code`` — the SAP user
    the request waits on — plus ``is_mine``, ``credentials_configured`` and
    ``can_decide``, so the page can say why a row it cannot act on is stuck.
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewCreditNoteApproval]

    def get(self, request):
        requested = (request.query_params.get("status") or "PENDING").upper()
        rows = self.client().list_credit_note_approvals(
            status=None if requested == "ALL" else requested,
            family=request.query_params.get("family"),
            # Clamped: the history views ask for more than the live queue, and
            # each row costs a HANA read of the draft's lines.
            limit=max(1, min(int(request.query_params.get("limit") or 100), 500)),
        )
        can_approve = CanApproveCreditNote().has_permission(request, self)
        return Response(self.annotate_rows(rows, can_approve))


class CreditNoteApprovalPendingCountView(_CreditNoteApprovalView):
    """GET /api/v1/warehouse/credit-note-approvals/pending-count/ — sidebar badge.

    Counts the whole company queue, not just the caller's own rows: a credit
    note stuck on somebody who never opens the app is exactly what the badge is
    for.
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewCreditNoteApproval]

    def get(self, request):
        total = self.client().count_pending_credit_note_approvals(
            family=request.query_params.get("family"),
        )
        return Response({"total": total})


class CreditNoteApprovalDecisionView(_CreditNoteApprovalView):
    """PATCH /api/v1/warehouse/credit-note-approvals/<wdd_code>/status/

    ``wdd_code`` is the SAP approval-request code (``OWDD.WddCode``).
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanApproveCreditNote]

    def patch(self, request, wdd_code):
        serializer = SapApprovalDecisionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        decision = serializer.validated_data["status"]
        reason = serializer.validated_data.get("rejection_reason", "")
        approved = decision == CreditNoteApprovalAudit.DECISION_APPROVED

        client = self.client()
        # Re-read the stage from SAP: the authorizer is whoever SAP says it is
        # right now, not whoever the page was rendered with.
        stage = client.credit_note_approval_stage(wdd_code)
        refusal = self.refuse_decision(stage, "credit note")
        if refusal is not None:
            return refusal
        approver = stage["approver_code"].strip()

        result = client.decide_credit_note_approval(
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
            CreditNoteApprovalAudit.objects.create(
                approval_code=stage["id"],
                draft_entry=stage.get("draft_entry"),
                obj_type=stage.get("obj_type") or "",
                doc_num=stage.get("doc_num"),
                card_code=stage.get("card_code") or "",
                party_name=(stage.get("party_name") or "")[:200],
                total_amount=_decimal(stage.get("total_amount")),
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
                "Credit-note approval %s was %s in SAP but the local audit row failed",
                stage["id"], decision,
            )


def _decimal(value):
    """HANA hands the total over as a decimal string; a bad one must not throw.

    The audit row is bookkeeping about a decision SAP has already accepted, so
    an unparseable amount costs the amount, never the row.
    """
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
