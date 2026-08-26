"""Shared Service Layer plumbing for the transfer document family.

`StockTransfers` (object 67) and `InventoryTransferRequests` (1250000001) share
a payload shape, an error shape and the same Cancel/Close actions, so the
mechanics live here and each writer only declares its entity set.

Two behaviours were established by posting into `TEST_JIVO_OIL_HANADB` rather
than read from documentation:

* **Approval procedures do not apply.** They are enforced by the SAP Business
  One client, not the Service Layer. A post on a route covered by two active
  approval templates still produced a real `OWTR` row with `WddStatus='-'` and
  no draft — so a 201 here means stock moved, and there is no draft to chase.
* **Cancel creates a reversing document.** It returns 204, flags the original
  `CANCELED='Y'`, and posts a second document to undo it — which consumes a
  further number from the month's series.
"""

import logging
from decimal import Decimal
from typing import Optional

import requests

from .auth import ServiceLayerSession
from ..exceptions import SAPConnectionError, SAPDataError, SAPValidationError

logger = logging.getLogger(__name__)

# A transfer runs SAP's notification procedure, which at JIVO carries dozens of
# validations, so it can outlast a short timeout. A timeout does NOT roll back
# the SAP-side commit, so callers must read back before retrying rather than
# assume nothing happened.
POST_TIMEOUT_SECONDS = 180
ACTION_TIMEOUT_SECONDS = 120


def json_safe(obj):
    """Make Decimals JSON-serialisable without losing the value to repr."""
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, dict):
        return {k: json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [json_safe(v) for v in obj]
    return obj


class TransferDocumentWriter:
    """Base for the Service Layer writers that post transfer documents."""

    #: OData entity set, e.g. "StockTransfers"
    entity_set: str = ""
    #: Human name used in log lines and error messages
    label: str = "document"

    def __init__(self, context):
        self.context = context
        self.sl_config = context.service_layer

    # ------------------------------------------------------------------
    # session
    # ------------------------------------------------------------------

    def _login(self):
        try:
            return ServiceLayerSession(self.sl_config).login()
        except requests.exceptions.ConnectionError as e:
            logger.error("SAP Service Layer unreachable: %s", e)
            raise SAPConnectionError("Unable to connect to SAP Service Layer.") from e
        except requests.exceptions.Timeout as e:
            logger.error("SAP Service Layer login timed out: %s", e)
            raise SAPConnectionError("SAP Service Layer login timed out.") from e
        except requests.exceptions.HTTPError as e:
            logger.error("SAP Service Layer login rejected: %s", e)
            raise SAPConnectionError(
                "SAP Service Layer login failed — check SL_USER and the "
                "configured company database."
            ) from e

    def _url(self, suffix: str = "") -> str:
        return f"{self.sl_config['base_url']}/b1s/v2/{self.entity_set}{suffix}"

    # ------------------------------------------------------------------
    # operations
    # ------------------------------------------------------------------

    def create(self, payload: dict) -> dict:
        """POST the document. Returns SAP's created document on success."""
        cookies = self._login()
        body = json_safe(payload)

        try:
            response = requests.post(
                self._url(),
                json=body,
                cookies=cookies,
                headers={"Content-Type": "application/json"},
                timeout=POST_TIMEOUT_SECONDS,
                verify=False,
            )
        except requests.exceptions.Timeout as e:
            # Deliberately a distinct message: the caller must read back before
            # retrying, because SAP may well have committed the document.
            logger.error("Timed out posting %s to SAP: %s", self.label, e)
            raise SAPConnectionError(
                f"SAP did not answer within {POST_TIMEOUT_SECONDS}s while posting "
                f"the {self.label}. The document may still have been created — "
                f"check SAP before posting again."
            ) from e
        except requests.exceptions.ConnectionError as e:
            logger.error("Connection error posting %s to SAP: %s", self.label, e)
            raise SAPConnectionError("Unable to connect to SAP Service Layer.") from e

        if response.status_code == 201:
            created = response.json()
            logger.info(
                "SAP %s created: DocEntry=%s DocNum=%s",
                self.label, created.get("DocEntry"), created.get("DocNum"),
            )
            return created

        message = self._error_message(response)

        if response.status_code == 400:
            logger.error("SAP rejected %s: %s", self.label, message)
            raise SAPValidationError(message)
        if response.status_code in (401, 403):
            logger.error("SAP authorisation failure posting %s: %s", self.label, message)
            raise SAPConnectionError("SAP authentication failed.")

        logger.error("SAP error posting %s: %s", self.label, message)
        raise SAPDataError(f"Failed to create the {self.label} in SAP: {message}")

    def cancel(self, doc_entry: int) -> None:
        """Cancel a posted document.

        SAP answers 204 and writes a *reversing* document — the original is
        flagged `CANCELED='Y'` and a second document undoes the movement. Note
        `CancelDate` stays null, so downstream reads must key off `CANCELED`.
        """
        self._action(doc_entry, "Cancel")

    def close(self, doc_entry: int) -> None:
        """Close a document without fulfilling it.

        For a request this is the honest end state when the remainder will never
        be served; `cancel` is for a request that should not have existed.
        """
        self._action(doc_entry, "Close")

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _action(self, doc_entry: int, action: str) -> None:
        cookies = self._login()
        try:
            response = requests.post(
                self._url(f"({int(doc_entry)})/{action}"),
                cookies=cookies,
                timeout=ACTION_TIMEOUT_SECONDS,
                verify=False,
            )
        except requests.exceptions.Timeout as e:
            raise SAPConnectionError(
                f"SAP did not answer while running {action} on {self.label} "
                f"{doc_entry}. Check SAP before retrying."
            ) from e
        except requests.exceptions.ConnectionError as e:
            raise SAPConnectionError("Unable to connect to SAP Service Layer.") from e

        if response.status_code in (200, 204):
            logger.info("SAP %s %s on DocEntry=%s", self.label, action, doc_entry)
            return

        message = self._error_message(response)
        logger.error(
            "SAP %s failed on %s %s: %s", action, self.label, doc_entry, message
        )
        if response.status_code == 400:
            raise SAPValidationError(message)
        raise SAPDataError(f"Failed to {action.lower()} the {self.label}: {message}")

    @staticmethod
    def _error_message(response) -> str:
        """Pull a readable message out of either Service Layer error shape.

        v2 returns `{"error": {"message": "..."}}` while older payloads nest it
        as `{"error": {"message": {"value": "..."}}}`.
        """
        try:
            payload = response.json()
        except Exception:
            return response.text or f"HTTP {response.status_code}"

        error = payload.get("error") if isinstance(payload, dict) else None
        if not isinstance(error, dict):
            return str(payload)

        message = error.get("message")
        if isinstance(message, dict):
            message = message.get("value")
        if not message:
            message = str(error)

        code = error.get("code")
        return f"{message} (SAP {code})" if code else str(message)
