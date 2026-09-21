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

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from company.permissions import HasCompanyContext
from sap_client.client import SAPClient
from sap_client.exceptions import SAPValidationError
from sap_client.hana.credit_note_approval_reader import OBJ_TYPE_AP_CREDIT_NOTE

from .models_credit_note_approval import CreditNoteApprovalAudit
from .permissions import (
    CanApproveCreditNote,
    CanPrintARCreditNote,
    CanViewCreditNoteApproval,
    approvable_credit_note_families,
    visible_credit_note_families,
)
from .serializers_sap_approval import SapApprovalDecisionSerializer
from .views_sap_approval_base import SapApprovalViewBase

logger = logging.getLogger(__name__)


class _CreditNoteApprovalView(SapApprovalViewBase):
    """The shared plumbing, with this queue's own SAPClient symbol."""

    queue_name = "credit-note-approval"

    def client(self) -> SAPClient:
        return SAPClient(company_code=self.company.code)

    def requested_families(self) -> str | None:
        """The ``family`` to read, narrowed to what the caller may actually see.

        A caller granted A/R only never reads an A/P row, whatever the query
        string asks for — and asking for the family they do not hold returns
        nothing rather than everything, because widening a refused request to
        ALL is how a filter becomes a hole.
        """
        visible = visible_credit_note_families(self.request.user)
        asked = (self.request.query_params.get("family") or "ALL").strip().upper()
        if asked != "ALL" and asked not in visible:
            return None  # nothing readable
        if asked != "ALL":
            return asked
        # ALL, scoped: one family means read that one, both means read both.
        return next(iter(visible)) if len(visible) == 1 else "ALL"


class CreditNoteApprovalListView(_CreditNoteApprovalView):
    """GET /api/v1/warehouse/credit-note-approvals/?status=PENDING&family=ALL

    ``status`` is PENDING (default), APPROVED, REJECTED or ALL; ``family`` is
    AR, AP or ALL (default). Each row carries ``approver_code`` — the SAP user
    the request waits on — plus ``is_mine``, ``credentials_configured`` and
    ``can_decide``, so the page can say why a row it cannot act on is stuck.
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewCreditNoteApproval]

    def get(self, request):
        family = self.requested_families()
        if family is None:
            return Response([])
        requested = (request.query_params.get("status") or "PENDING").upper()
        rows = self.client().list_credit_note_approvals(
            status=None if requested == "ALL" else requested,
            family=family,
            # Clamped: the history views ask for more than the live queue, and
            # each row costs a HANA read of the draft's lines.
            limit=max(1, min(int(request.query_params.get("limit") or 100), 500)),
        )
        # Approving is per family too, so a row's own family decides whether
        # this caller could act on it — not one blanket flag for the whole page.
        approvable = approvable_credit_note_families(request.user)
        for row in self.annotate_rows(rows, can_approve=True):
            row["can_decide"] = bool(
                row["can_decide"] and row.get("family") in approvable
            )
        return Response(rows)


class CreditNoteApprovalPendingCountView(_CreditNoteApprovalView):
    """GET /api/v1/warehouse/credit-note-approvals/pending-count/ — sidebar badge.

    Counts the whole company queue, not just the caller's own rows: a credit
    note stuck on somebody who never opens the app is exactly what the badge is
    for.
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewCreditNoteApproval]

    def get(self, request):
        family = self.requested_families()
        if family is None:
            return Response({"total": 0})
        total = self.client().count_pending_credit_note_approvals(family=family)
        return Response({"total": total})


class CreditNotePrintView(_CreditNoteApprovalView):
    """GET /api/v1/warehouse/credit-notes/<doc_entry>/print/ — the printed sheet.

    Keyed by SAP's ``DocEntry`` for the POSTED credit note (``ORIN``), not by
    the approval request: a request is a decision waiting to be taken, and what
    gets printed is the document SAP wrote once it was. The queue row carries
    that entry as ``posted_doc_entry``, and it is null until SAP has one — which
    is why nothing is printable from a pending row.

    A read, so the A/R view permission is enough. Printing a credit note the
    queue already lists is not a second chance to approve one.
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanPrintARCreditNote]

    def get(self, request, doc_entry):
        try:
            payload = self.client().credit_note_print(doc_entry)
        except SAPValidationError as e:
            # Cancelled, or a service credit note: it exists, but this sheet is
            # not the one to print it on. 404 like the sibling invoice print —
            # every one of these means "there is no sheet", and the message
            # says which.
            return Response({"detail": str(e)}, status=status.HTTP_404_NOT_FOUND)
        if not payload:
            return Response(
                {
                    "detail": (
                        f"SAP has no A/R credit note with entry {doc_entry} for "
                        f"{self.company.code}."
                    )
                },
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(payload)


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

        # WHICH family this document is decides the permission, and it is read
        # from SAP rather than from the request: the endpoint's own gate only
        # proves the caller may decide *something*.
        family = "AP" if str(stage.get("obj_type")) == OBJ_TYPE_AP_CREDIT_NOTE else "AR"
        if family not in approvable_credit_note_families(request.user):
            return Response(
                {
                    "error": (
                        f"This is an {'A/P' if family == 'AP' else 'A/R'} credit note, "
                        f"and you are not permitted to decide "
                        f"{'vendor' if family == 'AP' else 'customer'} credit notes."
                    )
                },
                status=status.HTTP_403_FORBIDDEN,
            )

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
