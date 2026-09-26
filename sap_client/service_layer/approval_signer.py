"""Prove the signer of an approval decision before anything else is written.

SAP Portal changed a credit note's lines and then approved it; to stop a
mistyped password leaving a changed-but-unapproved draft behind, it first made
one read AS the approver (``routes/creditNotes.js``: "Prove the SAP password
before touching the draft"). This is that step: log in as the account the
decision will be signed with and confirm the request is still pending, raising
exactly what :meth:`ApprovalRequestWriter.decide` would raise for the same
fault — so a flow that must write something first can fail before it does.

A subclass rather than a change to :mod:`.approval_writer`, so the existing
queues' writer is untouched.
"""

from ..exceptions import SAPValidationError
from .approval_writer import _REQUEST_STATUS_LABELS, REQUEST_PENDING, ApprovalRequestWriter


class ApprovalSignerCheck(ApprovalRequestWriter):
    """Log in as the signer and read the request — nothing is changed."""

    def verify(self, wdd_code: int, approver: str, password: str | None = None) -> str:
        """The SAP user that would sign, once SAP has accepted its login.

        ``password`` works as in :meth:`ApprovalRequestWriter.decide`: typed
        and used for this one login only, or omitted for the stored one.
        """
        user, user_password = self._approver_credentials(approver, password)
        session_config = dict(self.sl_config, username=user, password=user_password)
        cookies = self._get_session_cookies(session_config)
        current = self._get_request(wdd_code, cookies, user)
        status = current.get("Status")
        if status != REQUEST_PENDING:
            label = _REQUEST_STATUS_LABELS.get(status, f"in state {status}")
            raise SAPValidationError(
                f"Approval request {wdd_code} is {label}; it can no longer be decided."
            )
        return user
