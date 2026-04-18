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

    def get_stock_stats(self, filters: Dict[str, Any]) -> Dict:
        """Returns total, low, and critical counts across the full filtered dataset."""
        query, params = self._build_stats_query(filters)
        rows = self._execute(query, params)
        row = rows[0] if rows else (0, 0, 0)
        return {
            "total_items": int(row[0] or 0),
            "low_count": int(row[1] or 0),
            "critical_count": int(row[2] or 0),
        }

    # ------------------------------------------------------------------
    # Query Builders
    # ------------------------------------------------------------------

    def _build_where(self, filters: Dict[str, Any]) -> Tuple[str, List]:
        """Builds the shared WHERE clause and params from search/warehouse filters."""
        clauses = []
        params = []

        if filters.get("warehouse"):
            clauses.append('w."WhsCode" = ?')
            params.append(filters["warehouse"])

        if filters.get("search"):
            search_term = f"%{filters['search']}%"
            clauses.append(
                '(w."ItemCode" LIKE ? OR m."ItemName" LIKE ? OR w."WhsCode" LIKE ?)'
            )
            params.extend([search_term, search_term, search_term])

        where = f'WHERE {" AND ".join(clauses)}' if clauses else ''
        return where, params

    def _build_query(self, filters: Dict[str, Any]) -> Tuple[str, List]:
        schema = self.connection.schema
        where, params = self._build_where(filters)

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
            ORDER BY
                w."WhsCode" ASC,
                w."ItemCode" ASC
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
                    WHEN w."OnHand" < w."MinStock" AND w."OnHand" >= w."MinStock" * 0.6
                    THEN 1 ELSE 0
                END) AS low_count,
                SUM(CASE
                    WHEN w."OnHand" < w."MinStock" * 0.6
                    THEN 1 ELSE 0
                END) AS critical_count
            FROM "{schema}"."OITW" w
            JOIN "{schema}"."OITM" m
                ON w."ItemCode" = m."ItemCode"
            {where}
        """
        return query, params

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
