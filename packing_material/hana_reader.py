"""
packing_material/hana_reader.py

Every SAP read this board makes. Why each query is shaped the way it is --
which movement type is consumption, why the BOM is divided by
``OITT.Qauntity``, why delivery notes are ignored -- is recorded in
``constants.py`` rather than repeated here.

The reader returns flat rows and joins nothing across queries. Explosion,
ranking and roll-up all happen in ``services.py`` on plain dicts, so the
arithmetic that produces the numbers on screen is unit-testable without a HANA
connection.

Parameterised ``HanaConnection`` pattern: every value is a bound parameter.
The only things interpolated into SQL are internal literals this module owns
-- movement type numbers, item group codes, and the ``?`` placeholders
themselves.
"""

import logging
from typing import Any, Dict, List, Optional, Sequence, Set

from hdbcli import dbapi

from sap_client.exceptions import SAPConnectionError, SAPDataError
from sap_client.hana.connection import HanaConnection

from .constants import (
    BOM_LINE_TYPE_ITEM,
    BOM_TREE_TYPE_PRODUCTION,
    FG_ITEM_GROUP,
    PM_ITEM_GROUP,
    TRANS_TYPE_GOODS_ISSUE,
)

logger = logging.getLogger(__name__)


class PackingMaterialReader:
    """Reads one company's schema for the packing-material board."""

    def __init__(self, context, schema_override: Optional[str] = None):
        self.connection = HanaConnection(context.hana)
        self.schema_override = schema_override
        company_code = getattr(context, "company_code", "")
        self.company_code = company_code if isinstance(company_code, str) else ""
        self._columns_cache: Dict[str, Set[str]] = {}

    # ------------------------------------------------------------------
    # 1. Is code 105 still the packaging group?
    # ------------------------------------------------------------------

    def pm_group_name(self) -> str:
        """The name SAP currently gives item group ``PM_ITEM_GROUP``.

        Read so the response can state which group it counted instead of
        asserting that 105 means packaging. A renamed or re-numbered group
        would otherwise make every figure quietly wrong rather than visibly
        wrong.
        """
        schema = self._schema()
        rows = self._execute(
            f'SELECT "ItmsGrpNam" FROM "{schema}"."OITB" WHERE "ItmsGrpCod" = ?',
            [PM_ITEM_GROUP],
        )
        return (rows[0][0] or "") if rows else ""

    # ------------------------------------------------------------------
    # 2. The packaging item master
    # ------------------------------------------------------------------

    def pm_master(self) -> List[Dict[str, Any]]:
        """Name, unit, family and unit cost for every packing-material item.

        ``unit_price`` prefers the item's moving average and falls back to its
        last purchase price. Both are denominated in the INVENTORY unit, which
        is the unit every quantity in this module is in. ``POR1.Price`` is not
        an option and is not read: it is in the PURCHASE unit, which for
        several materials is a different unit entirely.

        ``U_Sub_Group`` is the packaging family -- LABEL, CARTON, CAPS, PET
        BOTTLES, PREFORM, SHRINK, TIN, POUCH and so on -- and SAP populates it
        for the whole packaging range. It is read defensively all the same,
        because a user-defined field is not guaranteed to exist in every
        company database and a missing one should blank a column rather than
        fail the request.
        """
        schema = self._schema()
        sub_group = self._optional_item_string(self._table_columns("OITM"), "U_Sub_Group")

        query = f"""
SELECT
    M."ItemCode",
    COALESCE(M."ItemName", '')   AS "ItemName",
    COALESCE(M."InvntryUom", '') AS "Uom",
    {sub_group}                  AS "SubGroup",
    CASE
        WHEN COALESCE(M."AvgPrice", 0) <> 0 THEN M."AvgPrice"
        ELSE COALESCE(M."LastPurPrc", 0)
    END                          AS "UnitPrice"
FROM "{schema}"."OITM" M
WHERE M."ItmsGrpCod" = {PM_ITEM_GROUP}
"""
        return [
            {
                "item_code": r[0] or "",
                "item_name": r[1] or "",
                "uom": r[2] or "",
                "sub_group": (r[3] or "").strip(),
                "unit_price": float(r[4] or 0),
            }
            for r in self._execute(query, [])
        ]

    # ------------------------------------------------------------------
    # 3. What is on the shelf right now, per warehouse
    # ------------------------------------------------------------------

    def warehouse_names(self, warehouses: Sequence[str]) -> List[Dict[str, Any]]:
        """SAP's own name and active flag for each stock warehouse.

        Read as its own query rather than joined into the stock read so a
        warehouse that holds nothing still gets a card with its real name on
        it, and so a warehouse code that does not exist in this company at all
        can be reported as missing instead of as empty.
        """
        if not warehouses:
            return []

        schema = self._schema()
        query = f"""
SELECT
    H."WhsCode",
    COALESCE(H."WhsName", '')      AS "WhsName",
    COALESCE(H."Inactive", 'N')    AS "Inactive"
FROM "{schema}"."OWHS" H
WHERE H."WhsCode" IN ({self._placeholders(warehouses)})
"""
        return [
            {
                "code": r[0] or "",
                "name": r[1] or "",
                "inactive": (r[2] or "N") == "Y",
            }
            for r in self._execute(query, list(warehouses))
        ]

    def pm_stock_by_warehouse(self, warehouses: Sequence[str]) -> List[Dict[str, Any]]:
        """Packing material on hand, one row per (warehouse, item).

        A snapshot, deliberately: ``OITW.OnHand`` is stock NOW. The response
        stamps ``fetched_at`` so nobody reads it as an end-of-period balance.

        Grouped by warehouse AND item, unlike the item-level reads elsewhere
        in the codebase, because the four cards ARE the per-warehouse split
        and each one opens onto its own item list.

        An inactive warehouse is NOT filtered out here -- see ``constants``.
        The warehouses asked for are explicit, and one SAP has decommissioned
        must show what is frozen in it with the flag saying so.

        Value prefers the warehouse's own moving average, then the item's,
        then last purchase price, matching what ``non_moving_rm`` does so the
        two dashboards put the same rupee value on the same pallet.
        """
        if not warehouses:
            return []

        schema = self._schema()
        query = f"""
SELECT
    W."WhsCode",
    W."ItemCode",
    ROUND(COALESCE(SUM(W."OnHand"), 0), 3) AS "OnHand",
    ROUND(
        COALESCE(
            SUM(
                W."OnHand" * CASE
                    WHEN COALESCE(W."AvgPrice", 0) <> 0 THEN W."AvgPrice"
                    WHEN COALESCE(M."AvgPrice", 0) <> 0 THEN M."AvgPrice"
                    ELSE COALESCE(M."LastPurPrc", 0)
                END
            ),
            0
        ),
        2
    ) AS "StockValue"
FROM "{schema}"."OITW" W
INNER JOIN "{schema}"."OITM" M
    ON M."ItemCode" = W."ItemCode"
WHERE M."ItmsGrpCod" = {PM_ITEM_GROUP}
  AND COALESCE(W."OnHand", 0) <> 0
  AND W."WhsCode" IN ({self._placeholders(warehouses)})
GROUP BY W."WhsCode", W."ItemCode"
"""
        return [
            {
                "warehouse": r[0] or "",
                "item_code": r[1] or "",
                "stock_qty": float(r[2] or 0),
                "stock_value": float(r[3] or 0),
            }
            for r in self._execute(query, list(warehouses))
        ]

    # ------------------------------------------------------------------
    # 4. What the line actually used
    # ------------------------------------------------------------------

    def pm_issued(
        self, warehouses: Sequence[str], date_from, date_to
    ) -> List[Dict[str, Any]]:
        """Packing material issued to production, per item, over the period.

        ``TransType`` 60 with ``OutQty`` out of the consumption warehouse --
        the actual goods issue. See ``constants`` for why this and not the
        store-to-line transfer, and why only the consumption warehouse.
        """
        if not warehouses:
            return []

        schema = self._schema()
        query = f"""
SELECT
    O."ItemCode",
    ROUND(COALESCE(SUM(O."OutQty"), 0), 3) AS "IssuedQty"
FROM "{schema}"."OINM" O
INNER JOIN "{schema}"."OITM" I
    ON I."ItemCode" = O."ItemCode"
WHERE O."TransType" = {TRANS_TYPE_GOODS_ISSUE}
  AND I."ItmsGrpCod" = {PM_ITEM_GROUP}
  AND O."DocDate" >= ?
  AND O."DocDate" <= ?
  AND O."Warehouse" IN ({self._placeholders(warehouses)})
GROUP BY O."ItemCode"
HAVING ROUND(COALESCE(SUM(O."OutQty"), 0), 3) > 0
"""
        params: List[Any] = [date_from, date_to, *warehouses]
        return [
            {"item_code": r[0] or "", "issued_qty": float(r[1] or 0)}
            for r in self._execute(query, params)
        ]

    # ------------------------------------------------------------------
    # 5. What was invoiced out
    # ------------------------------------------------------------------

    def dispatched_lines(
        self, date_from, date_to, intercompany_card_codes: Sequence[str]
    ) -> List[Dict[str, Any]]:
        """Invoiced items, per item, in pieces, over the period.

        Invoices count positive and credit notes negative, so the figure is
        net of sales returns -- a case that came back was not, in the end,
        dispatched. The gross return is kept alongside so the netting is
        visible rather than silent.

        Both finished goods AND packing material come back, tagged with their
        item group, out of one read rather than two: the finished goods are
        what gets exploded through the BOM, and the packing material invoiced
        as itself is the ``direct_pm`` figure the coverage block reports. They
        are two roll-ups of the same document lines, so reading them
        separately would let one be counted over a document set the other was
        not.

        ``intercompany_qty`` is the part of the net figure that went to a
        group company. It is summed here rather than fetched by a second
        query so both the with- and without-intercompany figures cost the same
        one read, and so they can never be computed over different documents.
        """
        schema = self._schema()

        # An empty list must not become "IN ()", which is a syntax error. No
        # configured group customers means no intercompany, which is 0.
        if intercompany_card_codes:
            ic_test = f'H."CardCode" IN ({self._placeholders(intercompany_card_codes)})'
        else:
            ic_test = "1 = 0"

        groups = f"{FG_ITEM_GROUP}, {PM_ITEM_GROUP}"
        query = f"""
SELECT
    X."ItemCode",
    X."ItmsGrpCod",
    COALESCE(I."ItemName", '')                       AS "ItemName",
    ROUND(COALESCE(SUM(X."Qty"), 0), 3)              AS "Qty",
    ROUND(COALESCE(SUM(X."IntercompanyQty"), 0), 3)  AS "IntercompanyQty",
    ROUND(COALESCE(SUM(X."ReturnQty"), 0), 3)        AS "ReturnQty",
    COUNT(DISTINCT X."DocEntry")                     AS "Docs"
FROM (
    SELECT
        L."ItemCode",
        M."ItmsGrpCod",
        H."DocEntry",
        L."Quantity"                                              AS "Qty",
        CASE WHEN {ic_test} THEN L."Quantity" ELSE 0 END           AS "IntercompanyQty",
        0                                                          AS "ReturnQty"
    FROM "{schema}"."OINV" H
    INNER JOIN "{schema}"."INV1" L
        ON L."DocEntry" = H."DocEntry"
    INNER JOIN "{schema}"."OITM" M
        ON M."ItemCode" = L."ItemCode"
    WHERE H."DocDate" >= ?
      AND H."DocDate" <= ?
      AND H."CANCELED" = 'N'
      AND M."ItmsGrpCod" IN ({groups})

    UNION ALL

    SELECT
        L."ItemCode",
        M."ItmsGrpCod",
        H."DocEntry",
        -L."Quantity"                                              AS "Qty",
        CASE WHEN {ic_test} THEN -L."Quantity" ELSE 0 END           AS "IntercompanyQty",
        L."Quantity"                                               AS "ReturnQty"
    FROM "{schema}"."ORIN" H
    INNER JOIN "{schema}"."RIN1" L
        ON L."DocEntry" = H."DocEntry"
    INNER JOIN "{schema}"."OITM" M
        ON M."ItemCode" = L."ItemCode"
    WHERE H."DocDate" >= ?
      AND H."DocDate" <= ?
      AND H."CANCELED" = 'N'
      AND M."ItmsGrpCod" IN ({groups})
) X
LEFT JOIN "{schema}"."OITM" I
    ON I."ItemCode" = X."ItemCode"
GROUP BY X."ItemCode", X."ItmsGrpCod", I."ItemName"
"""
        # Placeholder order follows the text of the statement: the CASE in the
        # invoice leg, that leg's dates, then the same two in the credit note.
        params: List[Any] = [
            *intercompany_card_codes,
            date_from,
            date_to,
            *intercompany_card_codes,
            date_from,
            date_to,
        ]
        return [
            {
                "item_code": r[0] or "",
                "item_group": int(r[1] or 0),
                "item_name": r[2] or "",
                "qty": float(r[3] or 0),
                "intercompany_qty": float(r[4] or 0),
                "return_qty": float(r[5] or 0),
                "document_count": int(r[6] or 0),
            }
            for r in self._execute(query, params)
        ]

    def dispatch_document_count(self, date_from, date_to) -> int:
        """How many bills carried finished goods over the period.

        Counted on its own because the per-item read above cannot give it: a
        bill with six items appears on six rows, so summing that column would
        report the lines, not the bills.
        """
        schema = self._schema()
        query = f"""
SELECT COUNT(DISTINCT H."DocEntry")
FROM "{schema}"."OINV" H
INNER JOIN "{schema}"."INV1" L
    ON L."DocEntry" = H."DocEntry"
INNER JOIN "{schema}"."OITM" M
    ON M."ItemCode" = L."ItemCode"
WHERE H."DocDate" >= ?
  AND H."DocDate" <= ?
  AND H."CANCELED" = 'N'
  AND M."ItmsGrpCod" = {FG_ITEM_GROUP}
"""
        rows = self._execute(query, [date_from, date_to])
        return int(rows[0][0] or 0) if rows else 0

    # ------------------------------------------------------------------
    # 6. The recipes
    # ------------------------------------------------------------------

    def pm_bom_lines(self) -> List[Dict[str, Any]]:
        """Every production-BOM line whose component is packing material.

        Returned whole -- 2,003 rows on the Oil company -- and not filtered to
        the items dispatched in the period. The whole set is one small read,
        while a filtered read means an ``IN`` list of a hundred-odd item codes
        that grows with the date range and buys nothing.

        ``QtyPerUnit`` is divided by ``OITT.Qauntity`` in SQL so no caller can
        forget it. A BOM written for a batch of 20 with a null or zero batch
        size is dropped by the ``NULLIF``: a recipe SAP cannot state a batch
        for cannot be exploded, and reporting it as per-1 would overstate that
        component twentyfold.
        """
        schema = self._schema()
        query = f"""
SELECT
    C."Father",
    C."Code",
    C."Quantity" / NULLIF(T."Qauntity", 0) AS "QtyPerUnit"
FROM "{schema}"."OITT" T
INNER JOIN "{schema}"."ITT1" C
    ON C."Father" = T."Code"
INNER JOIN "{schema}"."OITM" CI
    ON CI."ItemCode" = C."Code"
WHERE T."TreeType" = '{BOM_TREE_TYPE_PRODUCTION}'
  AND C."Type" = {BOM_LINE_TYPE_ITEM}
  AND CI."ItmsGrpCod" = {PM_ITEM_GROUP}
  AND C."Quantity" / NULLIF(T."Qauntity", 0) IS NOT NULL
"""
        return [
            {
                "parent_code": r[0] or "",
                "pm_code": r[1] or "",
                "qty_per_unit": float(r[2] or 0),
            }
            for r in self._execute(query, [])
        ]

    # ------------------------------------------------------------------
    # 7. Which of these codes are finished goods
    # ------------------------------------------------------------------

    def item_group_map(self, item_codes: Sequence[str]) -> Dict[str, int]:
        """Item group per code, for the codes asked about.

        Only needed on the FactoryFlow side: a gate-out line carries an item
        code but no item group, and 18 of August's distinct gate-out items
        were packing material rather than finished goods. Without this the
        board would try to explode a cap through a bill of materials and
        report it as a recipe gap.
        """
        codes = [code for code in dict.fromkeys(item_codes) if code]
        if not codes:
            return {}

        schema = self._schema()
        rows = self._execute(
            f"""
SELECT M."ItemCode", COALESCE(M."ItmsGrpCod", 0)
FROM "{schema}"."OITM" M
WHERE M."ItemCode" IN ({self._placeholders(codes)})
""",
            codes,
        )
        return {r[0]: int(r[1] or 0) for r in rows if r[0]}

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _schema(self) -> str:
        return self.schema_override or self.connection.schema

    @staticmethod
    def _placeholders(values: Sequence[Any]) -> str:
        return ", ".join("?" for _ in values)

    def _table_columns(self, table_name: str) -> Set[str]:
        """Columns SAP actually has on a table in THIS schema."""
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

    def _execute(self, query: str, params: List[Any]) -> List:
        conn = None
        cursor = None
        try:
            conn = self.connection.connect()
        except dbapi.Error as e:
            logger.error("SAP HANA connection failed for packing material: %s", e)
            raise SAPConnectionError(
                "Unable to connect to SAP HANA. Please try again later."
            ) from e
        try:
            cursor = conn.cursor()
            cursor.execute(query, params)
            return cursor.fetchall()
        except dbapi.ProgrammingError as e:
            logger.error("SAP HANA query error in packing material: %s", e)
            raise SAPDataError("SAP rejected the packing material query.") from e
        except dbapi.Error as e:
            logger.error("SAP HANA read failed in packing material: %s", e)
            raise SAPDataError("SAP could not return the packing material data.") from e
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
