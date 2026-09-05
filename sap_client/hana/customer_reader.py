"""Customer master reads (OCRD, CardType 'C').

Unlike the vendor list — which is small enough to ship whole to the frontend —
the customer master runs to thousands of rows, so ``search_customers`` searches
server-side and returns a capped page for a type-ahead picker.
"""

import logging
from typing import Optional

from hdbcli import dbapi

from .connection import HanaConnection
from ..exceptions import SAPConnectionError, SAPDataError

logger = logging.getLogger(__name__)


class HanaCustomerReader:
    def __init__(self, context):
        self.connection = HanaConnection(context.hana)

    def get_customer(self, card_code: str) -> Optional[dict]:
        """One customer by exact code (any status — the caller may hold a code
        the search would hide, e.g. a frozen account on an old record)."""
        card_code = (card_code or "").strip()
        if not card_code:
            return None
        rows = self._fetch(
            f"""
            SELECT "CardCode", IFNULL("CardName", '')
            FROM "{self.connection.schema}"."OCRD"
            WHERE "CardType" = 'C' AND "CardCode" = ?
            """,
            (card_code,),
        )
        if not rows:
            return None
        return {"customer_code": rows[0][0], "customer_name": rows[0][1] or rows[0][0]}

    def search_customers(self, search: Optional[str] = None, limit: int = 50) -> list[dict]:
        safe_limit = max(1, min(int(limit or 50), 100))
        where = [
            """"CardType" = 'C'""",
            """IFNULL("validFor", 'Y') = 'Y'""",
            """IFNULL("frozenFor", 'N') = 'N'""",
        ]
        params: list = []
        if search:
            term = f"%{search.lower()}%"
            where.append(
                """(LOWER("CardCode") LIKE ? OR LOWER(IFNULL("CardName", '')) LIKE ?)"""
            )
            params.extend([term, term])

        rows = self._fetch(
            f"""
            SELECT "CardCode", IFNULL("CardName", '')
            FROM "{self.connection.schema}"."OCRD"
            WHERE {" AND ".join(where)}
            ORDER BY "CardName"
            LIMIT {safe_limit}
            """,
            tuple(params),
        )
        return [
            {"customer_code": row[0], "customer_name": row[1] or row[0]}
            for row in rows
        ]

    def _fetch(self, sql: str, params: tuple) -> list:
        conn = None
        cursor = None
        try:
            conn = self.connection.connect()
        except dbapi.Error as e:
            logger.error("SAP HANA connection failed while reading customers: %s", e)
            raise SAPConnectionError("Unable to connect to SAP HANA.") from e

        try:
            cursor = conn.cursor()
            cursor.execute(sql, params)
            return cursor.fetchall()
        except dbapi.Error as e:
            logger.error("SAP HANA customer query failed: %s", e)
            raise SAPDataError("Failed to read customers from SAP.") from e
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
