import logging
from typing import List, Dict

from hdbcli import dbapi

from sap_client.hana.connection import HanaConnection
from sap_client.dtos import ProductionOrderDTO, ProductionComponentDTO
from sap_client.exceptions import SAPConnectionError, SAPDataError

logger = logging.getLogger(__name__)


class HanaProductionOrderReader:
    """
    Reads production orders (OWOR) and BOM components (WOR1) from SAP HANA.
    Only fetches Planned (P) and Released (R) orders.
    """

    def __init__(self, context):
        self.connection = HanaConnection(context.hana)

    def get_monthly_orders(self, year: int, month: int) -> List[ProductionOrderDTO]:
        """
        Fetch all production orders whose DueDate falls in the given year/month.
        Only returns status P (Planned) and R (Released).
        """
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
            schema = self.connection.schema

            query = f"""
                SELECT
                    T0."DocEntry"               AS doc_entry,
                    T0."DocNum"                 AS doc_num,
                    T0."ItemCode"               AS item_code,
                    IFNULL(T0."Dscription", '') AS item_name,
                    T0."PlannedQty"             AS planned_qty,
                    IFNULL(T0."CmpltQty", 0)   AS completed_qty,
                    IFNULL(T0."RjctQty", 0)     AS rejected_qty,
                    (T0."PlannedQty" - IFNULL(T0."CmpltQty", 0)) AS remaining_qty,
                    T0."PlannedDate"            AS planned_start_date,
                    T0."DueDate"                AS due_date,
                    T0."Status"                 AS sap_status,
                    IFNULL(T0."CardCode", '')   AS customer_code,
                    IFNULL(T0."CardName", '')   AS customer_name,
                    T0."BPLId"                  AS branch_id,
                    IFNULL(T0."Comments", '')   AS remarks
                FROM "{schema}"."OWOR" T0
                WHERE T0."Status" IN ('P', 'R')
                  AND YEAR(T0."DueDate") = ?
                  AND MONTH(T0."DueDate") = ?
                ORDER BY T0."DueDate" ASC
            """

            cursor.execute(query, (year, month))
            rows = cursor.fetchall()
            return self._rows_to_order_dtos(rows)

        except dbapi.ProgrammingError as e:
            logger.error(f"SAP HANA query error fetching production orders: {e}")
            raise SAPDataError(
                "Failed to retrieve production orders from SAP. Invalid query or parameters."
            ) from e
        except dbapi.Error as e:
            logger.error(f"SAP HANA data error fetching production orders: {e}")
            raise SAPDataError(
                "Failed to retrieve production orders from SAP. Please try again later."
            ) from e
        finally:
            self._close(cursor, conn)

    def get_orders_by_entries(self, doc_entries: List[int]) -> Dict[int, ProductionOrderDTO]:
        """
        Fetch specific production orders by their DocEntry values.
        Returns a dict keyed by doc_entry for O(1) lookup.
        """
        if not doc_entries:
            return {}

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
            schema = self.connection.schema
            placeholders = ', '.join(['?' for _ in doc_entries])

            query = f"""
                SELECT
                    T0."DocEntry"               AS doc_entry,
                    T0."DocNum"                 AS doc_num,
                    T0."ItemCode"               AS item_code,
                    IFNULL(T0."Dscription", '') AS item_name,
                    T0."PlannedQty"             AS planned_qty,
                    IFNULL(T0."CmpltQty", 0)   AS completed_qty,
                    IFNULL(T0."RjctQty", 0)     AS rejected_qty,
                    (T0."PlannedQty" - IFNULL(T0."CmpltQty", 0)) AS remaining_qty,
                    T0."PlannedDate"            AS planned_start_date,
                    T0."DueDate"                AS due_date,
                    T0."Status"                 AS sap_status,
                    IFNULL(T0."CardCode", '')   AS customer_code,
                    IFNULL(T0."CardName", '')   AS customer_name,
                    T0."BPLId"                  AS branch_id,
                    IFNULL(T0."Comments", '')   AS remarks
                FROM "{schema}"."OWOR" T0
                WHERE T0."DocEntry" IN ({placeholders})
            """

            cursor.execute(query, doc_entries)
            rows = cursor.fetchall()
            orders = self._rows_to_order_dtos(rows)
            return {o.doc_entry: o for o in orders}

        except dbapi.ProgrammingError as e:
            logger.error(f"SAP HANA query error fetching orders by entry: {e}")
            raise SAPDataError(
                "Failed to retrieve production orders from SAP."
            ) from e
        except dbapi.Error as e:
            logger.error(f"SAP HANA data error: {e}")
            raise SAPDataError(
                "Failed to retrieve production orders from SAP. Please try again later."
            ) from e
        finally:
            self._close(cursor, conn)

    def get_order_components(self, doc_entry: int) -> List[ProductionComponentDTO]:
        """
        Fetch BOM components (WOR1) for a single production order.
        """
        result = self.get_bulk_components([doc_entry])
        return result.get(doc_entry, [])

    def get_bulk_components(self, doc_entries: List[int]) -> Dict[int, List[ProductionComponentDTO]]:
        """
        Fetch BOM components for multiple production orders in a single query.
        Returns a dict: {doc_entry: [ProductionComponentDTO, ...]}
        """
        if not doc_entries:
            return {}

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
            schema = self.connection.schema
            placeholders = ', '.join(['?' for _ in doc_entries])

            query = f"""
                SELECT
                    T1."DocEntry"               AS doc_entry,
                    T1."ItemCode"               AS component_code,
                    IFNULL(T1."Dscription", '') AS component_name,
                    T1."PlannedQty"             AS planned_qty,
                    IFNULL(T1."IssuedQty", 0)  AS issued_qty,
                    (T1."PlannedQty" - IFNULL(T1."IssuedQty", 0)) AS remaining_qty,
                    IFNULL(T1."unitMsr", '')    AS uom
                FROM "{schema}"."WOR1" T1
                WHERE T1."DocEntry" IN ({placeholders})
                ORDER BY T1."DocEntry", T1."LineNum" ASC
            """

            cursor.execute(query, doc_entries)
            rows = cursor.fetchall()

            result: Dict[int, List[ProductionComponentDTO]] = {}
            for row in rows:
                doc_entry = int(row[0])
                comp = ProductionComponentDTO(
                    component_code=row[1],
                    component_name=row[2],
                    planned_qty=float(row[3]),
                    issued_qty=float(row[4]),
                    remaining_qty=float(row[5]),
                    uom=row[6],
                )
                result.setdefault(doc_entry, []).append(comp)

            return result

        except dbapi.ProgrammingError as e:
            logger.error(f"SAP HANA query error fetching components: {e}")
            raise SAPDataError(
                "Failed to retrieve BOM components from SAP."
            ) from e
        except dbapi.Error as e:
            logger.error(f"SAP HANA data error fetching components: {e}")
            raise SAPDataError(
                "Failed to retrieve BOM components from SAP. Please try again later."
            ) from e
        finally:
            self._close(cursor, conn)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _rows_to_order_dtos(self, rows) -> List[ProductionOrderDTO]:
        orders = []
        for row in rows:
            orders.append(ProductionOrderDTO(
                doc_entry=int(row[0]),
                doc_num=int(row[1]),
                item_code=row[2],
                item_name=row[3],
                planned_qty=float(row[4]),
                completed_qty=float(row[5]),
                rejected_qty=float(row[6]),
                remaining_qty=float(row[7]),
                planned_start_date=row[8],
                due_date=row[9],
                sap_status=row[10],
                customer_code=row[11],
                customer_name=row[12],
                branch_id=int(row[13]) if row[13] is not None else None,
                remarks=row[14],
            ))
        return orders

    @staticmethod
    def _close(cursor, conn):
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
