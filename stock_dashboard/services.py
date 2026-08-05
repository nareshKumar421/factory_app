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

_STATUS_SEVERITY = {"none": 0, "healthy": 0, "unset": 1, "low": 2, "critical": 3}
SLOW_MOVING_DAYS = 30
EXPORT_MAX_ROWS = 10000


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

        warehouses = self.reader.get_warehouses()

        # Stats and pagination must come from the same filtered row shape as the table.
        if is_grouped:
            filtered_stats = self.reader.get_grouped_stock_stats(filters)
        else:
            filtered_stats = self.reader.get_stock_stats(filters)

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
                "healthy_count": filtered_stats["healthy_count"],
                "low_stock_count": filtered_stats["low_count"],
                "critical_stock_count": filtered_stats["critical_count"],
                "warehouses": warehouses,
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "page": page,
                "page_size": page_size,
                "total_pages": total_pages,
            },
        }

    def get_as_of_stock_levels(self, filters: Dict[str, Any]) -> Dict:
        """
        Returns SAP movement reconstructed Stock Benchmark rows for a prior date.

        This proof endpoint keeps benchmark and item master values current, while
        reconstructing on-hand and movement age from SAP OINM posting history.
        """
        page = int(filters.get("page", 1))
        page_size = int(filters.get("page_size", 50))
        as_of_date = filters["as_of_date"]

        warehouses = self.reader.get_warehouses()
        filtered_stats = self.reader.get_as_of_stock_stats(filters, as_of_date)
        filtered_total = filtered_stats["total_items"]
        total_pages = max(1, (filtered_total + page_size - 1) // page_size)

        rows = self.reader.get_as_of_stock_levels(
            filters,
            as_of_date=as_of_date,
            page=page,
            page_size=page_size,
        )
        self._enrich_rows(rows)

        return {
            "data": rows,
            "meta": {
                "total_items": filtered_total,
                "healthy_count": filtered_stats["healthy_count"],
                "low_stock_count": filtered_stats["low_count"],
                "critical_stock_count": filtered_stats["critical_count"],
                "warehouses": warehouses,
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "as_of_date": as_of_date.isoformat(),
                "reconstruction_note": (
                    "On-hand and movement age are reconstructed from SAP OINM. "
                    "Benchmark and item master fields are current SAP values."
                ),
                "page": page,
                "page_size": page_size,
                "total_pages": total_pages,
            },
        }

    def get_stock_levels_for_export(self, filters: Dict[str, Any]) -> List[Dict]:
        """
        Returns all filtered rows (capped at EXPORT_MAX_ROWS) for the Excel export.

        Mirrors the table endpoints: grouped rows when 2+ warehouses are selected,
        SAP movement reconstruction when as_of_date is provided (never grouped,
        matching the as-of endpoint).
        """
        as_of_date = filters.get("as_of_date")
        is_grouped = len(filters.get("warehouse", [])) >= 2

        if as_of_date:
            rows = self.reader.get_as_of_stock_levels(
                filters, as_of_date=as_of_date, page=1, page_size=EXPORT_MAX_ROWS
            )
            self._enrich_rows(rows)
        elif is_grouped:
            rows = self.reader.get_grouped_stock_levels(filters, page=1, page_size=EXPORT_MAX_ROWS)
            self._enrich_grouped_rows(rows)
        else:
            rows = self.reader.get_stock_levels(filters, page=1, page_size=EXPORT_MAX_ROWS)
            self._enrich_rows(rows)
        return rows

    def get_item_detail(self, item_code: str, warehouses: List[str]) -> Dict:
        """Returns per-warehouse breakdown for a single item (expand detail)."""
        rows = self.reader.get_item_warehouses(item_code, warehouses)
        self._enrich_rows(rows)
        return {"data": rows}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _enrich_rows(self, rows: List[Dict]) -> None:
        """Adds stock and movement status to individual rows."""
        for row in rows:
            row["movement_status"] = self._movement_status(row)
            row["stock_status"] = self._stock_status(
                row["on_hand"],
                row["min_stock"],
                movement_status=row["movement_status"],
            )
            row["health_ratio"] = self._health_ratio(row)

    def _enrich_grouped_rows(self, rows: List[Dict]) -> None:
        """Adds computed stock and movement fields to grouped rows."""
        for row in rows:
            row["movement_status"] = self._movement_status(row)
            row["stock_status"] = self._stock_status(
                row["on_hand"],
                row["min_stock"],
                movement_status=row["movement_status"],
            )
            row["health_ratio"] = self._health_ratio(row)
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
    def _stock_status(
        on_hand: float,
        min_stock: float,
        movement_status: str | None = None,
    ) -> str:
        if movement_status == "slow":
            return "none"
        required_qty = min_stock
        if required_qty <= 0:
            return "unset"
        if on_hand >= required_qty:
            return "healthy"
        if on_hand >= required_qty * 0.6:
            return "low"
        return "critical"

    @staticmethod
    def _health_ratio(row: Dict) -> float:
        required_qty = row["min_stock"]
        return round(row["on_hand"] / required_qty, 2) if required_qty > 0 else 0.0

    @staticmethod
    def _movement_status(row: Dict) -> str:
        days_since_consumption = row.get("days_since_last_consumption")
        if (
            days_since_consumption is not None
            and days_since_consumption <= SLOW_MOVING_DAYS
        ):
            return "recent"

        return "slow"
