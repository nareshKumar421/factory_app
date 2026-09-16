"""Shared plumbing for the app's SAP approval queues.

Two pages decide SAP approval requests from this app — the transfer queue
(:mod:`warehouse.views_sap_approval`) and the credit-note queue
(:mod:`warehouse.views_credit_note_approval`) — and the rules that make either
safe are identical, because they are SAP's rules rather than either document's:

* SAP accepts a decision from exactly one account, the authorizer its approval
  template names on the request's *current* stage. Anyone else is refused with
  ``-6006``, so the authorizer is read fresh from HANA at decision time and
  never taken from the request body.
* That account's password must be configured (``SAP_APPROVER_CREDENTIALS``) or
  the app cannot authenticate as them at all.
* The caller must BE that authorizer (:class:`sap_client.models.SapApproverIdentity`),
  so a decision SAP records against ``USER37`` was genuinely taken by the person
  who is ``USER37`` rather than by whoever reached a page holding her password.

Subclasses supply :meth:`client` themselves, each importing ``SAPClient`` in
its own module — the two queues mock SAP independently in their tests, and a
single shared import would make one page's patch silently cover the other's.
"""

import logging

from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from sap_client.client import SAPClient
from sap_client.exceptions import (
    SAPConnectionError,
    SAPDataError,
    SAPValidationError,
)
from sap_client.models import SapApproverIdentity

logger = logging.getLogger(__name__)


class SapApprovalViewBase(APIView):
    """Company context, SAP identity and error shaping for an approval queue."""

    #: Named in the 502 log line so an outage says which queue hit it.
    queue_name = "SAP approval"

    def handle_exception(self, exc):
        if isinstance(exc, SAPValidationError):
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        if isinstance(exc, (SAPConnectionError, SAPDataError)):
            logger.error("SAP error in the %s queue: %s", self.queue_name, exc)
            return Response({"error": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        return super().handle_exception(exc)

    @property
    def company(self):
        # HasCompanyContext attaches request.company as a UserCompany.
        return self.request.company.company

    def client(self) -> SAPClient:
        """Overridden per queue so each module owns its own SAPClient symbol."""
        raise NotImplementedError

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

    # ------------------------------------------------------------------
    # The decision guards, in the order they must run
    # ------------------------------------------------------------------

    def refuse_decision(self, stage: dict, subject: str) -> Response | None:
        """``None`` if the caller may sign ``stage``, else the refusal to return.

        ``subject`` names the document in the error text ("transfer approval",
        "credit note"), which is all that differs between the two queues.
        """
        if stage["status"] != "PENDING":
            return Response(
                {
                    "error": (
                        f"This {subject} is already "
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
        return None

    def decision_remarks(self, approved: bool, reason: str) -> str:
        """SAP stamps the authorizer; carry the real actor in the remarks."""
        if approved:
            return f"Approved by {self.acting_name()} (Factory app)"
        return f"{reason} — {self.acting_name()} (Factory app)"

    def annotate_rows(self, rows: list, can_approve: bool) -> list:
        """Flag each listed row with what this caller can do about it.

        Rows the caller cannot act on are still listed — seeing that a document
        is stuck, and on whom, is the point of surfacing SAP's queue at all.
        """
        available = self.configured_approvers()
        mine = (self.my_sap_code() or "").upper()
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
        return rows
