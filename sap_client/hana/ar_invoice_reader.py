"""HANA reads behind the factory A/R-invoice module.

Mirror of ``ap_invoice_reader`` for the sales side: pick a customer's open
Sales Order lines, raise an A/R invoice (which SAP holds as an ObjType-13
approval draft), then track the draft until it becomes a posted OINV document.

Data facts (verified live):

* An A/R invoice line copied from a Sales Order lands in ``INV1`` with
  ``BaseType = 17`` and ``BaseEntry/BaseLine`` pointing at ``RDR1``; SAP also
  maintains ``RDR1.OpenQty`` directly, so open lines are simply
  ``LineStatus = 'O' AND OpenQty > 0`` — no anti-join needed. A draft pending
  approval does NOT reduce ``OpenQty`` (only the posted invoice does), so our
  own in-flight submissions are excluded app-side.
* ``OINV.draftKey`` links a posted invoice back to its approval draft.
* A/R drafts carry no batch allocations (``DRF16`` empty for ObjType 13);
  ``draft_lines`` feeds the FIFO allocation written onto the draft before it
  is added.
"""

import logging
from typing import Optional

from hdbcli import dbapi

from .connection import HanaConnection
from ..exceptions import SAPConnectionError, SAPDataError

logger = logging.getLogger(__name__)


