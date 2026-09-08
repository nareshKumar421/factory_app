"""
pm_demand/hana_reader.py

Every SAP read the PM Demand dashboard makes. Five queries, one company
schema. Why each one is shaped the way it is -- which movement type counts as
consumption, why the BOM is divided by ``OITT.Qauntity``, why delivery notes
are ignored -- is recorded in ``constants.py`` rather than repeated here.

The reader returns flat rows and joins nothing across queries. Explosion,
ranking and roll-up all happen in ``services.py`` on plain dicts, so the
arithmetic that produces the numbers on screen is unit-testable without a
HANA connection.

Parameterised ``HanaConnection`` pattern: every value is a bound parameter.
The only things interpolated into SQL are internal literals this module owns
-- movement type numbers, item group codes, and the ``?`` placeholders
themselves.
"""

import logging
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from hdbcli import dbapi

from sap_client.exceptions import SAPConnectionError, SAPDataError
from sap_client.hana.connection import HanaConnection

from .constants import (
    BOM_LINE_TYPE_ITEM,
    BOM_TREE_TYPE_PRODUCTION,
    FG_ITEM_GROUP,
    PM_ITEM_GROUP,
    TRANS_TYPE_GOODS_ISSUE,
    TRANS_TYPE_GOODS_RECEIPT,
    TRANS_TYPE_STOCK_TRANSFER,
)

logger = logging.getLogger(__name__)


