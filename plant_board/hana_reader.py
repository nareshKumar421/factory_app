"""
plant_board/hana_reader.py

The three SAP reads this board adds. Everything else on it comes from an
existing service.

Each one is here because no endpoint answered it: the standing age of a whole
floor (the batches endpoint answers one item at a time, which a wall board
cannot call 200 times a minute), oil yield loss, and where stock physically went
when it left the production floor.

Column names on ``OINM`` are the ones already in use elsewhere in this
codebase — ``Warehouse``, ``TransNum``, ``InQty``, ``OutQty``, ``DocDate``,
``TransType``, ``ItemCode`` — not guessed.

The litre gate is the same one ``planning_purchase`` uses and it is
load-bearing rather than decorative: ``OITM.SalPackUn`` is populated for the
*whole* item master, so without the ``U_IsLitre`` test a movement of 100,000
preforms reports as 100,000 litres — and, under this board's tonnage rule, as
100 tons.
"""

import logging
from typing import Any, Dict, List, Optional

from hdbcli import dbapi

from sap_client.exceptions import SAPConnectionError, SAPDataError
from sap_client.hana.connection import HanaConnection

from .constants import (
    BENCHMARK_ITEM_GROUP,
    FINISHED_ITEM_GROUP,
    PACKAGING_ITEM_GROUP,
    OIL_GROUP_TOKENS,
    PRODUCTION_FLOOR,
    TRANS_TYPE_GOODS_ISSUE,
    TRANS_TYPE_PRODUCTION_RECEIPT,
)

logger = logging.getLogger(__name__)


#: Litres in one transacted piece, gated on the item being a litre item at all.
#: Expects ``M`` = OITM.
LITRES_PER_UNIT = """
    CASE
        WHEN UPPER(IFNULL(M."U_IsLitre", 'N')) = 'Y' THEN IFNULL(M."SalPackUn", 0)
        ELSE 0
    END
"""


