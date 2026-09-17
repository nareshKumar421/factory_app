"""Customer master reads (OCRD, CardType 'C').

Unlike the vendor list — which is small enough to ship whole to the frontend —
the customer master runs to thousands of rows, so ``search_customers`` searches
server-side and returns a capped page for a type-ahead picker.

``get_credit_status`` reproduces the Account Balance panel SAP shows on a sales
document, so the operator raising an invoice can see the customer's credit
position before posting rather than after SAP refuses it. Read the docstring
there before changing which OCRD columns it uses — two of them are traps.
"""

import logging
from decimal import Decimal, InvalidOperation
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

    def get_credit_status(self, card_code: str) -> Optional[dict]:
        """One customer's credit position, the way SAP's own panel states it.

        Four numbers straight off OCRD, and SAP treats all four as live:

        * ``CreditLine`` — the credit limit.
        * ``Balance``    — what the account owes now (posted, unpaid A/R).
        * ``OrdersBal``  — open sales orders, ordered but not yet delivered.
        * ``DNotesBal``  — open delivery notes, delivered but not yet invoiced.

        ``exposure`` sums the last three, which is the total SAP's own credit
        check weighs against the limit, and ``available`` is what is left of the
        limit after it. Both are computed here rather than in the frontend so the
        number on the screen and the number in a log can never disagree.

        **``CreditLine = 0`` means no limit is set, NOT a zero limit.** Most of
        the master is in that state (905 of 1,184 Oil customers, 850 of 944 Mart,
        872 of 1,272 Beverages), so ``has_credit_limit`` says which it is and the
        caller must not render an unset limit as "₹0" — that reads as "blocked"
        on a customer SAP is perfectly happy to invoice.

        **``DebtLine`` is deliberately not read.** It is SAP's *commitment* limit
        and it looks inviting, but the master data is junk: 1,097 of Oil's 1,184
        customers carry exactly ``10.00``. Showing it would put a ₹10 limit next
        to a ₹12,000,000 balance on nearly every screen.

        Nothing here blocks anything. It is what the operator needs in order to
        decide, and SAP still runs its own check at posting.
        """
        card_code = (card_code or "").strip()
        if not card_code:
            return None
        rows = self._fetch(
            f"""
            SELECT
                "CardCode",
                IFNULL("CardName", ''),
                IFNULL("CreditLine", 0),
                IFNULL("Balance", 0),
                IFNULL("OrdersBal", 0),
                IFNULL("DNotesBal", 0),
                IFNULL("validFor", 'Y'),
                IFNULL("frozenFor", 'N')
            FROM "{self.connection.schema}"."OCRD"
            WHERE "CardType" = 'C' AND "CardCode" = ?
            """,
            (card_code,),
        )
        if not rows:
            return None

        row = rows[0]
        credit_limit = self._decimal(row[2])
        balance = self._decimal(row[3])
        open_orders = self._decimal(row[4])
        open_deliveries = self._decimal(row[5])
        exposure = balance + open_orders + open_deliveries
        has_limit = credit_limit > 0

        return {
            "customer_code": row[0],
            "customer_name": row[1] or row[0],
            "credit_limit": credit_limit,
            "has_credit_limit": has_limit,
            "balance": balance,
            "open_orders": open_orders,
            "open_deliveries": open_deliveries,
            "exposure": exposure,
            # Meaningless without a limit — None rather than a misleading number.
            "available": (credit_limit - exposure) if has_limit else None,
            "over_limit": bool(has_limit and exposure > credit_limit),
            # A frozen or inactive account is the other reason a post will be
            # refused, and the operator is looking right at this panel already.
            "is_active": row[6] == "Y",
            "is_frozen": row[7] == "Y",
        }

    @staticmethod
    def _decimal(value) -> Decimal:
        if value is None:
            return Decimal("0")
        try:
            return Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            return Decimal("0")

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
