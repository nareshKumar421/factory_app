"""SAP HANA reader for production / material / wastage reconciliation.

Reads SAP inventory movements (OINM) so the app's own records can be compared
against SAP:

* **FG produced** into the finished-goods warehouse (default ``BH-PF``) =
  inward ``InQty`` with ``TransType`` = **59** (Goods Receipt) — this is the
  finished-goods receipt from production.
* **RM/PM issued** to production = inward ``InQty`` with ``TransType`` =
  **67** (Stock Transfer) into the production/packing warehouse (default
  ``BH-PC``). SAP users approve the BOM by transferring the raw/packing
  material *into* BH-PC, so the approved BOM lands as an inward transfer.
* **Wastage** transferred into the wastage warehouse (default ``BH-WST``) =
  inward ``InQty`` with ``TransType`` = **67** (Stock Transfer). Scrap/rejected
  packing material is moved *into* BH-WST from the line, so it lands as an
  inward transfer, not a goods issue out.

Parameterised ``HanaConnection`` pattern (only fixed internal literals — the
InQty/OutQty column name — are interpolated; all values are bound params).
"""

import logging
from typing import Any, Dict, List, Optional, Sequence

from hdbcli import dbapi

from sap_client.exceptions import SAPConnectionError, SAPDataError
from sap_client.hana.connection import HanaConnection

logger = logging.getLogger(__name__)

FG_TRANS_TYPES: Sequence[int] = (59,)  # Goods Receipt (FG produced)
MATERIAL_TRANS_TYPES: Sequence[int] = (67,)  # Stock Transfer (BOM moved into BH-PC)
WASTE_TRANS_TYPES: Sequence[int] = (67,)  # Stock Transfer (scrap moved into BH-WST)


class ReconciliationReader:
    """Reads SAP OINM movements for one company."""

    def __init__(self, context):
        self.connection = HanaConnection(context.hana)

    # FG produced (InQty, TransType 59, into the FG warehouse) --------
    def fg_by_item(self, warehouse, date_from, date_to) -> List[Dict[str, Any]]:
        return self._by_item(date_from, date_to, FG_TRANS_TYPES, "in", warehouse)

    # RM/PM issued (InQty, TransType 67, into the production warehouse BH-PC)
    def material_issues_by_item(
        self, date_from, date_to, warehouse: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        return self._by_item(date_from, date_to, MATERIAL_TRANS_TYPES, "in", warehouse)

    # Wastage transferred in (InQty, TransType 67, into the wastage warehouse)
    def wastage_by_item(self, warehouse, date_from, date_to) -> List[Dict[str, Any]]:
        return self._by_item(date_from, date_to, WASTE_TRANS_TYPES, "in", warehouse)

    # Litres per piece, from the item master ---------------------------
    def litre_items(self) -> List[Dict[str, Any]]:
        """Every item SAP holds a volume for: ``{item_code, item_name, litres}``.

        ``OITM.SalPackUn`` is the litres in one piece — a 5 LTR tin reads 5, a
        250 ML bottle 0.25 — and it is the single source of litres across the
        app. ``U_IsLitre`` is the gate: cartons, caps and preforms carry a
        SalPackUn too, so without it a pallet of preforms would report litres.

        Returned whole (a few hundred rows a company) rather than looked up per
        code, because the reconciliation matches app SKUs by NAME as well: a run
        whose FG receipt has not reached SAP yet has no item code to key on.
        """
        schema = self.connection.schema
        query = f"""
SELECT
    I."ItemCode",
    COALESCE(I."ItemName", '') AS "ItemName",
    I."SalPackUn"
FROM "{schema}"."OITM" I
WHERE UPPER(COALESCE(I."U_IsLitre", 'N')) = 'Y'
  AND COALESCE(I."SalPackUn", 0) > 0
"""
        return [
            {
                "item_code": row[0] or "",
                "item_name": row[1] or "",
                "litres_per_piece": float(row[2] or 0),
            }
            for row in self._execute(query, [])
        ]

    # -- internal --------------------------------------------------------
    def _by_item(self, date_from, date_to, trans_types, direction, warehouse):
        schema = self.connection.schema
        qty_col = "InQty" if direction == "in" else "OutQty"  # fixed internal literal
        placeholders = ", ".join("?" for _ in trans_types)

        clauses = ['O."DocDate" >= ?', 'O."DocDate" <= ?', f'O."TransType" IN ({placeholders})']
        params: List[Any] = [date_from, date_to, *trans_types]
        if warehouse:
            clauses.append('O."Warehouse" = ?')
            params.append(warehouse)
        where = " AND ".join(clauses)

        query = f"""
SELECT
    O."ItemCode",
    COALESCE(I."ItemName", '') AS "ItemName",
    ROUND(COALESCE(SUM(O."{qty_col}"), 0), 3) AS "Qty"
FROM "{schema}"."OINM" O
LEFT JOIN "{schema}"."OITM" I
    ON I."ItemCode" = O."ItemCode"
WHERE {where}
GROUP BY O."ItemCode", I."ItemName"
HAVING ROUND(COALESCE(SUM(O."{qty_col}"), 0), 3) <> 0
ORDER BY "Qty" DESC
"""
        rows = self._execute(query, params)
        return [
            {
                "item_code": row[0] or "",
                "item_name": row[1] or "",
                "sap_qty": float(row[2] or 0),
            }
            for row in rows
        ]

    def _execute(self, query: str, params: List[Any]) -> List:
        conn = None
        cursor = None
        try:
            conn = self.connection.connect()
        except dbapi.Error as e:
            logger.error("SAP HANA connection failed for reconciliation: %s", e)
            raise SAPConnectionError(
                "Unable to connect to SAP HANA. Please try again later."
            ) from e
        try:
            cursor = conn.cursor()
            cursor.execute(query, params)
            return cursor.fetchall()
        except dbapi.ProgrammingError as e:
            logger.error("SAP HANA query error in reconciliation: %s", e)
            raise SAPDataError(
                "Failed to retrieve reconciliation data from SAP. Invalid query."
            ) from e
        except dbapi.Error as e:
            logger.error("SAP HANA data error in reconciliation: %s", e)
            raise SAPDataError(
                "Failed to retrieve reconciliation data from SAP. Please try again."
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