class PmDemandReader:
    """Reads one company's schema for the PM Demand dashboard."""

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
    # 2. Finished goods produced
    # ------------------------------------------------------------------

    def fg_produced(
        self, warehouses: Sequence[str], date_from, date_to
    ) -> List[Dict[str, Any]]:
        """Finished goods received from production, per item, in pieces.

        ``OINM.TransType`` 59 with ``InQty`` into the finished-goods
        warehouse is the goods receipt from production -- what the factory
        actually made, as opposed to what any plan said it would make.
        """
        if not warehouses:
            return []

        schema = self._schema()
        placeholders = self._placeholders(warehouses)
        query = f"""
SELECT
    O."ItemCode",
    COALESCE(I."ItemName", '') AS "ItemName",
    ROUND(COALESCE(SUM(O."InQty"), 0), 3) AS "Qty"
FROM "{schema}"."OINM" O
INNER JOIN "{schema}"."OITM" I
    ON I."ItemCode" = O."ItemCode"
WHERE O."TransType" = {TRANS_TYPE_GOODS_RECEIPT}
  AND I."ItmsGrpCod" = {FG_ITEM_GROUP}
  AND O."DocDate" >= ?
  AND O."DocDate" <= ?
  AND O."Warehouse" IN ({placeholders})
GROUP BY O."ItemCode", I."ItemName"
HAVING ROUND(COALESCE(SUM(O."InQty"), 0), 3) > 0
"""
        params: List[Any] = [date_from, date_to, *warehouses]
        return [
            {"item_code": r[0] or "", "item_name": r[1] or "", "qty": float(r[2] or 0)}
            for r in self._execute(query, params)
        ]

    # ------------------------------------------------------------------
    # 3. Finished goods dispatched
    # ------------------------------------------------------------------

    def fg_dispatched(
        self, date_from, date_to, intercompany_card_codes: Sequence[str]
    ) -> List[Dict[str, Any]]:
        """Finished goods invoiced out, per item, in pieces.

        Invoices count positive and credit notes negative, so the figure is
        net of sales returns -- a case that came back was not, in the end,
        dispatched. The gross return is kept alongside so the netting is
        visible rather than silent.

        ``intercompany_qty`` is the part of the net figure that went to a
        group company. It is summed here rather than fetched by a second
        query so that both the with- and without-intercompany views cost the
        same one read, and so they can never be computed over different
        document sets.
        """
        schema = self._schema()

        # An empty list must not become "IN ()", which is a syntax error.
        # No configured group customers means no intercompany, which is 0.
        if intercompany_card_codes:
            ic_placeholders = self._placeholders(intercompany_card_codes)
            ic_test = f'H."CardCode" IN ({ic_placeholders})'
        else:
            ic_test = "1 = 0"

        query = f"""
SELECT
    X."ItemCode",
    COALESCE(I."ItemName", '')                       AS "ItemName",
    ROUND(COALESCE(SUM(X."Qty"), 0), 3)              AS "Qty",
    ROUND(COALESCE(SUM(X."IntercompanyQty"), 0), 3)  AS "IntercompanyQty",
    ROUND(COALESCE(SUM(X."ReturnQty"), 0), 3)        AS "ReturnQty"
FROM (
    SELECT
        L."ItemCode",
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
      AND M."ItmsGrpCod" = {FG_ITEM_GROUP}

    UNION ALL

    SELECT
        L."ItemCode",
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
      AND M."ItmsGrpCod" = {FG_ITEM_GROUP}
) X
LEFT JOIN "{schema}"."OITM" I
    ON I."ItemCode" = X."ItemCode"
GROUP BY X."ItemCode", I."ItemName"
"""
        # Placeholder order: the CASE in the invoice leg, then that leg's
        # dates, then the same two in the credit-note leg.
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
                "item_name": r[1] or "",
                "qty": float(r[2] or 0),
                "intercompany_qty": float(r[3] or 0),
                "return_qty": float(r[4] or 0),
            }
            for r in self._execute(query, params)
        ]

    # ------------------------------------------------------------------
    # 4. The recipes
    # ------------------------------------------------------------------

    def pm_bom_lines(self) -> List[Dict[str, Any]]:
        """Every production-BOM line whose component is packing material.

        Returned whole -- around two thousand rows on the Oil company -- and
        not filtered to the parents in the period. The whole set is one small
        read, while a filtered read means an ``IN`` list of a hundred-odd item
        codes that grows with the date range and buys nothing.

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
    # 5. What the line actually used
    # ------------------------------------------------------------------

    def pm_movements(
        self,
        date_from,
        date_to,
        consumption_warehouses: Sequence[str],
        wastage_warehouses: Sequence[str],
        upstream_warehouses: Sequence[str],
    ) -> List[Dict[str, Any]]:
        """The four packing-material movements this dashboard reads, per item.

        One pass over ``OINM`` with conditional sums rather than four queries
        over the same rows:

        ``issued_qty``    TransType 60 out of the production warehouse. The
                          actual consumption -- see ``constants``.
        ``wastage_qty``   TransType 67 into the wastage warehouse. Scrap and
                          rejected packaging is moved *in* to BH-WST, so it
                          lands as an inward transfer, not an issue.
        ``in_house_qty``  TransType 59 into the production warehouse. Only
                          non-zero for packaging the factory makes itself, and
                          it is the flag that explains an item consumed in
                          volume with no purchase behind it.
        ``upstream_qty``  TransType 60 out of the blowing line. Its own stage,
                          reported separately and never added to a total.
        """
        all_warehouses = list(
            dict.fromkeys(
                [
                    *consumption_warehouses,
                    *wastage_warehouses,
                    *upstream_warehouses,
                ]
            )
        )
        if not all_warehouses:
            return []

        schema = self._schema()
        consumption_case, consumption_params = self._warehouse_case(
            consumption_warehouses, TRANS_TYPE_GOODS_ISSUE, "OutQty"
        )
        wastage_case, wastage_params = self._warehouse_case(
            wastage_warehouses, TRANS_TYPE_STOCK_TRANSFER, "InQty"
        )
        in_house_case, in_house_params = self._warehouse_case(
            consumption_warehouses, TRANS_TYPE_GOODS_RECEIPT, "InQty"
        )
        upstream_case, upstream_params = self._warehouse_case(
            upstream_warehouses, TRANS_TYPE_GOODS_ISSUE, "OutQty"
        )

        query = f"""
SELECT
    O."ItemCode",
    ROUND(COALESCE(SUM({consumption_case}), 0), 3) AS "IssuedQty",
    ROUND(COALESCE(SUM({wastage_case}), 0), 3)     AS "WastageQty",
    ROUND(COALESCE(SUM({in_house_case}), 0), 3)    AS "InHouseQty",
    ROUND(COALESCE(SUM({upstream_case}), 0), 3)    AS "UpstreamQty"
