"""Read SAP inventory transfer requests (OWTQ/WTQ1).

An inventory transfer request is the *ask*: it moves no stock, but each line
carries an `OpenQty` and a `LineStatus`, and an open line reserves the stock —
it adds to `OITW."IsCommited"` at the source and `OITW."OnOrder"` at the
destination (verified against live rows where the two match to the unit). A
transfer created against the request draws `OpenQty` down and closes the line.

SAP never expires a request, so anything the app raises it must also retire —
see `warehouse/management/commands/close_stale_transfer_requests.py`.
"""

import logging
from datetime import date
from decimal import Decimal
from typing import Optional

from hdbcli import dbapi

from .connection import HanaConnection
from ..exceptions import SAPConnectionError, SAPDataError

logger = logging.getLogger(__name__)

LINE_OPEN = "O"
LINE_CLOSED = "C"


class HanaTransferRequestReader:
    """Read inventory transfer requests from OWTQ/WTQ1."""

    def __init__(self, context):
        self.connection = HanaConnection(context.hana)

    def get_request(self, doc_entry: int) -> Optional[dict]:
        """One request with its lines, including open quantities."""
        header_rows = self._query(
            """
            SELECT
                H."DocEntry", H."DocNum", H."Series",
                TO_DATE(H."DocDate"), TO_DATE(H."DocDueDate"),
                IFNULL(H."Filler", ''), IFNULL(H."ToWhsCode", ''),
                IFNULL(H."DocStatus", ''), IFNULL(H."CANCELED", 'N'),
                IFNULL(H."Comments", ''), IFNULL(H."JrnlMemo", ''),
                H."BPLId", H."UserSign"
            FROM "{schema}"."OWTQ" H
            WHERE H."DocEntry" = ?
            """,
            (int(doc_entry),),
        )
        if not header_rows:
            return None

        row = header_rows[0]
        request = {
            "doc_entry": int(row[0]),
            "doc_num": int(row[1]) if row[1] is not None else None,
            "series": int(row[2]) if row[2] is not None else None,
            "doc_date": row[3],
            "due_date": row[4],
            "from_warehouse": row[5] or "",
            "to_warehouse": row[6] or "",
            "doc_status": row[7] or "",
            "cancelled": (row[8] or "N") == "Y",
            "comments": row[9] or "",
            "journal_memo": row[10] or "",
            "branch_id": int(row[11]) if row[11] is not None else None,
            "user_sign": int(row[12]) if row[12] is not None else None,
        }
        request["lines"] = self._lines(doc_entry)
        request["is_open"] = (
            not request["cancelled"]
            and any(line["line_status"] == LINE_OPEN for line in request["lines"])
        )
        return request

    def list_open_requests(
        self,
        *,
        from_warehouse: Optional[str] = None,
        to_warehouse: Optional[str] = None,
        older_than_days: Optional[int] = None,
        limit: int = 200,
    ) -> list[dict]:
        """Requests with at least one open line, newest first.

        `older_than_days` is how the stale sweep finds requests that are still
        reserving stock long after anyone stopped expecting them to be served.
        """
        where = ['H."CANCELED" = ?', 'L."LineStatus" = ?']
        params: list = ["N", LINE_OPEN]

        if from_warehouse:
            where.append('L."FromWhsCod" = ?')
            params.append(str(from_warehouse))
        if to_warehouse:
            where.append('L."WhsCode" = ?')
            params.append(str(to_warehouse))
        if older_than_days is not None:
            where.append('DAYS_BETWEEN(H."DocDate", CURRENT_DATE) >= ?')
            params.append(int(older_than_days))

        safe_limit = max(1, min(int(limit or 200), 1000))

        rows = self._query(
            f"""
            SELECT
                H."DocEntry", H."DocNum", TO_DATE(H."DocDate"),
                IFNULL(H."Filler", ''), IFNULL(H."ToWhsCode", ''),
                IFNULL(H."Comments", ''),
                DAYS_BETWEEN(H."DocDate", CURRENT_DATE) AS age_days,
                COUNT(L."LineNum") AS open_lines,
                SUM(L."OpenQty") AS open_qty
            FROM "{{schema}}"."OWTQ" H
            JOIN "{{schema}}"."WTQ1" L ON L."DocEntry" = H."DocEntry"
            WHERE {" AND ".join(where)}
            GROUP BY
                H."DocEntry", H."DocNum", H."DocDate", H."Filler",
                H."ToWhsCode", H."Comments"
            ORDER BY H."DocDate" DESC, H."DocEntry" DESC
            LIMIT {safe_limit}
            """,
            tuple(params),
        )
        return [
            {
                "doc_entry": int(r[0]),
                "doc_num": int(r[1]) if r[1] is not None else None,
                "doc_date": r[2],
                "from_warehouse": r[3] or "",
                "to_warehouse": r[4] or "",
                "comments": r[5] or "",
                "age_days": int(r[6] or 0),
                "open_lines": int(r[7] or 0),
                "open_quantity": Decimal(str(r[8] or 0)),
            }
            for r in rows
        ]

    def summarise_requests(self, doc_entries: list[int]) -> dict[int, dict]:
        """Per-document totals for many requests in one query.

        The reconciliation report needs a line summary for every request the app
        has raised; asking per document would be one round trip each, so this
        aggregates them together.
        """
        entries = sorted({int(d) for d in doc_entries if d is not None})
        if not entries:
            return {}

        placeholders = ", ".join(["?"] * len(entries))
        rows = self._query(
            f"""
            SELECT
                H."DocEntry",
                IFNULL(H."DocStatus", ''),
                IFNULL(H."CANCELED", 'N'),
                COUNT(L."LineNum"),
                SUM(CASE WHEN L."LineStatus" = 'O' THEN 1 ELSE 0 END),
                SUM(L."Quantity"),
                SUM(L."OpenQty"),
                DAYS_BETWEEN(H."DocDate", CURRENT_DATE)
            FROM "{{schema}}"."OWTQ" H
            JOIN "{{schema}}"."WTQ1" L ON L."DocEntry" = H."DocEntry"
            WHERE H."DocEntry" IN ({placeholders})
            GROUP BY H."DocEntry", H."DocStatus", H."CANCELED", H."DocDate"
            """,
            tuple(entries),
        )

        summary: dict[int, dict] = {}
        for row in rows:
            total = Decimal(str(row[5] or 0))
            open_qty = Decimal(str(row[6] or 0))
            summary[int(row[0])] = {
                "doc_status": row[1] or "",
                "cancelled": (row[2] or "N") == "Y",
                "line_count": int(row[3] or 0),
                "open_lines": int(row[4] or 0),
                "total_quantity": total,
                "open_quantity": open_qty,
                "served_quantity": total - open_qty,
                "age_days": int(row[7] or 0),
                "is_open": (row[2] or "N") == "N" and int(row[4] or 0) > 0,
            }
        return summary

    def open_quantities(self, doc_entry: int) -> dict[int, Decimal]:
        """Line number -> still-open quantity, for reconciling app state."""
        return {
            line["line_num"]: line["open_quantity"]
            for line in self._lines(doc_entry)
            if line["line_status"] == LINE_OPEN
        }

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _lines(self, doc_entry: int) -> list[dict]:
        rows = self._query(
            """
            SELECT
                L."LineNum", L."ItemCode", IFNULL(L."Dscription", ''),
                L."Quantity", L."OpenQty", IFNULL(L."LineStatus", ''),
                IFNULL(L."FromWhsCod", ''), IFNULL(L."WhsCode", ''),
                IFNULL(L."unitMsr", '')
            FROM "{schema}"."WTQ1" L
            WHERE L."DocEntry" = ?
            ORDER BY L."LineNum"
            """,
            (int(doc_entry),),
        )
        lines = []
        for r in rows:
            quantity = Decimal(str(r[3] or 0))
            open_qty = Decimal(str(r[4] or 0))
            lines.append(
                {
                    "line_num": int(r[0]),
                    "item_code": r[1] or "",
                    "item_name": r[2] or "",
                    "quantity": quantity,
                    "open_quantity": open_qty,
                    "served_quantity": quantity - open_qty,
                    "line_status": r[5] or "",
                    "from_warehouse": r[6] or "",
                    "to_warehouse": r[7] or "",
                    "uom": r[8] or "",
                }
            )
        return lines

    def _query(self, sql: str, params: tuple) -> list:
        conn = None
        cursor = None

        try:
            conn = self.connection.connect()
        except dbapi.Error as e:
            logger.error(
                "SAP HANA connection failed while reading transfer requests: %s", e
            )
            raise SAPConnectionError("Unable to connect to SAP HANA.") from e

        try:
            cursor = conn.cursor()
            cursor.execute(sql.replace("{schema}", self.connection.schema), params)
            return cursor.fetchall()
        except dbapi.Error as e:
            logger.error("SAP HANA transfer request query failed: %s", e)
            raise SAPDataError(
                "Failed to read inventory transfer requests from SAP."
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
