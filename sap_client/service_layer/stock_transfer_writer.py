"""
sap_client/service_layer/stock_transfer_writer.py

Writers for the two SAP documents the production-movement wrapper needs to move
packaging material between warehouses:

  - InventoryTransferRequestWriter -> POST /b1s/v2/InventoryTransferRequests
  - StockTransferWriter            -> POST /b1s/v2/StockTransfers  (OWTR/WTR1)

Why both: SAP's SBO_SP_TransactionNotification (error 67081, both companies)
REJECTS a direct StockTransfer INTO BH-PC from BH-PM/BH-LO unless it is based on
an Inventory Transfer Request (line BaseType 1250000001). So a BH-PM -> BH-PC
move is a two-step post: create the ITR, then create the StockTransfer that
bases its lines on it. BH-BS -> BH-PC is exempt and can be a one-step transfer.

Both follow the same Service-Layer conventions as GRPOWriter: login for
session cookies, POST, 201 = success, 400 -> SAPValidationError (SAP's verbatim
message, e.g. "(67081) …make Inventory Transfer Request…").
"""

import logging
from decimal import Decimal

import requests

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
    """Shared POST-a-document plumbing for Service Layer writers."""

    #: Service Layer collection, e.g. "StockTransfers". Set by subclasses.
    ENTITY = ""
    #: Human label for logs/errors.
    LABEL = "document"

    def __init__(self, context):
        self.context = context
        self.sl_config = context.service_layer

    def _get_session_cookies(self):
        try:
            return ServiceLayerSession(self.sl_config).login()
        except requests.exceptions.ConnectionError as e:
            logger.error("Failed to connect to SAP Service Layer: %s", e)
            raise SAPConnectionError("Unable to connect to SAP Service Layer")
        except requests.exceptions.Timeout as e:
            logger.error("SAP Service Layer connection timeout: %s", e)
            raise SAPConnectionError("SAP Service Layer connection timeout")
        except requests.exceptions.HTTPError as e:
            logger.error("SAP Service Layer authentication failed: %s", e)
            raise SAPConnectionError("SAP Service Layer authentication failed")

    def create(self, payload: dict) -> dict:
        cookies = self._get_session_cookies()
        url = f"{self.sl_config['base_url']}/b1s/v2/{self.ENTITY}"
        payload = _convert_decimals(payload)
        headers = {"Content-Type": "application/json"}

        try:
            response = requests.post(
                url, json=payload, cookies=cookies, headers=headers,
                timeout=30, verify=False,
            )

            if response.status_code == 201:
                data = response.json()
                logger.info("%s created: DocEntry=%s DocNum=%s",
                            self.LABEL, data.get("DocEntry"), data.get("DocNum"))
                return data

            if response.status_code == 400:
                msg = self._extract_error_message(response)
                logger.error("SAP validation error creating %s: %s", self.LABEL, msg)
                raise SAPValidationError(msg)

            if response.status_code in (401, 403):
                logger.error("SAP auth error creating %s", self.LABEL)
                raise SAPConnectionError("SAP authentication failed")

            msg = self._extract_error_message(response)
            logger.error("SAP error creating %s: %s", self.LABEL, msg)
            raise SAPDataError(f"Failed to create {self.LABEL}: {msg}")

        except requests.exceptions.ConnectionError as e:
            logger.error("Connection error creating %s: %s", self.LABEL, e)
            raise SAPConnectionError("Unable to connect to SAP Service Layer")
        except requests.exceptions.Timeout as e:
            logger.error("Timeout creating %s: %s", self.LABEL, e)
            raise SAPConnectionError("SAP Service Layer request timeout")
        except (SAPConnectionError, SAPDataError, SAPValidationError):
            raise
        except Exception as e:  # noqa: BLE001
            logger.error("Unexpected error creating %s: %s", self.LABEL, e)
            raise SAPDataError(f"Unexpected error: {e}")

    def _extract_error_message(self, response) -> str:
        try:
            data = response.json()
            if "error" in data:
                return data["error"].get("message", {}).get("value", str(data))
            return str(data)
        except Exception:  # noqa: BLE001
            return response.text or f"HTTP {response.status_code}"


class InventoryTransferRequestWriter(_ServiceLayerDocWriter):
    """
    POST /b1s/v2/InventoryTransferRequests.

    payload:
        FromWarehouse, ToWarehouse, DocDate (optional),
        StockTransferLines: [{ItemCode, Quantity, FromWarehouseCode, WarehouseCode}]
    Returns the created ITR (has DocEntry used as the StockTransfer base).
    """

    ENTITY = "InventoryTransferRequests"
    LABEL = "Inventory Transfer Request"


class StockTransferWriter(_ServiceLayerDocWriter):
    """
    POST /b1s/v2/StockTransfers  (OWTR).

    payload:
        FromWarehouse, ToWarehouse, DocDate (optional),
        StockTransferLines: [{
            ItemCode, Quantity, FromWarehouseCode, WarehouseCode,
            # to base on an ITR (required for transfers INTO BH-PC from BH-PM):
            BaseType: 1250000001, BaseEntry: <ITR DocEntry>, BaseLine: <n>
        }]
    """

    ENTITY = "StockTransfers"
    LABEL = "Stock Transfer"

    #: SAP object type for the Inventory Transfer Request UDO (line BaseType).
    ITR_BASE_TYPE = 1250000001
