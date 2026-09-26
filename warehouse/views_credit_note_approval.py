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

Two extras came over from SAP Portal's credit-note screen
(``backend_v1/routes/creditNotes.js``), both additive:

* **Without Qty Posting** — an optional ``without_qty_posting`` on the existing
  decision body. On an approval it is written to the draft's item lines before
  the decision, exactly as the portal did, so the credit note posts value-only
  (or moving stock). Absent, the decision behaves as it always has.
* **Withdraw** — the person who raised a pending credit-note request cancels
  it, signed as their own SAP account (stored password; the general SAP
  Approvals inbox is where a password can be typed instead).

``credit-note-approvals/<wdd_code>/actions/`` tells the page which of the two
this caller may use on one request, so the list endpoint stays as it was.
"""

import logging
from decimal import Decimal, InvalidOperation

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from company.permissions import HasCompanyContext
from sap_client.client import SAPClient
from sap_client.exceptions import SAPValidationError
from sap_client.hana.approval_inbox_reader import stale_message
from sap_client.hana.credit_note_approval_reader import OBJ_TYPE_AP_CREDIT_NOTE, OBJ_TYPES

from .models_credit_note_approval import CreditNoteApprovalAudit
from .permissions import (
    CanApproveCreditNote,
    CanPrintARCreditNote,
    CanViewCreditNoteApproval,
    approvable_credit_note_families,
    visible_credit_note_families,
)
from .serializers_sap_approval import CreditNoteDecisionSerializer
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
        serializer = CreditNoteDecisionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        decision = serializer.validated_data["status"]
        reason = serializer.validated_data.get("rejection_reason", "")
        approved = decision == CreditNoteApprovalAudit.DECISION_APPROVED
        without_qty = serializer.validated_data.get("without_qty_posting")

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

        changed_lines = None
        if approved and without_qty is not None:
            outcome = self._apply_without_qty_posting(client, wdd_code, approver, without_qty)
            if isinstance(outcome, Response):
                return outcome
            changed_lines = outcome

        result = client.decide_credit_note_approval(
            wdd_code,
            approve=approved,
            remarks=self.decision_remarks(approved, reason),
            approver=approver,
        )

        self._write_audit(stage, decision, reason, approver, result)
        payload = {**result, "signed_as": approver}
        if changed_lines is not None:
            payload["without_qty_posting_lines"] = changed_lines
        return Response(payload)

    def _apply_without_qty_posting(self, client, wdd_code, approver, want: bool):
        """Write Without Qty Posting to the draft's item lines before approving.

        Ported from SAP Portal (``creditNotes.js`` PATCH ``/:code``): only
        while the credit note is still a draft, only the item lines whose flag
        differs, and the signer is proved first — a refused login must not
        leave a changed-but-unapproved draft behind. If SAP refuses the change,
        nothing is approved. Returns how many lines changed, or the refusal.
        """
        state = client.approval_inbox_stage(wdd_code, with_item_lines=True)
        if state is None or state.get("object_type") not in OBJ_TYPES:
            return Response(
                {"error": f"SAP no longer holds credit-note approval request {wdd_code}."},
                status=status.HTTP_404_NOT_FOUND,
            )
        if not state.get("is_draft"):
            return Response(
                {
                    "error": (
                        "This credit note has already been posted, so Without Qty Posting "
                        "can no longer be changed. Correct it in SAP."
                    )
                },
                status=status.HTTP_409_CONFLICT,
            )
        if state.get("status") != "PENDING":
            return Response(
                {"error": stale_message(state), "code": "STALE_REQUEST"},
                status=status.HTTP_409_CONFLICT,
            )
        changed = [
            line["line_num"]
            for line in state.get("item_lines") or []
            if line["without_qty_posting"] != want
        ]
        if not changed:
            return 0
        client.verify_approval_signer(wdd_code, approver)
        try:
            client.set_draft_lines_without_qty_posting(state["draft_entry"], changed, want)
        except SAPValidationError as e:
            return Response(
                {
                    "error": (
                        "SAP would not update Without Qty Posting on this credit note, "
                        f"so it was NOT approved: {e}"
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        logger.info(
            "Credit-note approval %s: Without Qty Posting %s on draft %s lines %s by user %s",
            wdd_code, "set" if want else "cleared", state["draft_entry"],
            ",".join(str(n) for n in changed), self.request.user.pk,
        )
        return len(changed)

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


class _CreditNoteRequestView(_CreditNoteApprovalView):
    """One request read through the general approvals reader — it carries the
    originator, the effective (draft-aware) status and the draft's item lines,
    which the queue's own reader does not."""

    def credit_note_state(self, wdd_code, **options):
        """``(state, None)``, or ``(None, refusal)`` for a missing request, one
        that is not a credit note, or a family the caller cannot see."""
        state = self.client().approval_inbox_stage(wdd_code, **options)
        if state is None or state.get("object_type") not in OBJ_TYPES:
            return None, Response(
                {"error": f"Credit-note approval request {wdd_code} was not found in SAP."},
                status=status.HTTP_404_NOT_FOUND,
            )
        family = "AP" if state["object_type"] == OBJ_TYPE_AP_CREDIT_NOTE else "AR"
        state["family"] = family
        if family not in visible_credit_note_families(self.request.user):
            return None, Response(
                {"error": "You are not permitted to see this credit-note family."},
                status=status.HTTP_403_FORBIDDEN,
            )
        return state, None

    def stored_password(self, code) -> bool:
        return bool(code) and code.strip().upper() in self.configured_approvers()


