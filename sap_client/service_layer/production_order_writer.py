import logging
import requests
from decimal import Decimal

from ..exceptions import SAPConnectionError, SAPDataError, SAPValidationError
from .auth import ServiceLayerSession

logger = logging.getLogger(__name__)


def _convert_decimals(obj):
    if isinstance(obj, Decimal):
        return float(obj)
    elif isinstance(obj, dict):
        return {k: _convert_decimals(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_convert_decimals(item) for item in obj]
    return obj


class ProductionOrderWriter:
    """
    Creates Production Orders in SAP B1 Service Layer.
    SAP endpoint: POST /b1s/v2/ProductionOrders
    """

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
        """
        Create a Production Order in SAP B1.

        Expected payload keys:
            ItemNo                (str)   Finished product ItemCode
            PlannedQuantity       (float) Total planned quantity
            DueDate               (str)   YYYY-MM-DD
            StartDate             (str)   YYYY-MM-DD (optional)
            Warehouse             (str)   Production warehouse code (optional)
            Remarks               (str)   Remarks (optional)
            ProductionOrderStatus (str)   'boposPlanned' | 'boposReleased' (optional)
            ProductionOrderLines (list) BOM component lines (optional):
                ItemNo          (str)
                PlannedQuantity (float)
                Warehouse       (str, optional)

        Returns:
            dict with DocEntry, DocNum, Status from SAP response.
        """
        cookies = self._get_session_cookies()
        url = f"{self.sl_config['base_url']}/b1s/v2/ProductionOrders"
        payload = _convert_decimals(payload)

        try:
            response = requests.post(
                url,
                json=payload,
                cookies=cookies,
                headers={"Content-Type": "application/json"},
                timeout=30,
                verify=False
            )

            if response.status_code == 201:
                data = response.json()
                # SAP B1 Service Layer uses AbsoluteEntry / DocumentNumber
                doc_entry = data.get('AbsoluteEntry')
                doc_num   = data.get('DocumentNumber')
                # Normalise to DocEntry / DocNum for consistent consumption
                data['DocEntry'] = doc_entry
                data['DocNum']   = doc_num
                logger.info(
                    f"Production order created in SAP: DocNum={doc_num}, "
                    f"DocEntry={doc_entry}"
                )
                return data

            if response.status_code == 400:
                error_msg = self._extract_error_message(response)
                logger.error(f"SAP validation error creating production order: {error_msg}")
                raise SAPValidationError(error_msg)

            if response.status_code in (401, 403):
                logger.error("SAP authentication/authorization error")
                raise SAPConnectionError("SAP authentication failed")

            error_msg = self._extract_error_message(response)
            logger.error(f"SAP error creating production order: {error_msg}")
            raise SAPDataError(f"Failed to create production order: {error_msg}")

        except requests.exceptions.ConnectionError as e:
            logger.error(f"Connection error creating production order: {e}")
            raise SAPConnectionError("Unable to connect to SAP Service Layer")
        except requests.exceptions.Timeout as e:
            logger.error(f"Timeout creating production order: {e}")
            raise SAPConnectionError("SAP Service Layer request timeout")
        except (SAPConnectionError, SAPDataError, SAPValidationError):
            raise
        except Exception as e:
            logger.error(f"Unexpected error creating production order: {e}")
            raise SAPDataError(f"Unexpected error: {str(e)}")

    def _extract_error_message(self, response) -> str:
        try:
            error_data = response.json()
            if "error" in error_data:
                return error_data["error"].get("message", {}).get("value", str(error_data))
            return str(error_data)
        except Exception:
            return response.text or f"HTTP {response.status_code}"