FROM "{schema}"."OINM" O
INNER JOIN "{schema}"."OITM" I
    ON I."ItemCode" = O."ItemCode"
WHERE I."ItmsGrpCod" = {PM_ITEM_GROUP}
  AND O."DocDate" >= ?
  AND O."DocDate" <= ?
  AND O."TransType" IN (
      {TRANS_TYPE_GOODS_RECEIPT}, {TRANS_TYPE_GOODS_ISSUE},
      {TRANS_TYPE_STOCK_TRANSFER}
  )
  AND O."Warehouse" IN ({self._placeholders(all_warehouses)})
GROUP BY O."ItemCode"
"""
        # Placeholder order follows the text of the statement: the four SELECT
        # cases, then the two dates, then the warehouse filter.
        params: List[Any] = [
            *consumption_params,
            *wastage_params,
            *in_house_params,
            *upstream_params,
            date_from,
            date_to,
            *all_warehouses,
        ]
        rows = self._execute(query, params)
        return [
            {
                "item_code": r[0] or "",
                "issued_qty": float(r[1] or 0),
                "wastage_qty": float(r[2] or 0),
                "in_house_qty": float(r[3] or 0),
                "upstream_qty": float(r[4] or 0),
            }
            for r in rows
        ]

    # ------------------------------------------------------------------
    # 6. What is on the shelf right now
    # ------------------------------------------------------------------

    def pm_stock(self, warehouses: Sequence[str]) -> List[Dict[str, Any]]:
        """Packing material on hand in the stores the line can draw on.

        A snapshot, deliberately: ``OITW.OnHand`` is stock NOW, not stock at
        the end of the period being reported. Cover answers "how long will
        what I have last at the rate I have been burning it", so the stock
        must be today's while the rate comes from the period. Mixing the two
        is the point rather than an oversight, and the response stamps
        ``fetched_at`` so nobody reads the snapshot as historical.

        Inactive warehouses are skipped -- SAP keeps decommissioned stores
        with their last balance frozen in them, and Oil's BH-PP still carries
        packaging while being flagged inactive.

        Value prefers the warehouse's own moving average, then the item's,
        then last purchase price, matching what ``non_moving_rm`` does so the
        two dashboards put the same rupee value on the same pallet.
        """
        if not warehouses:
            return []

        schema = self._schema()
        placeholders = self._placeholders(warehouses)
        query = f"""
SELECT
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
INNER JOIN "{schema}"."OWHS" H
    ON H."WhsCode" = W."WhsCode"
WHERE M."ItmsGrpCod" = {PM_ITEM_GROUP}
  AND COALESCE(W."OnHand", 0) <> 0
  AND COALESCE(H."Inactive", 'N') <> 'Y'
  AND W."WhsCode" IN ({placeholders})
GROUP BY W."ItemCode"
"""
        return [
            {
                "item_code": r[0] or "",
                "stock_qty": float(r[1] or 0),
                "stock_value": float(r[2] or 0),
            }
            for r in self._execute(query, list(warehouses))
        ]

    # ------------------------------------------------------------------
    # 7. What is already on order
    # ------------------------------------------------------------------

    def pm_open_po(self, as_of) -> List[Dict[str, Any]]:
        """Packing material on open purchase orders, per item.

        Without this the cover panel is a false-alarm machine. Verified on
        the live Oil company: PM0000085 CAPS 1 OR 2 LTR had 5,660 on hand
        against 21,936 a working day -- 0.3 days, which reads as the line
        stopping tomorrow. It also had **1,148,000 units on open purchase
        orders**. The item is a standing bulk order being drawn down, bought
        in monthly lots of 350k-550k (TransType 20 into BH-PM), so a snapshot
        taken the day before a delivery always looks like an emergency and
        the same snapshot the day after looks fine.

        ``OpenQty`` is the undelivered remainder of a line, which is the only
        figure that matters here -- ordering against ``Quantity`` would count
        stock already received twice.

        ``earliest_due`` is reported and NOT used to discount the quantity,
        because plenty of these are overdue: the same cap's oldest open line
        was due 11 June, and HDPE BOTTLE 5 LTR has one due 6 January. Some of
        that is a late supplier and some is a purchase order nobody ever
        closed, and this module cannot tell which. So the date is surfaced,
        flagged when it has passed, and the judgement left to the buyer
        rather than guessed at here.
        """
        schema = self._schema()
        query = f"""