class PlantBoardReader:
    """The board's own SAP reads, for one company."""

    def __init__(self, context):
        self.connection = HanaConnection(context.hana)

    @property
    def schema(self) -> str:
        return self.connection.schema

    # ------------------------------------------------------------------
    # 1. Standing age of a whole floor
    # ------------------------------------------------------------------

    def last_out_dates(self, warehouse: str) -> Dict[str, Any]:
        """When each item last *left* this warehouse, keyed by item code.

        Aged on the outbound movement alone, which is the whole point. BH-PF is
        the floor product is produced INTO, so a receipt is arrival rather than
        movement: aged on "any movement", a batch that has sat for ten days
        reads as fresh the moment a new pallet of the same SKU lands beside it.

        An item with no outbound movement in its whole history is simply absent
        from the result. The caller treats that as "never shipped", which is
        different from "shipped a long time ago" and is the more urgent of the
        two — so it must not come back as a zero.
        """
        query = f"""
            SELECT N."ItemCode", MAX(N."DocDate") AS "LastOut"
            FROM "{self.schema}"."OINM" N
            WHERE N."Warehouse" = ?
              AND IFNULL(N."OutQty", 0) > 0
            GROUP BY N."ItemCode"
        """
        rows = self._rows(query, [warehouse])
        return {row["ItemCode"]: row["LastOut"] for row in rows if row.get("ItemCode")}

    # ------------------------------------------------------------------
    # 2. Oil yield loss
    # ------------------------------------------------------------------

    def oil_yield(self, date_from, date_to) -> Dict[str, Any]:
        """Litres of oil issued to the lines against litres that came out packed.

        This is the board's "OIL waste" tile, and it is a *yield* figure rather
        than a declared one — the business chose it over waste-log rows because
        a log only captures what somebody wrote down, while yield loss captures
        everything, including the quiet losses nobody logs.

        Issued is ``TransType 60`` (goods issue) out of the oil item groups.
        Packed is ``TransType 59`` (goods receipt from production) into the
        finished group. Both are converted to litres through the item master.

        ONE ASSUMPTION, AND IT IS DISCLOSED RATHER THAN BURIED. Bulk oil is
        transacted in litres already, so for an issued oil line that SAP does
        not flag as a litre item the raw quantity is taken as litres. Every such
        line is counted in ``assumed_litre_uom_lines`` so the board can say how
        much of the figure rests on that. If that count is ever large, the
        answer is to fix ``U_IsLitre`` on the item master rather than to trust
        this number.

        Returned as component figures, not just a loss. A single "waste %" with
        nothing behind it is unarguable-with; issued and packed side by side let
        a production head see which half moved.
        """
        group_filter = " OR ".join(
            [f"UPPER(IFNULL(G.\"ItmsGrpNam\", '')) LIKE '%{token}%'" for token in OIL_GROUP_TOKENS]
        )

        issued_query = f"""
            SELECT
                SUM(
                    CASE
                        WHEN UPPER(IFNULL(M."U_IsLitre", 'N')) = 'Y'
                            THEN N."OutQty" * IFNULL(M."SalPackUn", 0)
                        ELSE N."OutQty"
                    END
                )                                              AS "Litres",
                SUM(
                    CASE WHEN UPPER(IFNULL(M."U_IsLitre", 'N')) = 'Y' THEN 0 ELSE 1 END
                )                                              AS "AssumedLines",
                COUNT(*)                                       AS "Lines"
            FROM "{self.schema}"."OINM" N
            JOIN "{self.schema}"."OITM" M ON M."ItemCode" = N."ItemCode"
            LEFT JOIN "{self.schema}"."OITB" G ON G."ItmsGrpCod" = M."ItmsGrpCod"
            WHERE N."TransType" = {TRANS_TYPE_GOODS_ISSUE}
              AND IFNULL(N."OutQty", 0) > 0
              AND N."DocDate" >= ? AND N."DocDate" <= ?
              AND ({group_filter})
        """

        packed_query = f"""
            SELECT SUM(N."InQty" * ({LITRES_PER_UNIT})) AS "Litres", COUNT(*) AS "Lines"
            FROM "{self.schema}"."OINM" N
            JOIN "{self.schema}"."OITM" M ON M."ItemCode" = N."ItemCode"
            WHERE N."TransType" = {TRANS_TYPE_PRODUCTION_RECEIPT}
              AND IFNULL(N."InQty", 0) > 0
              AND N."DocDate" >= ? AND N."DocDate" <= ?
              AND M."ItmsGrpCod" = {FINISHED_ITEM_GROUP}
        """

        issued = (self._rows(issued_query, [date_from, date_to]) or [{}])[0]
        packed = (self._rows(packed_query, [date_from, date_to]) or [{}])[0]

        return {
            "issued_litres": float(issued.get("Litres") or 0),
            "packed_litres": float(packed.get("Litres") or 0),
            "issued_lines": int(issued.get("Lines") or 0),
            "packed_lines": int(packed.get("Lines") or 0),
            "assumed_litre_uom_lines": int(issued.get("AssumedLines") or 0),
        }

    def floor_production(self, date_from, date_to) -> List[Dict[str, Any]]:
        """Everything received onto the finished floor, by day.

        EVERY ITEM, not just the plan's. The plan lists what the month intends
        to make; the floor also makes things nobody planned, and on Oil in
        September that was a fifth of the month's tonnage. A produced figure
        read through the plan's own item list cannot see any of it, which makes
        the board report less than the plant made.

        One row per day that produced -- days with no output are simply absent,
        which is what makes "average per producing day" computable: the caller
        counts rows rather than calendar days. Pieces are in the item's own
        inventory unit, the same unit the plan is held in, so no conversion
        stands between the two.
        """
        query = f"""
            SELECT
                N."DocDate"                                   AS "Day",
                SUM(N."InQty")                                AS "Pieces",
                SUM(N."InQty" * ({LITRES_PER_UNIT}))          AS "Litres"
            FROM "{self.schema}"."OINM" N
            JOIN "{self.schema}"."OITM" M ON M."ItemCode" = N."ItemCode"
            WHERE N."TransType" = {TRANS_TYPE_PRODUCTION_RECEIPT}
              AND N."Warehouse" = ?
              AND IFNULL(N."InQty", 0) > 0
              AND N."DocDate" >= ? AND N."DocDate" <= ?
            GROUP BY N."DocDate"
            ORDER BY N."DocDate"
        """
        return self._rows(query, [PRODUCTION_FLOOR, date_from, date_to])

    # ------------------------------------------------------------------
    # 3. What was ordered this plan month, and how much of it came
    # ------------------------------------------------------------------

    def pm_purchase_orders(self, date_from, date_to, item_codes=None) -> Dict[str, Any]:
        """Packing-material purchase orders RAISED in the window.

        Three questions in one read: how many orders were placed, how much they
        ordered, and how much of that has arrived. Scoped by the order's own
        ``DocDate``, so this is buying activity for the plan month rather than
        the standing open-order book — a different question from the one the
        requirement sheet asks when it nets open orders off a shortage.

        RECEIVED IS ``Quantity - OpenQty``, WHICH IS SAP'S OWN ANSWER AND HAS
        ONE EDGE. A line closed by hand, or an order cancelled, also carries
        ``OpenQty = 0`` and therefore reads here as fully received. The closed
        line count comes back alongside so the size of that edge is visible
        rather than assumed away; on a month where it matters, the figure to
        reach for is the goods receipt itself.

        Quantities are in the PURCHASE unit, not the inventory unit. For
        packaging the two are the same — cartons and caps are bought and
        counted in pieces — and that equality is what lets the over-purchase
        tile compare ordered against the BOM requirement. It is an assumption,
        and it is the one to check first if that tile ever reads oddly: an item
        bought by the box and consumed by the piece would overstate the
        over-purchase by its pack size. The ordered, received and open figures
        here are all in the one unit, so at least they tie to each other.

        SCOPED TO THE PLAN'S OWN ITEMS when codes are given, which is how the
        board calls it. Without that the tile counts packing bought for things
        this month is not making -- on Oil in September that was preform for
        the blowing line and two newly created packs, real buying that the
        month's plan cannot account for and that made the tile disagree with
        every figure beside it. The unscoped read is kept for callers that
        genuinely want all packing material.

        Grouped by order AND item, so one read answers three things: the
        distinct order count, the totals, and the per-item quantities. Chunked
        at 400 codes like every other code list here, and aggregated in Python
        rather than in a second grouped query, which keeps this to one SAP
        round trip per chunk on a board that re-reads every minute.
        """
        codes = sorted({code for code in (item_codes or []) if code})
        chunks = [codes[i:i + 400] for i in range(0, len(codes), 400)] or [None]

        rows: List[Dict[str, Any]] = []
        for chunk in chunks:
            where = ""
            params: List[Any] = [date_from, date_to]
            if chunk:
                where = f'AND L."ItemCode" IN ({", ".join(["?"] * len(chunk))})'
                params.extend(chunk)
            query = f"""
                SELECT
                    H."DocEntry"                                  AS "DocEntry",
                    L."ItemCode"                                  AS "ItemCode",
                    COUNT(*)                                      AS "Lines",
                    ROUND(COALESCE(SUM(L."Quantity"), 0), 3)      AS "OrderedQty",
                    ROUND(COALESCE(SUM(L."OpenQty"), 0), 3)       AS "OpenQty",
                    -- Quantity x Price, both in the PURCHASE unit, so the
                    -- product is a clean amount. This is the one place the
                    -- purchase-unit price is safe to use: multiplied by the
                    -- quantity it was quoted against rather than by an
                    -- inventory figure.
                    ROUND(COALESCE(SUM(L."Quantity" * L."Price"), 0), 2) AS "OrderedValue",
                    ROUND(COALESCE(SUM(L."OpenQty" * L."Price"), 0), 2)  AS "OpenValue",
                    SUM(CASE WHEN L."LineStatus" <> 'O' THEN 1 ELSE 0 END) AS "ClosedLines"
                FROM "{self.schema}"."OPOR" H
                INNER JOIN "{self.schema}"."POR1" L ON L."DocEntry" = H."DocEntry"
                INNER JOIN "{self.schema}"."OITM" M ON M."ItemCode" = L."ItemCode"
                WHERE M."ItmsGrpCod" = {PACKAGING_ITEM_GROUP}
                  AND H."DocDate" >= ?
                  AND H."DocDate" <= ?
                  {where}
                GROUP BY H."DocEntry", L."ItemCode"
            """
            rows.extend(self._rows(query, params))

        orders = set()
        by_item: Dict[str, float] = {}
        lines = 0
        ordered = 0.0
        still_open = 0.0
        ordered_value = 0.0
        open_value = 0.0
        closed_lines = 0
        for row in rows:
            orders.add(row.get("DocEntry"))
            code = row.get("ItemCode") or ""
            qty = float(row.get("OrderedQty") or 0)
            by_item[code] = by_item.get(code, 0.0) + qty
            lines += int(row.get("Lines") or 0)
            ordered += qty
            still_open += float(row.get("OpenQty") or 0)
            ordered_value += float(row.get("OrderedValue") or 0)
            open_value += float(row.get("OpenValue") or 0)
            closed_lines += int(row.get("ClosedLines") or 0)

        return {
            "orders": len(orders),
            "lines": lines,
            "ordered_qty": round(ordered, 3),
            "open_qty": round(still_open, 3),
            # Never negative: an over-receipt would otherwise read as a
            # negative arrival, which is not a thing a store can do.
            "received_qty": round(max(0.0, ordered - still_open), 3),
            "ordered_value": round(ordered_value, 2),
            "open_value": round(open_value, 2),
            # What has arrived, in money: ordered less what is still open, on
            # the same per-line prices. Floored for the same reason as the
            # quantity -- a store cannot receive a negative amount.
            "received_value": round(max(0.0, ordered_value - open_value), 2),
            "closed_lines": closed_lines,
            # Ordered quantity per item, for the over-purchase arithmetic.
            "by_item": {code: round(qty, 3) for code, qty in by_item.items() if code},
        }

    def pm_open_po_by_age(self, raised_from, item_codes=None) -> Dict[str, Any]:
        """Open purchase-order quantity per item, split by when the order was
        raised: inside the plan month, or before it.

        The open book and the month's buying are different questions, and the
        difference is the interesting one -- an order placed this month is
        buying, an order still open from March is a chase. Read off the ORDER
        rather than off the requirement sheet because the sheet carries no
        document date: it nets open orders into a shortage and never asks their
        age.

        Only genuinely open lines: ``LineStatus = 'O'`` AND ``OpenQty > 0``, so
        a line a buyer closed by hand drops out rather than reading as
        outstanding. Grouped by item and document date and folded in Python --
        HANA will not group on a CASE over a parameter.
        """
        codes = sorted({code for code in (item_codes or []) if code})
        chunks = [codes[i:i + 400] for i in range(0, len(codes), 400)] or [None]
        cutoff = str(raised_from)[:10]

        by_item: Dict[str, Dict[str, float]] = {}
        for chunk in chunks:
            where = ""
            params: List[Any] = []
            if chunk:
                where = f'AND L."ItemCode" IN ({", ".join(["?"] * len(chunk))})'
                params.extend(chunk)
            query = f"""
                SELECT
                    L."ItemCode"                             AS "ItemCode",
                    H."DocDate"                              AS "DocDate",
                    COUNT(*)                                 AS "Lines",
                    ROUND(COALESCE(SUM(L."OpenQty"), 0), 3)  AS "OpenQty"
                FROM "{self.schema}"."OPOR" H
                INNER JOIN "{self.schema}"."POR1" L ON L."DocEntry" = H."DocEntry"
                INNER JOIN "{self.schema}"."OITM" M ON M."ItemCode" = L."ItemCode"
                WHERE M."ItmsGrpCod" = {PACKAGING_ITEM_GROUP}
                  AND L."LineStatus" = 'O'
                  AND L."OpenQty" > 0
                  {where}
                GROUP BY L."ItemCode", H."DocDate"
            """
            for row in self._rows(query, params):
                code = row.get("ItemCode") or ""
                if not code:
                    continue
                recent = str(row.get("DocDate") or "")[:10] >= cutoff
                held = by_item.setdefault(
                    code, {"recent_qty": 0.0, "older_qty": 0.0,
                           "recent_lines": 0, "older_lines": 0}
                )
                qty = float(row.get("OpenQty") or 0)
                lines = int(row.get("Lines") or 0)
                held["recent_qty" if recent else "older_qty"] += qty
                held["recent_lines" if recent else "older_lines"] += lines

        return {
            code: {
                "recent_qty": round(held["recent_qty"], 3),
                "older_qty": round(held["older_qty"], 3),
                "recent_lines": held["recent_lines"],
                "older_lines": held["older_lines"],
            }
            for code, held in by_item.items()
        }

    def pm_goods_receipts(self, date_from, date_to, item_codes=None) -> Dict[str, Any]:
        """Packing material RECEIVED in the window, off the goods receipts.

        The receipt document itself, not ``Quantity - OpenQty`` on the order.
        The order-side figure answers "how much of what we ordered this month
        has landed"; this one answers "how much landed this month", whatever
        month its order was raised in -- which on this company is the question
        worth asking, since every open packing line was already past due on 9
        September. It also sidesteps the edge that costs the order-side figure:
        a line closed by hand carries ``OpenQty = 0`` and reads there as fully
        received, while a goods receipt exists only where goods arrived.

        Cancelled receipts are excluded. Value is ``LineTotal`` -- the receipt's
        own amount after discount, which is what the company was billed, rather
        than quantity times a list price.

        Scoped to the plan's own item codes when given, so the figure describes
        the same items the tile above it does. Chunked at 400 like every other
        code list here: HANA takes a long ``IN`` badly and the board re-reads
        every minute.
        """
        codes = sorted({code for code in (item_codes or []) if code})
        chunks = [codes[i:i + 400] for i in range(0, len(codes), 400)] or [None]

        docs = set()
        lines = 0
        qty = 0.0
        value = 0.0
        by_item: Dict[str, Dict[str, float]] = {}

        for chunk in chunks:
            where = ""
            params: List[Any] = [date_from, date_to]
            if chunk:
                where = f'AND L."ItemCode" IN ({", ".join(["?"] * len(chunk))})'
                params.extend(chunk)
            query = f"""
                SELECT
                    H."DocEntry"                              AS "DocEntry",
                    L."ItemCode"                              AS "ItemCode",
                    COUNT(*)                                  AS "Lines",
                    ROUND(COALESCE(SUM(L."Quantity"), 0), 3)  AS "Qty",
                    ROUND(COALESCE(SUM(L."LineTotal"), 0), 2) AS "Value"
                FROM "{self.schema}"."OPDN" H
                INNER JOIN "{self.schema}"."PDN1" L ON L."DocEntry" = H."DocEntry"
                INNER JOIN "{self.schema}"."OITM" M ON M."ItemCode" = L."ItemCode"
                WHERE M."ItmsGrpCod" = {PACKAGING_ITEM_GROUP}
                  AND H."DocDate" >= ?
                  AND H."DocDate" <= ?
                  AND H."CANCELED" = 'N'
                  {where}
                GROUP BY H."DocEntry", L."ItemCode"
            """
            for row in self._rows(query, params):
                code = row.get("ItemCode") or ""
                row_qty = float(row.get("Qty") or 0)
                row_value = float(row.get("Value") or 0)
                docs.add(row.get("DocEntry"))
                lines += int(row.get("Lines") or 0)
                qty += row_qty
                value += row_value
                if code:
                    held = by_item.setdefault(code, {"qty": 0.0, "value": 0.0})
                    held["qty"] += row_qty
                    held["value"] += row_value

        return {
            "docs": len(docs),
            "lines": lines,
            "qty": round(qty, 3),
            "value": round(value, 2),
            "by_item": {
                code: {"qty": round(held["qty"], 3), "value": round(held["value"], 2)}
                for code, held in by_item.items()
            },
        }

    # ------------------------------------------------------------------
    # 4. What a piece holds, and what kind of material a code is
    # ------------------------------------------------------------------

    def packaging_stock(self, warehouses) -> List[Dict[str, Any]]:
        """Packaging material held, one row per store and item.

        PER ITEM, not per store, because the floor it stands on depends on WHAT
        it is: 300 five-litre bottles fill a pallet and 60,000 caps fill one.
        Aggregating to the store first would throw away the only thing that
        makes the area calculable.

        Priced at ``OITM.LastPurPrc``, the same price every other rupee figure
        on this board uses, so the stores' holding and the plan's requirement
        are quoted on one basis.

        Matched on the item GROUP NAME rather than on the code 105, and on the
        same name the requirement sheet uses -- a renumbered group would
        otherwise make this quietly wrong instead of visibly empty.
        """
        codes = [code for code in (warehouses or []) if code]
        if not codes:
            return []

        placeholders = ", ".join(["?"] * len(codes))
        query = f"""
            SELECT
                W."WhsCode"                                        AS "Warehouse",
                W."ItemCode"                                       AS "ItemCode",
                SUM(W."OnHand")                                    AS "Pieces",
                SUM(W."OnHand" * IFNULL(M."LastPurPrc", 0))        AS "Value",
                MAX(CASE WHEN IFNULL(M."LastPurPrc", 0) = 0 THEN 1 ELSE 0 END)
                                                                   AS "Unpriced"
            FROM "{self.schema}"."OITW" W
            JOIN "{self.schema}"."OITM" M ON M."ItemCode" = W."ItemCode"
            JOIN "{self.schema}"."OITB" G ON G."ItmsGrpCod" = M."ItmsGrpCod"
            WHERE G."ItmsGrpNam" = ?
              AND W."OnHand" > 0
              AND W."WhsCode" IN ({placeholders})
            GROUP BY W."WhsCode", W."ItemCode"
        """
        rows = self._rows(query, [BENCHMARK_ITEM_GROUP, *codes])
        return [
            {
                "warehouse": row.get("Warehouse") or "",
                "item_code": row.get("ItemCode") or "",
                "pieces": float(row.get("Pieces") or 0),
                "value": float(row.get("Value") or 0),
                "unpriced": bool(row.get("Unpriced")),
            }
            for row in rows
        ]

    def litres_per_piece(self, item_codes) -> Dict[str, float]:
        """Litres in one piece, per item code, straight off the item master.

        The Shifting band needs this because its own register counts BOXES. A
        box scan stores pieces, not volume, so the tonnage can only come from
        SAP — and it has to come through the same gate every other litre figure
        on this board passes: ``U_IsLitre = 'Y'``. An item without the flag is
        returned as 0 rather than guessed at from its name, and the caller
        counts those so a tile made of tonnes can say what it left out.
        """
        codes = [code for code in {c for c in (item_codes or []) if c}]
        if not codes:
            return {}

        out: Dict[str, float] = {}
        for start in range(0, len(codes), 400):
            chunk = codes[start:start + 400]
            placeholders = ", ".join(["?"] * len(chunk))
            query = f"""
                SELECT M."ItemCode", ({LITRES_PER_UNIT}) AS "Litres"
                FROM "{self.schema}"."OITM" M
                WHERE M."ItemCode" IN ({placeholders})
            """
            for row in self._rows(query, list(chunk)):
                out[row["ItemCode"]] = float(row.get("Litres") or 0)
        return out

    def classify_items(self, item_codes) -> Dict[str, str]:
        """RAW / PACKAGING / OTHER per item code, from the SAP item group.

        Reuses ``planning_purchase.classify_material`` on the same
        ``OITB.ItmsGrpNam`` it classifies on, so a material that reads as
        packaging on the requirement sheet cannot read as raw material here.
        Needed because ``WasteLog`` stores a material code and no kind, and the
        board has to split oil waste from packaging waste.

        Codes the query does not find come back absent rather than as OTHER —
        an unclassifiable code is a question, not a category, and the caller
        counts them so the tile can disclose its own edge.
        """
        from planning_purchase.hana_reader import classify_material

        codes = [code for code in (item_codes or []) if code]
        if not codes:
            return {}

        out: Dict[str, str] = {}
        for start in range(0, len(codes), 400):
            chunk = codes[start:start + 400]
            placeholders = ", ".join(["?"] * len(chunk))
            query = f"""
                SELECT M."ItemCode", IFNULL(G."ItmsGrpNam", '') AS "ItemGroup"
                FROM "{self.schema}"."OITM" M
                LEFT JOIN "{self.schema}"."OITB" G ON G."ItmsGrpCod" = M."ItmsGrpCod"
                WHERE M."ItemCode" IN ({placeholders})
            """
            for row in self._rows(query, list(chunk)):
                out[row["ItemCode"]] = classify_material(row.get("ItemGroup"))
        return out

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
            logger.error("SAP HANA connection failed (plant_board): %s", exc)
            raise SAPConnectionError(
                "Unable to connect to SAP HANA. Please try again later."
            ) from exc

        try:
            cursor = conn.cursor()
            cursor.execute(query, params or [])
            columns = [c[0] for c in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]
        except dbapi.Error as exc:
            logger.error("SAP HANA query failed (plant_board): %s", exc)
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
