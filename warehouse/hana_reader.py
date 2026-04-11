"""
warehouse/hana_reader.py

SAP HANA queries for the Warehouse Management module.
Reads from OITM, OITW, OIBT, OITB, OWHS, and OINM tables.
"""

import logging
from typing import Any, Dict, List, Optional

from hdbcli import dbapi

from sap_client.hana.connection import HanaConnection
from sap_client.exceptions import SAPConnectionError, SAPDataError

logger = logging.getLogger(__name__)


class HanaWarehouseReader:
    """
    Reads warehouse and inventory data from SAP HANA.

    Provides methods for:
    - Item stock across warehouses (OITM + OITW)
    - Item detail with per-warehouse breakdown
    - Available batches with FIFO ordering (OIBT)
    - Movement history (OINM)
    - Filter options (OWHS, OITB)
    - Low-stock alerts (OITW.MinStock)
    - Batch expiry report (OIBT)
    """

    def __init__(self, context):
        self.connection = HanaConnection(context.hana)

    # ------------------------------------------------------------------
    # Filter Options (lightweight queries for dropdowns)
    # ------------------------------------------------------------------

    def get_filter_options(self) -> Dict[str, List]:
        """Return distinct warehouses and item groups for filter dropdowns."""
        schema = self.connection.schema

        warehouses = self._execute(
            f"""
            SELECT "WhsCode", "WhsName"
            FROM "{schema}"."OWHS"
            WHERE "Inactive" = 'N' OR "Inactive" IS NULL
            ORDER BY "WhsCode"
            """,
            [],
        )

        item_groups = self._execute(
            f"""
            SELECT "ItmsGrpCod", "ItmsGrpNam"
            FROM "{schema}"."OITB"
            ORDER BY "ItmsGrpNam"
            """,
            [],
        )

        return {
            "warehouses": [
                {"code": r[0], "name": r[1] or ""} for r in warehouses
            ],
            "item_groups": [
                {"code": r[0], "name": r[1] or ""} for r in item_groups
            ],
        }

    # ------------------------------------------------------------------
    # Inventory List (paginated stock across warehouses)
    # ------------------------------------------------------------------

    def get_item_stock(self, filters: Dict[str, Any]) -> List[Dict]:
        """
        Returns item stock across warehouses.
        Filters: search, warehouse, item_group.
        Only returns items with OnHand > 0.
        """
        schema = self.connection.schema
        clauses = ['w."OnHand" > 0']
        params = []

        if filters.get("warehouse"):
            clauses.append('w."WhsCode" = ?')
            params.append(filters["warehouse"])

        if filters.get("item_group"):
            clauses.append('m."ItmsGrpCod" = ?')
            params.append(int(filters["item_group"]))

        if filters.get("search"):
            search_term = f"%{filters['search']}%"
            clauses.append(
                '(m."ItemCode" LIKE ? OR m."ItemName" LIKE ?)'
            )
            params.extend([search_term, search_term])

        query = f"""
            SELECT
                m."ItemCode",
                m."ItemName",
                g."ItmsGrpNam",
                w."WhsCode",
                w."OnHand",
                w."IsCommited",
                w."OnOrder",
                IFNULL(m."InvntryUom", '') AS uom,
                m."ManBtchNum",
                w."MinStock"
            FROM "{schema}"."OITW" w
            JOIN "{schema}"."OITM" m ON w."ItemCode" = m."ItemCode"
            LEFT JOIN "{schema}"."OITB" g ON m."ItmsGrpCod" = g."ItmsGrpCod"
            WHERE {' AND '.join(clauses)}
            ORDER BY w."WhsCode", m."ItemCode"
        """

        rows = self._execute(query, params)
        return [self._map_stock_row(r) for r in rows]

    def _map_stock_row(self, row) -> Dict:
        on_hand = float(row[4] or 0)
        min_stock = float(row[9] or 0)
        return {
            "item_code": row[0] or "",
            "item_name": row[1] or "",
            "item_group": row[2] or "",
            "warehouse": row[3] or "",
            "on_hand": on_hand,
            "committed": float(row[5] or 0),
            "on_order": float(row[6] or 0),
            "available": on_hand - float(row[5] or 0),
            "uom": row[7] or "",
            "is_batch_managed": (row[8] or "N") == "Y",
            "min_stock": min_stock,
            "is_below_min": min_stock > 0 and on_hand < min_stock,
        }

    # ------------------------------------------------------------------
    # Item Detail (single item, all warehouses)
    # ------------------------------------------------------------------

    def get_item_detail(self, item_code: str) -> Optional[Dict]:
        """Returns item master info and stock breakdown per warehouse."""
        schema = self.connection.schema

        # Item master
        master_rows = self._execute(
            f"""
            SELECT
                m."ItemCode",
                m."ItemName",
                g."ItmsGrpNam",
                IFNULL(m."InvntryUom", '') AS uom,
                m."ManBtchNum",
                IFNULL(m."U_Variety", '') AS variety,
                IFNULL(m."U_Sub_Group", '') AS sub_group
            FROM "{schema}"."OITM" m
            LEFT JOIN "{schema}"."OITB" g ON m."ItmsGrpCod" = g."ItmsGrpCod"
            WHERE m."ItemCode" = ?
            """,
            [item_code],
        )

        if not master_rows:
            return None

        r = master_rows[0]
        item = {
            "item_code": r[0] or "",
            "item_name": r[1] or "",
            "item_group": r[2] or "",
            "uom": r[3] or "",
            "is_batch_managed": (r[4] or "N") == "Y",
            "variety": r[5] or "",
            "sub_group": r[6] or "",
        }

        # Stock per warehouse
        whs_rows = self._execute(
            f"""
            SELECT
                w."WhsCode",
                h."WhsName",
                w."OnHand",
                w."IsCommited",
                w."OnOrder",
                w."MinStock"
            FROM "{schema}"."OITW" w
            LEFT JOIN "{schema}"."OWHS" h ON w."WhsCode" = h."WhsCode"
            WHERE w."ItemCode" = ?
              AND (w."OnHand" > 0 OR w."IsCommited" > 0 OR w."OnOrder" > 0)
            ORDER BY w."WhsCode"
            """,
            [item_code],
        )

        item["warehouse_stock"] = [
            {
                "warehouse_code": wr[0] or "",
                "warehouse_name": wr[1] or "",
                "on_hand": float(wr[2] or 0),
                "committed": float(wr[3] or 0),
                "on_order": float(wr[4] or 0),
                "available": float(wr[2] or 0) - float(wr[3] or 0),
                "min_stock": float(wr[5] or 0),
            }
            for wr in whs_rows
        ]

        # Totals
        item["total_on_hand"] = sum(
            ws["on_hand"] for ws in item["warehouse_stock"]
        )
        item["total_committed"] = sum(
            ws["committed"] for ws in item["warehouse_stock"]
        )
        item["total_on_order"] = sum(
            ws["on_order"] for ws in item["warehouse_stock"]
        )
        item["total_available"] = sum(
            ws["available"] for ws in item["warehouse_stock"]
        )

        return item

    # ------------------------------------------------------------------
    # Available Batches (FIFO — sorted by admission date)
    # ------------------------------------------------------------------

    def get_available_batches(
        self, item_code: str, warehouse_code: Optional[str] = None
    ) -> List[Dict]:
        """
        Returns available batches for an item, sorted by admission date
        (oldest first) to enforce FIFO.
        """
        schema = self.connection.schema
        clauses = ['"ItemCode" = ?', '"Quantity" > 0']
        params = [item_code]

        if warehouse_code:
            clauses.append('"WhsCode" = ?')
            params.append(warehouse_code)

        rows = self._execute(
            f"""
            SELECT
                "ItemCode",
                "BatchNum",
                "WhsCode",
                "Quantity",
                "InDate",
                "ExpDate"
            FROM "{schema}"."OIBT"
            WHERE {' AND '.join(clauses)}
            ORDER BY "InDate" ASC, "BatchNum" ASC
            """,
            params,
        )

        return [
            {
                "item_code": r[0] or "",
                "batch_number": r[1] or "",
                "warehouse_code": r[2] or "",
                "quantity": float(r[3] or 0),
                "admission_date": str(r[4]) if r[4] else None,
                "expiry_date": str(r[5]) if r[5] else None,
            }
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Movement History (OINM)
    # ------------------------------------------------------------------

    def get_movement_history(
        self, item_code: str, filters: Dict[str, Any]
    ) -> List[Dict]:
        """Returns inventory movement history for an item from OINM."""
        schema = self.connection.schema
        clauses = ['"ItemCode" = ?']
        params = [item_code]

        if filters.get("warehouse"):
            clauses.append('"Warehouse" = ?')
            params.append(filters["warehouse"])

        if filters.get("from_date"):
            clauses.append('"CreateDate" >= ?')
            params.append(filters["from_date"])

        if filters.get("to_date"):
            clauses.append('"CreateDate" <= ?')
            params.append(filters["to_date"])

        rows = self._execute(
            f"""
            SELECT
                "ItemCode",
                "Warehouse",
                "InQty",
                "OutQty",
                "TransType",
                "CreateDate",
                "DocNum",
                "Balance"
            FROM "{schema}"."OINM"
            WHERE {' AND '.join(clauses)}
            ORDER BY "CreateDate" DESC, "DocNum" DESC
            """,
            params,
        )

        return [
            {
                "item_code": r[0] or "",
                "warehouse": r[1] or "",
                "in_qty": float(r[2] or 0),
                "out_qty": float(r[3] or 0),
                "trans_type": int(r[4] or 0),
                "create_date": str(r[5]) if r[5] else None,
                "doc_num": int(r[6] or 0),
                "balance": float(r[7] or 0),
            }
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Low Stock Alerts
    # ------------------------------------------------------------------

    def get_min_stock_alerts(
        self, warehouse_code: Optional[str] = None
    ) -> List[Dict]:
        """Returns items where current stock is below MinStock threshold."""
        schema = self.connection.schema
        clauses = ['w."MinStock" > 0', 'w."OnHand" < w."MinStock"']
        params = []

        if warehouse_code:
            clauses.append('w."WhsCode" = ?')
            params.append(warehouse_code)

        rows = self._execute(
            f"""
            SELECT
                w."ItemCode",
                m."ItemName",
                w."WhsCode",
                w."OnHand",
                w."MinStock",
                IFNULL(m."InvntryUom", '') AS uom
            FROM "{schema}"."OITW" w
            JOIN "{schema}"."OITM" m ON w."ItemCode" = m."ItemCode"
            WHERE {' AND '.join(clauses)}
            ORDER BY (w."OnHand" / w."MinStock") ASC
            """,
            params,
        )

        return [
            {
                "item_code": r[0] or "",
                "item_name": r[1] or "",
                "warehouse": r[2] or "",
                "on_hand": float(r[3] or 0),
                "min_stock": float(r[4] or 0),
                "uom": r[5] or "",
                "shortage": float(r[4] or 0) - float(r[3] or 0),
            }
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Batch Expiry Report (for non-moving FG)
    # ------------------------------------------------------------------

    def get_batch_expiry_report(
        self, warehouse_code: Optional[str] = None
    ) -> List[Dict]:
        """
        Returns batches with stock, including age in days since admission.
        Used for non-moving FG tracking and expiry alerts.
        """
        schema = self.connection.schema
        clauses = ['b."Quantity" > 0']
        params = []

        if warehouse_code:
            clauses.append('b."WhsCode" = ?')
            params.append(warehouse_code)

        rows = self._execute(
            f"""
            SELECT
                b."ItemCode",
                m."ItemName",
                g."ItmsGrpNam",
                b."BatchNum",
                b."WhsCode",
                b."Quantity",
                b."InDate",
                b."ExpDate",
                DAYS_BETWEEN(b."InDate", CURRENT_DATE) AS days_age,
                IFNULL(m."InvntryUom", '') AS uom,
                IFNULL(m."U_Variety", '') AS variety
            FROM "{schema}"."OIBT" b
            JOIN "{schema}"."OITM" m ON b."ItemCode" = m."ItemCode"
            LEFT JOIN "{schema}"."OITB" g ON m."ItmsGrpCod" = g."ItmsGrpCod"
            WHERE {' AND '.join(clauses)}
            ORDER BY days_age DESC
            """,
            params,
        )

        return [
            {
                "item_code": r[0] or "",
                "item_name": r[1] or "",
                "item_group": r[2] or "",
                "batch_number": r[3] or "",
                "warehouse": r[4] or "",
                "quantity": float(r[5] or 0),
                "admission_date": str(r[6]) if r[6] else None,
                "expiry_date": str(r[7]) if r[7] else None,
                "days_age": int(r[8] or 0),
                "uom": r[9] or "",
                "variety": r[10] or "",
            }
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Stock in a specific warehouse for a specific item (for capacity check)
    # ------------------------------------------------------------------

    def get_item_stock_in_warehouse(
        self, item_code: str, warehouse_code: str
    ) -> float:
        """Returns current OnHand quantity for an item in a warehouse."""
        schema = self.connection.schema
        rows = self._execute(
            f"""
            SELECT "OnHand"
            FROM "{schema}"."OITW"
            WHERE "ItemCode" = ? AND "WhsCode" = ?
            """,
            [item_code, warehouse_code],
        )
        if rows:
            return float(rows[0][0] or 0)
        return 0.0

    # ------------------------------------------------------------------
    # Execution Helper
    # ------------------------------------------------------------------

    def _execute(self, query: str, params: List[Any]) -> List:
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
            logger.error(f"SAP HANA query error in warehouse: {e}")
            raise SAPDataError(
                f"Warehouse HANA query error: {e}"
            ) from e
        except dbapi.Error as e:
            logger.error(f"SAP HANA data error in warehouse: {e}")
            raise SAPDataError(
                f"Warehouse HANA error: {e}"
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
