"""SAP reads a dismantle (disassembly) needs before it can be posted.

Dismantling a finished good in SAP is a **disassembly production order**
(``OWOR."Type" = 'D'``), completed by a Receipt from Production that brings the
components back and a Goods Issue that consumes the parent. Three things have to
be read from SAP before any of that can be built, and none is guessable:

* **the recipe** -- which components one piece of the parent yields, and in what
  quantity
* **what is actually in the warehouse** -- only stock that exists can be taken
  apart, and only an item with a production BOM can be exploded at all
* **the parent's own identity** -- its UoM, whether SAP will demand a batch, and
  the box size its recipe is written for

THE BOM TRAP, AGAIN
-------------------
``OITT."Qauntity"`` (SAP's own typo) is the batch the recipe is written for and
is usually the box -- 16 for a 16-piece carton -- so the per-PIECE quantity is
``ITT1."Quantity" / OITT."Qauntity"``. ``ITT1."Type"`` must be 4: type 290 is a
resource (the ``JWPL...`` labour line), it lives in ORSC rather than OITM, and
SAP leaves it off a disassembly order by itself -- 0 labour lines across 411 live
orders. Both rules are the ones ``packing_material/hana_reader.py`` and
``planning_purchase/hana_reader.py`` already obey; this module obeys them too.

Where ``OITT."Qauntity"`` disagrees with ``OITM."SalFactor2"`` the explosion is
wrong -- 24 Oil recipes are written per box against a batch size of 1, which
inflates every component by the box size. SAP's own disassembly screen divides by
the same field and inflates identically, so this is reported rather than
corrected: silently "fixing" it here would make the app's document disagree with
the one SAP would have produced, and the real fix is in the item master.
"""

import logging
from typing import Iterable, List, Optional

from hdbcli import dbapi

from .connection import HanaConnection
from ..exceptions import SAPConnectionError, SAPDataError

logger = logging.getLogger(__name__)

# ITT1."Type" 4 is an inventory item; 290 is a resource (labour), which has no
# OITM row and never appears on a disassembly order.
BOM_LINE_TYPE_ITEM = 4
# Only production BOMs can be taken apart. A sales BOM (TreeType 'S') is exploded
# by SAP on the document itself and has no components to receive back.
BOM_TREE_TYPE_PRODUCTION = "P"


