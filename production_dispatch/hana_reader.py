"""
production_dispatch/hana_reader.py

The report's SAP reads, from Oil's schema: finished goods made and finished
goods sold.

WHAT COUNTS, AND WHY
--------------------
These are the workbook's own definitions. On 2026-10-09 these queries were
checked against its Data sheet for Jul-Sep 2026: every item and month of
production and dispatch agreed to the unit.

* **Items.** Finished goods (``FG*``) that are litre items (``U_IsLitre``).
  ``SalPackUn`` populates the whole item master, caps and cartons included, so
  without the gate those would be counted as oil.
* **Production** is the receipt from production (``OIGN``/``IGN1``) against a
  STANDARD production order (``OWOR."Type" = 'S'``). Disassembly and special
  orders are not output.
* **Dispatch** is every delivery (``ODLN``) and every A/R invoice (``OINV``)
  line NOT copied from a delivery (``BaseType`` 15), so a delivered-then-billed
  line is counted once, on its delivery. Item documents only, no reserve
  invoices (``isIns``): a reserve invoice moves no stock, its delivery does.
  Stock transfers to the company's own depots are not dispatch; a depot's
  sale later is.
Returns are not read: the workbook carried them for reference only, never
deducted, and the page shows only production and dispatch.

Cancelled documents are left out on ``CANCELED = 'N'``, which also leaves out
the cancellation documents (``'C'``).

Every line says whether its customer is a group company, rather than the
query dropping them, so the page can say how much it left out.
"""

import logging
from datetime import date
from typing import Any, Dict, List, Optional, Sequence

from hdbcli import dbapi

from sap_client.exceptions import SAPConnectionError, SAPDataError
from sap_client.hana.connection import HanaConnection

logger = logging.getLogger(__name__)

PRODUCTION = "PRODUCTION"
DISPATCH = "DISPATCH"


def _placeholders(values: Sequence[Any]) -> str:
    """``?, ?, ?`` for an IN list, or a list that matches nothing when empty."""
    return ", ".join(["?"] * len(values)) or "''"


#: Finished litre goods, on ``M`` = OITM.
_FG_LITRE_ITEM = """
    L."ItemCode" LIKE 'FG%'
    AND UPPER(IFNULL(M."U_IsLitre", 'N')) = 'Y'
"""


