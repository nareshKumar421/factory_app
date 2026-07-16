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
    @staticmethod
    def _sap_write(fn, payload, *, label):
        """Run a SAP Service-Layer write, turning SAP failures into a
        :class:`MarketplaceError` so the client sees the real reason (SAP's own
        validation message) instead of an opaque HTTP 500.
        """
        from sap_client.exceptions import (
            SAPConnectionError, SAPDataError, SAPValidationError,
        )
        try:
            return fn(payload)
        except SAPValidationError as e:
            raise MarketplaceError(
                f"SAP rejected the {label}: {e}",
                code="SAP_VALIDATION", status_code=422,
            )
        except SAPConnectionError as e:
            raise MarketplaceError(
                f"Couldn't reach SAP to post the {label}: {e}",
                code="SAP_UNAVAILABLE", status_code=502,
            )
        except SAPDataError as e:
            raise MarketplaceError(
                f"SAP error while posting the {label}: {e}",
                code="SAP_ERROR", status_code=502,
            )

    @staticmethod
    def _series(series):
        """SAP expects a numeric Series id; ignore blank/non-numeric master values."""
        series = (series or "").strip()
        return int(series) if series.isdigit() else None

    @staticmethod
    def _branch(branch_id):
        """SAP expects a numeric Business Place id; ignore blank/non-numeric."""
        if branch_id in (None, ""):
            return None
        try:
            return int(branch_id)
        except (TypeError, ValueError):
            return None

    def create_delivery_note(
        self, *, ref, card_code, warehouse_code, fg_lines, doc_date,
        num_at_card="", comments="", series="", tax_code="", branch_id=None,
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
                self._line(l, warehouse_code, tax_code) for l in fg_lines
            ],
        }
        sid = self._series(series)
        if sid is not None:
            payload["Series"] = sid
        bpl = self._branch(branch_id)
        if bpl is not None:
            payload["BPLId"] = bpl  # SAP GST branch (ODRF.BPLId); required by localization
        data = self._sap_write(self.client.create_delivery_note, payload, label="delivery note")
        return {"DocEntry": data.get("DocEntry"), "DocNum": str(data.get("DocNum") or "")}

    def create_goods_issue(
        self, *, ref, warehouse_code, pm_lines, doc_date, num_at_card="", comments="", series="",
        branch_id=None,
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
                self._line(l, warehouse_code, "") for l in pm_lines
            ],
        }
        sid = self._series(series)
        if sid is not None:
            payload["Series"] = sid
        bpl = self._branch(branch_id)
        if bpl is not None:
            payload["BPLId"] = bpl
        data = self._sap_write(self.client.create_goods_issue, payload, label="goods issue")
        return {"DocEntry": data.get("DocEntry"), "DocNum": str(data.get("DocNum") or "")}

    @staticmethod
    def _line(line, warehouse_code, tax_code):
        row = {
            "ItemCode": line["item_code"],
            "Quantity": float(Decimal(line["required_quantity"])),
            "WarehouseCode": line.get("warehouse_code") or warehouse_code,
        }
        if tax_code:
            row["VatGroup"] = tax_code  # per-line tax (GST) from the warehouse master
        return row