class HanaDismantleReader:
    """Read the recipe, the stock and the item master a dismantle needs."""

    def __init__(self, context):
        self.connection = HanaConnection(context.hana)

    # ------------------------------------------------------------------
    # The recipe
    # ------------------------------------------------------------------

    def bom_components(self, item_code: str) -> List[dict]:
        """What one PIECE of ``item_code`` yields, straight from its production BOM.

        Empty when the item has no production BOM -- which is a real answer, not
        an error: an item SAP cannot explode cannot be dismantled, and the caller
        says so in those words rather than posting an order with no components.
        """
        if not (item_code or "").strip():
            return []

        rows = self._query(
            """
            SELECT
                C."Code",
                I."ItemName",
                C."Quantity" / NULLIF(T."Qauntity", 0) AS "QtyPerPiece",
                I."InvntryUom",
                I."ManBtchNum",
                I."ItmsGrpCod",
                C."Warehouse"
            FROM "{schema}"."OITT" T
            INNER JOIN "{schema}"."ITT1" C ON C."Father" = T."Code"
            INNER JOIN "{schema}"."OITM" I ON I."ItemCode" = C."Code"
            WHERE T."Code" = ?
              AND T."TreeType" = ?
              AND C."Type" = ?
              AND C."Quantity" / NULLIF(T."Qauntity", 0) IS NOT NULL
            ORDER BY C."ChildNum"
            """,
            (str(item_code), BOM_TREE_TYPE_PRODUCTION, BOM_LINE_TYPE_ITEM),
        )
        return [
            {
                "item_code": row[0] or "",
                "item_name": row[1] or "",
                "qty_per_piece": float(row[2] or 0),
                "uom": row[3] or "",
                "is_batch_managed": (row[4] or "N") == "Y",
                "item_group": row[5],
                "bom_warehouse": row[6] or "",
            }
            for row in rows
            if (row[0] or "").strip()
        ]

    # ------------------------------------------------------------------
    # The parent
    # ------------------------------------------------------------------

    def parent_info(self, item_code: str) -> Optional[dict]:
        """The parent item's master row plus the batch size its recipe assumes.

        ``bom_batch_size`` and ``pieces_per_box`` are both returned so the caller
        can compare them: they should be the same number, and where they are not
        the per-piece explosion is off by their ratio.
        """
        rows = self._query(
            """
            SELECT
                I."ItemCode",
                I."ItemName",
                I."InvntryUom",
                I."ManBtchNum",
                I."SalFactor2",
                T."Qauntity",
                I."InvntItem"
            FROM "{schema}"."OITM" I
            LEFT JOIN "{schema}"."OITT" T
                ON T."Code" = I."ItemCode" AND T."TreeType" = ?
            WHERE I."ItemCode" = ?
            """,
            (BOM_TREE_TYPE_PRODUCTION, str(item_code)),
        )
        if not rows:
            return None
        row = rows[0]
        return {
            "item_code": row[0] or "",
            "item_name": row[1] or "",
            "uom": row[2] or "",
            "is_batch_managed": (row[3] or "N") == "Y",
            "pieces_per_box": float(row[4] or 0),
            "bom_batch_size": float(row[5]) if row[5] is not None else None,
            "is_inventory_item": (row[6] or "N") == "Y",
        }

    # ------------------------------------------------------------------
    # What is there to take apart
    # ------------------------------------------------------------------

    def dismantlable_stock(
        self,
        warehouse_code: str,
        *,
        search: str = "",
        limit: int = 50,
    ) -> List[dict]:
        """Items held in ``warehouse_code`` that SAP could actually explode.

        Joined to ``OITT`` rather than filtered afterwards: an item with stock but
        no production BOM cannot be dismantled at all, and offering it would only
        let the operator pick something that fails at post time.
        """
        safe_limit = max(1, min(int(limit or 50), 200))
        where = [
            'W."WhsCode" = ?',
            'W."OnHand" > 0',
            'I."InvntItem" = ?',
            'T."TreeType" = ?',
        ]
        params: list = [str(warehouse_code), "Y", BOM_TREE_TYPE_PRODUCTION]

        if search:
            where.append('(UPPER(I."ItemCode") LIKE ? OR UPPER(I."ItemName") LIKE ?)')
            token = "%" + str(search).strip().upper() + "%"
            params += [token, token]

        rows = self._query(
            """
            SELECT
                W."ItemCode",
                I."ItemName",
                W."OnHand",
                I."InvntryUom",
                I."ManBtchNum",
                I."SalFactor2",
                T."Qauntity"
            FROM "{schema}"."OITW" W
            INNER JOIN "{schema}"."OITM" I ON I."ItemCode" = W."ItemCode"
            INNER JOIN "{schema}"."OITT" T ON T."Code" = W."ItemCode"
            WHERE """
            + " AND ".join(where)
            + """
            ORDER BY I."ItemName"
            LIMIT """
            + str(safe_limit),
            tuple(params),
        )
        return [
            {
                "item_code": row[0] or "",
                "item_name": row[1] or "",
                "on_hand": float(row[2] or 0),
                "uom": row[3] or "",
                "is_batch_managed": (row[4] or "N") == "Y",
                "pieces_per_box": float(row[5] or 0),
                "bom_batch_size": float(row[6]) if row[6] is not None else None,
            }
            for row in rows
        ]

    def return_batches(
        self, warehouse_codes: Iterable[str], *, prefix: str = "GR-"
    ) -> List[dict]:
        """The batches a goods return minted, as SAP actually holds them.

        The returns module derives its batch numbers from the return's entry
        number, so they can be found by prefix -- but the number cannot be
        RE-derived and trusted, because the formula changed: returns posted
        before the fix numbered their lines by position (``GR-20260827-0001-0``)
        and ones posted after number them by the line's database id
        (``GR-20260827-0001-18``). Recomputing therefore produces a batch that
        does not exist for every return booked before the change.

        So the batch is read back rather than computed. Everything in the
        warehouse whose batch number starts with ``GR-`` comes back in one query;
        the caller matches by item and entry number.
        """
        codes = [str(c) for c in warehouse_codes if c]
        if not codes:
            return []

        rows = self._query(
            """
            SELECT "ItemCode", "BatchNum", "WhsCode", "Quantity"
            FROM "{schema}"."OIBT"
            WHERE "WhsCode" IN ("""
            + ", ".join(["?"] * len(codes))
            + """)
              AND "BatchNum" LIKE ?
              AND "Quantity" > 0
            ORDER BY "BatchNum"
            """,
            tuple(codes + [f"{prefix}%"]),
        )
        return [
            {
                "item_code": row[0] or "",
                "batch_number": row[1] or "",
                "warehouse_code": row[2] or "",
                "quantity": float(row[3] or 0),
            }
            for row in rows
        ]

    def existing_batches(
        self, item_codes: Iterable[str], batch_numbers: Iterable[str]
    ) -> set:
        """Which (item, batch) pairs SAP already knows -- in ANY warehouse.

        A Receipt from Production may not reuse a batch number that exists
        (``590001 Duplicate Batch not Allowed, Batch No Must be Unique``), and the
        rule is company-wide rather than per warehouse, so the minted numbers are
        checked against the whole of ``OBTN`` before the document is built.
        """
        items = [c for c in {str(c) for c in item_codes} if c]
        batches = [b for b in {str(b) for b in batch_numbers} if b]
        if not items or not batches:
            return set()

        rows = self._query(
            """
            SELECT "ItemCode", "DistNumber"
            FROM "{schema}"."OBTN"
            WHERE "ItemCode" IN ("""
            + ", ".join(["?"] * len(items))
            + """)
              AND "DistNumber" IN ("""
            + ", ".join(["?"] * len(batches))
            + ")",
            tuple(items + batches),
        )
        return {(row[0], row[1]) for row in rows}

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _query(self, sql: str, params: tuple) -> list:
        conn = None
        cursor = None

        try:
            conn = self.connection.connect()
        except dbapi.Error as e:
            logger.error("SAP HANA connection failed while reading dismantle data: %s", e)
            raise SAPConnectionError("Unable to connect to SAP HANA.") from e

        try:
            cursor = conn.cursor()
            cursor.execute(sql.replace("{schema}", self.connection.schema), params)
            return cursor.fetchall()
        except dbapi.Error as e:
            logger.error("SAP HANA dismantle query failed: %s", e)
            raise SAPDataError(
                "Failed to read the data SAP needs for a dismantle."
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