class ProductionDispatchReader:
    """Oil's finished goods in and out, for one schema."""

    def __init__(self, context):
        self.connection = HanaConnection(context.hana)

    @property
    def schema(self) -> str:
        return self.connection.schema

    # ------------------------------------------------------------------
    # Items
    # ------------------------------------------------------------------

    def items(self) -> List[Dict[str, Any]]:
        """Every finished litre item, with what the report groups and converts by.

        The workbook's labels are SAP's columns crossed over, and kept that way
        so the page reads like the sheet: VARIETY is ``U_Sub_Group`` (CANOLA,
        OLIVE...) and SUBGROUP is ``U_Variety`` (COLD PRESS, POMACE...).
        """
        query = f"""
            SELECT
                M."ItemCode" AS "ItemCode",
                IFNULL(M."ItemName", '') AS "ItemName",
                IFNULL(M."U_Sub_Group", '') AS "Variety",
                IFNULL(M."U_Variety", '') AS "Subgroup",
                IFNULL(M."U_SKU", '') AS "Sku",
                IFNULL(M."U_Packing_Type", '') AS "PackingType",
                IFNULL(M."SalFactor2", 0) AS "PiecesPerBox",
                IFNULL(M."SalPackUn", 0) AS "LitresPerUnit"
            FROM "{self.schema}"."OITM" M
            WHERE M."ItemCode" LIKE 'FG%'
              AND UPPER(IFNULL(M."U_IsLitre", 'N')) = 'Y'
        """
        return self._rows(query)

    # ------------------------------------------------------------------
    # Movements
    # ------------------------------------------------------------------

    def daily(
        self, date_from: date, date_to: date, group_card_codes: Sequence[str]
    ) -> List[Dict[str, Any]]:
        """Production and dispatch per day and item, dispatch split by group company or not.

        Aggregated in SAP: a quarter is about 4,500 document lines but well
        under half that many day-item pairs.
        """
        codes = list(group_card_codes)
        group = f"""CASE WHEN H."CardCode" IN ({_placeholders(codes)}) THEN 1 ELSE 0 END"""
        query = f"""
            SELECT "Kind", "Day", "ItemCode", "IsGroup", SUM("Qty") AS "Qty"
            FROM (
                SELECT '{PRODUCTION}' AS "Kind", H."DocDate" AS "Day", L."ItemCode",
                       0 AS "IsGroup", L."Quantity" AS "Qty"
                FROM "{self.schema}"."OIGN" H
                JOIN "{self.schema}"."IGN1" L ON L."DocEntry" = H."DocEntry"
                JOIN "{self.schema}"."OWOR" W ON W."DocEntry" = L."BaseEntry" AND L."BaseType" = 202
                JOIN "{self.schema}"."OITM" M ON M."ItemCode" = L."ItemCode"
                WHERE H."DocDate" BETWEEN ? AND ?
                  AND H."CANCELED" = 'N'
                  AND W."Type" = 'S'
                  AND {_FG_LITRE_ITEM}

                UNION ALL

                SELECT '{DISPATCH}', H."DocDate", L."ItemCode", {group}, L."Quantity"
                FROM "{self.schema}"."ODLN" H
                JOIN "{self.schema}"."DLN1" L ON L."DocEntry" = H."DocEntry"
                JOIN "{self.schema}"."OITM" M ON M."ItemCode" = L."ItemCode"
                WHERE H."DocDate" BETWEEN ? AND ?
                  AND H."CANCELED" = 'N'
                  AND H."DocType" = 'I'
                  AND {_FG_LITRE_ITEM}

                UNION ALL

                SELECT '{DISPATCH}', H."DocDate", L."ItemCode", {group}, L."Quantity"
                FROM "{self.schema}"."OINV" H
                JOIN "{self.schema}"."INV1" L ON L."DocEntry" = H."DocEntry"
                JOIN "{self.schema}"."OITM" M ON M."ItemCode" = L."ItemCode"
                WHERE H."DocDate" BETWEEN ? AND ?
                  AND H."CANCELED" = 'N'
                  AND H."DocType" = 'I'
                  AND H."isIns" = 'N'
                  AND IFNULL(L."BaseType", -1) <> 15
                  AND {_FG_LITRE_ITEM}
            )
            GROUP BY "Kind", "Day", "ItemCode", "IsGroup"
        """
        span = [date_from, date_to]
        params = [
            *span,
            *codes, *span,
            *codes, *span,
        ]
        return self._rows(query, params)

    def documents(
        self,
        date_from: date,
        date_to: date,
        group_card_codes: Sequence[str],
        item_code: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """The document lines behind :meth:`daily`, one row each.

        Production and dispatch only -- the two the report adds up. The same
        filters as :meth:`daily`, line for line, so the lines always sum to it.
        """
        codes = list(group_card_codes)
        group = f"""CASE WHEN H."CardCode" IN ({_placeholders(codes)}) THEN 1 ELSE 0 END"""
        item_filter = 'AND L."ItemCode" = ?' if item_code else ""
        columns = """
            H."DocDate" AS "Day", H."DocNum" AS "DocNum",
            L."ItemCode" AS "ItemCode", IFNULL(L."Dscription", '') AS "ItemName",
            IFNULL(L."WhsCode", '') AS "Warehouse", L."Quantity" AS "Qty"
        """
        query = f"""
            SELECT * FROM (
                SELECT '{PRODUCTION}' AS "Kind", 'Receipt from Production' AS "DocType",
                       {columns}, '' AS "CardCode", '' AS "CardName", 0 AS "IsGroup",
                       L."LineNum" AS "LineNum"
                FROM "{self.schema}"."OIGN" H
                JOIN "{self.schema}"."IGN1" L ON L."DocEntry" = H."DocEntry"
                JOIN "{self.schema}"."OWOR" W ON W."DocEntry" = L."BaseEntry" AND L."BaseType" = 202
                JOIN "{self.schema}"."OITM" M ON M."ItemCode" = L."ItemCode"
                WHERE H."DocDate" BETWEEN ? AND ?
                  AND H."CANCELED" = 'N'
                  AND W."Type" = 'S'
                  AND {_FG_LITRE_ITEM}
                  {item_filter}

                UNION ALL

                SELECT '{DISPATCH}', 'Delivery', {columns},
                       H."CardCode", IFNULL(H."CardName", ''), {group}, L."LineNum"
                FROM "{self.schema}"."ODLN" H
                JOIN "{self.schema}"."DLN1" L ON L."DocEntry" = H."DocEntry"
                JOIN "{self.schema}"."OITM" M ON M."ItemCode" = L."ItemCode"
                WHERE H."DocDate" BETWEEN ? AND ?
                  AND H."CANCELED" = 'N'
                  AND H."DocType" = 'I'
                  AND {_FG_LITRE_ITEM}
                  {item_filter}

                UNION ALL

                SELECT '{DISPATCH}', 'A/R Invoice', {columns},
                       H."CardCode", IFNULL(H."CardName", ''), {group}, L."LineNum"
                FROM "{self.schema}"."OINV" H
                JOIN "{self.schema}"."INV1" L ON L."DocEntry" = H."DocEntry"
                JOIN "{self.schema}"."OITM" M ON M."ItemCode" = L."ItemCode"
                WHERE H."DocDate" BETWEEN ? AND ?
                  AND H."CANCELED" = 'N'
                  AND H."DocType" = 'I'
                  AND H."isIns" = 'N'
                  AND IFNULL(L."BaseType", -1) <> 15
                  AND {_FG_LITRE_ITEM}
                  {item_filter}
            )
            ORDER BY "Day", "Kind" DESC, "DocNum", "LineNum"
        """
        span = [date_from, date_to]
        item = [item_code] if item_code else []
        params = [
            *span, *item,
            *codes, *span, *item,
            *codes, *span, *item,
        ]
        return self._rows(query, params)

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
            logger.error("SAP HANA connection failed (production_dispatch): %s", exc)
            raise SAPConnectionError(
                "Unable to connect to SAP HANA. Please try again later."
            ) from exc

        try:
            cursor = conn.cursor()
            cursor.execute(query, params or [])
            columns = [c[0] for c in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]
        except dbapi.Error as exc:
            logger.error("SAP HANA query failed (production_dispatch): %s", exc)
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
