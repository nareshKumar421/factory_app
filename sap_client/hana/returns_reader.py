"""SAP reads a customer goods return needs before it can be posted.

A standalone A/R Return has to carry four things the app does not otherwise know,
each mandatory in `SBO_SP_TRANSACTIONNOTIFICATION` or by SAP itself:

* **Variety** (`CostingCode`, Dimension 1) — error 160013, and 160015 insists it
  match the item's own `U_Sub_Group`
* **Return cost** — error 160021 for batch-managed items. This is what values the
  returned stock: `ReturnCost x Quantity` becomes `OINM.TransValue` exactly.
* **Tax code** — error 160009
* **Customer group** — error 160012 forbids returns for branch customers

None of it is guessable, so all four are read from SAP rather than defaulted.
"""

import logging
from decimal import Decimal
from typing import Iterable, Optional

from hdbcli import dbapi

from .connection import HanaConnection
from ..exceptions import SAPConnectionError, SAPDataError

logger = logging.getLogger(__name__)

# OCRD.GroupCode for internal branch customers. Returns are refused for these
# (error 160012), because a branch's stock comes back by transfer, not by return.
BRANCH_CUSTOMER_GROUP = 100


class HanaReturnsReader:
    """Read the SAP master data and costs an A/R Return needs."""

    def __init__(self, context):
        self.connection = HanaConnection(context.hana)

    # ------------------------------------------------------------------
    # Variety (Dimension 1)
    # ------------------------------------------------------------------

    def variety_codes(self, item_codes: Iterable[str]) -> dict[str, str]:
        """Item code -> the Dimension-1 profit centre SAP expects as its Variety.

        Resolved as `OITM.U_Sub_Group` -> `OPRC.PrcName` -> `OPRC.PrcCode`. The
        code and the name are NOT the same string — GROUNDNUT is `GROUNDNT` —
        so this has to be looked up rather than derived.

        Items missing from the result have no mapping and cannot be returned
        until the SAP team adds one.
        """
        codes = [c for c in {str(c) for c in item_codes} if c]
        if not codes:
            return {}

        placeholders = ", ".join(["?"] * len(codes))
        rows = self._query(
            f"""
            SELECT I."ItemCode", P."PrcCode"
            FROM "{{schema}}"."OITM" I
            JOIN "{{schema}}"."OPRC" P
                ON P."PrcName" = I."U_Sub_Group" AND P."DimCode" = 1
            WHERE I."ItemCode" IN ({placeholders})
            """,
            tuple(codes),
        )
        return {row[0]: row[1] for row in rows if row[1]}

    # ------------------------------------------------------------------
    # Return cost
    # ------------------------------------------------------------------

    def return_costs(
        self, item_codes: Iterable[str], warehouse: str
    ) -> dict[str, Decimal]:
        """Item code -> the unit cost to value the returned stock at.

        Finished goods are batch-valued (`OITM.EvalSystem = 'B'`), so
        `OITW.AvgPrice` is 0 and useless here. The workable figure is
        `OINM.CalcPrice` from the item's most recent movement — preferring the
        warehouse the goods are coming back into, falling back to its last
        movement anywhere, since a first-ever return into a returns warehouse has
        no local history.
        """
        codes = [c for c in {str(c) for c in item_codes} if c]
        if not codes:
            return {}

        placeholders = ", ".join(["?"] * len(codes))
        # Rank each item's movements twice: within the target warehouse first,
        # then anywhere. The lowest rank per item wins.
        rows = self._query(
            f"""
            SELECT "ItemCode", "CalcPrice"
            FROM (
                SELECT
                    M."ItemCode",
                    M."CalcPrice",
                    ROW_NUMBER() OVER (
                        PARTITION BY M."ItemCode"
                        ORDER BY
                            CASE WHEN M."Warehouse" = ? THEN 0 ELSE 1 END,
                            M."TransNum" DESC
                    ) AS rn
                FROM "{{schema}}"."OINM" M
                WHERE M."ItemCode" IN ({placeholders})
                  AND IFNULL(M."CalcPrice", 0) > 0
            )
            WHERE rn = 1
            """,
            (str(warehouse), *codes),
        )
        return {row[0]: Decimal(str(row[1])) for row in rows}

    # ------------------------------------------------------------------
    # Tax code
    # ------------------------------------------------------------------

    def sales_tax_codes(
        self, card_code: str, item_codes: Iterable[str]
    ) -> dict[str, str]:
        """Item code -> the tax code this customer was last billed for it.

        Indian GST depends on the place of supply, so the tax code is a property
        of the customer-item pair (`CG+SG@5` intra-state vs `IGST@5` inter-state)
        and cannot come from the item master — `OITM.TaxCodeAR` is populated on
        exactly one item company-wide. Reversing the code the original sale used
        is both correct and defensible.

        Falls back to the customer's most recently used code for anything, which
        gets the intra/inter-state part right even for an item never sold to them.
        """
        codes = [c for c in {str(c) for c in item_codes} if c]
        if not codes or not card_code:
            return {}

        placeholders = ", ".join(["?"] * len(codes))
        rows = self._query(
            f"""
            SELECT "ItemCode", "TaxCode"
            FROM (
                SELECT
                    L."ItemCode",
                    L."TaxCode",
                    ROW_NUMBER() OVER (
                        PARTITION BY L."ItemCode" ORDER BY H."DocDate" DESC, H."DocEntry" DESC
                    ) AS rn
                FROM "{{schema}}"."INV1" L
                JOIN "{{schema}}"."OINV" H ON H."DocEntry" = L."DocEntry"
                WHERE H."CardCode" = ?
                  AND H."CANCELED" = 'N'
                  AND L."ItemCode" IN ({placeholders})
                  AND IFNULL(L."TaxCode", '') <> ''
            )
            WHERE rn = 1
            """,
            (str(card_code), *codes),
        )
        per_item = {row[0]: row[1] for row in rows}

        missing = [c for c in codes if c not in per_item]
        if missing:
            fallback = self.customer_default_tax_code(card_code)
            if fallback:
                for code in missing:
                    per_item[code] = fallback
        return per_item

    def customer_default_tax_code(self, card_code: str) -> str:
        """The last tax code this customer was billed under, for any item."""
        rows = self._query(
            """
            SELECT L."TaxCode"
            FROM "{schema}"."INV1" L
            JOIN "{schema}"."OINV" H ON H."DocEntry" = L."DocEntry"
            WHERE H."CardCode" = ?
              AND H."CANCELED" = 'N'
              AND IFNULL(L."TaxCode", '') <> ''
            ORDER BY H."DocDate" DESC, H."DocEntry" DESC
            LIMIT 1
            """,
            (str(card_code),),
        )
        return rows[0][0] if rows else ""

    # ------------------------------------------------------------------
    # What a customer could plausibly return
    # ------------------------------------------------------------------

    def customer_items(
        self, card_code: str, *, search: str = "", limit: int = 100
    ) -> list[dict]:
        """Items this customer has actually been invoiced, newest first.

        The right list to offer on a return, for two reasons. It is far narrower
        than the item master (a busy customer has 100-450 distinct items against
        2,277 company-wide), and — more usefully — an item absent from it has no
        tax code, so the return would be refused at posting (error 160009).
        Offering only this list turns that late refusal into a choice the
        operator never makes.

        Carries the last price and tax code so a manually-entered line can be
        pre-filled the way an invoice-based one already is.
        """
        if not (card_code or "").strip():
            return []

        where = ['H."CardCode" = ?', "H.\"CANCELED\" = 'N'"]
        params: list = [str(card_code)]
        if search:
            term = f"%{search.strip().upper()}%"
            where.append(
                "(UPPER(L.\"ItemCode\") LIKE ? OR UPPER(IFNULL(L.\"Dscription\", '')) LIKE ?)"
            )
            params.extend([term, term])

        safe_limit = max(1, min(int(limit or 100), 300))

        rows = self._query(
            f"""
            SELECT "ItemCode", "ItemName", "Uom", "TaxCode", "Price",
                   "DocNum", "LastBilled"
            FROM (
                SELECT
                    L."ItemCode",
                    IFNULL(L."Dscription", '') AS "ItemName",
                    IFNULL(L."unitMsr", '') AS "Uom",
                    IFNULL(L."TaxCode", '') AS "TaxCode",
                    IFNULL(L."Price", 0) AS "Price",
                    H."DocNum",
                    TO_DATE(H."DocDate") AS "LastBilled",
                    ROW_NUMBER() OVER (
                        PARTITION BY L."ItemCode"
                        ORDER BY H."DocDate" DESC, H."DocEntry" DESC
                    ) AS rn
                FROM "{{schema}}"."INV1" L
                JOIN "{{schema}}"."OINV" H ON H."DocEntry" = L."DocEntry"
                WHERE {" AND ".join(where)}
            )
            WHERE rn = 1
            ORDER BY "LastBilled" DESC, "ItemCode"
            LIMIT {safe_limit}
            """,
            tuple(params),
        )
        return [
            {
                "item_code": row[0],
                "item_name": row[1] or "",
                "uom": row[2] or "",
                "tax_code": row[3] or "",
                "last_price": float(row[4] or 0),
                "last_invoice_num": str(row[5] or ""),
                "last_billed": row[6],
            }
            for row in rows
        ]

    # ------------------------------------------------------------------
    # Customer
    # ------------------------------------------------------------------

    def customer_group(self, card_code: str) -> Optional[int]:
        """`OCRD.GroupCode`, so a return for a branch customer can be refused."""
        rows = self._query(
            'SELECT "GroupCode" FROM "{schema}"."OCRD" WHERE "CardCode" = ?',
            (str(card_code),),
        )
        if not rows or rows[0][0] is None:
            return None
        return int(rows[0][0])

    def warehouse_branch(self, warehouse_code: str) -> Optional[int]:
        """`OWHS.BPLid` — the branch a return must be stamped with.

        Omitting it fails with `-5002 Specify an active branch [ORDN.BPLId]`, and
        note the Service Layer spells the field `BPL_IDAssignedToInvoice` on a
        marketing document, not `BPLID`.
        """
        rows = self._query(
            'SELECT "BPLid" FROM "{schema}"."OWHS" WHERE "WhsCode" = ?',
            (str(warehouse_code),),
        )
        if not rows or rows[0][0] is None:
            return None
        return int(rows[0][0])

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _query(self, sql: str, params: tuple) -> list:
        conn = None
        cursor = None

        try:
            conn = self.connection.connect()
        except dbapi.Error as e:
            logger.error("SAP HANA connection failed while reading return data: %s", e)
            raise SAPConnectionError("Unable to connect to SAP HANA.") from e

        try:
            cursor = conn.cursor()
            cursor.execute(sql.replace("{schema}", self.connection.schema), params)
            return cursor.fetchall()
        except dbapi.Error as e:
            logger.error("SAP HANA goods-return query failed: %s", e)
            raise SAPDataError(
                "Failed to read the data SAP needs for a goods return."
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
