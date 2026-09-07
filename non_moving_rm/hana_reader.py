"""
non_moving_rm/hana_reader.py

Executes SAP HANA queries for the Non-Moving Raw Material Dashboard.

The report is built here rather than read out of SAP's
``REPORT_BP_NON_MOVING_RM`` procedure. That procedure lived in the Beverages
schema, answered for all three companies at once, and returned no warehouse —
so the dashboard had to filter its output down to one branch and then guess
each item's warehouse by pro-rating against current stock. It also stopped
answering at all, which is what surfaced as "SAP data error" on the page.

What replaces it is one query per company schema, at the grain the dashboard
actually displays: one row per (item, warehouse).

  - stock       OITW.OnHand, in active warehouses only (OWHS.Inactive)
  - value       OnHand x unit cost, preferring the warehouse's own moving
                average (OITW.AvgPrice), then the item's (OITM.AvgPrice),
                then its last purchase price
  - age         days since the last OINM row that moved quantity in or out of
                THAT warehouse; stock SAP never moved falls back to
                OITM.CreateDate
  - consumption issues (OINM.OutQty) over the trailing 365 days as a percentage
                of what is on hand now, so 0% reads as "nothing left it all year"

Aging per warehouse is deliberate: a label consumed daily at BH-PC while an
identical pallet rots in another store is exactly the stock this dashboard
exists to surface, and item-level aging hides it.
"""

import logging
from typing import Dict, List, Optional, Set

from hdbcli import dbapi

from sap_client.hana.connection import HanaConnection
from sap_client.exceptions import SAPConnectionError, SAPDataError

logger = logging.getLogger(__name__)


COMPANY_BRANCH_LABELS = {
    "JIVO_OIL": "OIL",
    "JIVO_MART": "MART",
    "JIVO_BEVERAGES": "BEV",
}

# Window the consumption ratio is measured over, in days.
CONSUMPTION_WINDOW_DAYS = 365


