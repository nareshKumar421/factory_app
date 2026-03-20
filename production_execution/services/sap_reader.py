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
        """Get full detail of a production order including components."""
        schema = self.client.context.config['hana']['schema']

        header_sql = """
            SELECT
                W."DocEntry", W."DocNum", W."ItemCode", W."ProdName",
                W."PlannedQty", W."CmpltQty", W."RjctQty",
                (W."PlannedQty" - W."CmpltQty" - W."RjctQty") AS "RemainingQty",
                W."StartDate", W."DueDate", W."Warehouse", W."Status"
            FROM "{schema}"."OWOR" W
            WHERE W."DocEntry" = {doc_entry}
        """.format(schema=schema, doc_entry=int(doc_entry))

        components_sql = """
            SELECT
                C."ItemCode", C."ItemName", C."PlannedQty",
                C."IssuedQty", C."Warehouse", C."UomCode"
            FROM "{schema}"."WOR1" C
            WHERE C."DocEntry" = {doc_entry}
        """.format(schema=schema, doc_entry=int(doc_entry))

        headers = self._execute(header_sql)
        if not headers:
            raise SAPReadError(f"Production order {doc_entry} not found.")

        components = self._execute(components_sql)
        return {
            'header': headers[0],
            'components': components,
        }

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
