"""
amounts_board/hana_reader.py

The board's SAP reads, for one company schema.

STOCK VALUE IS SAP'S OWN
------------------------
``OITW."StockValue"`` -- the per-warehouse value SAP's inventory valuation keeps,
the same column the stock dashboard and the admin board's PM tile sum. Rows with
nothing on hand are left out, as the stock dashboard leaves them out: on live
(2026-10-06) Oil carried Rs 1.05 Cr of NEGATIVE value on packing-material rows
whose quantity was zero -- valuation leftovers, not stock anybody can count. So
this board ties to what is in the godowns, not to the inventory G/L account.

DEBTORS ARE THE LEDGER BALANCE, NOT THE OPEN BILLS
--------------------------------------------------
A customer's ``OCRD."Balance"`` is debits less credits on their account. The
open-invoice route (``DocTotal - PaidToDate`` on OINV) reads several times too
high here, because most receipts are booked on account and never matched to a
bill: on live Oil's open invoices summed to Rs 92.5 Cr against Rs 6.3 Cr
actually owed. Only customers in debit count; one in credit has paid in advance
and is not a debtor.

THE OLDEST DEBT IS FIFO
-----------------------
For the same reason, "the oldest unmatched invoice" is not the oldest debt -- it
is usually a bill paid long ago whose receipt was never matched. Instead the
customer's payments are taken to clear their oldest bills first: walking their
debits newest-first, the ones needed to make up the balance are what is still
owed, and the oldest of those is how long the debt has been standing.
"""

import logging
from typing import Any, Dict, List, Optional, Sequence

from hdbcli import dbapi

from sap_client.exceptions import SAPConnectionError, SAPDataError
from sap_client.hana.connection import HanaConnection

logger = logging.getLogger(__name__)


def _placeholders(values: Sequence[Any]) -> str:
    """``?, ?, ?`` for an IN list, or a list that matches nothing when empty."""
    return ", ".join(["?"] * len(values)) or "''"


