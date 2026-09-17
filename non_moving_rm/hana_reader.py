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
                OITM.CreateDate. Packing material is aged on production alone
                -- see below.
  - consumption issues (OINM.OutQty) over the trailing 365 days as a percentage
                of what is on hand now, so 0% reads as "nothing left it all year"

Aging per warehouse is deliberate: a label consumed daily at BH-PC while an
identical pallet rots in another store is exactly the stock this dashboard
exists to surface, and item-level aging hides it.

PACKING MATERIAL IS AGED DIFFERENTLY, AND ONLY PACKING MATERIAL
---------------------------------------------------------------
For item group 105 the clock above answers the wrong question. Packaging is
shuffled between godowns constantly -- BH-PM and BH-BS exist only to feed
BH-PC -- and every one of those transfers writes an OINM row, so a pallet
nobody has used in two years reads as "moved 3 days ago" the moment somebody
restacks it. That is the loophole this module now closes.

For packing material, and nothing else:

  - the clock resets only on PRODUCTION -- an issue to a production order
    (``TransType`` 60) or a receipt from one (59, the bottles the factory
    blows itself). Warehouse transfers (67), and every other document type,
    leave the age where it was.
  - it is asked of the ITEM, not of the (item, warehouse) pair. A store that
    only ever feeds the floor issues nothing to production by definition, so
    aging BH-PM's own rows on production would report every carton in the
    feeding stores as dead. "Days since this material was last consumed" is
    the figure the rule asks for, and it is the same number on each warehouse
    row holding that item.
  - stock that has NEVER been consumed falls back to the item's last
    non-transfer movement -- in practice its GRPO -- and only then to
    ``OITM.CreateDate``. Without that, packaging bought last week would come
    back aged from the day somebody first typed the item into SAP. A purchase
    still cannot be faked by moving a pallet: transfers are excluded from the
    fallback too.

The per-warehouse any-movement date is still computed and still returned, as
``last_warehouse_movement_date`` / ``days_since_warehouse_movement``, so the
restack is visible next to the age rather than lost. ``movement_basis`` says
which of the two rules produced the headline figure on every row.

Because that headline date belongs to a movement in a DIFFERENT warehouse than
the row it is printed on, the row also carries the store it happened in:
``last_movement_warehouse`` / ``last_movement_warehouse_name``. Without it the
date is untraceable -- a glass bottle shown against BH-PM as "moved 8 days ago"
was in fact issued to production out of BH-PP, and SAP's own SKU WISE DETAILS
query, which is asked for one warehouse at a time, answers "no rows" for BH-PM
and reads as if the board were wrong. On ordinary items the movement warehouse
is the row's own; it is blank only where the age fell back to ``CreateDate``
and there is no movement to point at.

THE PRODUCTION RULE CAN BE SWITCHED OFF: ``count_production=False``
-------------------------------------------------------------------
Everything above lets a production entry reset the clock -- explicitly for
packing material, and implicitly for everything else, because an issue to a
production order is a movement like any other. That is the right default and
it is what the factory looks at day to day.

It is not the only question worth asking. "We are still eating through the
glass we bought in December, but when did we last actually BUY any?" is a
buying question, not a consumption one, and production answers it wrongly: on
15 September 2026 Beverages' PM0000643 (GLASS BOTTLE 200 MLS NEW, Rs 20.5 L)
read 8 days idle off a production issue dated 7 September, while its last
Goods Receipt PO was 17 December 2025 -- 272 days. The board called it
Recently Moved; the buyer would call it a year's stock.

With ``count_production=False`` NOTHING internal resets the clock. Not
production, not a receipt from production, not a transfer, not an issue. The
only movement that counts is the last GOODS RECEIPT PO, ``TransType`` 20,
asked of the ITEM the way the production rule is -- a purchase lands in
whichever store took delivery, and aging each warehouse on its own receipts
would report every store the goods were later moved to as dead.

Two things about that are worth stating, because both were measured rather
than assumed:

  - **InQty only.** TransType 20 also writes the reversing leg of a cancelled
    or returned receipt, as OutQty, and on 16 of the 60 Beverages items that
    have one it is dated LATER than the last real receipt. Counting it would
    let a cancellation read as a fresh purchase.
  - **Plenty of stock was never purchased at all**, and this is the honest
    limit of the rule. In Beverages 89 of 231 stocked packing items and 117 of
    194 raw materials have no GRPO in the company's whole history: bottles the
    factory blows itself (TransType 59), and stock that arrived by transfer
    (67). Those rows fall back to ``OITM.CreateDate`` and are flagged
    ``movement_basis = 'none'`` rather than quietly dated, so "never bought in
    this company" cannot be misread as "bought a very long time ago".