class CreditNoteApprovalActionsView(_CreditNoteRequestView):
    """GET /api/v1/warehouse/credit-note-approvals/<wdd_code>/actions/

    What this caller may additionally do on one request: withdraw it (they
    raised it, it is pending, their password is stored) and set Without Qty
    Posting when approving (it is still a draft with item lines, and they
    could approve it). Read on demand when a row is opened, so the queue's
    list endpoint is unchanged.
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewCreditNoteApproval]

    def get(self, request, wdd_code):
        state, refusal = self.credit_note_state(wdd_code, with_item_lines=True)
        if refusal is not None:
            return refusal
        mine = (self.my_sap_code() or "").upper()
        pending = state["status"] == "PENDING"
        originator = (state.get("originator_code") or "").upper()
        approver = (state.get("approver_code") or "").upper()

        is_originator = bool(mine) and originator == mine
        withdraw_note = None
        if not pending:
            withdraw_note = "Only a pending request can be withdrawn."
        elif not is_originator:
            withdraw_note = "Only the person who raised it can withdraw it."
        elif not self.stored_password(mine):
            withdraw_note = (
                "Your SAP password is not stored on the server. Withdraw it from SAP "
                "Approvals, where you can type it, or in SAP."
            )

        lines = state.get("item_lines") or []
        flags = [line["without_qty_posting"] for line in lines]
        current = (True if all(flags) else False if not any(flags) else None) if flags else None
        can_set = bool(
            pending
            and state.get("is_draft")
            and lines
            and state["family"] in approvable_credit_note_families(request.user)
            and mine
            and approver == mine
            and self.stored_password(approver)
        )
        return Response({
            "wdd_code": state["wdd_code"],
            "status": state["status"],
            "is_originator": is_originator,
            "can_withdraw": withdraw_note is None,
            "withdraw_note": withdraw_note,
            "without_qty_posting": {
                # True: every item line credits value only; False: every one
                # moves stock; None: mixed, or no item lines at all.
                "current": current,
                "item_lines": len(lines),
                "can_set": can_set,
            },
        })


class CreditNoteApprovalWithdrawView(_CreditNoteRequestView):
    """POST /api/v1/warehouse/credit-note-approvals/<wdd_code>/withdraw/

    The originator cancels their own pending credit-note request in SAP
    (SAP Portal's ``POST /credit-notes/:code/cancel``). Gated on seeing the
    document's family, then on BEING its originator in SAP; signed with the
    originator's stored password. Guards: pending (409), mapped (403), the
    originator (403), a stored password (400).
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewCreditNoteApproval]

    def post(self, request, wdd_code):
        state, refusal = self.credit_note_state(wdd_code)
        if refusal is not None:
            return refusal
        if state["status"] != "PENDING":
            return Response(
                {"error": stale_message(state), "code": "STALE_REQUEST"},
                status=status.HTTP_409_CONFLICT,
            )
        mine = self.my_sap_code()
        if not mine:
            return Response(
                {
                    "error": (
                        f"Your account is not linked to a SAP user in {self.company.code}. "
                        "Ask an administrator to map you on the SAP Identities page."
                    )
                },
                status=status.HTTP_403_FORBIDDEN,
            )
        originator = state.get("originator_code") or ""
        if originator.upper() != mine.upper():
            return Response(
                {
                    "error": (
                        f"Only the person who raised this credit note ({originator or 'unknown'}) "
                        f"can withdraw it. You act as {mine}."
                    )
                },
                status=status.HTTP_403_FORBIDDEN,
            )
        if not self.stored_password(originator):
            return Response(
                {
                    "error": (
                        f"Your SAP password for {originator} is not stored on the server. "
                        "Withdraw it from SAP Approvals, where you can type it, or in SAP."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        result = self.client().withdraw_approval_request(
            wdd_code, originator=originator, subject="Credit note"
        )
        logger.info(
            "Credit-note approval request %s withdrawn in SAP as %s by user %s",
            wdd_code, originator, request.user.pk,
        )
        return Response({**result, "signed_as": result.get("signed_as") or originator})


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