class HanaNonMovingRMReader:
    """
    Reads non-moving stock data from one company's SAP HANA schema.

    Provides two queries:
      1. get_non_moving_report() - stock by movement age, per item and warehouse
      2. get_item_groups()       - reads item groups from OITB for the dropdown
    """

    def __init__(self, context, schema_override: Optional[str] = None):
        self.connection = HanaConnection(context.hana)
        self.schema_override = schema_override
        company_code = getattr(context, "company_code", "")
        self.company_code = company_code if isinstance(company_code, str) else ""
        self._columns_cache: Dict[str, Set[str]] = {}

    # ------------------------------------------------------------------
    # Public Methods
    # ------------------------------------------------------------------

    def get_non_moving_report(
        self,
        *,
        age: int,
        item_group: int,
        branch_label: str,
    ) -> List[Dict]:
        """
        Reads stock by movement age from the company's own schema.

        Args:
            age: Only return stock untouched for MORE than this many days;
                 0 returns every stocked item/warehouse pair.
            item_group: Item group code from OITB (e.g. 105), or 0 for all groups.
            branch_label: Label stamped on every row (one schema is one branch).

        Returns:
            One dict per (item, warehouse) holding stock.
        """
        query, params = self._build_report_query(age=age, item_group=item_group)
        rows = self._execute(query, params)
        return [self._map_report_row(r, branch_label) for r in rows]

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

    # ------------------------------------------------------------------
    # Query Builders
    # ------------------------------------------------------------------

    def _build_report_query(self, *, age: int, item_group: int):
        schema = self._schema()
        item_columns = self._table_columns("OITM")

        # Both filters are stitched in rather than bound as "? = 0 OR ..." so
        # HANA never has to type a parameter it then ignores.
        params: List = []
        group_filter = ""
        if item_group:
            group_filter = 'AND M."ItmsGrpCod" = ?'
            params.append(item_group)

        age_filter = ""
        if age > 0:
            age_filter = 'WHERE "DaysSinceLastMovement" > ?'
            params.append(age)

        query = f"""
WITH Stock AS (
    SELECT
        M."ItemCode",
        M."ItemName",
        G."ItmsGrpNam" AS "ItemGroupName",
        {self._optional_item_string(item_columns, "U_Sub_Group")} AS "SubGroup",
        M."CreateDate",
        W."WhsCode",
        COALESCE(H."WhsName", W."WhsCode") AS "WhsName",
        COALESCE(W."OnHand", 0) AS "OnHand",
        CASE
            WHEN COALESCE(W."AvgPrice", 0) <> 0 THEN W."AvgPrice"
            WHEN COALESCE(M."AvgPrice", 0) <> 0 THEN M."AvgPrice"
            ELSE COALESCE(M."LastPurPrc", 0)
        END AS "UnitCost",
        {self._litres_per_unit_expr(item_columns)} AS "LitresPerUnit"
    FROM "{schema}"."OITW" W
    INNER JOIN "{schema}"."OITM" M
        ON M."ItemCode" = W."ItemCode"
    INNER JOIN "{schema}"."OITB" G
        ON G."ItmsGrpCod" = M."ItmsGrpCod"
    INNER JOIN "{schema}"."OWHS" H
        ON H."WhsCode" = W."WhsCode"
    WHERE COALESCE(W."OnHand", 0) > 0
      AND COALESCE(H."Inactive", 'N') <> 'Y'
      {group_filter}
),
Movement AS (
    SELECT
        N."ItemCode",
        N."Warehouse",
        MAX(
            CASE
                WHEN COALESCE(N."InQty", 0) <> 0 OR COALESCE(N."OutQty", 0) <> 0
                THEN N."DocDate"
            END
        ) AS "LastMovementDate",
        SUM(
            CASE
                WHEN N."DocDate" >= ADD_DAYS(CURRENT_DATE, -{CONSUMPTION_WINDOW_DAYS})
                THEN COALESCE(N."OutQty", 0)
                ELSE 0
            END
        ) AS "IssuedInWindow"
    FROM "{schema}"."OINM" N
    WHERE N."ItemCode" IN (SELECT "ItemCode" FROM Stock)
    GROUP BY N."ItemCode", N."Warehouse"
),
Report AS (
    SELECT
        S."ItemCode",
        S."ItemName",
        S."ItemGroupName",
        S."OnHand" AS "Quantity",
        ROUND(S."OnHand" * S."LitresPerUnit", 3) AS "Litres",
        S."SubGroup",
        ROUND(S."OnHand" * S."UnitCost", 4) AS "Value",
        COALESCE(V."LastMovementDate", S."CreateDate") AS "LastMovementDate",
        CASE
            WHEN COALESCE(V."LastMovementDate", S."CreateDate") IS NULL THEN 0
            ELSE DAYS_BETWEEN(COALESCE(V."LastMovementDate", S."CreateDate"), CURRENT_DATE)
        END AS "DaysSinceLastMovement",
        ROUND(COALESCE(V."IssuedInWindow", 0) / S."OnHand" * 100, 2) AS "ConsumptionRatio",
        S."WhsCode",
        S."WhsName"
    FROM Stock S
    LEFT JOIN Movement V
        ON V."ItemCode" = S."ItemCode"
       AND V."Warehouse" = S."WhsCode"
)
SELECT
    "ItemCode",
    "ItemName",
    "ItemGroupName",
    "Quantity",
    "Litres",
    "SubGroup",
    "Value",
    "LastMovementDate",
    "DaysSinceLastMovement",
    "ConsumptionRatio",
    "WhsCode",
    "WhsName"
FROM Report
{age_filter}
ORDER BY "DaysSinceLastMovement" DESC, "Value" DESC, "ItemCode", "WhsCode"
"""
        return query, params

    def _schema(self) -> str:
        return self.schema_override or self.connection.schema

    def _table_columns(self, table_name: str) -> Set[str]:
        """Columns SAP actually has on a table in THIS schema.

        The user-defined fields this report reads are not guaranteed to exist
        in every company database, and a missing one would otherwise fail the
        whole query instead of blanking one column.
        """
        key = table_name.upper()
        if key in self._columns_cache:
            return self._columns_cache[key]

        rows = self._execute(
            """
                SELECT "COLUMN_NAME"
                FROM "SYS"."TABLE_COLUMNS"
                WHERE "SCHEMA_NAME" = ? AND "TABLE_NAME" = ?
            """,
            [self._schema(), key],
        )
        columns = {row[0] for row in rows}
        self._columns_cache[key] = columns
        return columns

    @staticmethod
    def _optional_item_string(columns: Set[str], column: str, fallback: str = "") -> str:
        if column not in columns:
            return "'" + fallback + "'"
        return 'COALESCE(TO_NVARCHAR(M."' + column + "\"), '" + fallback + "')"

    @classmethod
    def _litres_per_unit_expr(cls, item_columns: Set[str]) -> str:
        """Litres in one stocked unit, from ``OITM.SalPackUn``.

        ``U_IsLitre`` is the gate: SalPackUn carries a number for cartons, caps
        and labels too, so without the flag a pallet of preforms would report
        its piece count as litres.
        """
        if "SalPackUn" not in item_columns:
            return "0"
        is_litre_expr = cls._optional_item_string(item_columns, "U_IsLitre", "N")
        return f"""CASE
            WHEN UPPER({is_litre_expr}) = 'Y' THEN COALESCE(M."SalPackUn", 0)
            ELSE 0
        END"""

    # ------------------------------------------------------------------
    # Row Mappers
    # ------------------------------------------------------------------

    def _map_report_row(self, row, branch_label: str) -> Dict:
        """Maps one row of the report query.

        ``branch`` is stamped in here rather than selected: a schema is one
        branch, so carrying the same constant down from SAP on every row would
        only cost the query a parameter it has to type.
        """
        return {
            "branch": branch_label or "",
            "item_code": row[0] or "",
            "item_name": row[1] or "",
            "item_group_name": row[2] or "",
            "quantity": float(row[3] or 0),
            "litres": float(row[4] or 0),
            "sub_group": row[5] or "",
            "value": float(row[6] or 0),
            "last_movement_date": row[7].strftime("%Y-%m-%d %H:%M:%S") if row[7] else None,
            "days_since_last_movement": int(row[8] or 0),
            "consumption_ratio": float(row[9] or 0),
            "warehouse": row[10] or "",
            "warehouse_name": row[11] or row[10] or "",
        }

    def _map_item_group_row(self, row) -> Dict:
        return {
            "item_group_code": int(row[0]),
            "item_group_name": row[1] or "",
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
