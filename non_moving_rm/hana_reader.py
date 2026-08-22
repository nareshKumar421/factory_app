"""
non_moving_rm/hana_reader.py

Executes SAP HANA queries for the Non-Moving Raw Material Dashboard.
Reads the SAP procedure output used by the Non-Moving workbook.
"""

import logging
from typing import Dict, List, Optional

from hdbcli import dbapi

from sap_client.hana.connection import HanaConnection
from sap_client.exceptions import SAPConnectionError, SAPDataError

logger = logging.getLogger(__name__)


COMPANY_BRANCH_LABELS = {
    "JIVO_OIL": "OIL",
    "JIVO_MART": "MART",
    "JIVO_BEVERAGES": "BEV",
}


class HanaNonMovingRMReader:
    """
    Reads non-moving raw material data from SAP HANA.

    Provides two queries:
      1. get_non_moving_report() - calls REPORT_BP_NON_MOVING_RM
      2. get_item_groups()       - reads item groups from OITB for dropdown
    """

    def __init__(self, context, schema_override: Optional[str] = None):
        self.connection = HanaConnection(context.hana)
        self.schema_override = schema_override
        company_code = getattr(context, "company_code", "")
        self.company_code = company_code if isinstance(company_code, str) else ""

    # ------------------------------------------------------------------
    # Public Methods
    # ------------------------------------------------------------------

    def get_non_moving_report(self, age: int, item_group: int) -> List[Dict]:
        """
        Reads the non-moving report from the SAP procedure.

        Args:
            age: Number of days since last movement (e.g. 45)
            item_group: Item group code from OITB (e.g. 105), or 0 for all

        Returns:
            List of dicts with non-moving item details.
        """
        query, params = self._build_report_query(age=age, item_group=item_group)
        rows = self._execute(query, params)
        return [self._map_report_row(r) for r in rows]

    def _build_report_query(self, age: int, item_group: int):
        schema = self._schema()
        return f'CALL "{schema}"."REPORT_BP_NON_MOVING_RM"(?, ?)', [age, item_group or 0]

    def get_item_groups(self) -> List[Dict]:
        """
        Reads all item groups from OITB table for the dropdown filter.

        Returns:
            List of dicts with ItmsGrpCod and ItmsGrpNam.
        """
        schema = self._schema()
        query = f'SELECT "ItmsGrpCod", "ItmsGrpNam" FROM "{schema}"."OITB" ORDER BY "ItmsGrpNam"'
        rows = self._execute(query, [])
        return [self._map_item_group_row(r) for r in rows]

    def get_stock_age_report(
        self,
        *,
        age: int,
        item_group: int,
        branch_label: str,
    ) -> List[Dict]:
        """
        Reads non-moving stock directly from the selected company schema.

        This fills gaps where the central REPORT_BP_NON_MOVING_RM procedure
        does not return rows for a company/item-group combination.
        """
        query, params = self._build_stock_age_query(
            age=age,
            item_group=item_group,
            branch_label=branch_label,
        )
        rows = self._execute(query, params)
        return [self._map_report_row(r) for r in rows]

    def get_warehouse_distribution(self, item_codes: List[str]) -> List[Dict]:
        """
        Reads current warehouse-level stock for the given item codes.
        """
        unique_codes = sorted({code for code in item_codes if code})
        if not unique_codes:
            return []

        rows = []
        for chunk in self._chunks(unique_codes, 200):
            query, params = self._build_warehouse_distribution_query(chunk)
            rows.extend(self._execute(query, params))
        return [self._map_warehouse_distribution_row(r) for r in rows]

    def _build_stock_age_query(
        self,
        *,
        age: int,
        item_group: int,
        branch_label: str,
    ):
        schema = self._schema()
        query = f"""
WITH StockItems AS (
    SELECT
        M."ItemCode",
        M."ItemName",
        G."ItmsGrpNam" AS "ItemGroupName",
        COALESCE(M."U_Sub_Group", '') AS "SubGroup",
        COALESCE(M."U_IsLitre", 'N') AS "IsLitre",
        COALESCE(M."SalPackUn", 0) AS "LitresPerUnit",
        M."CreateDate",
        M."AvgPrice" AS "ItemAvgPrice",
        M."LastPurPrc",
        W."WhsCode",
        W."OnHand",
        W."AvgPrice" AS "WarehouseAvgPrice"
    FROM "{schema}"."OITW" W
    INNER JOIN "{schema}"."OITM" M
        ON M."ItemCode" = W."ItemCode"
    INNER JOIN "{schema}"."OITB" G
        ON G."ItmsGrpCod" = M."ItmsGrpCod"
    INNER JOIN "{schema}"."OWHS" H
        ON H."WhsCode" = W."WhsCode"
    WHERE W."OnHand" > 0
      AND COALESCE(H."Inactive", 'N') <> 'Y'
      AND G."ItmsGrpCod" = ?
),
LastMovement AS (
    SELECT
        N."ItemCode",
        MAX(N."DocDate") AS "LastMovementDate"
    FROM "{schema}"."OINM" N
    INNER JOIN StockItems S
        ON S."ItemCode" = N."ItemCode"
       AND S."WhsCode" = N."Warehouse"
    WHERE COALESCE(N."InQty", 0) <> 0
       OR COALESCE(N."OutQty", 0) <> 0
    GROUP BY N."ItemCode"
),
ReportRows AS (
    SELECT
        ? AS "Branch",
        S."ItemCode",
        S."ItemName",
        S."ItemGroupName",
        SUM(S."OnHand") AS "Quantity",
        SUM(
            CASE WHEN S."IsLitre" = 'Y'
                 THEN S."OnHand" * S."LitresPerUnit"
                 ELSE 0 END
        ) AS "Litres",
        S."SubGroup",
        ROUND(
            SUM(
                S."OnHand" *
                CASE
                    WHEN COALESCE(S."WarehouseAvgPrice", 0) <> 0 THEN S."WarehouseAvgPrice"
                    WHEN COALESCE(S."ItemAvgPrice", 0) <> 0 THEN S."ItemAvgPrice"
                    ELSE COALESCE(S."LastPurPrc", 0)
                END
            ),
            4
        ) AS "Value",
        MAX(COALESCE(L."LastMovementDate", S."CreateDate")) AS "LastMovementDate"
    FROM StockItems S
    LEFT JOIN LastMovement L
        ON L."ItemCode" = S."ItemCode"
    GROUP BY
        S."ItemCode",
        S."ItemName",
        S."ItemGroupName",
        S."SubGroup"
)
SELECT
    "Branch",
    "ItemCode",
    "ItemName",
    "ItemGroupName",
    "Quantity",
    "Litres",
    "SubGroup",
    "Value",
    "LastMovementDate",
    CASE
        WHEN "LastMovementDate" IS NULL THEN 0
        ELSE DAYS_BETWEEN("LastMovementDate", CURRENT_DATE)
    END AS "DaysSinceLastMovement",
    0 AS "ConsumptionRatio"
FROM ReportRows
WHERE ? <= 0
   OR CASE
          WHEN "LastMovementDate" IS NULL THEN 0
          ELSE DAYS_BETWEEN("LastMovementDate", CURRENT_DATE)
      END > ?
ORDER BY "DaysSinceLastMovement" DESC, "ItemCode"
"""
        return query, [item_group, branch_label, age, age]

    def _build_warehouse_distribution_query(self, item_codes: List[str]):
        schema = self._schema()
        placeholders = ", ".join("?" for _ in item_codes)
        query = f"""
SELECT
    T1."ItemCode",
    T1."WhsCode",
    COALESCE(T5."WhsName", T1."WhsCode") AS "WhsName",
    SUM(COALESCE(T1."OnHand", 0)) AS "Quantity"
FROM "{schema}"."OITW" T1
LEFT JOIN "{schema}"."OWHS" T5
    ON T5."WhsCode" = T1."WhsCode"
WHERE T1."ItemCode" IN ({placeholders})
  AND COALESCE(T5."Inactive", 'N') <> 'Y'
GROUP BY T1."ItemCode", T1."WhsCode", COALESCE(T5."WhsName", T1."WhsCode")
HAVING SUM(COALESCE(T1."OnHand", 0)) <> 0
ORDER BY T1."WhsCode", T1."ItemCode"
"""
        return query, item_codes

    def _schema(self) -> str:
        return self.schema_override or self.connection.schema

    @staticmethod
    def _chunks(values: List[str], size: int):
        for index in range(0, len(values), size):
            yield values[index:index + size]

    # ------------------------------------------------------------------
    # Row Mappers
    # ------------------------------------------------------------------

    def _map_report_row(self, row) -> Dict:
        return {
            "branch": row[0] or "",
            "item_code": row[1] or "",
            "item_name": row[2] or "",
            "item_group_name": row[3] or "",
            "quantity": float(row[4] or 0),
            "litres": float(row[5] or 0),
            "sub_group": row[6] or "",
            "warehouse": "",
            "value": float(row[7] or 0),
            "last_movement_date": row[8].strftime("%Y-%m-%d %H:%M:%S") if row[8] else None,
            "days_since_last_movement": int(row[9] or 0),
            "consumption_ratio": float(row[10] or 0),
        }

    def _map_item_group_row(self, row) -> Dict:
        return {
            "item_group_code": int(row[0]),
            "item_group_name": row[1] or "",
        }

    def _map_warehouse_distribution_row(self, row) -> Dict:
        return {
            "item_code": row[0] or "",
            "warehouse": row[1] or "",
            "warehouse_name": row[2] or row[1] or "",
            "quantity": float(row[3] or 0),
        }

    # ------------------------------------------------------------------
    # Execution Helper
    # ------------------------------------------------------------------

    def _execute(self, query: str, params: List) -> List:
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
            cursor.execute(query, params)
            return cursor.fetchall()

        except dbapi.ProgrammingError as e:
            logger.error(f"SAP HANA query error in non-moving RM: {e}")
            raise SAPDataError(
                "Failed to retrieve non-moving RM data from SAP. Invalid query."
            ) from e
        except dbapi.Error as e:
            logger.error(f"SAP HANA data error in non-moving RM: {e}")
            raise SAPDataError(
                "Failed to retrieve non-moving RM data from SAP. Please try again."
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
