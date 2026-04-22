"""
stock_dashboard/hana_reader.py

Executes SAP HANA SQL queries for the Stock Dashboard.
Reads from SAP B1 HANA tables: OITW (Item Warehouses), OITM (Item Master).
"""

import logging
from typing import Any, Dict, List

from hdbcli import dbapi

from sap_client.hana.connection import HanaConnection
from sap_client.exceptions import SAPConnectionError, SAPDataError

logger = logging.getLogger(__name__)


class HanaStockDashboardReader:
    """
    Reads stock level data directly from SAP HANA.

    Returns items where MinStock is set, along with current OnHand
    quantities, warehouse code, and inventory UOM.
    """

    def __init__(self, context):
        self.connection = HanaConnection(context.hana)

    # ------------------------------------------------------------------
    # Public Methods
    # ------------------------------------------------------------------

    def get_stock_levels(self, filters: Dict[str, Any]) -> List[Dict]:
        """
        Returns one row per item-warehouse combination where MinStock != 0.

        Each row includes on-hand qty, min stock, UOM, and warehouse.
        """
        query, params = self._build_query(filters)
        rows = self._execute(query, params)
        return [self._map_row(r) for r in rows]

    # ------------------------------------------------------------------
    # Query Builder
    # ------------------------------------------------------------------

    def _build_query(self, filters: Dict[str, Any]):
        schema = self.connection.schema
        clauses = [
            'w."MinStock" != NULL',
        ]
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
            WHERE {' AND '.join(clauses)}
            ORDER BY
                w."WhsCode" ASC,
                w."ItemCode" ASC
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
