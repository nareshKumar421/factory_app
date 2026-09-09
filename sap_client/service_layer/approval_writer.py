"""Decide SAP approval requests (approve / reject) through the Service Layer.

SAP models the decision as a PATCH on ``ApprovalRequests(WddCode)`` carrying an
``ApprovalRequestDecisions`` entry with the deciding user's own Service Layer
credentials — SAP authenticates that user and records them as the approver, so
whoever signs MUST be an authorizer on the request's current approval-template
stage (SAP-side setup). Every stage in this estate names exactly one authorizer
(``WST1``, ``MaxReqr = 1``), so callers pass that user's code as ``approver``
and its password is looked up in the company's ``approvers`` map. The factory
employee who actually clicked is recorded app-side (``InvoiceApprovalAudit`` /
``SapApprovalAudit``) and inside the decision's ``Remarks``.
"""

import logging

import requests

from ..exceptions import SAPConnectionError, SAPDataError, SAPValidationError
from .auth import ServiceLayerSession

logger = logging.getLogger(__name__)

# SAP enum values, request header + decision line.
REQUEST_PENDING = "arsPending"
DECISION_APPROVED = "ardApproved"
DECISION_REJECTED = "ardNotApproved"

_REQUEST_STATUS_LABELS = {
    "arsApproved": "already approved",
    "arsNotApproved": "already rejected",
    "arsCanceled": "cancelled in SAP",
    "arsGenerated": "already posted in SAP",
}

# WDD1.Remarks is a short NVARCHAR — keep well under SAP's limit.
_REMARKS_MAX = 200


