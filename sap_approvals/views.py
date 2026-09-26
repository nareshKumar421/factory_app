"""SAP Approvals API — the general approval inbox ported from SAP Portal.

``/api/v1/sap-approvals/``:

* ``GET requests/`` — requests that involve the caller, any document type.
* ``GET requests/<wdd_code>/`` — one request, every stage, the draft's lines.
* ``POST requests/<wdd_code>/decision/`` — approve or reject.
* ``POST requests/<wdd_code>/withdraw/`` — the originator cancels a pending one.
* ``GET pending-count/`` — how many wait on the caller (the sidebar badge).

Ported from SAP Portal's ``routes/sap.js`` ``/approval-requests`` routes
(~2780–3013) and ``services/sapApprovals.js``. What changed and why is in
``docs/README.md``; the load-bearing parts:

* **Identity is SapApproverIdentity, nothing else.** The caller's mapped SAP
  user code for the company decides what they see and what they may sign.
* **A typed password never lets anyone act as someone else.** The caller must
  still BE the stage's authorizer (or the request's originator, to withdraw);
  the password only replaces the stored ``SAP_APPROVER_CREDENTIALS`` entry for
  that one Service Layer call. It is never stored, logged, cached or returned.
* **Re-deciding is refused.** The portal let an approver on a decided request
  send another decision; JI refuses anything that is not pending (an open
  business decision, recorded in the README).

The decision guards run in this order, each before SAP is called: pending by
the effective rule (409 ``STALE_REQUEST``), the caller is mapped (403) and is an
authorizer of the current stage (403), approving a document SAP already posted
(409 ``DUPLICATE_DOCUMENT`` unless ``confirm_duplicate``), a password to sign
with — typed or stored (400). The portal asked for the password last for the
same reason: a leftover or a duplicate is reported before anyone types one.
"""

import logging

from django.views.decorators.debug import sensitive_variables
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from company.permissions import HasCompanyContext
from sap_client.client import SAPClient
from sap_client.exceptions import SAPConnectionError, SAPDataError, SAPValidationError
from sap_client.hana.approval_inbox_reader import OBJECT_TYPE_LABELS, stale_message
from warehouse.views_sap_approval_base import SapApprovalViewBase

from .constants import DecisionAction
from .models import SapApprovalDecision
from .permissions import (
    DECIDE_PERMISSION,
    WITHDRAW_PERMISSION,
    CanDecideSapApprovals,
    CanViewSapApprovalInbox,
    CanWithdrawOwnSapApprovals,
)
from .serializers import DecisionSerializer, RequestFilterSerializer, WithdrawSerializer

logger = logging.getLogger(__name__)

STALE_REQUEST = "STALE_REQUEST"
DUPLICATE_DOCUMENT = "DUPLICATE_DOCUMENT"


