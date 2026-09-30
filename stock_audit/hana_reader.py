"""SAP reads for a stock audit: a whole warehouse's on-hand, and one item's.

The warehouse item picker (``HanaWarehouseReader.get_warehouse_stock``) stops at
200 rows; an audit needs every item the warehouse holds, so it has its own read.
Negative on-hand is kept: SAP saying -12 is itself something to audit.
"""
import logging
from decimal import Decimal
from typing import List, Optional

from hdbcli import dbapi

from sap_client.context import CompanyContext
from sap_client.exceptions import SAPConnectionError, SAPDataError
from sap_client.hana.connection import HanaConnection

logger = logging.getLogger(__name__)

_COLUMNS = '''
    W."ItemCode", IFNULL(I."ItemName", ''), IFNULL(I."ItmsGrpCod", 0),
    IFNULL(I."InvntryUom", ''), IFNULL(W."OnHand", 0)
'''


def _row(row) -> dict:
    return {
        'item_code': row[0],
        'item_name': row[1] or '',
        'item_group': int(row[2] or 0),
        'uom': row[3] or '',
        'on_hand': Decimal(str(row[4] or 0)),
    }


class StockAuditReader:
    def __init__(self, company_code: str):
        self.connection = HanaConnection(CompanyContext(company_code).hana)

    def _query(self, sql: str, params: tuple) -> list:
        try:
            conn = self.connection.connect()
        except dbapi.Error as e:
            logger.error("SAP HANA connection failed: %s", e)
            raise SAPConnectionError(
                "Unable to connect to SAP HANA. Please try again later.") from e
        try:
            cursor = conn.cursor()
            try:
                cursor.execute(sql.format(schema=self.connection.schema), params)
                return cursor.fetchall()
            finally:
                cursor.close()
        except dbapi.Error as e:
            logger.error("SAP HANA stock audit query failed: %s", e)
            raise SAPDataError("Failed to read the warehouse's stock from SAP.") from e
        finally:
            conn.close()

    def warehouse_stock(self, warehouse_code: str) -> List[dict]:
        """Every stock item the warehouse holds a quantity of, any sign."""
        rows = self._query(
            f'''
            SELECT {_COLUMNS}
            FROM "{{schema}}"."OITW" W
            JOIN "{{schema}}"."OITM" I ON I."ItemCode" = W."ItemCode"
            WHERE W."WhsCode" = ? AND I."InvntItem" = 'Y' AND IFNULL(W."OnHand", 0) <> 0
            ORDER BY W."ItemCode"
            ''',
            (warehouse_code,),
        )
        return [_row(r) for r in rows]

    def item(self, warehouse_code: str, item_code: str) -> Optional[dict]:
        """One stock item, with the warehouse's on-hand (0 if it holds none)."""
        rows = self._query(
            f'''
            SELECT I."ItemCode", IFNULL(I."ItemName", ''), IFNULL(I."ItmsGrpCod", 0),
                   IFNULL(I."InvntryUom", ''), IFNULL(W."OnHand", 0)
            FROM "{{schema}}"."OITM" I
            LEFT JOIN "{{schema}}"."OITW" W
                   ON W."ItemCode" = I."ItemCode" AND W."WhsCode" = ?
            WHERE I."ItemCode" = ? AND I."InvntItem" = 'Y'
            ''',
            (warehouse_code, item_code),
        )
        return _row(rows[0]) if rows else None

    def search_items(self, warehouse_code: str, search: str, limit: int = 20) -> List[dict]:
        """Stock items by code or name, for adding one SAP did not list."""
        term = f"%{search.strip().upper()}%"
        rows = self._query(
            f'''
            SELECT I."ItemCode", IFNULL(I."ItemName", ''), IFNULL(I."ItmsGrpCod", 0),
                   IFNULL(I."InvntryUom", ''), IFNULL(W."OnHand", 0)
            FROM "{{schema}}"."OITM" I
            LEFT JOIN "{{schema}}"."OITW" W
                   ON W."ItemCode" = I."ItemCode" AND W."WhsCode" = ?
            WHERE I."InvntItem" = 'Y'
              AND (UPPER(I."ItemCode") LIKE ? OR UPPER(IFNULL(I."ItemName", '')) LIKE ?)
            ORDER BY I."ItemCode"
            LIMIT {max(1, min(int(limit), 50))}
            ''',
            (warehouse_code, term, term),
        )
        return [_row(r) for r in rows]