class ApprovalRequestWriter:
    """Approve or reject one SAP approval request."""

    def __init__(self, context):
        self.context = context
        self.sl_config = context.service_layer

    def _approver_credentials(self, approver: str | None = None) -> tuple[str, str]:
        """The SAP user that signs the decision.

        SAP authenticates this user and checks it may decide the request. When
        ``approver`` names a SAP user — the authorizer the request's current
        stage actually belongs to — its password comes from the company's
        ``approvers`` map (``SAP_APPROVER_CREDENTIALS``). That is the only way a
        multi-authorizer estate works: SAP refuses every account but the stage's
        own with ``-6006``.

        With no ``approver``, falls back to the single configured approval
        account and then to the Service Layer session user.
        """
        if approver:
            code = approver.strip().upper()
            approvers = self.sl_config.get("approvers") or {}
            password = approvers.get(code)
            if not password:
                raise SAPValidationError(
                    f"No SAP password is configured for '{approver}', the authorizer on "
                    "this request's current approval stage. Add it to "
                    "SAP_APPROVER_CREDENTIALS before deciding this from the app."
                )
            return approver.strip(), password
        user = self.sl_config.get("approval_username") or self.sl_config["username"]
        password = self.sl_config.get("approval_password") or self.sl_config["password"]
        return user, password

    def decide(
        self,
        wdd_code: int,
        approve: bool,
        remarks: str = "",
        approver: str | None = None,
        subject: str = "Invoice",
    ) -> dict:
        """Record a decision on approval request ``wdd_code``.

        ``approver`` is the SAP user code to sign as; pass the request's
        current-stage authorizer so SAP accepts the decision. ``subject`` only
        names the document in the success message.

        Pre-checks that the request is still pending so a stale page gets a
        clean validation error instead of a raw SAP one.
        """
        approver_user, approver_password = self._approver_credentials(approver)
        # Log the Service Layer session in AS the approver, so both the session
        # and the decision line carry the same authenticated approver — the shape
        # SAP accepts most reliably.
        session_config = dict(
            self.sl_config, username=approver_user, password=approver_password
        )
        cookies = self._get_session_cookies(session_config)
        current = self._get_request(wdd_code, cookies, approver_user)

        status = current.get("Status")
        if status != REQUEST_PENDING:
            label = _REQUEST_STATUS_LABELS.get(status, f"in state {status}")
            raise SAPValidationError(
                f"Approval request {wdd_code} is {label}; it can no longer be decided."
            )

        payload = {
            "ApprovalRequestDecisions": [
                {
                    "ApproverUserName": approver_user,
                    "ApproverPassword": approver_password,
                    "Status": DECISION_APPROVED if approve else DECISION_REJECTED,
                    "Remarks": (remarks or "")[:_REMARKS_MAX],
                }
            ]
        }
        url = f"{self.sl_config['base_url']}/b1s/v2/ApprovalRequests({int(wdd_code)})"

        try:
            response = requests.patch(
                url, json=payload, cookies=cookies, timeout=30, verify=False
            )
        except requests.exceptions.ConnectionError as e:
            logger.error("Connection error deciding approval request %s: %s", wdd_code, e)
            raise SAPConnectionError("Unable to connect to SAP Service Layer")
        except requests.exceptions.Timeout as e:
            logger.error("Timeout deciding approval request %s: %s", wdd_code, e)
            raise SAPConnectionError("SAP Service Layer request timeout")

        if response.status_code in (200, 204):
            action = "approved" if approve else "rejected"
            logger.info(
                "Approval request %s %s in SAP by %s", wdd_code, action, approver_user
            )
            return {
                "message": f"{subject} {action} in SAP.",
                "signed_as": approver_user,
            }

        error_msg = self._extract_error_message(response)
        if response.status_code == 400:
            logger.error("SAP rejected the decision on %s: %s", wdd_code, error_msg)
            raise SAPValidationError(error_msg)
        if response.status_code in (401, 403):
            # SAP answered and refused this approver; it is not unreachable.
            logger.error(
                "SAP refused the decision on %s signed as %s: %s",
                wdd_code, approver_user, error_msg,
            )
            raise SAPValidationError(self._refusal(approver_user, error_msg))
        logger.error("SAP error deciding approval request %s: %s", wdd_code, error_msg)
        raise SAPDataError(f"Failed to record the decision in SAP: {error_msg}")

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _get_request(self, wdd_code: int, cookies, approver_user: str = "") -> dict:
        url = (
            f"{self.sl_config['base_url']}/b1s/v2/"
            f"ApprovalRequests({int(wdd_code)})?$select=Code,Status,DraftEntry"
        )
        try:
            response = requests.get(url, cookies=cookies, timeout=30, verify=False)
        except requests.exceptions.ConnectionError as e:
            logger.error("Connection error reading approval request %s: %s", wdd_code, e)
            raise SAPConnectionError("Unable to connect to SAP Service Layer")
        except requests.exceptions.Timeout as e:
            logger.error("Timeout reading approval request %s: %s", wdd_code, e)
            raise SAPConnectionError("SAP Service Layer request timeout")

        if response.status_code == 404:
            raise SAPValidationError(f"Approval request {wdd_code} was not found in SAP.")
        if response.status_code in (401, 403):
            raise SAPValidationError(
                self._refusal(
                    approver_user or self._approver_credentials()[0],
                    self._extract_error_message(response),
                )
            )
        if response.status_code >= 400:
            raise SAPDataError(
                f"Failed to read approval request {wdd_code}: "
                f"{self._extract_error_message(response)}"
            )
        try:
            return response.json()
        except ValueError as e:
            raise SAPDataError("SAP returned an unexpected approval-request response") from e

    def _get_session_cookies(self, session_config=None):
        try:
            return ServiceLayerSession(session_config or self.sl_config).login()
        except requests.exceptions.ConnectionError as e:
            logger.error("Failed to connect to SAP Service Layer: %s", e)
            raise SAPConnectionError("Unable to connect to SAP Service Layer")
        except requests.exceptions.Timeout as e:
            logger.error("SAP Service Layer connection timeout: %s", e)
            raise SAPConnectionError("SAP Service Layer connection timeout")
        except requests.exceptions.HTTPError as e:
            # A login SAP itself answered ("bad credentials", "no licence") is a
            # configuration fault, not an outage — name the user it refused.
            user = (session_config or self.sl_config).get("username")
            detail = (
                self._extract_error_message(e.response)
                if e.response is not None
                else str(e)
            )
            logger.error("SAP Service Layer login failed for %s: %s", user, detail)
            raise SAPValidationError(
                f"SAP refused the Service Layer login for user '{user}': {detail}"
            )

    @staticmethod
    def _refusal(approver_user: str, error_msg: str) -> str:
        """Phrase a SAP 401/403 on a decision so the approver reads the real cause.

        SAP only accepts a decision from the request's OWN current-stage
        approver: a service account — superuser or not — gets ``-6006 "You are
        not permitted to perform this action"``. That is an authorization
        answer, so it must never surface as "SAP is unavailable".
        """
        message = f"SAP refused the decision signed as '{approver_user}': {error_msg}"
        if "-6006" in error_msg or "not permitted" in error_msg.lower():
            message += (
                " — this SAP user is not an authorizer on the request's current "
                "approval stage. Ask the SAP admin to add them to the approval "
                "template, or have the assigned approver decide it."
            )
        return message

    @staticmethod
    def _extract_error_message(response) -> str:
        try:
            error_data = response.json()
            if "error" in error_data:
                error = error_data["error"]
                message = error.get("message")
                if isinstance(message, dict):
                    message = message.get("value")
                message = str(message or error_data)
                # SAP's numeric code (-6006 and friends) is the searchable part.
                code = error.get("code")
                if code not in (None, "") and str(code) not in message:
                    return f"({code}) {message}"
                return message
            return str(error_data)
        except Exception:
            return response.text or f"HTTP {response.status_code}"
