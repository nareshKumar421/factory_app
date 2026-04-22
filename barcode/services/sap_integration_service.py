import logging

logger = logging.getLogger(__name__)


class BarcodeSAPIntegration:
    """
    Handles SAP stock transfer calls triggered by barcode operations.
    Deferred: actual SAP calls via sap_client will be wired when SAP network is available.
    For now, this service validates and logs the intent.
    """

    def __init__(self, company_code: str):
        self.company_code = company_code

    def create_stock_transfer(self, from_warehouse: str, to_warehouse: str,
                              items: list[dict]) -> dict | None:
        """
        Create a stock transfer in SAP.
        items: [{'item_code': str, 'batch_number': str, 'quantity': Decimal, 'uom': str}]

        Returns SAP doc entry dict on success, None if SAP is unavailable.
        Raises ValueError on validation failure.
        """
        if from_warehouse == to_warehouse:
            raise ValueError("Source and destination warehouse are the same.")
        if not items:
            raise ValueError("No items to transfer.")

        # Validate items
        for item in items:
            if not item.get('item_code'):
                raise ValueError("Item code is required for stock transfer.")
            if not item.get('quantity') or float(item['quantity']) <= 0:
                raise ValueError(f"Invalid quantity for {item.get('item_code')}.")

        # TODO: Wire to sap_client when SAP network is available
        # from sap_client.client import SAPClient
        # from sap_client.registry import get_company_config
        # config = get_company_config(self.company_code)
        # client = SAPClient(config)
        # payload = {
        #     "DocDate": timezone.now().strftime('%Y-%m-%d'),
        #     "StockTransferLines": [
        #         {
        #             "ItemCode": item['item_code'],
        #             "Quantity": float(item['quantity']),
        #             "FromWarehouseCode": from_warehouse,
        #             "WarehouseCode": to_warehouse,
        #         }
        #         for item in items
        #     ]
        # }
        # result = client.create_stock_transfer(payload)
        # return result

        logger.info(
            f"SAP stock transfer prepared (not posted): "
            f"{from_warehouse} → {to_warehouse}, {len(items)} items. "
            f"Waiting for SAP integration to be wired."
        )

        # Return None to indicate SAP posting is deferred
        return None
