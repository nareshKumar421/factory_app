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

    def get_open_production_orders(self) -> list:
        """Get planned/released production orders with remaining qty > 0."""
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
            WHERE W."Status" IN ('P', 'R')
              AND (W."PlannedQty" - W."CmpltQty" - W."RjctQty") > 0
            ORDER BY W."DueDate" ASC
        """.format(schema=self.client.context.config['hana']['schema'])
        try:
            return self._execute(sql)
        except Exception as e:
            logger.error(f"Failed to fetch open production orders: {e}")
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
                C."LineNum", C."ItemCode", C."ItemName", C."PlannedQty",
                C."IssuedQty", C."wareHouse" AS "Warehouse", C."UomCode"
            FROM "{schema}"."WOR1" C
            WHERE C."DocEntry" = {val}
            ORDER BY C."LineNum" ASC
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

    def resolve_item_code_by_name(self, name: str) -> str:
        """Resolve a SAP item code (OITM.ItemCode) from an exact ItemName.

        Returns the code only when the name maps to exactly one item; returns
        None when there is no match or the name is ambiguous (so callers can
        surface a clear error instead of silently picking the wrong item).
        """
        if not name:
            return None
        schema = self.client.context.config['hana']['schema']
        safe_name = name.replace("'", "''")
        try:
            # The value may already be a valid item code (some older runs stored
            # the code in the product field) — use it directly if so.
            code_rows = self._execute(
                'SELECT "ItemCode" FROM "{schema}"."OITM" WHERE "ItemCode" = \'{v}\''
                .format(schema=schema, v=safe_name)
            )
            if len(code_rows) == 1:
                return code_rows[0]['ItemCode']
            # Otherwise resolve from the item name (unique match only).
            rows = self._execute(
                'SELECT "ItemCode" FROM "{schema}"."OITM" WHERE "ItemName" = \'{v}\''
                .format(schema=schema, v=safe_name)
            )
        except Exception as e:
            logger.error(f"Failed to resolve item code for '{name}': {e}")
            raise SAPReadError(f"Failed to resolve item code for '{name}': {e}")
        if len(rows) == 1:
            return rows[0]['ItemCode']
        if len(rows) > 1:
            logger.warning(
                f"Item name '{name}' is ambiguous — matches {len(rows)} SAP items; "
                f"cannot resolve a single item code."
            )
        return None

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

    def search_items(self, search: str = '', limit: int = 50, produced_only: bool = False) -> list:
        """Search SAP item master (OITM).

        When produced_only=True, restrict to finished goods that have a
        production BOM defined (present in OITT) — i.e. SKUs a production run
        can be started for. Otherwise return all items (e.g. raw-material lookup).
        """
        schema = self.client.context.config['hana']['schema']
        where_clause = 'WHERE 1=1'
        if search:
            safe_search = search.replace("'", "''")
            where_clause += (
                f" AND (LOWER(T0.\"ItemCode\") LIKE LOWER('%{safe_search}%')"
                f" OR LOWER(T0.\"ItemName\") LIKE LOWER('%{safe_search}%'))"
            )
        if produced_only:
            where_clause += f' AND T0."ItemCode" IN (SELECT "Code" FROM "{schema}"."OITT")'
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
