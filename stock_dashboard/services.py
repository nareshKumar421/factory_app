"""
stock_dashboard/services.py

Business logic for the Stock Dashboard.
Calculates stock health ratios and categorizes items by urgency.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List

from sap_client.context import CompanyContext

from .hana_reader import HanaStockDashboardReader

logger = logging.getLogger(__name__)

_STATUS_SEVERITY = {"healthy": 0, "unset": 1, "low": 2, "critical": 3}


class StockDashboardService:
    """
    Orchestrates SAP HANA reads and business calculations for the stock dashboard.

    Usage:
        service = StockDashboardService(company_code="JIVO_OIL")
        result = service.get_stock_levels(filters)
    """

    def __init__(self, company_code: str):
        self.company_code = company_code
        self.context = CompanyContext(company_code)
        self.reader = HanaStockDashboardReader(self.context)

    def get_stock_levels(self, filters: Dict[str, Any]) -> Dict:
        """
        Returns paginated stock level data with health status.

        When multiple warehouses are selected, items are grouped by item_code
        with aggregated quantities. Otherwise returns individual warehouse rows.
        """
        page = int(filters.get("page", 1))
        page_size = int(filters.get("page_size", 50))
        warehouse_list = filters.get("warehouse", [])
        is_grouped = len(warehouse_list) >= 2

        # Global stats (no status/warehouse filter) for benchmark cards
        global_filters = {k: v for k, v in filters.items() if k not in ("status", "warehouse")}
        global_stats = self.reader.get_stock_stats(global_filters)
        warehouses = self.reader.get_warehouses()

        # Filtered stats for pagination
        has_filters = filters.get("status") or filters.get("warehouse")
        if has_filters:
            if is_grouped:
                filtered_stats = self.reader.get_grouped_stock_stats(filters)
            else:
                filtered_stats = self.reader.get_stock_stats(filters)
        else:
            filtered_stats = global_stats

        filtered_total = filtered_stats["total_items"]
        total_pages = max(1, (filtered_total + page_size - 1) // page_size)

        if is_grouped:
            rows = self.reader.get_grouped_stock_levels(filters, page=page, page_size=page_size)
            self._enrich_grouped_rows(rows)
        else:
            rows = self.reader.get_stock_levels(filters, page=page, page_size=page_size)
            self._enrich_rows(rows)

        return {
            "data": rows,
            "meta": {
                "total_items": filtered_total,
                "healthy_count": global_stats["healthy_count"],
                "low_stock_count": global_stats["low_count"],
                "critical_stock_count": global_stats["critical_count"],
                "warehouses": warehouses,
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "page": page,
                "page_size": page_size,
                "total_pages": total_pages,
            },
        }

    def get_item_detail(self, item_code: str, warehouses: List[str]) -> Dict:
        """Returns per-warehouse breakdown for a single item (expand detail)."""
        rows = self.reader.get_item_warehouses(item_code, warehouses)
        self._enrich_rows(rows)
        return {"data": rows}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _enrich_rows(self, rows: List[Dict]) -> None:
        """Adds stock_status and health_ratio to individual rows."""
        for row in rows:
            row["stock_status"] = self._stock_status(row["on_hand"], row["min_stock"])
            row["health_ratio"] = (
                round(row["on_hand"] / row["min_stock"], 2)
                if row["min_stock"] > 0 else 0.0
            )

    def _enrich_grouped_rows(self, rows: List[Dict]) -> None:
        """Adds computed fields to grouped (multi-warehouse) rows."""
        for row in rows:
            row["stock_status"] = self._stock_status(row["on_hand"], row["min_stock"])
            row["health_ratio"] = (
                round(row["on_hand"] / row["min_stock"], 2)
                if row["min_stock"] > 0 else 0.0
            )
            row["warehouse"] = f"{row['warehouse_count']} warehouses"

            # Determine worst individual warehouse status
            if row.pop("critical_warehouses", 0) > 0:
                worst = "critical"
            elif row.pop("low_warehouses", 0) > 0:
                worst = "low"
            else:
                worst = row["stock_status"]

            row["has_warning"] = (
                _STATUS_SEVERITY.get(worst, 0) > _STATUS_SEVERITY.get(row["stock_status"], 0)
            )

    @staticmethod
    def _stock_status(on_hand: float, min_stock: float) -> str:
        if min_stock <= 0:
            return "unset"
        if on_hand >= min_stock:
            return "healthy"
        if on_hand >= min_stock * 0.6:
            return "low"
        return "critical"
