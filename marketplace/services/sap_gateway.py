"""SAP boundary for the marketplace confirm flow.

Wraps :class:`sap_client.SAPClient`. When ``settings.MARKETPLACE_SIMULATE_SAP`` is
true (defaults to DEBUG), all SAP calls are skipped and synthetic document numbers
are returned so the scan→confirm flow can be demoed/tested without a live SAP.
"""
import logging
from decimal import Decimal

from django.conf import settings

from .errors import MarketplaceError

logger = logging.getLogger(__name__)


class MarketplaceSapGateway:
    def __init__(self, company_code):
        self.company_code = company_code
        self.simulate = bool(getattr(settings, "MARKETPLACE_SIMULATE_SAP", False))
        self._client = None

    @property
    def client(self):
        if self._client is None:
            from sap_client.client import SAPClient
            self._client = SAPClient(self.company_code)
        return self._client

    # ── stock ────────────────────────────────────────────────────────────────
    def verify_stock(self, lines, warehouse_code):
        """Raise MarketplaceError(INSUFFICIENT_STOCK) if any line lacks on-hand.

        Best-effort in real mode (uses the WMS HANA reader if available); a no-op
        in simulate mode.
        """
        if self.simulate:
            return
        try:
            from warehouse.services.wms_hana_reader import WMSHanaReader
        except Exception:  # reader not available in this deployment
            logger.warning("WMSHanaReader unavailable; skipping marketplace stock check")
            return
        try:
            reader = WMSHanaReader(self.company_code)
        except Exception as e:  # pragma: no cover - env specific
            logger.warning("Could not init WMSHanaReader (%s); skipping stock check", e)
            return
        checker = getattr(reader, "get_available_stock", None)
        if not callable(checker):
            logger.warning("WMSHanaReader.get_available_stock missing; skipping stock check")
            return
        short = []
        for line in lines:
            required = Decimal(line["required_quantity"])
            try:
                available = Decimal(str(checker(line["item_code"], warehouse_code)))
            except Exception as e:  # pragma: no cover - env specific
                logger.warning("Stock check failed for %s (%s)", line["item_code"], e)
                continue
            if available < required:
                short.append({
                    "item_code": line["item_code"],
                    "required": str(required),
                    "available": str(available),
                })
        if short:
            raise MarketplaceError(
                "Insufficient stock for one or more items.",
                code="INSUFFICIENT_STOCK", status_code=409, detail=short,
            )

    # ── writes ───────────────────────────────────────────────────────────────
    def create_delivery_note(
        self, *, ref, card_code, warehouse_code, fg_lines, doc_date,
        num_at_card="", comments="",
    ):
        if not fg_lines:
            return {"DocEntry": None, "DocNum": ""}
        if self.simulate:
            return {"DocEntry": 900000 + int(ref), "DocNum": f"SIMDN-{ref}"}
        payload = {
            "CardCode": card_code,
            "DocDate": doc_date.isoformat(),
            # Traceability back to the marketplace order — also the key a future
            # duplicate-guard would query SAP on before re-posting.
            "NumAtCard": num_at_card or "",
            "Comments": comments or f"Marketplace dispatch {ref}",
            "DocumentLines": [
                {
                    "ItemCode": l["item_code"],
                    "Quantity": float(Decimal(l["required_quantity"])),
                    "WarehouseCode": l.get("warehouse_code") or warehouse_code,
                }
                for l in fg_lines
            ],
        }
        data = self.client.create_delivery_note(payload)
        return {"DocEntry": data.get("DocEntry"), "DocNum": str(data.get("DocNum") or "")}

    def create_goods_issue(
        self, *, ref, warehouse_code, pm_lines, doc_date, num_at_card="", comments="",
    ):
        if not pm_lines:
            return {"DocEntry": None, "DocNum": ""}
        if self.simulate:
            return {"DocEntry": 800000 + int(ref), "DocNum": f"SIMGI-{ref}"}
        payload = {
            "DocDate": doc_date.isoformat(),
            "NumAtCard": num_at_card or "",
            "Comments": comments or f"Marketplace dispatch {ref} packing-material consumption",
            "DocumentLines": [
                {
                    "ItemCode": l["item_code"],
                    "Quantity": float(Decimal(l["required_quantity"])),
                    "WarehouseCode": l.get("warehouse_code") or warehouse_code,
                }
                for l in pm_lines
            ],
        }
        data = self.client.create_goods_issue(payload)
        return {"DocEntry": data.get("DocEntry"), "DocNum": str(data.get("DocNum") or "")}
