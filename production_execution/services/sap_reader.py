import logging
from sap_client.client import SAPClient
from sap_client.exceptions import SAPConnectionError, SAPDataError

logger = logging.getLogger(__name__)


class SAPReadError(Exception):
    pass


class ProductionOrderReader:
    """Reads production orders from SAP HANA for a specific company."""

    def __init__(self, company_code: str):
        self.company_code = company_code
        try:
            self.client = SAPClient(company_code=company_code)
        except Exception as e:
            raise SAPReadError(f"Failed to initialize SAP client: {e}")

    def get_released_production_orders(self) -> list:
        """Get all released production orders with remaining qty > 0."""
        sql = """
            SELECT
                W."DocEntry",
                W."DocNum",
                W."ItemCode",
                W."ProdName",
                W."PlannedQty",
                W."CmpltQty",
                W."RjctQty",
                (W."PlannedQty" - W."CmpltQty" - W."RjctQty") AS "RemainingQty",
                W."StartDate",
                W."DueDate",
                W."Warehouse",
                W."Status"
            FROM "{schema}"."OWOR" W
            WHERE W."Status" = 'R'
              AND (W."PlannedQty" - W."CmpltQty" - W."RjctQty") > 0
            ORDER BY W."DueDate" ASC
        """.format(schema=self.client.context.config['hana']['schema'])
        try:
            return self._execute(sql)
        except Exception as e:
            logger.error(f"Failed to fetch released production orders: {e}")
            raise SAPReadError(f"Failed to fetch production orders: {e}")

    def get_production_order_detail(self, doc_entry: int) -> dict:
        """Get full detail of a production order including components.
        Tries DocEntry first, falls back to DocNum if not found."""
        schema = self.client.context.config['hana']['schema']
        safe_val = int(doc_entry)

        header_sql = """
            SELECT
                W."DocEntry", W."DocNum", W."ItemCode", W."ProdName",
                W."PlannedQty", W."CmpltQty", W."RjctQty",
                (W."PlannedQty" - W."CmpltQty" - W."RjctQty") AS "RemainingQty",
                W."StartDate", W."DueDate", W."Warehouse", W."Status"
            FROM "{schema}"."OWOR" W
            WHERE W."DocEntry" = {val}
        """.format(schema=schema, val=safe_val)

        headers = self._execute(header_sql)

        # Fallback: value might be DocNum instead of DocEntry
        if not headers:
            header_sql = """
                SELECT
                    W."DocEntry", W."DocNum", W."ItemCode", W."ProdName",
                    W."PlannedQty", W."CmpltQty", W."RjctQty",
                    (W."PlannedQty" - W."CmpltQty" - W."RjctQty") AS "RemainingQty",
                    W."StartDate", W."DueDate", W."Warehouse", W."Status"
                FROM "{schema}"."OWOR" W
                WHERE W."DocNum" = {val}
            """.format(schema=schema, val=safe_val)
            headers = self._execute(header_sql)

        if not headers:
            raise SAPReadError(f"Production order {doc_entry} not found.")

        actual_doc_entry = headers[0]['DocEntry']
        components_sql = """
            SELECT
                C."ItemCode", C."ItemName", C."PlannedQty",
                C."IssuedQty", C."Warehouse", C."UomCode"
            FROM "{schema}"."WOR1" C
            WHERE C."DocEntry" = {val}
        """.format(schema=schema, val=int(actual_doc_entry))

        components = self._execute(components_sql)
        return {
            'header': headers[0],
            'components': components,
        }

    def get_production_orders_by_entries(self, doc_entries: list) -> dict:
        """Batch fetch production orders by doc entries.
        Returns {lookup_value: row_dict} — tries DocEntry first, falls back to DocNum."""
        if not doc_entries:
            return {}
        schema = self.client.context.config['hana']['schema']
        entries_str = ', '.join(str(int(e)) for e in doc_entries)
        sql = """
            SELECT
                W."DocEntry", W."DocNum", W."ItemCode", W."ProdName",
                W."PlannedQty", W."CmpltQty", W."RjctQty",
                W."StartDate", W."DueDate", W."Status"
            FROM "{schema}"."OWOR" W
            WHERE W."DocEntry" IN ({entries})
        """.format(schema=schema, entries=entries_str)
        try:
            rows = self._execute(sql)
            result = {r['DocEntry']: r for r in rows}

            # For any values not found by DocEntry, try DocNum fallback
            missing = [e for e in doc_entries if e not in result]
            if missing:
                missing_str = ', '.join(str(int(e)) for e in missing)
                fallback_sql = """
                    SELECT
                        W."DocEntry", W."DocNum", W."ItemCode", W."ProdName",
                        W."PlannedQty", W."CmpltQty", W."RjctQty",
                        W."StartDate", W."DueDate", W."Status"
                    FROM "{schema}"."OWOR" W
                    WHERE W."DocNum" IN ({entries})
                """.format(schema=schema, entries=missing_str)
                fallback_rows = self._execute(fallback_sql)
                for r in fallback_rows:
                    # Key by DocNum so the caller can look up by the value it passed
                    result[r['DocNum']] = r

            return result
        except Exception as e:
            logger.error(f"Failed to batch fetch production orders: {e}")
            raise SAPReadError(f"Failed to batch fetch production orders: {e}")

    def get_bom_by_item_code(self, item_code: str) -> list:
        """Fetch BOM components for a finished good from SAP OITT/ITT1 tables."""
        schema = self.client.context.config['hana']['schema']
        safe_item = item_code.replace("'", "''")
        sql = """
            SELECT
                T1."Code"      AS "ItemCode",
                T1."ItemName"  AS "ItemName",
                T1."Quantity"  AS "PlannedQty",
                COALESCE(T1."Uom", I."InvntryUom") AS "UomCode",
                T1."Warehouse" AS "Warehouse"
            FROM "{schema}"."OITT" T0
            INNER JOIN "{schema}"."ITT1" T1 ON T0."Code" = T1."Father"
            LEFT JOIN "{schema}"."OITM" I ON T1."Code" = I."ItemCode"
            WHERE T0."Code" = '{item_code}'
            ORDER BY T1."VisOrder" ASC
        """.format(schema=schema, item_code=safe_item)
        try:
            return self._execute(sql)
        except Exception as e:
            logger.error(f"Failed to fetch BOM for item {item_code}: {e}")
            raise SAPReadError(f"Failed to fetch BOM for item {item_code}: {e}")

    def get_bom_components_for_run(self, sap_doc_entry: int = None, item_code: str = None) -> list:
        """
        Fetch BOM components with priority:
        1. If sap_doc_entry provided → fetch from WOR1 (production order components)
        2. Else if item_code provided → fetch from OITT/ITT1 (item BOM master)
        Returns a normalized list of dicts with keys:
            ItemCode, ItemName, PlannedQty, IssuedQty, UomCode
        """
        if sap_doc_entry:
            detail = self.get_production_order_detail(sap_doc_entry)
            return detail.get('components', [])
        elif item_code:
            components = self.get_bom_by_item_code(item_code)
            # Normalize: BOM master doesn't have IssuedQty
            for comp in components:
                comp.setdefault('IssuedQty', 0)
            return components
        return []

    def search_items(self, search: str = '', limit: int = 50) -> list:
        """Search SAP item master (OITM) for raw materials."""
        schema = self.client.context.config['hana']['schema']
        where_clause = 'WHERE 1=1'
        if search:
            safe_search = search.replace("'", "''")
            where_clause += (
                f" AND (LOWER(T0.\"ItemCode\") LIKE LOWER('%{safe_search}%')"
                f" OR LOWER(T0.\"ItemName\") LIKE LOWER('%{safe_search}%'))"
            )
        sql = """
            SELECT TOP {limit}
                T0."ItemCode",
                T0."ItemName",
                T0."InvntryUom" AS "UomCode"
            FROM "{schema}"."OITM" T0
            {where_clause}
            ORDER BY T0."ItemName" ASC
        """.format(schema=schema, limit=limit, where_clause=where_clause)
        try:
            return self._execute(sql)
        except Exception as e:
            logger.error(f"Failed to search SAP items: {e}")
            raise SAPReadError(f"Failed to search items: {e}")

    def _execute(self, sql: str) -> list:
        try:
            conn = self.client.context.hana
            from hdbcli import dbapi
            connection = dbapi.connect(
                address=conn['host'],
                port=conn['port'],
                user=conn['user'],
                password=conn['password'],
            )
            cursor = connection.cursor()
            cursor.execute(sql)
            cols = [c[0] for c in cursor.description]
            rows = cursor.fetchall()
            cursor.close()
            connection.close()
            return [dict(zip(cols, row)) for row in rows]
        except Exception as e:
            raise SAPReadError(str(e))
