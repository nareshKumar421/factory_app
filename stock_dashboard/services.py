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

        # Full-dataset counts for benchmark cards — unaffected by pagination
        stats = self.reader.get_stock_stats(filters)
        total_items = stats["total_items"]
        total_pages = max(1, (total_items + page_size - 1) // page_size)

        # Paginated rows for the current page
        rows = self.reader.get_stock_levels(filters, page=page, page_size=page_size)

        for row in rows:
            row["stock_status"] = self._stock_status(row["on_hand"], row["min_stock"])
            row["health_ratio"] = round(
                row["on_hand"] / row["min_stock"], 2
            ) if row["min_stock"] > 0 else 0.0

        # Status filter applies to displayed rows only (not used by the frontend)
        status_filter = filters.get("status", "all")
        if status_filter and status_filter != "all":
            rows = [r for r in rows if r["stock_status"] == status_filter]

        return {
            "data": rows,
            "meta": {
                "total_items": total_items,
                "low_stock_count": stats["low_count"],
                "critical_stock_count": stats["critical_count"],
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
          'healthy'  — on_hand >= min_stock
          'low'      — on_hand < min_stock but >= 60% of min_stock
          'critical' — on_hand < 60% of min_stock
        """
        if on_hand >= min_stock:
            return "healthy"
        if on_hand >= min_stock * 0.6:
            return "low"
        return "critical"
