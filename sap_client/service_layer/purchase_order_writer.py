"""Creates Purchase Orders in SAP B1 via the Service Layer.

SAP endpoint: ``POST /b1s/v1/PurchaseOrders`` (`OPOR` header, `POR1` lines).

This is the platform's first *outbound purchasing* write. A purchase order is a
commitment to a supplier, so two things are non-negotiable and are enforced by
the callers rather than here: the order must be approved by someone other than
its author, and it must be impossible to post twice. This class does one thing —
turn a validated payload into a SAP document — and reports precisely what SAP
said when it refuses.
"""

import logging

import requests

from ..exceptions import SAPConnectionError, SAPDataError, SAPValidationError
from .auth import ServiceLayerSession

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT_SECONDS = 45


def _to_float(value):
    """Decimals do not survive JSON serialisation; floats do."""
    if isinstance(value, dict):
        return {k: _to_float(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_float(v) for v in value]
    if hasattr(value, "quantize"):  # Decimal
        return float(value)
    return value


class PurchaseOrderWriter:
    """Posts a purchase order to SAP B1 for one company."""

    ENDPOINT = "/b1s/v1/PurchaseOrders"

    def __init__(self, context):
        self.context = context
        self.sl_config = context.service_layer

    def _login(self):
        try:
            return ServiceLayerSession(self.sl_config).login()
        except requests.exceptions.ConnectionError as e:
            logger.error("SAP Service Layer unreachable: %s", e)
            raise SAPConnectionError("Unable to connect to SAP Service Layer") from e
        except requests.exceptions.Timeout as e:
            logger.error("SAP Service Layer login timed out: %s", e)
            raise SAPConnectionError("SAP Service Layer connection timeout") from e
        except requests.exceptions.HTTPError as e:
            logger.error("SAP Service Layer authentication failed: %s", e)
            raise SAPConnectionError("SAP Service Layer authentication failed") from e

    def create(self, payload: dict) -> dict:
        """Create a purchase order and return the SAP identifiers.

        Expected payload:
            CardCode                 (str)   vendor code
            DocDate / DocDueDate     (str)   YYYY-MM-DD
            Comments                 (str)   optional
            BPL_IDAssignedToInvoice  (int)   optional branch, for multi-branch companies
            DocumentLines            (list)
                ItemCode         (str)
                Quantity         (float)
                Price            (float, optional)
                WarehouseCode    (str, optional)
                ShipDate         (str, optional) per-line required date

        Returns ``{"DocEntry": int, "DocNum": int, "raw": <SAP response>}``.
        """
        self._validate(payload)

        cookies = self._login()
        url = f"{self.sl_config['base_url']}{self.ENDPOINT}"
        body = _to_float(payload)

        try:
            response = requests.post(
                url,
                json=body,
                cookies=cookies,
                headers={"Content-Type": "application/json"},
                timeout=REQUEST_TIMEOUT_SECONDS,
                verify=False,
            )
        except requests.exceptions.Timeout as e:
            # A timeout is the dangerous case: SAP may have created the document
            # anyway. Surfaced as a connection error so the caller leaves the
            # order un-posted and a human reconciles, rather than auto-retrying
            # into a duplicate commitment.
            logger.error("Timed out posting purchase order to SAP: %s", e)
            raise SAPConnectionError(
                "SAP did not answer in time. The purchase order may or may not "
                "have been created — check SAP before retrying."
            ) from e
        except requests.exceptions.RequestException as e:
            logger.error("Failed to post purchase order to SAP: %s", e)
            raise SAPConnectionError("Unable to reach SAP Service Layer") from e

        if response.status_code == 201:
            data = response.json()
            doc_entry = data.get("DocEntry")
            doc_num = data.get("DocNum")
            logger.info(
                "Purchase order created in SAP: DocNum=%s DocEntry=%s vendor=%s",
                doc_num, doc_entry, payload.get("CardCode"),
            )
            return {"DocEntry": doc_entry, "DocNum": doc_num, "raw": data}

        message = self._error_message(response)
        logger.error(
            "SAP rejected purchase order (%s): %s", response.status_code, message
        )
        if response.status_code in (400, 401, 403):
            raise SAPValidationError(f"SAP rejected the purchase order: {message}")
        raise SAPDataError(f"SAP purchase order post failed: {message}")

    @staticmethod
    def _validate(payload: dict) -> None:
        if not payload.get("CardCode"):
            raise SAPValidationError("A vendor (CardCode) is required.")
        lines = payload.get("DocumentLines") or []
        if not lines:
            raise SAPValidationError("A purchase order needs at least one line.")
        for index, line in enumerate(lines):
            if not line.get("ItemCode"):
                raise SAPValidationError(f"Line {index + 1} has no item code.")
            if not line.get("Quantity") or float(line["Quantity"]) <= 0:
                raise SAPValidationError(
                    f"Line {index + 1} ({line.get('ItemCode')}) needs a quantity above zero."
                )

    @staticmethod
    def _error_message(response) -> str:
        """Pull SAP's own message out of its error envelope.

        SAP nests it as ``{"error": {"message": {"value": "..."}}}``, and when it
        does not the raw text is more use than a generic string — a buyer chasing
        a rejected order needs the actual reason.
        """
        try:
            error = response.json().get("error", {})
            message = error.get("message")
            if isinstance(message, dict):
                return message.get("value") or str(message)
            return str(message or error) or response.text[:500]
        except ValueError:
            return response.text[:500]
