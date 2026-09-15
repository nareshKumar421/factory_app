"""
admin_board/hana_reader.py

The SAP reads this board adds. Everything else on it comes from an existing
service.

Each is here because no endpoint answered it at the shape a wall board needs:
month-to-date output as one aggregate rather than a page of movements, the
production plan's own total, combined dispatch across two company schemas, and
stock tonnage for a named handful of warehouses.

Column names are the ones already in use in this codebase — ``Warehouse``,
``InQty``, ``DocDate``, ``TransType``, ``ItemCode`` on ``OINM``; ``OnHand`` and
``AvgPrice`` on ``OITW`` — not guessed.

TWO TRAPS THAT MADE THE FIRST DRAFT WRONG
-----------------------------------------
**The litre gate is load-bearing, not decorative.** ``OITM.SalPackUn`` is
populated for the *whole* item master, so without the ``U_IsLitre`` test a
movement of 100,000 preforms reports as 100,000 litres and, under this board's
tonnage rule, as 100 tons. On BH-BT the gate is worth about 9 tonnes of
non-litre stock that would otherwise be counted as oil.

**The production plan is NOT ``OWOR``, and its columns are not the standard
document shape.** It lives in ``OFCT``/``FCT1``, keyed ``AbsID`` — there is no
``DocEntry`` and no ``PlannedQty``; the line quantity is plain ``Quantity`` and
the line date is plain ``Date``. A query written to the usual SAP document
pattern fails outright rather than returning nothing, which is the better of
the two failures but still a surprise.
"""

import logging
from typing import Any, Dict, List, Optional

from hdbcli import dbapi

from sap_client.exceptions import SAPConnectionError, SAPDataError
from sap_client.hana.connection import HanaConnection

from .constants import TRANS_TYPE_PRODUCTION_RECEIPT

logger = logging.getLogger(__name__)


#: Litres in one transacted piece, gated on the item being a litre item at all.
#:
#: Expects ``M`` = OITM. Identical to ``plant_board``'s, deliberately: two
#: boards computing tonnage two ways is how the same warehouse ends up with two
#: different weights on two screens in the same room.
LITRES_PER_UNIT = """
    CASE
        WHEN UPPER(IFNULL(M."U_IsLitre", 'N')) = 'Y' THEN IFNULL(M."SalPackUn", 0)
        ELSE 0
    END
"""