SELECT
    L."ItemCode",
    ROUND(COALESCE(SUM(L."OpenQty"), 0), 3) AS "OpenQty",
    MIN(L."ShipDate")                       AS "EarliestDue",
    COUNT(*)                                AS "Lines"
FROM "{schema}"."POR1" L
INNER JOIN "{schema}"."OPOR" H
    ON H."DocEntry" = L."DocEntry"
INNER JOIN "{schema}"."OITM" M
    ON M."ItemCode" = L."ItemCode"
WHERE M."ItmsGrpCod" = {PM_ITEM_GROUP}
  AND H."CANCELED" = 'N'
  AND H."DocStatus" = 'O'
  AND L."LineStatus" = 'O'
  AND COALESCE(L."OpenQty", 0) > 0
GROUP BY L."ItemCode"
"""
        rows = self._execute(query, [])
        return [
            {
                "item_code": r[0] or "",
                "open_po_qty": float(r[1] or 0),
                "earliest_due": r[2].date() if hasattr(r[2], "date") else r[2],
                "po_lines": int(r[3] or 0),
            }
            for r in rows
        ]

    # ------------------------------------------------------------------
    # 8. The packaging item master
    # ------------------------------------------------------------------

    def pm_master(self) -> List[Dict[str, Any]]:
        """Name, unit, family and unit cost for every packing-material item.

        ``unit_price`` prefers the item's moving average and falls back to its
        last purchase price. Both are denominated in the INVENTORY unit, which
        is the unit every quantity in this module is in. ``POR1.Price`` is
        not an option and is not read: it is in the PURCHASE unit, which for
        several materials is a different unit entirely.

        ``U_Sub_Group`` is the packaging family -- LABEL, CARTON, CAPS, PET
        BOTTLES, PREFORM, SHRINK, TIN, POUCH and so on -- and SAP populates it
        for the whole packaging range. It is read defensively all the same,
        because a user-defined field is not guaranteed to exist in every
        company database and a missing one should blank a column rather than
        fail the request.
        """
        schema = self._schema()
        item_columns = self._table_columns("OITM")
        sub_group = self._optional_item_string(item_columns, "U_Sub_Group")

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
    # Internals
    # ------------------------------------------------------------------

    def _schema(self) -> str:
        return self.schema_override or self.connection.schema

    @staticmethod
    def _placeholders(values: Sequence[Any]) -> str:
        return ", ".join("?" for _ in values)

    def _warehouse_case(
        self, warehouses: Sequence[str], trans_type: int, qty_column: str
    ) -> Tuple[str, List[Any]]:
        """A conditional sum for one movement type in one warehouse list.

        An empty list yields the literal 0 rather than an empty ``IN ()``:
        a company with no blowing line reports no upstream stage, which is
        the truth, not an error.
        """
        if not warehouses:
            return "0", []
        clause = (
            f'CASE WHEN O."TransType" = {trans_type} '
            f'AND O."Warehouse" IN ({self._placeholders(warehouses)}) '
            f'THEN COALESCE(O."{qty_column}", 0) ELSE 0 END'
        )
        return clause, list(warehouses)

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
            logger.error("SAP HANA connection failed for PM demand: %s", e)
            raise SAPConnectionError(
                "Unable to connect to SAP HANA. Please try again later."
            ) from e
        try:
            cursor = conn.cursor()
            cursor.execute(query, params)
            return cursor.fetchall()
        except dbapi.ProgrammingError as e:
            logger.error("SAP HANA query error in PM demand: %s", e)
            raise SAPDataError("SAP rejected the PM demand query.") from e
        except dbapi.Error as e:
            logger.error("SAP HANA read failed in PM demand: %s", e)
            raise SAPDataError("SAP could not return the PM demand data.") from e
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
