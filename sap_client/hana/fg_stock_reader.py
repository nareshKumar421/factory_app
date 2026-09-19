"""On-hand stock of FG items in one warehouse, for the invoice approver.

The approver's job on the Invoice Approval page is to check a bill against what
is physically in the warehouse, so every FG line needs its current OITW on-hand
beside it. This reader answers that for a set of item codes at a time — the list
endpoint asks once for a whole page of invoices rather than once per line.

Ported from the OMS API's own `get_fg_warehouse_stock`, which is what served
this field while the page read OMS over HTTP. The query is kept deliberately
identical so the column keeps meaning the same number it always did.
"""
import logging
from typing import List, Optional

from hdbcli import dbapi

from .connection import HanaConnection
from ..exceptions import SAPConnectionError, SAPDataError

logger = logging.getLogger(__name__)


class HanaFGStockReader:

    def __init__(self, context):
        self.connection = HanaConnection(context.hana)

    def get_fg_warehouse_stock(
        self,
        item_codes: Optional[List[str]] = None,
        warehouse_code: Optional[str] = None,
    ) -> List[dict]:
        """Per-warehouse on-hand for FG items.

        ``item_codes`` / ``warehouse_code`` narrow the result to what is actually
        on the invoices in hand. Without them the whole FG catalogue across every
        warehouse comes back — a few thousand rows.
        """
        # Nothing to ask about. Returning early also keeps us from emitting
        # `IN ()`, which is a syntax error rather than an empty result.
        if item_codes is not None and not item_codes:
            return []

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

            # Parameters bind positionally, in the order their placeholders appear
            # in the FINAL string — not the order the fragments are built here.
            # The warehouse placeholder sits in the JOIN, above the WHERE clause,
            # so it binds first even though the item filter is assembled first.
            # Getting this backwards does not error: it filters on the wrong
            # column and quietly returns the wrong stock.
            join_params = []
            where_params = []

            whs_join = ""
            if warehouse_code:
                whs_join = 'AND T1."WhsCode" = ?'
                join_params.append(warehouse_code)

            filters = ['T0."ItemCode" LIKE \'FG%\'']
            if item_codes:
                item_codes = list(item_codes)
                placeholders = ",".join("?" for _ in item_codes)
                filters.append(f'T0."ItemCode" IN ({placeholders})')
                where_params.extend(item_codes)

            where = " AND ".join(filters)

            # Driven off OITM, not OITW: an item with no stock row for this
            # warehouse must still come back — with its name and a NULL OnHand —
            # rather than vanishing from the result. "Not stocked here" and
            # "stocked, empty" are different answers to the approver.
            query = f"""
                SELECT
                    T0."ItemCode",
                    T0."ItemName",
                    T1."WhsCode",
                    T1."OnHand"
                FROM "{schema}"."OITM" AS T0
                LEFT JOIN "{schema}"."OITW" AS T1
                    ON T1."ItemCode" = T0."ItemCode"
                    {whs_join}
                WHERE {where}
                ORDER BY T1."OnHand" DESC, T1."WhsCode"
            """

            cursor.execute(query, join_params + where_params)
            rows = cursor.fetchall()

            return [
                {
                    "ItemCode": row[0],
                    "ItemName": row[1],
                    "WhsCode": row[2],
                    "OnHand": row[3],
                }
                for row in rows
            ]

        except dbapi.ProgrammingError as e:
            logger.error(f"SAP HANA query error for FG stock: {e}")
            raise SAPDataError(
                "Failed to retrieve FG stock from SAP. Invalid query or parameters."
            ) from e
        except dbapi.Error as e:
            logger.error(f"SAP HANA data error for FG stock: {e}")
            raise SAPDataError(
                "Failed to retrieve FG stock from SAP. Please try again later."
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
