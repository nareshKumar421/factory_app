"""
stock_dashboard/services.py

Business logic for the Stock Dashboard.
Calculates stock health ratios and categorizes items by urgency.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sap_client.context import CompanyContext

from .hana_reader import HanaStockDashboardReader

logger = logging.getLogger(__name__)

_STATUS_SEVERITY = {"none": 0, "healthy": 0, "unset": 1, "low": 2, "critical": 3}
SLOW_MOVING_DAYS = 30
EXPORT_MAX_ROWS = 10000

# Inventory units that are a mass or a volume rather than a count of things.
#
# A case weight only converts an on-hand figure that is a piece count: multiply
# a KG balance by kg-per-case and the answer is nonsense. Stated as a deny-list
# because the countable units are open-ended and keep arriving -- the plan
# reader records `PCS` for 97 of 98 items and `DRM` for the one drum SKU
# (`planning_purchase/hana_reader.py`), and a drum is still a thing you count.
_MASS_OR_VOLUME_UOMS = frozenset(
    {"KG", "KGS", "KGM", "GM", "GMS", "GRM", "MT", "TON", "TONNE",
     "L", "LT", "LTR", "LTRS", "LITRE", "LITRES", "ML", "CC", "M3"}
)


def _is_piece_uom(uom: str) -> bool:
    """True where an on-hand quantity in `uom` is a count of pieces.

    An unknown or blank unit answers False. SAP leaves `InvntryUom` empty often
    enough that guessing "piece" there would fold unweighable rows into a
    tonnage total silently; answering False instead pushes them into the
    caller's disclosure count, where somebody can see them.
    """
    return bool((uom or "").strip()) and (uom or "").strip().upper() not in _MASS_OR_VOLUME_UOMS


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
                # Weight of the stock that is under its benchmark, and how many
                # of those rows SAP holds no case weight for. Null where the
                # read cannot answer it — see `get_stock_stats`.
                "below_benchmark_tonnes": filtered_stats.get("below_benchmark_tonnes"),
                "unweighed_below_benchmark": filtered_stats.get(
                    "unweighed_below_benchmark", 0
                ),
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

    def get_item_batches(self, item_code: str, warehouse: str) -> Dict:
        """One item's batches in one warehouse, with make and expiry dates.

        `meta` carries the counts a caller needs to caption the list honestly:
        how much stock is here, and how many batches SAP holds no make date for.
        A list that silently showed 9 of 12 batches' ages would read as complete.
        """
        rows = self.reader.get_item_batches(item_code, warehouse)
        movements = self.reader.get_item_movements(item_code, warehouse)
        ages = self.reader.get_item_movement_ages(item_code, warehouse)
        return {
            "data": rows,
            "movements": movements,
            "meta": {
                "item_code": item_code,
                "warehouse": warehouse,
                "batch_count": len(rows),
                "total_quantity": sum(r["quantity"] for r in rows),
                "without_mfg_date": sum(1 for r in rows if not r["mfg_date"]),
                "without_exp_date": sum(1 for r in rows if not r["exp_date"]),
                "oldest_age_days": max(
                    (r["age_days"] for r in rows if r["age_days"] is not None), default=None
                ),
                # Two ages, because on a floor stock is produced INTO they differ:
                # an inbound receipt is arrival, not movement. See
                # `get_item_movement_ages`.
                **ages,
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            },
        }

    def get_warehouse_occupancy(
        self, warehouse: str, item_groups: Optional[List[int]] = None
    ) -> Dict:
        """One warehouse's stock with the pack fields needed to count pallets.

        Deliberately does NO pallet arithmetic. The pieces-per-pallet figures are
        board policy, not SAP fact -- the boxes-per-pallet divisor came from
        measuring how pallets are actually built, and the loose-SKU figures are
        estimates standing in for answers the floor has not given yet. Keeping
        them in the frontend, named and unit-tested, means they can be corrected
        without a backend release. This endpoint's job is to hand over the two
        SAP fields honestly and let the caller convert.

        `unconfigured_items` counts SKUs SAP holds no pieces-per-box for, so a
        board can say how much of its own figure rests on a fallback.

        `unweighed_items` and `non_piece_items` are the same disclosure for
        tonnage. A warehouse total in tonnes is only as complete as the item
        master behind it: the first counts SKUs with no case weight recorded (or
        a company with no `U_Gross_Weight` field at all), the second counts rows
        whose on-hand is a mass or a volume rather than a piece count, where a
        pack factor does not apply. A board showing tonnes must show these next
        to it -- a confident total over a half-weighed warehouse is the failure
        mode here, and it looks identical to a correct one.
        """
        rows = self.reader.get_warehouse_occupancy(warehouse, item_groups=item_groups)
        return {
            "data": rows,
            "meta": {
                "warehouse": warehouse,
                "item_groups": list(item_groups or []),
                "item_count": len(rows),
                "total_on_hand": sum(r["on_hand"] for r in rows),
                "total_value": sum(r["stock_value"] for r in rows),
                "loose_items": sum(1 for r in rows if (r["pieces_per_box"] or 0) <= 1),
                "unconfigured_items": sum(1 for r in rows if r["pieces_per_box"] is None),
                "unweighed_items": sum(
                    1 for r in rows if r["gross_weight_per_case"] is None
                ),
                "non_piece_items": sum(
                    1 for r in rows if not _is_piece_uom(r["uom"])
                ),
                "fetched_at": datetime.now(timezone.utc).isoformat(),
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