The switch applies to every row, RM and PM alike, so one column means one
thing for the whole table. ``movement_basis`` says which rule answered:
``production`` / ``any`` with the rule on, ``grpo`` / ``none`` with it off.

The consumption percentage is deliberately NOT rewired by the switch. It
measures what was issued over the trailing year, which stays true whichever
clock the age is on -- and next to a GRPO age it is the cross-check that makes
the row readable: 124% consumed, 272 days since purchase, says the stock is
moving and the buying stopped.
"""

import logging
from typing import Dict, List, Optional, Set

from hdbcli import dbapi

from packing_material.constants import (
    PM_ITEM_GROUP,
    PM_ITEM_GROUP_NAME,
    TRANS_TYPE_GOODS_ISSUE,
    TRANS_TYPE_GRPO,
    TRANS_TYPE_PRODUCTION_RECEIPT,
    TRANS_TYPE_TRANSFER_IN,
)
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

# The movement types that count as production for packing material: material
# issued to a production order, and material a production order made. Both are
# borrowed from ``packing_material.constants`` rather than restated, so the two
# boards cannot drift apart on what "consumed" means.
PRODUCTION_TRANS_TYPES = (TRANS_TYPE_GOODS_ISSUE, TRANS_TYPE_PRODUCTION_RECEIPT)

# What each row's age was measured against, carried on the row itself.
BASIS_PRODUCTION = "production"
BASIS_ANY_MOVEMENT = "any"

# ...and the two the rule answers with when production is switched off. The
# second is not a rule so much as an admission: this item has never been
# bought in this company, so there is no purchase to age it from.
BASIS_GRPO = "grpo"
BASIS_NEVER_PURCHASED = "none"


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
        count_production: bool = True,
    ) -> List[Dict]:
        """
        Reads stock by movement age from the company's own schema.

        Args:
            age: Only return stock untouched for MORE than this many days;
                 0 returns every stocked item/warehouse pair.
            item_group: Item group code from OITB (e.g. 105), or 0 for all groups.
            branch_label: Label stamped on every row (one schema is one branch).
            count_production: Whether a production entry resets the clock. The
                default is the board's standing rule. False ages every row on
                its last Goods Receipt PO instead -- see the module docstring.

        Returns:
            One dict per (item, warehouse) holding stock.
        """
        query, params = self._build_report_query(
            age=age,
            item_group=item_group,
            count_production=count_production,
        )
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

    def _build_report_query(
        self,
        *,
        age: int,
        item_group: int,
        count_production: bool = True,
    ):
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

        # Written once and reused: the same "did this row move stock" and "is
        # this row inside the consumption window" tests are asked of OINM five
        # times over, and two of them drifting apart would be invisible.
        production_types = ", ".join(str(t) for t in PRODUCTION_TRANS_TYPES)
        moved = 'COALESCE(N."InQty", 0) <> 0 OR COALESCE(N."OutQty", 0) <> 0'
        in_window = f'N."DocDate" >= ADD_DAYS(CURRENT_DATE, -{CONSUMPTION_WINDOW_DAYS})'

        # A purchase RECEIVED, not merely a row bearing the GRPO document type:
        # the reversing leg of a cancelled receipt carries TransType 20 too and
        # is routinely dated later than the receipt it undoes.
        received = f'COALESCE(N."InQty", 0) > 0 AND N."TransType" = {TRANS_TYPE_GRPO}'

        # The three expressions the production switch actually swaps. Everything
        # else in the query -- both dates, both warehouses, the consumption
        # window -- is computed the same way either way, so the two modes cannot
        # drift apart on anything except which clock they read.
        if count_production:
            movement_date_expr = """CASE
            WHEN S."IsPackingMaterial" = 1
                THEN COALESCE(
                    I."LastProductionDate",
                    I."LastNonTransferDate",
                    S."CreateDate"
                )
            ELSE COALESCE(V."LastMovementDate", S."CreateDate")
        END"""
            movement_warehouse_expr = """CASE
            WHEN S."IsPackingMaterial" = 1
                THEN CASE
                    WHEN I."LastProductionDate" IS NOT NULL THEN P."ProductionWarehouse"
                    WHEN I."LastNonTransferDate" IS NOT NULL THEN P."NonTransferWarehouse"
                END
            WHEN V."LastMovementDate" IS NOT NULL THEN S."WhsCode"
        END"""
            basis_expr = f"""CASE
            WHEN S."IsPackingMaterial" = 1 THEN '{BASIS_PRODUCTION}'
            ELSE '{BASIS_ANY_MOVEMENT}'
        END"""
        else:
            movement_date_expr = 'COALESCE(I."LastGrpoDate", S."CreateDate")'
            movement_warehouse_expr = """CASE
            WHEN I."LastGrpoDate" IS NOT NULL THEN P."GrpoWarehouse"
        END"""
            basis_expr = f"""CASE
            WHEN I."LastGrpoDate" IS NOT NULL THEN '{BASIS_GRPO}'
            ELSE '{BASIS_NEVER_PURCHASED}'
        END"""

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
        SUM(COALESCE(W."OnHand", 0)) OVER (PARTITION BY M."ItemCode") AS "ItemOnHand",
        CASE
            WHEN COALESCE(W."AvgPrice", 0) <> 0 THEN W."AvgPrice"
            WHEN COALESCE(M."AvgPrice", 0) <> 0 THEN M."AvgPrice"
            ELSE COALESCE(M."LastPurPrc", 0)
        END AS "UnitCost",
        {self._litres_per_unit_expr(item_columns)} AS "LitresPerUnit",
        CASE
            WHEN M."ItmsGrpCod" = {PM_ITEM_GROUP} THEN 1
            WHEN UPPER(COALESCE(G."ItmsGrpNam", '')) = '{PM_ITEM_GROUP_NAME}' THEN 1
            ELSE 0
        END AS "IsPackingMaterial"
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
            CASE WHEN {moved} THEN N."DocDate" END
        ) AS "LastMovementDate",
        MAX(
            CASE
                WHEN ({moved}) AND N."TransType" IN ({production_types})
                THEN N."DocDate"
            END
        ) AS "LastProductionDate",
        MAX(
            CASE
                WHEN ({moved}) AND COALESCE(N."TransType", 0) <> {TRANS_TYPE_TRANSFER_IN}
                THEN N."DocDate"
            END
        ) AS "LastNonTransferDate",
        MAX(
            CASE WHEN {received} THEN N."DocDate" END
        ) AS "LastGrpoDate",
        SUM(
            CASE WHEN {in_window} THEN COALESCE(N."OutQty", 0) ELSE 0 END
        ) AS "IssuedInWindow",
        SUM(
            CASE
                WHEN ({in_window}) AND N."TransType" = {TRANS_TYPE_GOODS_ISSUE}
                THEN COALESCE(N."OutQty", 0)
                ELSE 0
            END
        ) AS "ProductionIssuedInWindow"
    FROM "{schema}"."OINM" N
    WHERE N."ItemCode" IN (SELECT "ItemCode" FROM Stock)
    GROUP BY N."ItemCode", N."Warehouse"
),
ItemMovement AS (
    -- The same movements asked of the ITEM. Rolled up from Movement rather
    -- than read from OINM a second time: the per-warehouse result is already
    -- small, and OINM is not.
    SELECT
        "ItemCode",
        MAX("LastProductionDate") AS "LastProductionDate",
        MAX("LastNonTransferDate") AS "LastNonTransferDate",
        MAX("LastGrpoDate") AS "LastGrpoDate",
        SUM("ProductionIssuedInWindow") AS "ProductionIssuedInWindow"
    FROM Movement
    GROUP BY "ItemCode"
),
ItemMovementSource AS (
    -- WHICH warehouse those item-level dates came out of. A packing-material
    -- row is aged on a movement in some other store -- the production floor
    -- the godown feeds -- so unless the row names that store the date cannot
    -- be traced: somebody looking the item up in the warehouse in front of
    -- them finds nothing moved and reads the two answers as contradicting.
    --
    -- Ranked rather than aggregated because the answer wanted is "whose date
    -- won", not "the largest warehouse code". The NULL guard is the point of
    -- the CASE: without it a store that never produced sorts first under
    -- DESC on some HANA revisions and gets named for a date it never saw.
    SELECT
        "ItemCode",
        MAX(CASE WHEN "ProductionRank" = 1 THEN "Warehouse" END) AS "ProductionWarehouse",
        MAX(CASE WHEN "NonTransferRank" = 1 THEN "Warehouse" END) AS "NonTransferWarehouse",
        MAX(CASE WHEN "GrpoRank" = 1 THEN "Warehouse" END) AS "GrpoWarehouse"
    FROM (
        SELECT
            "ItemCode",
            "Warehouse",
            ROW_NUMBER() OVER (
                PARTITION BY "ItemCode"
                ORDER BY
                    CASE WHEN "LastProductionDate" IS NULL THEN 1 ELSE 0 END,
                    "LastProductionDate" DESC,
                    "Warehouse"
            ) AS "ProductionRank",
            ROW_NUMBER() OVER (
                PARTITION BY "ItemCode"
                ORDER BY
                    CASE WHEN "LastNonTransferDate" IS NULL THEN 1 ELSE 0 END,
                    "LastNonTransferDate" DESC,
                    "Warehouse"
            ) AS "NonTransferRank",
            -- The store that took delivery. Named for the same reason as the
            -- other two: a receipt into BH-PM printed against a BH-BS row is
            -- unverifiable unless the row says where to look it up.
            ROW_NUMBER() OVER (
                PARTITION BY "ItemCode"
                ORDER BY
                    CASE WHEN "LastGrpoDate" IS NULL THEN 1 ELSE 0 END,
                    "LastGrpoDate" DESC,
                    "Warehouse"
            ) AS "GrpoRank"
        FROM Movement
    )
    GROUP BY "ItemCode"
),
Anchored AS (
    SELECT
        S."ItemCode",
        S."ItemName",
        S."ItemGroupName",
        S."SubGroup",
        S."OnHand",
        S."UnitCost",
        S."LitresPerUnit",
        S."WhsCode",
        S."WhsName",
        S."IsPackingMaterial",
        {movement_date_expr} AS "MovementDate",
        COALESCE(V."LastMovementDate", S."CreateDate") AS "WarehouseMovementDate",
        {movement_warehouse_expr} AS "MovementWarehouse",
        {basis_expr} AS "MovementBasis",
        CASE
            WHEN S."IsPackingMaterial" = 1
                THEN COALESCE(I."ProductionIssuedInWindow", 0)
            ELSE COALESCE(V."IssuedInWindow", 0)
        END AS "IssuedInWindow",
        CASE
            WHEN S."IsPackingMaterial" = 1 THEN S."ItemOnHand"
            ELSE S."OnHand"
        END AS "ConsumptionBase"
    FROM Stock S
    LEFT JOIN Movement V
        ON V."ItemCode" = S."ItemCode"
       AND V."Warehouse" = S."WhsCode"
    LEFT JOIN ItemMovement I
        ON I."ItemCode" = S."ItemCode"
    LEFT JOIN ItemMovementSource P
        ON P."ItemCode" = S."ItemCode"
),
Report AS (
    SELECT
        A."ItemCode",
        A."ItemName",
        A."ItemGroupName",
        A."OnHand" AS "Quantity",
        ROUND(A."OnHand" * A."LitresPerUnit", 3) AS "Litres",
        A."SubGroup",
        ROUND(A."OnHand" * A."UnitCost", 4) AS "Value",
        A."MovementDate" AS "LastMovementDate",
        CASE
            WHEN A."MovementDate" IS NULL THEN 0
            ELSE DAYS_BETWEEN(A."MovementDate", CURRENT_DATE)
        END AS "DaysSinceLastMovement",
        ROUND(A."IssuedInWindow" / A."ConsumptionBase" * 100, 2) AS "ConsumptionRatio",
        A."WhsCode",
        A."WhsName",
        A."MovementBasis",
        A."WarehouseMovementDate" AS "LastWarehouseMovementDate",
        CASE
            WHEN A."WarehouseMovementDate" IS NULL THEN 0
            ELSE DAYS_BETWEEN(A."WarehouseMovementDate", CURRENT_DATE)
        END AS "DaysSinceWarehouseMovement",
        COALESCE(A."MovementWarehouse", '') AS "MovementWhsCode",
        COALESCE(MH."WhsName", A."MovementWarehouse", '') AS "MovementWhsName"
    FROM Anchored A
    LEFT JOIN "{schema}"."OWHS" MH
        ON MH."WhsCode" = A."MovementWarehouse"
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
    "WhsName",
    "MovementBasis",
    "LastWarehouseMovementDate",
    "DaysSinceWarehouseMovement",
    "MovementWhsCode",
    "MovementWhsName"
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
            "movement_basis": row[12] or BASIS_ANY_MOVEMENT,
            "last_warehouse_movement_date": (
                row[13].strftime("%Y-%m-%d %H:%M:%S") if row[13] else None
            ),
            "days_since_warehouse_movement": int(row[14] or 0),
            "last_movement_warehouse": row[15] or "",
            "last_movement_warehouse_name": row[16] or row[15] or "",
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