class _InboxView(SapApprovalViewBase):
    """Company context, identity and error shaping for the inbox.

    Reuses the approval queues' base for the company, the caller's SAP code,
    the configured passwords and the remarks that name the real actor. Its
    ``refuse_decision``/``annotate_rows`` are deliberately not used: the inbox
    guards run in a different order (409 before 403, see the module docstring)
    and a row is decidable without a stored password, because the approver may
    type theirs.
    """

    queue_name = "SAP approvals inbox"

    def handle_exception(self, exc):
        # The sap-integration mapping: refusal 400, unreachable 503, broken 502.
        if isinstance(exc, SAPValidationError):
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        if isinstance(exc, SAPConnectionError):
            logger.error("SAP unreachable in the %s: %s", self.queue_name, exc)
            return Response(
                {"error": "SAP is currently unavailable. Please try again in a moment."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        if isinstance(exc, SAPDataError):
            logger.error("SAP data error in the %s: %s", self.queue_name, exc)
            return Response({"error": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        return super().handle_exception(exc)

    def client(self) -> SAPClient:
        return SAPClient(company_code=self.company.code)

    def unmapped_message(self) -> str:
        return (
            f"Your account is not linked to a SAP user in {self.company.code}, so the "
            "app cannot tell which SAP approvals are yours. Ask an administrator to "
            "map you on the SAP Identities page."
        )

    def has_stored_password(self, code: str | None) -> bool:
        if not code:
            return False
        return code.strip().upper() in {c.upper() for c in self.configured_approvers()}

    def identity(self, mine: str | None) -> dict | None:
        if not mine:
            return None
        return {"sap_user_code": mine, "credentials_configured": self.has_stored_password(mine)}

    def annotate(self, rows: list, mine: str) -> list:
        """What the caller can do about each row — the page follows these flags."""
        user = self.request.user
        can_decide = user.has_perm(DECIDE_PERMISSION)
        can_withdraw = user.has_perm(WITHDRAW_PERMISSION)
        stored = self.has_stored_password(mine)
        for row in rows:
            pending = row.get("status") == "PENDING"
            row["is_mine"] = bool(pending and row.get("waiting_on_me"))
            row["is_originator"] = bool(
                row.get("originator_code")
                and row["originator_code"].upper() == mine.upper()
            )
            # The only account the caller can ever sign as is their own.
            row["credentials_configured"] = stored
            row["can_decide"] = bool(can_decide and row["is_mine"])
            row["can_withdraw"] = bool(can_withdraw and pending and row["is_originator"])
        return rows

    def not_found(self, wdd_code) -> Response:
        return Response(
            {"error": f"Approval request {wdd_code} was not found in SAP."},
            status=status.HTTP_404_NOT_FOUND,
        )

    def stale_response(self, stage: dict) -> Response:
        return Response(
            {"error": stale_message(stage), "code": STALE_REQUEST, "status": stage.get("status")},
            status=status.HTTP_409_CONFLICT,
        )

    def write_audit(self, stage: dict, action: str, signed_as: str, remarks: str,
                    typed: bool, confirmed_duplicate: bool = False) -> None:
        """After SAP accepted. A bookkeeping failure never undoes SAP's record."""
        try:
            SapApprovalDecision.objects.create(
                company=self.company,
                wdd_code=stage["wdd_code"],
                object_type=stage.get("object_type") or "",
                draft_entry=stage.get("draft_entry"),
                action=action,
                signed_as=(signed_as or "")[:50],
                remarks=remarks or "",
                typed_password=typed,
                confirmed_duplicate=confirmed_duplicate,
                created_by=self.request.user,
            )
        except Exception:
            logger.exception(
                "SAP approval request %s: %s accepted by SAP but the local audit row failed",
                stage.get("wdd_code"), action,
            )


class ApprovalRequestListAPI(_InboxView):
    """GET ?scope=waiting_on_me|raised_by_me|all ?status ?object_type ?date_from
    ?date_to ?search ?limit — requests that involve the caller, newest first."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewSapApprovalInbox]

    def get(self, request):
        filters = RequestFilterSerializer(data=request.query_params)
        filters.is_valid(raise_exception=True)
        reader_filters = filters.to_reader_filters()
        object_types = [
            {"code": code, "label": label} for code, label in OBJECT_TYPE_LABELS.items()
        ]
        mine = self.my_sap_code()
        if not mine:
            # Not an error: the page explains the missing mapping instead.
            return Response({
                "results": [], "count": 0, "limit": reader_filters["limit"],
                "truncated": False, "identity": None,
                "message": self.unmapped_message(), "object_types": object_types,
            })
        rows = self.client().list_approval_inbox(mine, **reader_filters)
        self.annotate(rows, mine)
        return Response({
            "results": rows,
            "count": len(rows),
            "limit": reader_filters["limit"],
            # A full page means there may be more: narrow the filters.
            "truncated": len(rows) >= reader_filters["limit"],
            "identity": self.identity(mine),
            "object_types": object_types,
        })


class ApprovalRequestDetailAPI(_InboxView):
    """GET — one request: header, every stage (WDD1), the draft's lines,
    siblings and posted duplicates. Only for a request that involves the caller."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewSapApprovalInbox]

    def get(self, request, wdd_code):
        mine = self.my_sap_code()
        if not mine:
            return Response({"error": self.unmapped_message()}, status=status.HTTP_403_FORBIDDEN)
        data = self.client().approval_inbox_detail(wdd_code, mine)
        if data is None:
            return self.not_found(wdd_code)
        if not self._visible(data, mine):
            return Response(
                {"error": "You are not on this SAP approval request, so you cannot open it."},
                status=status.HTTP_403_FORBIDDEN,
            )
        self.annotate([data], mine)
        return Response(data)

    @staticmethod
    def _visible(data: dict, mine: str) -> bool:
        """The portal's rule: you raised it, or a line of yours — which, while
        the request is pending, must be undecided and at the current stage."""
        code = mine.upper()
        if (data.get("originator_code") or "").upper() == code:
            return True
        pending = data.get("status") == "PENDING"
        for stage in data.get("stages") or []:
            if (stage.get("user_code") or "").upper() != code:
                continue
            if not pending or (stage.get("status") == "PENDING" and stage.get("is_current")):
                return True
        return False


class ApprovalRequestDecisionAPI(_InboxView):
    """POST {approve, remarks, sap_password?, confirm_duplicate?} — approve or reject,
    signed as the caller's own SAP account. Guards: see the module docstring."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanDecideSapApprovals]

    @sensitive_variables("body", "typed")
    def post(self, request, wdd_code):
        body = DecisionSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        approve = body.validated_data["approve"]
        remarks = body.validated_data["remarks"]
        confirm_duplicate = body.validated_data["confirm_duplicate"]
        typed = body.typed_password()

        client = self.client()
        stage = client.approval_inbox_stage(wdd_code, with_duplicates=approve)
        if stage is None:
            return self.not_found(wdd_code)

        # 1. Still pending, by the draft's say — not just OWDD's.
        if stage["status"] != "PENDING":
            return self.stale_response(stage)

        # 2. The caller IS an authorizer of the stage now waiting.
        refusal = self._refuse_identity(stage)
        if refusal is not None:
            return refusal
        signer = self._signer(stage)

        # 3. Approving a document SAP already posted posts it again.
        posted = stage.get("posted_duplicates") or []
        if approve and posted and not confirm_duplicate:
            return Response(
                {
                    "error": self._duplicate_message(stage, posted),
                    "code": DUPLICATE_DOCUMENT,
                    "duplicate_of": posted,
                },
                status=status.HTTP_409_CONFLICT,
            )

        # 4. Something to sign with: the password typed now, or the stored one.
        if typed is None and not self.has_stored_password(signer):
            return Response(
                {
                    "error": (
                        f"No SAP password is stored for {signer}. Type your SAP password "
                        "to sign this decision — it is used for this one decision and "
                        "never saved."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        if approve and posted:
            logger.warning(
                "SAP approval request %s approved over a posted duplicate (%s) by user %s as %s",
                wdd_code,
                ", ".join(str(p.get("doc_num")) for p in posted),
                request.user.pk,
                signer,
            )
        result = client.decide_approval_request(
            wdd_code,
            approve=approve,
            remarks=self._remarks(approve, remarks),
            approver=signer,
            password=typed,
            subject=stage.get("object_type_label") or "Document",
        )
        signed_as = result.get("signed_as") or signer
        self.write_audit(
            stage,
            DecisionAction.APPROVE if approve else DecisionAction.REJECT,
            signed_as,
            remarks,
            typed=typed is not None,
            confirmed_duplicate=bool(approve and posted),
        )
        return Response({
            "message": result.get("message") or "Decision recorded in SAP.",
            "signed_as": signed_as,
            "wdd_code": stage["wdd_code"],
            "action": "APPROVE" if approve else "REJECT",
        })

    def _refuse_identity(self, stage: dict) -> Response | None:
        mine = self.my_sap_code()
        if not mine:
            return Response({"error": self.unmapped_message()}, status=status.HTTP_403_FORBIDDEN)
        authorizers = stage.get("authorizer_codes") or []
        if not authorizers:
            return Response(
                {
                    "error": (
                        "SAP does not name an authorizer on this request's current stage, "
                        "so it cannot be decided from the app."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        if mine.upper() not in {code.upper() for code in authorizers}:
            name = stage.get("approver_name")
            who = f"{authorizers[0]} ({name})" if name else ", ".join(authorizers)
            return Response(
                {
                    "error": (
                        f"This approval is waiting on {who}. You act as {mine}, and SAP "
                        "accepts a decision only from the authorizer it named — so only "
                        "they can decide this one."
                    )
                },
                status=status.HTTP_403_FORBIDDEN,
            )
        return None

    def _signer(self, stage: dict) -> str:
        """The caller's code as SAP spells it on the stage."""
        mine = self.my_sap_code().upper()
        return next(c for c in stage["authorizer_codes"] if c.upper() == mine)

    def _remarks(self, approve: bool, remarks: str) -> str:
        """SAP stamps the SAP account; the app user rides in the remarks."""
        if approve and remarks:
            return f"{remarks} — approved by {self.acting_name()} (Factory app)"
        return self.decision_remarks(approve, remarks)

    @staticmethod
    def _duplicate_message(stage: dict, posted: list) -> str:
        """The portal's DUPLICATE_DOCUMENT wording (routes/sap.js)."""
        label = stage.get("object_type_label") or "document"
        shown = ", ".join(
            f"{label} #{p.get('doc_num') or p.get('doc_entry')}"
            + (f" dated {p['doc_date']}" if p.get("doc_date") else "")
            for p in posted
        )
        own = stage.get("already_posted_as")
        if own and any(p["doc_entry"] == own["doc_entry"] for p in posted):
            head = (
                f"This draft has already been posted in SAP as {shown}, so this request "
                "is a leftover and approving it posts nothing new. "
            )
        else:
            head = (
                f"This document is already posted in SAP as {shown} — same party, date, "
                "amount and reference. Approving this copy would post it a second time. "
            )
        return head + (
            "Reject this request to clear it — or confirm explicitly if this really is "
            "a separate document."
        )


class ApprovalRequestWithdrawAPI(_InboxView):
    """POST {sap_password?} — the originator cancels their own pending request.

    Guards, in order: pending (409 ``STALE_REQUEST``), the caller is mapped
    (403) and is the originator (403), a password to sign with (400).
    """

    permission_classes = [IsAuthenticated, HasCompanyContext, CanWithdrawOwnSapApprovals]

    @sensitive_variables("body", "typed")
    def post(self, request, wdd_code):
        body = WithdrawSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        typed = body.typed_password()

        client = self.client()
        stage = client.approval_inbox_stage(wdd_code)
        if stage is None:
            return self.not_found(wdd_code)
        if stage["status"] != "PENDING":
            return self.stale_response(stage)

        mine = self.my_sap_code()
        if not mine:
            return Response({"error": self.unmapped_message()}, status=status.HTTP_403_FORBIDDEN)
        originator = stage.get("originator_code") or ""
        if originator.upper() != mine.upper():
            return Response(
                {
                    "error": (
                        f"Only the person who raised this request ({originator or 'unknown'}) "
                        f"can withdraw it. You act as {mine}."
                    )
                },
                status=status.HTTP_403_FORBIDDEN,
            )
        if typed is None and not self.has_stored_password(originator):
            return Response(
                {
                    "error": (
                        f"No SAP password is stored for {originator}. Type your SAP password "
                        "to withdraw this request — it is used once and never saved."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        result = client.withdraw_approval_request(
            wdd_code,
            originator=originator,
            password=typed,
            subject=stage.get("object_type_label") or "Approval request",
        )
        signed_as = result.get("signed_as") or originator
        self.write_audit(stage, DecisionAction.WITHDRAW, signed_as, "", typed=typed is not None)
        return Response({
            "message": result.get("message") or "Request withdrawn in SAP.",
            "signed_as": signed_as,
            "wdd_code": stage["wdd_code"],
            "action": "WITHDRAW",
        })


class PendingCountAPI(_InboxView):
    """GET — requests waiting on the caller's own SAP user. Drives the badge."""

    permission_classes = [IsAuthenticated, HasCompanyContext, CanViewSapApprovalInbox]

    def get(self, request):
        mine = self.my_sap_code()
        if not mine:
            return Response({"total": 0})
        return Response({"total": self.client().count_approval_inbox_waiting(mine)})
