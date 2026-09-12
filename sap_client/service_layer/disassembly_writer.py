"""The three SAP writes a dismantle is made of.

A disassembly in SAP is not one document but three, and their ORDER IS FIXED by
``SBO_SP_TransactionNotification`` -- all three companies enforce it:

1. ``POST /b1s/v2/ProductionOrders`` with ``ProductionOrderType =
   'bopotDisassembly'`` -- the order itself (`OWOR."Type" = 'D'`).
2. ``POST /b1s/v2/InventoryGenEntries`` -- Receipt from Production, which brings
   the components back. Every line carries ``BaseType 202`` / ``BaseEntry`` =
   the order / ``BaseLine`` = the order's own line number.
3. ``POST /b1s/v2/InventoryGenExits`` -- Goods Issue, which consumes the parent.
   One line, ``BaseType 202`` / ``BaseEntry`` = the order, and **no BaseLine**:
   the parent is the order's header, not one of its component lines.

**The receipt must come before the issue.** Reversing them is refused with
``20206 Cannot add Goods Issue: no Goods Receipt posted for Disassembly Order N``.
(The mirror rule, ``20205``, applies to normal production orders -- there the
issue comes first -- which is why this writer is separate from anything that
posts a production receipt.)

Two more rules from the same procedure shape the payloads:

* ``60003 Please select Variety`` -- every Goods Issue line needs ``CostingCode``
  (Dimension 1). Unconditional; it is not waived for a document based on an order.
* ``590001 Duplicate Batch not Allowed`` -- a batch number received from a
  production order must not already exist anywhere in the company.

What does NOT apply is just as load-bearing: ``590005``/``60002`` ("Not Allowed
to Goods Receipt/Issue Manualy") and the GL-5100013 rules fire only on lines with
``BaseType != 202``. A disassembly's documents are all based on the order, so the
Service Layer user may post them where it could not post a free-hand goods
movement.
"""

import logging

import requests

from ..exceptions import SAPConnectionError, SAPDataError, SAPValidationError
from .delivery_note_writer import _ServiceLayerDocWriter
from .auth import ServiceLayerSession

logger = logging.getLogger(__name__)

#: SAP's object type for a production order -- what a completion document's lines
#: point at through BaseType.
PRODUCTION_ORDER_OBJECT_TYPE = 202


class ProductionReceiptWriter(_ServiceLayerDocWriter):
    """POST /b1s/v2/InventoryGenEntries -- Receipt from Production.

    On a disassembly order this receives the COMPONENTS (the parent is issued by
    ``ProductionIssueWriter``), which is the reverse of what the same document
    does for a normal production order.
    """

    endpoint = "InventoryGenEntries"
    label = "Receipt from Production"


class ProductionIssueWriter(_ServiceLayerDocWriter):
    """POST /b1s/v2/InventoryGenExits -- Issue for Production.

    On a disassembly order this consumes the PARENT finished good. Kept apart
    from ``GoodsIssueWriter`` (same endpoint, packing-material consumption) only
    for the label: a failure on this one has to read as a dismantle failure.
    """

    endpoint = "InventoryGenExits"
    label = "Issue for Production"


class DisassemblyOrderWriter:
    """Create and close the disassembly production order itself.

    ``ProductionOrderWriter`` already posts production orders; this adds the two
    things a dismantle needs beyond it -- the disassembly type, and closing the
    order once both completion documents are in, which is what leaves it at the
    ``Status = 'L'`` every finished dismantle in SAP carries.
    """

    #: ProductionOrders.ProductionOrderType for a disassembly order.
    DISASSEMBLY_TYPE = "bopotDisassembly"
    #: ProductionOrders.ProductionOrderStatus values used here.
    STATUS_RELEASED = "boposReleased"
    STATUS_CLOSED = "boposClosed"

    def __init__(self, context):
        self.context = context
        self.sl_config = context.service_layer

    def create(self, payload: dict) -> dict:
        """Create the order, forcing the disassembly type.

        The type is set here rather than left to the caller because a payload
        that reaches SAP without it is accepted as a STANDARD production order --
        which consumes the components and produces the finished good, the exact
        opposite of a dismantle, and is not something a later step would notice.
        """
        from .production_order_writer import ProductionOrderWriter

        payload = dict(payload)
        payload["ProductionOrderType"] = self.DISASSEMBLY_TYPE
        payload.setdefault("ProductionOrderStatus", self.STATUS_RELEASED)
        return ProductionOrderWriter(self.context).create(payload)

    def close(self, doc_entry: int) -> None:
        """PATCH the order to Closed once both completion documents are posted.

        A dismantle whose documents are in is finished whether or not this
        succeeds -- the stock has already moved -- so callers treat a failure
        here as a warning, not as a failed dismantle.
        """
        cookies = self._get_session_cookies()
        url = f"{self.sl_config['base_url']}/b1s/v2/ProductionOrders({int(doc_entry)})"
        try:
            response = requests.patch(
                url,
                json={"ProductionOrderStatus": self.STATUS_CLOSED},
                cookies=cookies,
                headers={"Content-Type": "application/json"},
                timeout=30,
                verify=False,
            )
        except requests.exceptions.ConnectionError as e:
            logger.error("Connection error closing production order %s: %s", doc_entry, e)
            raise SAPConnectionError("Unable to connect to SAP Service Layer")
        except requests.exceptions.Timeout as e:
            logger.error("Timeout closing production order %s: %s", doc_entry, e)
            raise SAPConnectionError("SAP Service Layer request timeout")

        if response.status_code in (200, 204):
            logger.info("Disassembly order %s closed", doc_entry)
            return
        error_msg = self._extract_error_message(response)
        if response.status_code == 400:
            raise SAPValidationError(error_msg)
        if response.status_code in (401, 403):
            raise SAPConnectionError("SAP authentication failed")
        raise SAPDataError(f"Failed to close production order {doc_entry}: {error_msg}")

    def get_lines(self, doc_entry: int) -> list[dict]:
        """The order's component lines as SAP stored them, with their LineNum.

        Read back rather than assumed: the receipt has to name each component's
        ``BaseLine``, and that is SAP's line number on the order it just created,
        not the position the app sent them in. SAP drops resource (labour) lines
        and can re-order what it keeps, so sending our own index would attach
        quantities to the wrong components.
        """
        cookies = self._get_session_cookies()
        url = (
            f"{self.sl_config['base_url']}/b1s/v2/ProductionOrders({int(doc_entry)})"
            "?$select=ProductionOrderLines"
        )
        try:
            response = requests.get(
                url, cookies=cookies, timeout=30, verify=False,
            )
        except requests.exceptions.ConnectionError as e:
            logger.error("Connection error reading production order %s: %s", doc_entry, e)
            raise SAPConnectionError("Unable to connect to SAP Service Layer")
        except requests.exceptions.Timeout as e:
            logger.error("Timeout reading production order %s: %s", doc_entry, e)
            raise SAPConnectionError("SAP Service Layer request timeout")

        if response.status_code != 200:
            raise SAPDataError(
                f"Failed to read production order {doc_entry}: "
                f"{self._extract_error_message(response)}"
            )
        return response.json().get("ProductionOrderLines") or []

    # ------------------------------------------------------------------
    # internals (mirrors _ServiceLayerDocWriter; this one posts no document)
    # ------------------------------------------------------------------

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

    @staticmethod
    def _extract_error_message(response) -> str:
        try:
            error_data = response.json()
            if "error" in error_data:
                return error_data["error"].get("message", {}).get("value", str(error_data))
            return str(error_data)
        except Exception:
            return response.text or f"HTTP {response.status_code}"
