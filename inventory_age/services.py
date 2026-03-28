"""
inventory_age/services.py

Business logic for the Inventory Age & Value dashboard.
Filters SP results, computes summary statistics.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List

from sap_client.context import CompanyContext

from .hana_reader import HanaInventoryAgeReader

logger = logging.getLogger(__name__)


class InventoryAgeService:
    """
    Orchestrates SAP HANA reads and business calculations for
    the inventory age dashboard.
    """

    def __init__(self, company_code: str):
        self.company_code = company_code
        self.context = CompanyContext(company_code)
        self.reader = HanaInventoryAgeReader(self.context)

    # ------------------------------------------------------------------
    # Lightweight: filter options only (no SP call)
    # ------------------------------------------------------------------

    def get_filter_options(self) -> Dict[str, List]:
        return self.reader.get_filter_options()

    # ------------------------------------------------------------------
    # Full report (requires at least item_group filter)
    # ------------------------------------------------------------------

    def get_inventory_age(self, filters: Dict[str, Any]) -> Dict:
        """
        Returns filtered inventory age data with summary stats.
        Caller must ensure item_group is provided.
        """
        rows = self.reader.get_inventory_age()

        # Apply filters
        rows = self._apply_filters(rows, filters)

        # Build warehouse summary
        warehouse_summary = self._build_warehouse_summary(rows)

        # Build meta
        total_items = len(rows)
        total_value = sum(r["in_stock_value"] for r in rows)
        total_quantity = sum(r["on_hand"] for r in rows)
        total_litres = sum(r["litres"] for r in rows)

        return {
            "data": rows,
            "meta": {
                "total_items": total_items,
                "total_value": round(total_value, 2),
                "total_quantity": round(total_quantity, 2),
                "total_litres": round(total_litres, 2),
                "warehouse_count": len(warehouse_summary),
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            },
            "warehouse_summary": warehouse_summary,
        }

    def _apply_filters(
        self, rows: List[Dict], filters: Dict[str, Any]
    ) -> List[Dict]:
        search = (filters.get("search") or "").strip().lower()
        warehouse = (filters.get("warehouse") or "").strip()
        item_group = (filters.get("item_group") or "").strip()
        sub_group = (filters.get("sub_group") or "").strip()
        variety = (filters.get("variety") or "").strip()
        min_age = filters.get("min_age")

        filtered = rows

        if item_group:
            filtered = [r for r in filtered if r["item_group"] == item_group]

        if search:
            filtered = [
                r
                for r in filtered
                if search in r["item_code"].lower()
                or search in r["item_name"].lower()
            ]

        if warehouse:
            filtered = [r for r in filtered if r["warehouse"] == warehouse]

        if sub_group:
            filtered = [r for r in filtered if r["sub_group"] == sub_group]

        if variety:
            filtered = [r for r in filtered if r["variety"] == variety]

        if min_age is not None:
            filtered = [r for r in filtered if r["days_age"] >= min_age]

        return filtered

    def _build_warehouse_summary(self, rows: List[Dict]) -> List[Dict]:
        by_whs: Dict[str, Dict] = {}
        for r in rows:
            whs = r["warehouse"]
            if whs not in by_whs:
                by_whs[whs] = {
                    "warehouse": whs,
                    "item_count": 0,
                    "total_value": 0.0,
                    "total_quantity": 0.0,
                    "total_litres": 0.0,
                }
            by_whs[whs]["item_count"] += 1
            by_whs[whs]["total_value"] += r["in_stock_value"]
            by_whs[whs]["total_quantity"] += r["on_hand"]
            by_whs[whs]["total_litres"] += r["litres"]

        summary = sorted(by_whs.values(), key=lambda x: x["total_value"], reverse=True)
        for s in summary:
            s["total_value"] = round(s["total_value"], 2)
            s["total_quantity"] = round(s["total_quantity"], 2)
            s["total_litres"] = round(s["total_litres"], 2)

        return summary
