"""
HANA reader for Sales Orders (ORDR / RDR1).
Reads open/released Sales Orders from SAP B1 HANA tables for outbound dispatch.
"""
import logging
from typing import Dict, List, Any

from sap_client.hana.connection import HanaConnection
from sap_client.exceptions import SAPConnectionError, SAPDataError

logger = logging.getLogger(__name__)


class HanaSalesOrderReader:
    """
    Reads open Sales Orders from SAP HANA for outbound dispatch sync.
    Queries ORDR (Sales Order header) and RDR1 (Sales Order lines).
    """

    def __init__(self, context):
        self.connection = HanaConnection(context.hana)

    def get_open_sales_orders(self, filters: Dict[str, Any] = None) -> List[Dict]:
        """
        Get open Sales Orders with their line items.

        Args:
            filters: Optional dict with keys:
                - customer_code: Filter by customer BP code
                - from_date: Filter orders from this date (YYYY-MM-DD)
                - to_date: Filter orders up to this date (YYYY-MM-DD)
                - item_code: Filter by item code in lines

        Returns:
            List of dicts, each representing a Sales Order with nested items.
        """
        filters = filters or {}
        query, params = self._build_query(filters)
        rows = self._execute(query, params)
        return self._group_into_orders(rows)

    def _build_query(self, filters: Dict[str, Any]):
        schema = self.connection.schema

        where_clauses = [
            f'T0."DocStatus" = \'O\'',  # Open orders only
            f'T0."CANCELED" = \'N\'',   # Not cancelled
        ]
        params = []

        if filters.get("customer_code"):
            where_clauses.append(f'T0."CardCode" = ?')
            params.append(filters["customer_code"])

        if filters.get("from_date"):
            where_clauses.append(f'T0."DocDueDate" >= ?')
            params.append(filters["from_date"])

        if filters.get("to_date"):
            where_clauses.append(f'T0."DocDueDate" <= ?')
            params.append(filters["to_date"])

        if filters.get("item_code"):
            where_clauses.append(f'T1."ItemCode" = ?')
            params.append(filters["item_code"])

        where_sql = " AND ".join(where_clauses)

        query = f"""
            SELECT
                T0."DocEntry"           AS doc_entry,
                T0."DocNum"             AS doc_num,
                T0."CardCode"           AS customer_code,
                T0."CardName"           AS customer_name,
                T0."Address2"           AS ship_to_address,
                T0."DocDueDate"         AS due_date,
                T0."BPLId"              AS branch_id,
                T0."Comments"           AS comments,
                T1."LineNum"            AS line_num,
                T1."ItemCode"           AS item_code,
                T1."Dscription"         AS item_name,
                T1."Quantity"           AS ordered_qty,
                T1."DelivrdQty"         AS delivered_qty,
                (T1."Quantity" - T1."DelivrdQty") AS remaining_qty,
                IFNULL(T1."unitMsr", '') AS uom,
                IFNULL(T1."WhsCode", '') AS warehouse_code,
                '' AS batch_number
            FROM "{schema}"."ORDR" T0
            INNER JOIN "{schema}"."RDR1" T1
                ON T0."DocEntry" = T1."DocEntry"
            WHERE {where_sql}
                AND (T1."Quantity" - T1."DelivrdQty") > 0
            ORDER BY T0."DocDueDate" ASC, T0."DocNum" ASC, T1."LineNum" ASC
        """
        return query, params

    def _execute(self, query: str, params: list) -> list:
        """Execute HANA query and return rows."""
        conn = None
        cursor = None
        try:
            conn = self.connection.connect()
            cursor = conn.cursor()
            cursor.execute(query, params)
            columns = [desc[0].lower() for desc in cursor.description]
            rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
            return rows
        except Exception as e:
            logger.error(f"HANA query error (Sales Orders): {e}")
            if "connection" in str(e).lower() or "communication" in str(e).lower():
                raise SAPConnectionError(f"HANA connection error: {e}")
            raise SAPDataError(f"HANA query error: {e}")
        finally:
            if cursor:
                cursor.close()
            if conn:
                conn.close()

    def _group_into_orders(self, rows: List[Dict]) -> List[Dict]:
        """Group flat rows into nested order -> items structure."""
        orders = {}
        for row in rows:
            doc_entry = row["doc_entry"]
            if doc_entry not in orders:
                orders[doc_entry] = {
                    "doc_entry": doc_entry,
                    "doc_num": row["doc_num"],
                    "customer_code": row["customer_code"],
                    "customer_name": row["customer_name"],
                    "ship_to_address": row.get("ship_to_address") or "",
                    "due_date": row["due_date"],
                    "branch_id": row.get("branch_id"),
                    "comments": row.get("comments") or "",
                    "items": [],
                }
            orders[doc_entry]["items"].append({
                "line_num": row["line_num"],
                "item_code": row["item_code"],
                "item_name": row["item_name"],
                "ordered_qty": float(row["ordered_qty"]),
                "delivered_qty": float(row["delivered_qty"]),
                "remaining_qty": float(row["remaining_qty"]),
                "uom": row["uom"],
                "warehouse_code": row["warehouse_code"],
                "batch_number": row.get("batch_number") or "",
            })

        return list(orders.values())
