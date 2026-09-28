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

What came over from SAP Portal's credit-note screen
(``backend_v1/routes/creditNotes.js``):

* **Without Qty Posting** — an optional ``without_qty_posting`` on the
  decision body. On an approval it is written to the draft's item lines before
  the decision, exactly as the portal did, so the credit note posts value-only
  (or moving stock). Absent, the lines stay as SAP holds them.
* **Withdraw** — the person who raised a pending credit-note request cancels
  it, signed as their own SAP account.
* **A typed SAP password** (``sap_password``) on a decision or a withdraw, for
  approvers with no stored one. Used for that one call, never stored or logged.
* **The duplicate guard** — an approval is refused (409
  ``DUPLICATE_CREDIT_NOTE``) while SAP holds a posted credit note for the same
  party and amount, unless the approver confirms; and a request whose draft
  was already decided is refused as stale, whatever OWDD still says.
* **The approver's comment** (``approval_comment``) in SAP's remarks.
* **Attachments** — the files of the credit note and of the documents it was
  copied from, readable with the queue's own view right.

``credit-note-approvals/<wdd_code>/actions/`` tells the page which of these
this caller may use on one request, so the list stays one read per row.
"""

import logging
from decimal import Decimal, InvalidOperation

from django.views.decorators.debug import sensitive_variables
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from company.permissions import HasCompanyContext
from sap_client.client import SAPClient
from sap_client.exceptions import SAPConnectionError, SAPDataError, SAPValidationError
from sap_client.hana.approval_inbox_reader import stale_message
from sap_client.hana.credit_note_approval_reader import OBJ_TYPE_AP_CREDIT_NOTE, OBJ_TYPES
from sap_documents import services as document_services
from sap_documents.constants import DOCUMENT_TYPES

from .models_credit_note_approval import CreditNoteApprovalAudit
from .permissions import (
    CanApproveCreditNote,
    CanPrintARCreditNote,
    CanViewCreditNoteApproval,
    approvable_credit_note_families,
    visible_credit_note_families,
)
from .serializers_sap_approval import (
    CreditNoteDecisionSerializer,
    CreditNoteListFilterSerializer,
    CreditNoteWithdrawSerializer,
)
from .views_sap_approval_base import SapApprovalViewBase

logger = logging.getLogger(__name__)

STALE_REQUEST = "STALE_REQUEST"
# SAP Portal's code for the same refusal (creditNotes.js), kept so the page and
# anything scripted against the portal read it the same way.
DUPLICATE_CREDIT_NOTE = "DUPLICATE_CREDIT_NOTE"


def duplicate_message(posted: list) -> str:
    """SAP Portal's wording for an approval over an already-posted credit note."""
    shown = ", ".join(
        f"#{p.get('doc_num') or p.get('doc_entry')}" + (f" dated {p['doc_date']}" if p.get("doc_date") else "")
        for p in posted
    )
    return (
        f"This credit note is already posted in SAP as {shown}. Approving it again would "
        "credit the same amount twice. Reject or withdraw this request to clear it — or "
        "confirm explicitly if this really is a separate credit note."
    )


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

    Searched in SAP, not on the page: ``party``, ``doc_num``, ``code``,
    ``date_from``, ``date_to``, and ``limit`` / ``offset`` to page on
    (:class:`CreditNoteListFilterSerializer`).
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewCreditNoteApproval]

    def get(self, request):
        family = self.requested_families()
        if family is None:
            return Response([])
        params = request.query_params.copy()
        params["status"] = (params.get("status") or "PENDING").upper()
        filters = CreditNoteListFilterSerializer(data=params)
        filters.is_valid(raise_exception=True)
        requested = filters.validated_data["status"]
        rows = self.client().list_credit_note_approvals(
            status=None if requested == "ALL" else requested,
            family=family,
            # Clamped by the serializer: each row costs a HANA read of the
            # draft's lines.
            limit=filters.validated_data["limit"],
            **filters.to_reader_filters(),
        )
        # Approving is per family too, so a row's own family decides whether
        # this caller could act on it — not one blanket flag for the whole page.
        approvable = approvable_credit_note_families(request.user)
        # The approver may type their SAP password, so a missing stored one no
        # longer makes a row undecidable; ``credentials_configured`` still says
        # whether the page must ask for it.
        for row in self.annotate_rows(rows, can_approve=True, typed_password_allowed=True):
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

    Guards, in order — each one SAP Portal's ``creditNotes.js`` PATCH had:

    1. The approve right of the document's own family, read from SAP.
    2. The shared identity guards: the caller IS the stage's authorizer, and
       there is a password to sign with — stored, or typed now
       (``sap_password``).
    3. Still pending **by the draft's say**, not OWDD's alone: a second
       template's request can stay ``W`` after its draft was decided
       (409 ``STALE_REQUEST``).
    4. On an approval, SAP holds no posted credit note for the same party and
       amount, unless the approver confirms (409 ``DUPLICATE_CREDIT_NOTE``).
       Approving a duplicate credits the party twice.
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanApproveCreditNote]

    @sensitive_variables("serializer", "typed")
    def patch(self, request, wdd_code):
        serializer = CreditNoteDecisionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        decision = serializer.validated_data["status"]
        reason = serializer.validated_data.get("rejection_reason", "")
        approved = decision == CreditNoteApprovalAudit.DECISION_APPROVED
        without_qty = serializer.validated_data.get("without_qty_posting")
        comment = serializer.validated_data.get("approval_comment", "")
        confirm_duplicate = serializer.validated_data.get("confirm_duplicate", False)
        typed = serializer.typed_password()

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

        refusal = self.refuse_decision(stage, "credit note", typed_password=typed is not None)
        if refusal is not None:
            return refusal
        approver = stage["approver_code"].strip()

        # 3 and 4 need the general approvals reader: it knows the draft's own
        # status and reads SAP's posted duplicates (failing closed if it can't).
        state = client.approval_inbox_stage(
            wdd_code,
            with_duplicates=approved,
            with_item_lines=approved and without_qty is not None,
        )
        if state is None or state.get("object_type") not in OBJ_TYPES:
            return Response(
                {"error": f"SAP no longer holds credit-note approval request {wdd_code}."},
                status=status.HTTP_404_NOT_FOUND,
            )
        if state.get("status") != "PENDING":
            return Response(
                {"error": stale_message(state), "code": STALE_REQUEST},
                status=status.HTTP_409_CONFLICT,
            )
        posted = state.get("posted_duplicates") or []
        if approved and posted and not confirm_duplicate:
            return Response(
                {
                    "error": duplicate_message(posted),
                    "code": DUPLICATE_CREDIT_NOTE,
                    "duplicate_of": posted,
                },
                status=status.HTTP_409_CONFLICT,
            )

        changed_lines = None
        if approved and without_qty is not None:
            outcome = self._apply_without_qty_posting(client, wdd_code, state, approver, without_qty, typed)
            if isinstance(outcome, Response):
                return outcome
            changed_lines = outcome

        if approved and posted:
            logger.warning(
                "Credit-note approval %s approved over a posted duplicate (%s) by user %s as %s",
                wdd_code,
                ", ".join(str(p.get("doc_num") or p.get("doc_entry")) for p in posted),
                request.user.pk,
                approver,
            )
        result = client.decide_credit_note_approval(
            wdd_code,
            approve=approved,
            remarks=self.decision_remarks(approved, reason, comment),
            approver=approver,
            password=typed,
        )

        self._write_audit(stage, decision, reason, approver, result)
        payload = {**result, "signed_as": approver}
        if changed_lines is not None:
            payload["without_qty_posting_lines"] = changed_lines
        return Response(payload)

    def _apply_without_qty_posting(self, client, wdd_code, state, approver, want: bool, typed):
        """Write Without Qty Posting to the draft's item lines before approving.

        Ported from SAP Portal (``creditNotes.js`` PATCH ``/:code``): only
        while the credit note is still a draft, only the item lines whose flag
        differs, and the signer is proved first — a refused login must not
        leave a changed-but-unapproved draft behind. If SAP refuses the change,
        nothing is approved. Returns how many lines changed, or the refusal.
        ``state`` is the request as :meth:`patch` read it, item lines included.
        """
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
        changed = [
            line["line_num"]
            for line in state.get("item_lines") or []
            if line["without_qty_posting"] != want
        ]
        if not changed:
            return 0
        client.verify_approval_signer(wdd_code, approver, password=typed)
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

    What this caller may additionally do on one request, read on demand when a
    row is opened (so the queue's list stays one HANA read per row):

    * withdraw it — they raised it and it is pending;
    * set Without Qty Posting when approving — still a draft with item lines,
      and they are its authorizer;
    * whether a password must be typed for either (none stored for them);
    * ``posted_duplicates`` — credit notes SAP already posted for the same
      party and amount, which the decision refuses to approve over unless
      confirmed. ``duplicate_check_failed`` says the check could not run; the
      decision then re-runs it and fails closed.
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewCreditNoteApproval]

    def get(self, request, wdd_code):
        duplicate_check_failed = False
        try:
            state, refusal = self.credit_note_state(wdd_code, with_item_lines=True, with_duplicates=True)
        except (SAPConnectionError, SAPDataError) as e:
            # This is a read for the page; the decision itself gates on the check.
            logger.warning("Credit-note approval %s: duplicate check failed: %s", wdd_code, e)
            duplicate_check_failed = True
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
        )
        return Response({
            "wdd_code": state["wdd_code"],
            "status": state["status"],
            "is_originator": is_originator,
            "can_withdraw": withdraw_note is None,
            "withdraw_note": withdraw_note,
            # No stored password: the page asks for it, used once, never saved.
            "password_stored": self.stored_password(mine),
            "posted_duplicates": state.get("posted_duplicates") or [],
            "duplicate_check_failed": duplicate_check_failed,
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
    password they type (``sap_password``) or, failing that, their stored one.
    Guards: pending (409), mapped (403), the originator (403), something to
    sign with (400).
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewCreditNoteApproval]

    @sensitive_variables("body", "typed")
    def post(self, request, wdd_code):
        body = CreditNoteWithdrawSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        typed = body.typed_password()
        state, refusal = self.credit_note_state(wdd_code)
        if refusal is not None:
            return refusal
        if state["status"] != "PENDING":
            return Response(
                {"error": stale_message(state), "code": STALE_REQUEST},
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
        if typed is None and not self.stored_password(originator):
            return Response(
                {
                    "error": (
                        f"No SAP password is stored for {originator}. Type your SAP password "
                        "to withdraw it — it is used for this one call and never saved."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        result = self.client().withdraw_approval_request(
            wdd_code, originator=originator, password=typed, subject="Credit note"
        )
        logger.info(
            "Credit-note approval request %s withdrawn in SAP as %s by user %s",
            wdd_code, originator, request.user.pk,
        )
        return Response({**result, "signed_as": result.get("signed_as") or originator})



class CreditNoteApprovalAttachmentsView(_CreditNoteRequestView):
    """GET /api/v1/warehouse/credit-note-approvals/<wdd_code>/attachments/

    The files an approver checks before deciding: the credit note's own
    attachment entry and those of the documents it was copied from (SAP
    Portal's Attachments tab). Read through the draft, which SAP keeps after
    the credit note is posted. The queue's view right is enough — the approver
    needs the scan, not the whole document browser.
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewCreditNoteApproval]

    def get(self, request, wdd_code):
        state, refusal = self.credit_note_state(wdd_code)
        if refusal is not None:
            return refusal
        sources = [
            {**source, "lines": document_services.attachment_lines(self.company.code, source["abs_entry"])}
            for source in self.attachment_sources(state)
        ]
        return Response({"wdd_code": state["wdd_code"], "sources": sources})

    def attachment_sources(self, state) -> list[dict]:
        """``[{label, abs_entry}]``: this credit note first, then its base documents."""
        draft_entry = state.get("draft_entry")
        if not draft_entry:
            return []
        doc = document_services.document_detail(
            self.company.code, DOCUMENT_TYPES["Drafts"], int(draft_entry)
        )
        if doc is None:
            return []
        sources, seen = [], set()
        own = doc.get("attachment_entry")
        if own:
            seen.add(own)
            sources.append({"label": state.get("object_type_label") or "Credit note", "abs_entry": own})
        for base in doc.get("base_documents") or []:
            entry = base.get("attachment_entry")
            if entry and entry not in seen:
                seen.add(entry)
                number = base.get("doc_num") or base.get("base_entry")
                sources.append({"label": f"{base.get('type_label') or 'Document'} #{number}", "abs_entry": entry})
        return sources


class CreditNoteApprovalAttachmentDownloadView(CreditNoteApprovalAttachmentsView):
    """GET .../<wdd_code>/attachments/<abs_entry>/<line>/download/

    One file, only if it belongs to this credit note or a document it was
    copied from — the queue's view right must not open every attachment in SAP.
    Served and recorded exactly as the document browser serves one.
    """

    def get(self, request, wdd_code, abs_entry, line):
        state, refusal = self.credit_note_state(wdd_code)
        if refusal is not None:
            return refusal
        if abs_entry not in {source["abs_entry"] for source in self.attachment_sources(state)}:
            return Response(
                {"detail": f"Attachment {abs_entry} does not belong to this credit note."},
                status=status.HTTP_404_NOT_FOUND,
            )
        try:
            served = document_services.fetch_attachment(self.company, request.user, abs_entry, line)
        except document_services.AttachmentNotFound as e:
            return Response({"detail": str(e)}, status=status.HTTP_404_NOT_FOUND)
        except SAPValidationError as e:
            if getattr(e, "status", None) == 404:
                return Response(
                    {"detail": f"The attachment file service has no copy of this file. {e}"},
                    status=status.HTTP_404_NOT_FOUND,
                )
            raise
        return document_services.served_file_response(served)

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