class HanaARInvoiceReader:
    def __init__(self, context):
        self.connection = HanaConnection(context.hana)

    def open_so_lines(
        self,
        card_code: str,
        search: Optional[str] = None,
        limit: int = 300,
    ) -> list[dict]:
        """One customer's open Sales Order lines (invoiceable open quantity)."""
        card_code = (card_code or "").strip()
        if not card_code:
            return []
        safe_limit = max(1, min(int(limit or 300), 500))

        where = [
            """IFNULL(H."CANCELED", 'N') = 'N'""",
            """H."DocStatus" = 'O'""",
            """H."CardCode" = ?""",
            """IFNULL(L."LineStatus", 'O') = 'O'""",
            """IFNULL(L."OpenQty", 0) > 0""",
        ]
        params: list = [card_code]
        if search:
            term = f"%{search.lower()}%"
            where.append(
                """(
                    LOWER(TO_NVARCHAR(H."DocNum")) LIKE ?
                    OR LOWER(IFNULL(H."NumAtCard", '')) LIKE ?
                    OR LOWER(IFNULL(H."Comments", '')) LIKE ?
                    OR LOWER(IFNULL(L."ItemCode", '')) LIKE ?
                    OR LOWER(IFNULL(L."Dscription", '')) LIKE ?
                )"""
            )
            params.extend([term] * 5)

        rows = self._query(
            f"""
            SELECT
                H."DocEntry", H."DocNum", H."DocDate",
                IFNULL(H."NumAtCard", ''), IFNULL(H."Comments", ''),
                H."BPLId", IFNULL(H."CardName", ''),
                L."LineNum", IFNULL(L."ItemCode", ''), IFNULL(L."Dscription", ''),
                L."OpenQty", IFNULL(L."Price", 0),
                IFNULL(L."OpenSum", L."OpenQty" * IFNULL(L."Price", 0)),
                IFNULL(NULLIF(L."TaxCode", ''), IFNULL(L."VatGroup", '')),
                IFNULL(L."WhsCode", ''), IFNULL(L."unitMsr", '')
            FROM "{{schema}}"."ORDR" H
            JOIN "{{schema}}"."RDR1" L ON L."DocEntry" = H."DocEntry"
            WHERE {" AND ".join(where)}
            ORDER BY H."DocDate" DESC, H."DocEntry" DESC, L."LineNum"
            LIMIT {safe_limit}
            """,
            tuple(params),
        )

        lines = []
        for (
            doc_entry, doc_num, doc_date, num_at_card, comments,
            branch_id, card_name,
            line_num, item_code, description,
            open_qty, price, open_total, tax_code, whs_code, uom,
        ) in rows:
            lines.append({
                "so_doc_entry": int(doc_entry),
                "so_doc_num": int(doc_num) if doc_num is not None else None,
                "so_doc_date": self._date(doc_date),
                "so_customer_ref": num_at_card or "",
                "so_comments": comments or "",
                "branch_id": int(branch_id) if branch_id is not None else None,
                "customer_name": card_name or "",
                "line_num": int(line_num),
                "item_code": item_code or "",
                "description": description or "",
                # Invoicing a base SO line without a Quantity copies the OPEN
                # quantity — this is what the invoice will carry.
                "open_qty": float(open_qty) if open_qty is not None else 0.0,
                "price": float(price or 0),
                "open_total": float(open_total or 0),
                "tax_code": tax_code or "",
                "warehouse_code": whs_code or "",
                "uom": uom or "",
            })
        return lines

    def invoice_for_draft(self, draft_entry: int) -> Optional[dict]:
        """The posted OINV invoice created from one approval draft, if any."""
        rows = self._query(
            """
            SELECT "DocEntry", "DocNum", "DocTotal"
            FROM "{schema}"."OINV"
            WHERE "draftKey" = ? AND IFNULL("CANCELED", 'N') = 'N'
            LIMIT 1
            """,
            (int(draft_entry),),
        )
        if not rows:
            return None
        doc_entry, doc_num, doc_total = rows[0]
        return {
            "doc_entry": int(doc_entry),
            "doc_num": int(doc_num) if doc_num is not None else None,
            "doc_total": float(doc_total or 0),
        }

    def draft_state(self, draft_entry: int) -> Optional[dict]:
        """The draft's document status plus its latest approval request."""
        rows = self._query(
            """
            SELECT
                D."DocStatus", D."WddStatus", D."DocTotal",
                W."WddCode", W."Status",
                (SELECT MAX(S."Remarks") FROM "{schema}"."WDD1" S
                 WHERE S."WddCode" = W."WddCode" AND S."Status" = 'N')
            FROM "{schema}"."ODRF" D
            LEFT JOIN "{schema}"."OWDD" W
                ON W."DraftEntry" = D."DocEntry" AND W."ObjType" = '13'
               AND W."WddCode" = (
                    SELECT MAX(W2."WddCode") FROM "{schema}"."OWDD" W2
                    WHERE W2."DraftEntry" = D."DocEntry" AND W2."ObjType" = '13'
               )
            WHERE D."DocEntry" = ? AND D."ObjType" = '13'
            """,
            (int(draft_entry),),
        )
        if not rows:
            return None
        doc_status, wdd_status, doc_total, wdd_code, request_status, reject_remarks = rows[0]
        return {
            "doc_status": doc_status,
            "wdd_status": wdd_status,
            "doc_total": float(doc_total or 0),
            "approval_code": int(wdd_code) if wdd_code is not None else None,
            "approval_status": request_status,  # 'W' waiting / 'Y' approved / 'N' rejected
            "reject_remarks": reject_remarks or None,
        }

    def draft_lines(self, draft_entry: int) -> list[dict]:
        """The draft's own lines (DRF1) — the authoritative LineNum/quantity/
        warehouse set the batch allocation must be written against."""
        rows = self._query(
            """
            SELECT L."LineNum", IFNULL(L."ItemCode", ''), L."Quantity",
                   IFNULL(L."WhsCode", '')
            FROM "{schema}"."DRF1" L
            JOIN "{schema}"."ODRF" D
                ON D."DocEntry" = L."DocEntry" AND D."ObjType" = '13'
            WHERE L."DocEntry" = ?
            ORDER BY L."LineNum"
            """,
            (int(draft_entry),),
        )
        return [
            {
                "line_num": int(line_num),
                "item_code": item_code or "",
                "quantity": float(quantity) if quantity is not None else 0.0,
                "warehouse_code": whs_code or "",
            }
            for line_num, item_code, quantity, whs_code in rows
        ]

    def last_sale_defaults(self, card_code: str, item_codes: list[str]) -> dict:
        """Item -> {price, tax_code} from the customer's most recent invoice
        line for it — the defaults a direct (cash) sale line starts from."""
        codes = [c for c in {str(c) for c in item_codes} if c]
        card_code = (card_code or "").strip()
        if not codes or not card_code:
            return {}
        placeholders = ", ".join(["?"] * len(codes))
        rows = self._query(
            f"""
            SELECT "ItemCode", "Price", "TaxCode" FROM (
                SELECT
                    L."ItemCode", L."Price",
                    IFNULL(NULLIF(L."TaxCode", ''), IFNULL(L."VatGroup", '')) AS "TaxCode",
                    ROW_NUMBER() OVER (
                        PARTITION BY L."ItemCode"
                        ORDER BY H."DocDate" DESC, H."DocEntry" DESC
                    ) AS "rn"
                FROM "{{schema}}"."OINV" H
                JOIN "{{schema}}"."INV1" L ON L."DocEntry" = H."DocEntry"
                WHERE H."CardCode" = ?
                  AND IFNULL(H."CANCELED", 'N') = 'N'
                  AND L."ItemCode" IN ({placeholders})
            ) WHERE "rn" = 1
            """,
            tuple([card_code] + codes),
        )
        return {
            row[0]: {
                "price": float(row[1]) if row[1] is not None else None,
                "tax_code": row[2] or "",
            }
            for row in rows
        }

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    @staticmethod
    def _date(value):
        if value is None:
            return None
        if hasattr(value, "date"):
            value = value.date()
        return value.strftime("%Y-%m-%d")

    def _query(self, sql: str, params: tuple) -> list:
        conn = None
        cursor = None
        try:
            conn = self.connection.connect()
        except dbapi.Error as e:
            logger.error("SAP HANA connection failed while reading A/R invoices: %s", e)
            raise SAPConnectionError("Unable to connect to SAP HANA.") from e

        try:
            cursor = conn.cursor()
            cursor.execute(sql.replace("{schema}", self.connection.schema), params)
            return cursor.fetchall()
        except dbapi.Error as e:
            logger.error("SAP HANA A/R invoice query failed: %s", e)
            raise SAPDataError("Failed to read A/R invoice data from SAP.") from e
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
