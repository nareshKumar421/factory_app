"""Decide SAP approval requests (approve / reject) through the Service Layer.

SAP models the decision as a PATCH on ``ApprovalRequests(WddCode)`` carrying an
``ApprovalRequestDecisions`` entry with the deciding user's own Service Layer
credentials — SAP authenticates that user and records them as the approver, so
the configured SL account MUST be an approver on the relevant approval-template
stage (SAP-side setup). The factory employee who actually clicked is recorded
app-side (``InvoiceApprovalAudit``) and inside the decision's ``Remarks``.
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

    def _approver_credentials(self) -> tuple[str, str]:
        """The SAP user that signs the decision. SAP authenticates this user and
        checks it may decide the request; the integration account (SL_USER) is
        usually not an approver, so a configured approver account is preferred.
        Falls back to the Service Layer session user when none is configured."""
        user = self.sl_config.get("approval_username") or self.sl_config["username"]
        password = self.sl_config.get("approval_password") or self.sl_config["password"]
        return user, password

    def decide(self, wdd_code: int, approve: bool, remarks: str = "") -> dict:
        """Record a decision on approval request ``wdd_code``.

        Pre-checks that the request is still pending so a stale page gets a
        clean validation error instead of a raw SAP one.
        """
        approver_user, approver_password = self._approver_credentials()
        # Log the Service Layer session in AS the approver, so both the session
        # and the decision line carry the same authenticated approver — the shape
        # SAP accepts most reliably.
        session_config = dict(
            self.sl_config, username=approver_user, password=approver_password
        )
        cookies = self._get_session_cookies(session_config)
        current = self._get_request(wdd_code, cookies)

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
            logger.info("Approval request %s %s in SAP", wdd_code, action)
            return {"message": f"Invoice {action} in SAP."}

        error_msg = self._extract_error_message(response)
        if response.status_code == 400:
            logger.error("SAP rejected the decision on %s: %s", wdd_code, error_msg)
            raise SAPValidationError(error_msg)
        if response.status_code in (401, 403):
            logger.error("SAP auth error deciding approval request %s", wdd_code)
            raise SAPConnectionError("SAP authentication failed")
        logger.error("SAP error deciding approval request %s: %s", wdd_code, error_msg)
        raise SAPDataError(f"Failed to record the decision in SAP: {error_msg}")

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _get_request(self, wdd_code: int, cookies) -> dict:
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
            raise SAPConnectionError("SAP authentication failed")
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
            logger.error("SAP Service Layer authentication failed: %s", e)
            raise SAPConnectionError("SAP Service Layer authentication failed")

    @staticmethod
    def _extract_error_message(response) -> str:
        try:
            error_data = response.json()
            if "error" in error_data:
                message = error_data["error"].get("message")
                if isinstance(message, dict):
                    return message.get("value", str(error_data))
                return str(message or error_data)
            return str(error_data)
        except Exception:
            return response.text or f"HTTP {response.status_code}"