class AmountsReader:
    """Stock values and debtors, for one company."""

    def __init__(self, context):
        self.connection = HanaConnection(context.hana)

    @property
    def schema(self) -> str:
        return self.connection.schema

    # ------------------------------------------------------------------
    # Stock
    # ------------------------------------------------------------------

    def stock_by_godown(self, item_groups: Sequence[int]) -> List[Dict[str, Any]]:
        """Value and item count per godown per item group, stock on hand only."""
        groups = list(item_groups)
        query = f"""
            SELECT
                W."WhsCode" AS "Warehouse",
                IFNULL(O."WhsName", '') AS "WarehouseName",
                M."ItmsGrpCod" AS "ItemGroup",
                COUNT(*) AS "Items",
                SUM(W."StockValue") AS "Value"
            FROM "{self.schema}"."OITW" W
            JOIN "{self.schema}"."OITM" M ON M."ItemCode" = W."ItemCode"
            LEFT JOIN "{self.schema}"."OWHS" O ON O."WhsCode" = W."WhsCode"
            WHERE W."OnHand" <> 0
              AND M."ItmsGrpCod" IN ({_placeholders(groups)})
            GROUP BY W."WhsCode", O."WhsName", M."ItmsGrpCod"
        """
        return self._rows(query, groups)

    def godown_items(self, warehouse: str, item_group: int) -> List[Dict[str, Any]]:
        """What one godown holds of one item group, most valuable first."""
        query = f"""
            SELECT
                W."ItemCode" AS "ItemCode",
                IFNULL(M."ItemName", '') AS "ItemName",
                IFNULL(M."InvntryUom", '') AS "Uom",
                W."OnHand" AS "Quantity",
                W."StockValue" AS "Value"
            FROM "{self.schema}"."OITW" W
            JOIN "{self.schema}"."OITM" M ON M."ItemCode" = W."ItemCode"
            WHERE W."WhsCode" = ?
              AND M."ItmsGrpCod" = ?
              AND W."OnHand" <> 0
            ORDER BY W."StockValue" DESC, W."ItemCode"
        """
        return self._rows(query, [warehouse, item_group])

    # ------------------------------------------------------------------
    # Debtors
    # ------------------------------------------------------------------

    def debtor_balances(self, group_card_codes: Sequence[str]) -> List[Dict[str, Any]]:
        """Customers in debit, split into outside customers (C) and group (G).

        Group companies and the company's own branches are returned as their
        own line rather than dropped, so the tile can say how much it left out.
        """
        codes = list(group_card_codes)
        query = f"""
            SELECT "Kind", COUNT(*) AS "Customers", SUM("Balance") AS "Amount"
            FROM (
                SELECT
                    CASE WHEN "CardCode" IN ({_placeholders(codes)}) THEN 'G' ELSE 'C' END AS "Kind",
                    "Balance"
                FROM "{self.schema}"."OCRD"
                WHERE "CardType" = 'C'
                  AND "Balance" > 0
            )
            GROUP BY "Kind"
        """
        return self._rows(query, codes)

    def oldest_debt(self, group_card_codes: Sequence[str], floor: float) -> Optional[Dict[str, Any]]:
        """The longest-standing debt among outside customers owing ``floor`` or more.

        FIFO, as the module docstring explains. Ties on the date go to the
        bigger balance, the one a reader would chase first.
        """
        codes = list(group_card_codes)
        query = f"""
            WITH "BAL" AS (
                SELECT "CardCode", "CardName", "Balance"
                FROM "{self.schema}"."OCRD"
                WHERE "CardType" = 'C'
                  AND "Balance" >= ?
                  AND "CardCode" NOT IN ({_placeholders(codes)})
            ),
            "DEB" AS (
                SELECT
                    J."ShortName" AS "CardCode",
                    J."RefDate",
                    J."Debit",
                    SUM(J."Debit") OVER (
                        PARTITION BY J."ShortName"
                        ORDER BY J."RefDate" DESC, J."TransId" DESC, J."Line_ID" DESC
                    ) AS "Cum"
                FROM "{self.schema}"."JDT1" J
                JOIN "BAL" B ON B."CardCode" = J."ShortName"
                WHERE J."Debit" > 0
            ),
            "SINCE" AS (
                SELECT D."CardCode", MIN(D."RefDate") AS "Since"
                FROM "DEB" D
                JOIN "BAL" B ON B."CardCode" = D."CardCode"
                WHERE D."Cum" - D."Debit" < B."Balance"
                GROUP BY D."CardCode"
            )
            SELECT TOP 1
                S."CardCode" AS "CardCode",
                IFNULL(B."CardName", '') AS "CardName",
                B."Balance" AS "Balance",
                S."Since" AS "Since"
            FROM "SINCE" S
            JOIN "BAL" B ON B."CardCode" = S."CardCode"
            ORDER BY S."Since" ASC, B."Balance" DESC
        """
        rows = self._rows(query, [floor, *codes])
        return rows[0] if rows else None

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def _rows(self, query: str, params: Optional[List[Any]] = None) -> List[Dict[str, Any]]:
        """Run one statement and return dicts. The connection is always closed."""
        conn = None
        cursor = None
        try:
            conn = self.connection.connect()
        except dbapi.Error as exc:
            logger.error("SAP HANA connection failed (amounts_board): %s", exc)
            raise SAPConnectionError(
                "Unable to connect to SAP HANA. Please try again later."
            ) from exc

        try:
            cursor = conn.cursor()
            cursor.execute(query, params or [])
            columns = [c[0] for c in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]
        except dbapi.Error as exc:
            logger.error("SAP HANA query failed (amounts_board): %s", exc)
            raise SAPDataError("SAP rejected this request.") from exc
        finally:
            if cursor is not None:
                try:
                    cursor.close()
                except dbapi.Error:
                    pass
            if conn is not None:
                try:
                    conn.close()
                except dbapi.Error:
                    pass