class AdminBoardReader:
    """The board's own SAP reads, for one company."""

    def __init__(self, context):
        self.connection = HanaConnection(context.hana)

    @property
    def schema(self) -> str:
        return self.connection.schema

    # ------------------------------------------------------------------
    # Production
    # ------------------------------------------------------------------

    def production(self, date_from, date_to) -> Dict[str, Any]:
        """Output received onto the finished floor, by day.

        One row per day that produced. Days with no production are simply
        absent, which is what makes the "average per producing day" definition
        computable — the caller counts the rows rather than the calendar.

        Aggregated in SAP rather than paged out and summed here: a wall board
        re-reads this every minute and the movement table is the largest in the
        schema.
        """
        query = f"""
            SELECT
                N."DocDate" AS "Day",
                SUM(N."InQty") AS "Pieces",
                SUM(N."InQty" * {LITRES_PER_UNIT}) AS "Litres"
            FROM "{self.schema}"."OINM" N
            JOIN "{self.schema}"."OITM" M ON M."ItemCode" = N."ItemCode"
            WHERE N."TransType" = ?
              AND N."Warehouse" = ?
              AND N."DocDate" >= ?
              AND N."DocDate" <= ?
            GROUP BY N."DocDate"
            ORDER BY N."DocDate"
        """
        from .constants import PRODUCTION_FLOOR

        return {
            "days": self._rows(
                query,
                [TRANS_TYPE_PRODUCTION_RECEIPT, PRODUCTION_FLOOR, date_from, date_to],
            )
        }

    def production_plan(self, date_from, date_to) -> Optional[Dict[str, Any]]:
        """The month's production plan, in litres, or None if none is filed.

        ``OFCT``/``FCT1``, keyed ``AbsID``. See the module docstring for why
        that matters.

        THE WHOLE MONTH IS DATED TO DAY ONE. Every line of the live September
        plan carries the first of the month, so there is no daily phasing to
        read and a "plan to date" cannot be summed by date — the caller
        pro-rates by elapsed days instead. Returning the plan's own window lets
        it do that without assuming a calendar month.
        """
        query = f"""
            SELECT
                H."AbsID" AS "PlanId",
                MIN(H."Name") AS "Name",
                MIN(H."StartDate") AS "StartDate",
                MAX(H."EndDate") AS "EndDate",
                SUM(L."Quantity") AS "Pieces",
                SUM(L."Quantity" * {LITRES_PER_UNIT}) AS "Litres"
            FROM "{self.schema}"."OFCT" H
            JOIN "{self.schema}"."FCT1" L ON L."AbsID" = H."AbsID"
            LEFT JOIN "{self.schema}"."OITM" M ON M."ItemCode" = L."ItemCode"
            WHERE L."Date" >= ? AND L."Date" <= ?
            GROUP BY H."AbsID"
            ORDER BY H."AbsID" DESC
        """
        rows = self._rows(query, [date_from, date_to])
        return rows[0] if rows else None

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    def dispatch(self, date_from, date_to, exclude_card_codes) -> Dict[str, Any]:
        """Invoiced volume by day, for one company.

        Cancelled invoices are dropped on the UPPER-CASE ``CANCELED`` flag,
        which is how ``OINV`` spells it — ``ORCT``/``OVPM`` spell the same flag
        ``Canceled``, and mixing the two up returns everything.

        Intercompany customers are excluded here rather than by the caller, so
        no call site can forget: this figure is summed with another company's
        and a group-internal transfer would otherwise be counted on both sides.
        """
        placeholders = ", ".join(["?"] * len(exclude_card_codes)) or "''"
        query = f"""
            SELECT
                H."DocDate" AS "Day",
                COUNT(DISTINCT H."DocEntry") AS "Bills",
                SUM(L."Quantity" * {LITRES_PER_UNIT}) AS "Litres"
            FROM "{self.schema}"."OINV" H
            JOIN "{self.schema}"."INV1" L ON L."DocEntry" = H."DocEntry"
            LEFT JOIN "{self.schema}"."OITM" M ON M."ItemCode" = L."ItemCode"
            WHERE H."CANCELED" = 'N'
              AND H."DocDate" >= ?
              AND H."DocDate" <= ?
              AND H."CardCode" NOT IN ({placeholders})
            GROUP BY H."DocDate"
            ORDER BY H."DocDate"
        """
        params = [date_from, date_to, *exclude_card_codes]
        return {"days": self._rows(query, params)}

    # ------------------------------------------------------------------
    # Stock
    # ------------------------------------------------------------------

    def warehouse_stock(self, warehouses) -> List[Dict[str, Any]]:
        """Tonnage, pieces and value on hand, per warehouse.

        Rows holding no stock are excluded, which matters for value: 68
        zero-quantity rows on the finished floor carry a combined negative
        seventeen lakh, so summing every ledger row values a warehouse well
        below what is actually standing in it.
        """
        if not warehouses:
            return []
        placeholders = ", ".join(["?"] * len(warehouses))
        query = f"""
            SELECT
                T."WhsCode" AS "Warehouse",
                COUNT(*) AS "Skus",
                SUM(T."OnHand") AS "Pieces",
                SUM(T."OnHand" * {LITRES_PER_UNIT}) AS "Litres",
                SUM(T."OnHand" * IFNULL(T."AvgPrice", 0)) AS "Value"
            FROM "{self.schema}"."OITW" T
            JOIN "{self.schema}"."OITM" M ON M."ItemCode" = T."ItemCode"
            WHERE T."WhsCode" IN ({placeholders})
              AND T."OnHand" <> 0
            GROUP BY T."WhsCode"
        """
        return self._rows(query, list(warehouses))

    def tank_contents(self, warehouse: str, limit: int = 4) -> List[Dict[str, Any]]:
        """What is in the tanks, largest first.

        Named varieties rather than a bare total, because "869 tonnes of oil" is
        not an answer anybody can act on — which oil is the question that
        follows it.
        """
        query = f"""
            SELECT
                M."ItemName" AS "Item",
                SUM(T."OnHand" * {LITRES_PER_UNIT}) AS "Litres"
            FROM "{self.schema}"."OITW" T
            JOIN "{self.schema}"."OITM" M ON M."ItemCode" = T."ItemCode"
            WHERE T."WhsCode" = ?
              AND T."OnHand" <> 0
            GROUP BY M."ItemName"
            ORDER BY 2 DESC
        """
        return self._rows(query, [warehouse])[:limit]

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
            logger.error("SAP HANA connection failed (admin_board): %s", exc)
            raise SAPConnectionError(
                "Unable to connect to SAP HANA. Please try again later."
            ) from exc

        try:
            cursor = conn.cursor()
            cursor.execute(query, params or [])
            columns = [c[0] for c in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]
        except dbapi.Error as exc:
            logger.error("SAP HANA query failed (admin_board): %s", exc)
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
