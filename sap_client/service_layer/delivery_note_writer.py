"""Outbound Delivery Note + Goods Issue writers for the SAP Service Layer.

Used by the marketplace-dispatch confirm flow:
  * ``DeliveryNoteWriter``  → POST /b1s/v2/DeliveryNotes   (customer delivery; auto-decrements FG stock)
  * ``GoodsIssueWriter``    → POST /b1s/v2/InventoryGenExits (packing-material consumption)

Modelled on ``grpo_writer.GRPOWriter`` (same session/error handling).
"""
import logging
import requests
from decimal import Decimal

from ..exceptions import SAPConnectionError, SAPDataError, SAPValidationError
from .auth import ServiceLayerSession

logger = logging.getLogger(__name__)


def _convert_decimals(obj):
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, dict):
        return {k: _convert_decimals(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_convert_decimals(item) for item in obj]
    return obj


class _ServiceLayerDocWriter:
    """Shared POST-a-document helper for Service Layer object endpoints."""

    #: Service Layer object collection, e.g. "DeliveryNotes"
    endpoint = ""
    label = "document"

    def __init__(self, context):
        self.context = context
        self.sl_config = context.service_layer

    def _get_session_cookies(self):
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
        cookies = self._get_session_cookies()
        url = f"{self.sl_config['base_url']}/b1s/v2/{self.endpoint}"
        payload = _convert_decimals(payload)
        headers = {"Content-Type": "application/json"}

        try:
            response = requests.post(
                url, json=payload, cookies=cookies, headers=headers,
                timeout=30, verify=False,
            )
            if response.status_code == 201:
                data = response.json()
                logger.info(f"{self.label} created: DocNum={data.get('DocNum')}")
                return data
            if response.status_code == 400:
                error_msg = self._extract_error_message(response)
                logger.error(f"SAP validation error creating {self.label}: {error_msg}")
                raise SAPValidationError(error_msg)
            if response.status_code in (401, 403):
                logger.error("SAP authentication/authorization error")
                raise SAPConnectionError("SAP authentication failed")
            error_msg = self._extract_error_message(response)
            logger.error(f"SAP error creating {self.label}: {error_msg}")
            raise SAPDataError(f"Failed to create {self.label}: {error_msg}")
        except requests.exceptions.ConnectionError as e:
            logger.error(f"Connection error while creating {self.label}: {e}")
            raise SAPConnectionError("Unable to connect to SAP Service Layer")
        except requests.exceptions.Timeout as e:
            logger.error(f"Timeout while creating {self.label}: {e}")
            raise SAPConnectionError("SAP Service Layer request timeout")
        except (SAPConnectionError, SAPDataError, SAPValidationError):
            raise
        except Exception as e:
            logger.error(f"Unexpected error creating {self.label}: {e}")
            raise SAPDataError(f"Unexpected error: {str(e)}")

    def _extract_error_message(self, response) -> str:
        try:
            error_data = response.json()
            if "error" in error_data:
                return error_data["error"].get("message", {}).get("value", str(error_data))
            return str(error_data)
        except Exception:
            return response.text or f"HTTP {response.status_code}"


class DeliveryNoteWriter(_ServiceLayerDocWriter):
    """POST /b1s/v2/DeliveryNotes — outbound customer delivery (decrements FG stock).

    Payload:
        {"CardCode": str, "DocDate": "YYYY-MM-DD",
         "DocumentLines": [{"ItemCode": str, "Quantity": float, "WarehouseCode": str, ...}]}
    """

    endpoint = "DeliveryNotes"
    label = "Delivery Note"


class GoodsIssueWriter(_ServiceLayerDocWriter):
    """POST /b1s/v2/InventoryGenExits — goods issue (consumes packing materials)."""

    endpoint = "InventoryGenExits"
    label = "Goods Issue"
