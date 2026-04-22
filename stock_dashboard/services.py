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

        Meta counts (total_items, low_stock_count, critical_stock_count) always
        reflect the full filtered dataset so benchmark cards are unaffected by
        which page is displayed.

        Health logic:
          - on_hand >= min_stock        → 'healthy'
          - on_hand < min_stock          → 'low'       (below minimum)
          - on_hand < min_stock * 0.6    → 'critical'  (below 60% of minimum)
        """
        page = int(filters.get("page", 1))
        page_size = int(filters.get("page_size", 50))

        # Global stats (status/warehouse-independent) for the benchmark cards
        global_filters = {k: v for k, v in filters.items() if k not in ("status", "warehouse")}
        global_stats = self.reader.get_stock_stats(global_filters)
        warehouses = self.reader.get_warehouses()

        # Filtered stats for pagination
        has_filters = filters.get("status") or filters.get("warehouse")
        filtered_stats = self.reader.get_stock_stats(filters) if has_filters else global_stats
        filtered_total = filtered_stats["total_items"]
        total_pages = max(1, (filtered_total + page_size - 1) // page_size)

        # Paginated rows for the current page
        rows = self.reader.get_stock_levels(filters, page=page, page_size=page_size)

        for row in rows:
            row["stock_status"] = self._stock_status(row["on_hand"], row["min_stock"])
            row["health_ratio"] = round(
                row["on_hand"] / row["min_stock"], 2
            ) if row["min_stock"] > 0 else 0.0

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

    @staticmethod
    def _stock_status(on_hand: float, min_stock: float) -> str:
        """
        Returns a stock health label:
          'unset'    — min_stock is 0 (no minimum configured)
          'healthy'  — on_hand >= min_stock
          'low'      — on_hand < min_stock but >= 60% of min_stock
          'critical' — on_hand < 60% of min_stock
        """
        if min_stock <= 0:
            return "unset"
        if on_hand >= min_stock:
            return "healthy"
        if on_hand >= min_stock * 0.6:
            return "low"
        return "critical"
