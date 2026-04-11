"""
warehouse/services.py

Business logic for the Warehouse Management module.
Phase 1: Read-only inventory visibility from SAP HANA.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sap_client.context import CompanyContext

from .hana_reader import HanaWarehouseReader

logger = logging.getLogger(__name__)


class WarehouseService:
    """
    Orchestrates SAP HANA reads for warehouse inventory data.
    """

    def __init__(self, company_code: str):
        self.company_code = company_code
        self.context = CompanyContext(company_code)
        self.reader = HanaWarehouseReader(self.context)

    # ------------------------------------------------------------------
    # Filter Options
    # ------------------------------------------------------------------

    def get_filter_options(self) -> Dict[str, List]:
        return self.reader.get_filter_options()

    # ------------------------------------------------------------------
    # Inventory List
    # ------------------------------------------------------------------

    def get_inventory(self, filters: Dict[str, Any]) -> Dict:
        """Returns inventory list with summary stats."""
        rows = self.reader.get_item_stock(filters)

        total_items = len(rows)
        total_on_hand = sum(r["on_hand"] for r in rows)
        below_min_count = sum(1 for r in rows if r["is_below_min"])

        warehouse_set = set(r["warehouse"] for r in rows)

        return {
            "data": rows,
            "meta": {
                "total_items": total_items,
                "total_on_hand": round(total_on_hand, 2),
                "warehouse_count": len(warehouse_set),
                "below_min_count": below_min_count,
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            },
        }

    # ------------------------------------------------------------------
    # Item Detail
    # ------------------------------------------------------------------

    def get_item_detail(self, item_code: str) -> Optional[Dict]:
        """Returns item master + warehouse breakdown + batches."""
        item = self.reader.get_item_detail(item_code)
        if not item:
            return None

        # Add batch info if batch-managed
        if item["is_batch_managed"]:
            item["batches"] = self.reader.get_available_batches(item_code)
        else:
            item["batches"] = []

        return item

    # ------------------------------------------------------------------
    # Movement History
    # ------------------------------------------------------------------

    def get_movement_history(
        self, item_code: str, filters: Dict[str, Any]
    ) -> Dict:
        """Returns movement history for an item."""
        movements = self.reader.get_movement_history(item_code, filters)
        return {
            "item_code": item_code,
            "movements": movements,
            "meta": {
                "total_movements": len(movements),
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            },
        }

    # ------------------------------------------------------------------
    # Dashboard Summary
    # ------------------------------------------------------------------

    def get_dashboard_summary(
        self, warehouse_code: Optional[str] = None
    ) -> Dict:
        """Returns dashboard summary with alerts."""
        low_stock_alerts = self.reader.get_min_stock_alerts(warehouse_code)

        return {
            "low_stock_alerts": low_stock_alerts[:20],
            "low_stock_count": len(low_stock_alerts),
            "meta": {
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            },
        }

    # ------------------------------------------------------------------
    # Batch Expiry / Non-Moving Report
    # ------------------------------------------------------------------

    def get_batch_expiry_report(
        self, warehouse_code: Optional[str] = None
    ) -> Dict:
        """Returns batch expiry report for non-moving FG tracking."""
        batches = self.reader.get_batch_expiry_report(warehouse_code)

        return {
            "data": batches,
            "meta": {
                "total_batches": len(batches),
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            },
        }
