import logging
import requests
from decimal import Decimal

from ..exceptions import SAPConnectionError, SAPDataError, SAPValidationError
from .auth import ServiceLayerSession

logger = logging.getLogger(__name__)


def _convert_decimals(obj):
    """Recursively convert Decimal objects to float for JSON serialization"""
    if isinstance(obj, Decimal):
        return float(obj)
    elif isinstance(obj, dict):
        return {k: _convert_decimals(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_convert_decimals(item) for item in obj]
    return obj


class ReturnsWriter:
    """A/R Returns (customer goods return) writer for SAP Service Layer.

    Posts a standalone `Returns` document (SAP object ORDN) that moves returned
    finished goods back into a goods-return warehouse. The financial credit note
    (A/R Credit Memo built on this Return) is a separate step.
    """

    def __init__(self, context):
        self.context = context
        self.sl_config = context.service_layer

    def _get_session_cookies(self):
        """Get authenticated session cookies from Service Layer"""
        try:
            session = ServiceLayerSession(self.sl_config)
            return session.login()
        except requests.exceptions.ConnectionError as e:
            logger.error(f"Failed to connect to SAP Service Layer: {e}")
            raise SAPConnectionError("Unable to connect to SAP Service Layer")
        except requests.exceptions.Timeout as e:
            logger.error(f"SAP Service Layer connection timeout: {e}")
            raise SAPConnectionError("SAP Service Layer connection timeout")
        except requests.exceptions.HTTPError as e:
            logger.error(f"SAP Service Layer authentication failed: {e}")
            raise SAPConnectionError("SAP Service Layer authentication failed")

    def create(self, payload: dict) -> dict:
        """
        Create an A/R Returns document in SAP Business One.

        Args:
            payload: dict containing:
                - CardCode: Customer code
                - NumAtCard / Comments: reference to the source invoice (optional)
                - DocumentLines: list of line items with:
                    - ItemCode: Item code
                    - Quantity: Quantity returned
                    - WarehouseCode: goods-return warehouse the stock returns into
                    - UnitPrice: Unit price (optional)
                    - TaxCode: Tax code (optional)

        Returns:
            dict: Created Returns document response from SAP (incl. DocEntry, DocNum)
        """
        cookies = self._get_session_cookies()

        url = f"{self.sl_config['base_url']}/b1s/v2/Returns"

        # Convert Decimal objects to float for JSON serialization
        payload = _convert_decimals(payload)

        headers = {
            "Content-Type": "application/json",
        }

        try:
            response = requests.post(
                url,
                json=payload,
                cookies=cookies,
                headers=headers,
                timeout=30,
                verify=False
            )

            if response.status_code == 201:
                logger.info(f"A/R Return created successfully: {response.json().get('DocNum')}")
                return response.json()

            # Handle SAP error responses
            if response.status_code == 400:
                error_msg = self._extract_error_message(response)
                logger.error(f"SAP validation error: {error_msg}")
                raise SAPValidationError(error_msg)

            if response.status_code in (401, 403):
                logger.error("SAP authentication/authorization error")
                raise SAPConnectionError("SAP authentication failed")

            # Other errors
            error_msg = self._extract_error_message(response)
            logger.error(f"SAP error creating A/R Return: {error_msg}")
            raise SAPDataError(f"Failed to create A/R Return: {error_msg}")

        except requests.exceptions.ConnectionError as e:
            logger.error(f"Connection error while creating A/R Return: {e}")
            raise SAPConnectionError("Unable to connect to SAP Service Layer")
        except requests.exceptions.Timeout as e:
            logger.error(f"Timeout while creating A/R Return: {e}")
            raise SAPConnectionError("SAP Service Layer request timeout")
        except (SAPConnectionError, SAPDataError, SAPValidationError):
            raise
        except Exception as e:
            logger.error(f"Unexpected error creating A/R Return: {e}")
            raise SAPDataError(f"Unexpected error: {str(e)}")

    def _extract_error_message(self, response) -> str:
        """Extract error message from SAP response"""
        try:
            error_data = response.json()
            if "error" in error_data:
                return error_data["error"].get("message", {}).get("value", str(error_data))
            return str(error_data)
        except Exception:
            return response.text or f"HTTP {response.status_code}"
