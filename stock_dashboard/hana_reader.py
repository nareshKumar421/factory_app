"""
stock_dashboard/hana_reader.py

Executes SAP HANA SQL queries for the Stock Dashboard.
Reads from SAP B1 HANA tables: OITW (Item Warehouses), OITM (Item Master).
"""

import logging
from typing import Any, Dict, List, Tuple

from hdbcli import dbapi

from sap_client.hana.connection import HanaConnection
from sap_client.exceptions import SAPConnectionError, SAPDataError

logger = logging.getLogger(__name__)


class HanaStockDashboardReader:
    """
    Reads stock level data directly from SAP HANA.

    Returns items with current OnHand quantities, warehouse code, and
    inventory UOM. Pagination is handled via LIMIT/OFFSET.
    """

    def __init__(self, context):
        self.connection = HanaConnection(context.hana)

    # ------------------------------------------------------------------
    # Public Methods
    # ------------------------------------------------------------------

    def get_stock_levels(self, filters: Dict[str, Any], page: int = 1, page_size: int = 50) -> List[Dict]:
        """Returns one page of item-warehouse rows, ordered by warehouse then item code."""
        query, params = self._build_query(filters)
        offset = (page - 1) * page_size
        paginated_query = f"{query} LIMIT ? OFFSET ?"
        rows = self._execute(paginated_query, params + [page_size, offset])
        return [self._map_row(r) for r in rows]

    def get_warehouses(self) -> List[str]:
        """Returns sorted list of distinct warehouse codes present in OITW."""
        schema = self.connection.schema
        query = f"""
            SELECT DISTINCT w."WhsCode"
            FROM "{schema}"."OITW" w
            ORDER BY w."WhsCode" ASC
        """
        rows = self._execute(query, [])
        return [r[0] for r in rows if r[0]]

    def get_stock_stats(self, filters: Dict[str, Any]) -> Dict:
        """Returns total, healthy, low, and critical counts across the full filtered dataset."""
        query, params = self._build_stats_query(filters)
        rows = self._execute(query, params)
        row = rows[0] if rows else (0, 0, 0, 0)
        return {
            "total_items": int(row[0] or 0),
            "healthy_count": int(row[1] or 0),
            "low_count": int(row[2] or 0),
            "critical_count": int(row[3] or 0),
        }

    # ------------------------------------------------------------------
    # Query Builders
    # ------------------------------------------------------------------

    # Maps each status to its SQL condition based on the health thresholds
    _STATUS_SQL = {
        "unset":    'w."MinStock" = 0',
        "healthy":  'w."MinStock" > 0 AND w."OnHand" >= w."MinStock"',
        "low":      'w."MinStock" > 0 AND w."OnHand" < w."MinStock" AND w."OnHand" >= w."MinStock" * 0.6',
        "critical": 'w."MinStock" > 0 AND w."OnHand" < w."MinStock" * 0.6',
    }

    # Status conditions for aggregated (grouped) queries — uses SUM aliases
    _GROUPED_STATUS_SQL = {
        "unset":    "min_stock = 0",
        "healthy":  "min_stock > 0 AND on_hand >= min_stock",
        "low":      "min_stock > 0 AND on_hand < min_stock AND on_hand >= min_stock * 0.6",
        "critical": "min_stock > 0 AND on_hand < min_stock * 0.6",
    }

    # Maps frontend sort column names to SQL expressions
    _SORT_COL_SQL = {
        "item_code":    'w."ItemCode"',
        "item_name":    'm."ItemName"',
        "warehouse":    'w."WhsCode"',
        "on_hand":      'w."OnHand"',
        "min_stock":    'w."MinStock"',
        # health_ratio is computed, so we use the ratio expression directly
        "health_ratio": 'CASE WHEN w."MinStock" > 0 THEN w."OnHand" / w."MinStock" ELSE 0 END',
    }

    # For grouped queries the aliases are different
    _SORT_COL_GROUPED = {
        "item_code":    "item_code",
        "item_name":    "item_name",
        "warehouse":    "warehouse_count",
        "on_hand":      "on_hand",
        "min_stock":    "min_stock",
        "health_ratio": "CASE WHEN min_stock > 0 THEN on_hand / min_stock ELSE 0 END",
    }

    def _build_order_by(self, filters: Dict[str, Any], grouped: bool = False) -> str:
        col = filters.get("sort_by", "health_ratio")
        direction = filters.get("sort_dir", "asc").upper()
        col_map = self._SORT_COL_GROUPED if grouped else self._SORT_COL_SQL
        sql_col = col_map.get(col, col_map["health_ratio"])
        return f"ORDER BY {sql_col} {direction}"

    def _build_base_where(self, filters: Dict[str, Any]) -> Tuple[List[str], List]:
        """Base WHERE clauses for warehouse and search (no status)."""
        clauses = []
        params = []

        warehouse_list = filters.get("warehouse", [])
        if warehouse_list:
            placeholders = ", ".join("?" for _ in warehouse_list)
            clauses.append(f'w."WhsCode" IN ({placeholders})')
            params.extend(warehouse_list)

        if filters.get("search"):
            search_term = f"%{filters['search']}%"
            clauses.append(
                '(w."ItemCode" LIKE ? OR m."ItemName" LIKE ? OR w."WhsCode" LIKE ?)'
            )
            params.extend([search_term, search_term, search_term])

        return clauses, params

    def _build_where(self, filters: Dict[str, Any]) -> Tuple[str, List]:
        """Full WHERE clause including status filter (for non-grouped queries)."""
        clauses, params = self._build_base_where(filters)

        status_list = filters.get("status", [])
        if status_list:
            conditions = [f'({self._STATUS_SQL[s]})' for s in status_list if s in self._STATUS_SQL]
            if conditions:
                clauses.append(f'({" OR ".join(conditions)})')

        where = f'WHERE {" AND ".join(clauses)}' if clauses else ''
        return where, params

    def _status_where_clause(self, filters: Dict[str, Any], sql_map: Dict) -> str:
        """Builds a status filter fragment from the given SQL map."""
        status_list = filters.get("status", [])
        if not status_list:
            return ""
        conditions = [f"({sql_map[s]})" for s in status_list if s in sql_map]
        return f'WHERE ({" OR ".join(conditions)})' if conditions else ""

    def _build_query(self, filters: Dict[str, Any]) -> Tuple[str, List]:
        schema = self.connection.schema
        where, params = self._build_where(filters)
        order_by = self._build_order_by(filters)

        query = f"""
            SELECT
                w."ItemCode",
                m."ItemName",
                w."WhsCode",
                w."OnHand",
                w."MinStock",
                IFNULL(m."InvntryUom", '')  AS uom
            FROM "{schema}"."OITW" w
            JOIN "{schema}"."OITM" m
                ON w."ItemCode" = m."ItemCode"
            {where}
            {order_by}
        """
        return query, params

    def _build_stats_query(self, filters: Dict[str, Any]) -> Tuple[str, List]:
        """
        Counts total, low, and critical items using the same thresholds as the service layer:
          critical: on_hand < min_stock * 0.6
          low:      on_hand < min_stock AND on_hand >= min_stock * 0.6
        """
        schema = self.connection.schema
        where, params = self._build_where(filters)

        query = f"""
            SELECT
                COUNT(*) AS total_items,
                SUM(CASE
                    WHEN w."MinStock" > 0 AND w."OnHand" >= w."MinStock"
                    THEN 1 ELSE 0
                END) AS healthy_count,
                SUM(CASE
                    WHEN w."MinStock" > 0 AND w."OnHand" < w."MinStock" AND w."OnHand" >= w."MinStock" * 0.6
                    THEN 1 ELSE 0
                END) AS low_count,
                SUM(CASE
                    WHEN w."MinStock" > 0 AND w."OnHand" < w."MinStock" * 0.6
                    THEN 1 ELSE 0
                END) AS critical_count
            FROM "{schema}"."OITW" w
            JOIN "{schema}"."OITM" m
                ON w."ItemCode" = m."ItemCode"
            {where}
        """
        return query, params

    # ------------------------------------------------------------------
    # Grouped Queries (multi-warehouse)
    # ------------------------------------------------------------------

    def get_grouped_stock_levels(
        self, filters: Dict[str, Any], page: int = 1, page_size: int = 50
    ) -> List[Dict]:
        """Returns one page of item rows grouped across warehouses."""
        query, params = self._build_grouped_query(filters)
        offset = (page - 1) * page_size
        paginated_query = f"{query} LIMIT ? OFFSET ?"
        rows = self._execute(paginated_query, params + [page_size, offset])
        return [self._map_grouped_row(r) for r in rows]

    def get_grouped_stock_stats(self, filters: Dict[str, Any]) -> Dict:
        """Stats for grouped items (multi-warehouse)."""
        query, params = self._build_grouped_stats_query(filters)
        rows = self._execute(query, params)
        row = rows[0] if rows else (0, 0, 0, 0)
        return {
            "total_items": int(row[0] or 0),
            "healthy_count": int(row[1] or 0),
            "low_count": int(row[2] or 0),
            "critical_count": int(row[3] or 0),
        }

    def get_item_warehouses(
        self, item_code: str, warehouses: List[str]
    ) -> List[Dict]:
        """Returns per-warehouse rows for a single item (expand detail)."""
        schema = self.connection.schema
        placeholders = ", ".join("?" for _ in warehouses)
        query = f"""
            SELECT
                w."ItemCode",
                m."ItemName",
                w."WhsCode",
                w."OnHand",
                w."MinStock",
                IFNULL(m."InvntryUom", '') AS uom
            FROM "{schema}"."OITW" w
            JOIN "{schema}"."OITM" m
                ON w."ItemCode" = m."ItemCode"
            WHERE w."ItemCode" = ? AND w."WhsCode" IN ({placeholders})
            ORDER BY w."WhsCode" ASC
        """
        rows = self._execute(query, [item_code] + warehouses)
        return [self._map_row(r) for r in rows]

    def _build_grouped_query(self, filters: Dict[str, Any]) -> Tuple[str, List]:
        schema = self.connection.schema
        base_clauses, params = self._build_base_where(filters)
        base_where = f'WHERE {" AND ".join(base_clauses)}' if base_clauses else ""
        status_where = self._status_where_clause(filters, self._GROUPED_STATUS_SQL)
        order_by = self._build_order_by(filters, grouped=True)

        query = f"""
            SELECT * FROM (
                SELECT
                    w."ItemCode"    AS item_code,
                    m."ItemName"    AS item_name,
                    SUM(w."OnHand")    AS on_hand,
                    SUM(w."MinStock")  AS min_stock,
                    IFNULL(m."InvntryUom", '') AS uom,
                    COUNT(*)           AS warehouse_count,
                    SUM(CASE WHEN w."MinStock" > 0
                              AND w."OnHand" < w."MinStock" * 0.6
                         THEN 1 ELSE 0 END) AS critical_wh,
                    SUM(CASE WHEN w."MinStock" > 0
                              AND w."OnHand" < w."MinStock"
                              AND w."OnHand" >= w."MinStock" * 0.6
                         THEN 1 ELSE 0 END) AS low_wh
                FROM "{schema}"."OITW" w
                JOIN "{schema}"."OITM" m ON w."ItemCode" = m."ItemCode"
                {base_where}
                GROUP BY w."ItemCode", m."ItemName", m."InvntryUom"
            ) g
            {status_where}
            {order_by}
        """
        return query, params

    def _build_grouped_stats_query(self, filters: Dict[str, Any]) -> Tuple[str, List]:
        schema = self.connection.schema
        base_clauses, params = self._build_base_where(filters)
        base_where = f'WHERE {" AND ".join(base_clauses)}' if base_clauses else ""
        status_where = self._status_where_clause(filters, self._GROUPED_STATUS_SQL)

        query = f"""
            SELECT
                COUNT(*) AS total_items,
                SUM(CASE WHEN min_stock > 0 AND on_hand >= min_stock
                    THEN 1 ELSE 0 END) AS healthy_count,
                SUM(CASE WHEN min_stock > 0 AND on_hand < min_stock
                              AND on_hand >= min_stock * 0.6
                    THEN 1 ELSE 0 END) AS low_count,
                SUM(CASE WHEN min_stock > 0 AND on_hand < min_stock * 0.6
                    THEN 1 ELSE 0 END) AS critical_count
            FROM (
                SELECT
                    SUM(w."OnHand")   AS on_hand,
                    SUM(w."MinStock") AS min_stock
                FROM "{schema}"."OITW" w
                JOIN "{schema}"."OITM" m ON w."ItemCode" = m."ItemCode"
                {base_where}
                GROUP BY w."ItemCode"
            ) g
            {status_where}
        """
        return query, params

    def _map_grouped_row(self, row) -> Dict:
        return {
            "item_code": row[0] or "",
            "item_name": row[1] or "",
            "on_hand": float(row[2] or 0),
            "min_stock": float(row[3] or 0),
            "uom": row[4] or "",
            "warehouse_count": int(row[5] or 0),
            "critical_warehouses": int(row[6] or 0),
            "low_warehouses": int(row[7] or 0),
        }

    # ------------------------------------------------------------------
    # Row Mapper
    # ------------------------------------------------------------------

    def _map_row(self, row) -> Dict:
        on_hand = float(row[3] or 0)
        min_stock = float(row[4] or 0)

        return {
            "item_code": row[0] or "",
            "item_name": row[1] or "",
            "warehouse": row[2] or "",
            "on_hand": on_hand,
            "min_stock": min_stock,
            "uom": row[5] or "",
        }

    # ------------------------------------------------------------------
    # Execution Helper
    # ------------------------------------------------------------------

    def _execute(self, query: str, params: List) -> List:
        conn = None
        cursor = None

        try:
            conn = self.connection.connect()
        except dbapi.Error as e:
            logger.error(f"SAP HANA connection failed: {e}")
            raise SAPConnectionError(
                "Unable to connect to SAP HANA. Please try again later."
            ) from e

        try:
            cursor = conn.cursor()
            cursor.execute(query, params)
            return cursor.fetchall()

        except dbapi.ProgrammingError as e:
            logger.error(f"SAP HANA query error in stock dashboard: {e}")
            raise SAPDataError(
                "Failed to retrieve stock dashboard data from SAP. Invalid query."
            ) from e
        except dbapi.Error as e:
            logger.error(f"SAP HANA data error in stock dashboard: {e}")
            raise SAPDataError(
                "Failed to retrieve stock dashboard data from SAP. Please try again."
            ) from e
        finally:
            if cursor:
                try:
                    cursor.close()
                except Exception:
                    pass
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
