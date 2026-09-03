"""HANA reads for the factory invoice-approval page.

A/R invoices raised at head office land in SAP as **drafts** (`ODRF`, ObjType 13)
whose approval procedure opens an **approval request** (`OWDD`) with per-approver
stage lines (`WDD1`). This reader lists those requests for one warehouse so the
factory approver can verify each invoice against physical stock before deciding.

Key data facts (verified live):

* `OWDD.DraftEntry` — not ``DocEntry`` — is the FK to the draft's `ODRF.DocEntry`.
* Editing a draft cancels its request and opens a new one, so stale `OWDD` rows
  with ``Status = 'W'`` point at drafts whose ``WddStatus`` is ``'C'``. Every
  query therefore keeps only the LATEST request per draft, and PENDING further
  requires the draft itself to say ``WddStatus = 'W'`` and ``DocStatus = 'O'``.
* Drafts here carry no batch allocations (`DRF16` is empty for ObjType 13);
  batches are picked when the approved draft is posted.
"""

import logging
from decimal import Decimal

from hdbcli import dbapi

from .connection import HanaConnection
from ..exceptions import SAPConnectionError, SAPDataError, SAPValidationError

logger = logging.getLogger(__name__)

# App-facing statuses (the FE tabs) -> filter over OWDD/ODRF.
STATUS_FILTERS = {
    "PENDING": """W."Status" = 'W' AND D."DocStatus" = 'O' AND D."WddStatus" = 'W'""",
    "APPROVED": """W."Status" = 'Y'""",
    "REJECTED": """W."Status" = 'N'""",
}

_OWDD_STATUS_TO_APP = {"W": "PENDING", "Y": "APPROVED", "N": "REJECTED"}

# Latest request per draft — older OWDD rows are superseded, never a live state.
_LATEST_REQUEST = """W."WddCode" = (
    SELECT MAX(W2."WddCode") FROM "{schema}"."OWDD" W2
    WHERE W2."DraftEntry" = W."DraftEntry" AND W2."ObjType" = '13'
)"""


def _amount(value) -> str | None:
    if value is None:
        return None
    return f"{Decimal(value):.2f}"


def _iso(create_date, create_time) -> str | None:
    """OWDD stamps date and an HHMM smallint separately; merge into one ISO string."""
    if create_date is None:
        return None
    hhmm = int(create_time or 0)
    return f"{create_date.strftime('%Y-%m-%d')}T{hhmm // 100:02d}:{hhmm % 100:02d}:00"


def _date(value) -> str | None:
    return value.strftime("%Y-%m-%d") if value is not None else None


class HanaApprovalReader:
    """List/inspect SAP approval requests on A/R invoice drafts."""

    def __init__(self, context):
        self.connection = HanaConnection(context.hana)

    # ------------------------------------------------------------------
    # List + count
    # ------------------------------------------------------------------

    def list_approvals(
        self, warehouse: str, status: str | None = None, limit: int = 200
    ) -> list[dict]:
        """Approval requests whose draft ships from ``warehouse``, newest first.

        Returns rows shaped for the approver page: header fields plus the draft's
        lines (as ``invoice_payload.DocumentLines``) and per-line live on-hand
        stock (``fg_stock``) read from OITW in the same round trip.
        """
        if not (warehouse or "").strip():
            raise SAPValidationError("warehouse (whs) is required")
        if status and status not in STATUS_FILTERS:
            raise SAPValidationError(f"Unknown approval status: {status}")

        clauses = [_LATEST_REQUEST]
        if status:
            clauses.append(STATUS_FILTERS[status])
        where = " AND ".join(clauses)

        headers = self._query(
            f"""
            SELECT
                W."WddCode", W."Status", W."CreateDate", W."CreateTime",
                D."DocEntry", D."DocNum", D."CardCode", D."CardName",
                D."DocTotal", D."DocDate", D."DocDueDate", D."BPLName", D."Comments",
                O."U_NAME",
                (SELECT MIN(CASE WHEN L."BaseType" = 17 THEN L."BaseRef" END)
                 FROM "{{schema}}"."DRF1" L WHERE L."DocEntry" = D."DocEntry") AS "BaseSO",
                (SELECT MAX(S."Remarks") FROM "{{schema}}"."WDD1" S
                 WHERE S."WddCode" = W."WddCode" AND S."Status" = 'N') AS "RejectRemarks"
            FROM "{{schema}}"."OWDD" W
            JOIN "{{schema}}"."ODRF" D
                ON D."DocEntry" = W."DraftEntry" AND D."ObjType" = '13'
            LEFT JOIN "{{schema}}"."OUSR" O ON O."USERID" = W."OwnerID"
            WHERE W."ObjType" = '13'
              AND {where}
              AND EXISTS (SELECT 1 FROM "{{schema}}"."DRF1" L2
                          WHERE L2."DocEntry" = D."DocEntry" AND L2."WhsCode" = ?)
            ORDER BY W."WddCode" DESC
            LIMIT {int(limit)}
            """,
            (warehouse,),
        )
        if not headers:
            return []

        lines_by_doc = self._draft_lines([row[4] for row in headers])

        rows = []
        for (
            wdd_code, owdd_status, create_date, create_time,
            doc_entry, doc_num, card_code, card_name,
            doc_total, doc_date, due_date, branch, comments,
            owner_name, base_so, reject_remarks,
        ) in headers:
            doc_lines, fg_stock = lines_by_doc.get(doc_entry, ([], []))
            rows.append({
                # WddCode is the id the decision endpoint acts on.
                "id": int(wdd_code),
                "doc_entry": int(doc_entry),
                "doc_num": int(doc_num) if doc_num is not None else None,
                "so_number": (base_so or "").strip() or str(doc_num),
                "card_code": card_code,
                "party_name": card_name or card_code,
                "total_amount": _amount(doc_total),
                "branch": branch,
                "warehouse": warehouse,
                "status": _OWDD_STATUS_TO_APP.get(owdd_status, owdd_status),
                "rejection_reason": reject_remarks or None,
                "error_message": None,
                "invoice_payload": {
                    "DocObjectCode": "13",
                    "DocDate": _date(doc_date),
                    "DocDueDate": _date(due_date),
                    "CardCode": card_code,
                    "Comments": comments,
                    "DocumentLines": doc_lines,
                },
                "fg_stock": fg_stock,
                "created_at": _iso(create_date, create_time),
                "created_by": owner_name,
            })
        return rows

    def request_warehouses(self, wdd_code: int) -> set:
        """Upper-cased warehouse codes on the draft behind one approval request.

        Used to authorise a decision/detail lookup against the warehouses the
        acting user manages — the request is addressed by WddCode, not warehouse,
        so the warehouse has to be resolved from its draft lines.
        """
        rows = self._query(
            """
            SELECT DISTINCT L."WhsCode"
            FROM "{schema}"."OWDD" W
            JOIN "{schema}"."DRF1" L ON L."DocEntry" = W."DraftEntry"
            WHERE W."WddCode" = ? AND W."ObjType" = '13'
            """,
            (int(wdd_code),),
        )
        return {r[0].strip().upper() for r in rows if r[0]}

    def pending_count(self, warehouse: str) -> int:
        if not (warehouse or "").strip():
            raise SAPValidationError("warehouse (whs) is required")
        rows = self._query(
            f"""
            SELECT COUNT(*)
            FROM "{{schema}}"."OWDD" W
            JOIN "{{schema}}"."ODRF" D
                ON D."DocEntry" = W."DraftEntry" AND D."ObjType" = '13'
            WHERE W."ObjType" = '13'
              AND {_LATEST_REQUEST}
              AND {STATUS_FILTERS["PENDING"]}
              AND EXISTS (SELECT 1 FROM "{{schema}}"."DRF1" L2
                          WHERE L2."DocEntry" = D."DocEntry" AND L2."WhsCode" = ?)
            """,
            (warehouse,),
        )
        return int(rows[0][0]) if rows else 0

    # ------------------------------------------------------------------
    # History
    # ------------------------------------------------------------------

    def approval_history(self, wdd_code: int) -> list[dict]:
        """Full SAP trail for the draft behind one approval request.

        Covers every request the draft has had (edits supersede requests), so a
        rejected-then-fixed invoice shows its whole story: each request's raise
        by the originator plus every decided approver stage line.
        """
        draft = self._query(
            """
            SELECT "DraftEntry" FROM "{schema}"."OWDD"
            WHERE "WddCode" = ? AND "ObjType" = '13'
            """,
            (int(wdd_code),),
        )
        if not draft:
            raise SAPValidationError(f"Approval request {wdd_code} was not found in SAP.")

        rows = self._query(
            """
            SELECT
                W."WddCode", W."CreateDate", W."CreateTime",
                OWN."U_NAME" AS "OwnerName",
                S."StepCode", S."Status", S."Remarks", S."UpdateDate", S."UpdateTime",
                AP."U_NAME" AS "ApproverName"
            FROM "{schema}"."OWDD" W
            LEFT JOIN "{schema}"."WDD1" S ON S."WddCode" = W."WddCode"
            LEFT JOIN "{schema}"."OUSR" OWN ON OWN."USERID" = W."OwnerID"
            LEFT JOIN "{schema}"."OUSR" AP ON AP."USERID" = S."UserID"
            WHERE W."DraftEntry" = ? AND W."ObjType" = '13'
            ORDER BY W."WddCode", S."StepCode"
            """,
            (int(draft[0][0]),),
        )

        history: list[dict] = []
        seen_requests: set[int] = set()
        for (
            code, create_date, create_time, owner_name,
            step_code, line_status, remarks, update_date, update_time,
            approver_name,
        ) in rows:
            code = int(code)
            if code not in seen_requests:
                seen_requests.add(code)
                history.append({
                    "id": code * 100,  # stable, distinct from stage-line ids below
                    "status": "PENDING",
                    "created_by_name": owner_name,
                    "remarks": None,
                    "created_at": _iso(create_date, create_time),
                })
            if step_code is None or line_status == "W":
                continue  # no stage line, or an approver who has not decided yet
            history.append({
                "id": code * 100 + int(step_code),
                "status": _OWDD_STATUS_TO_APP.get(line_status, line_status),
                "created_by_name": approver_name,
                "remarks": remarks or None,
                "created_at": _iso(update_date, update_time),
            })
        return history

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _draft_lines(self, doc_entries: list[int]) -> dict[int, tuple[list, list]]:
        """DocEntry -> (DocumentLines, fg_stock) for a set of drafts, one query."""
        entries = sorted({int(e) for e in doc_entries})
        if not entries:
            return {}
        placeholders = ", ".join(["?"] * len(entries))
        rows = self._query(
            f"""
            SELECT
                L."DocEntry", L."LineNum", L."ItemCode", L."Dscription",
                L."Quantity", L."WhsCode", L."TaxCode", L."Price",
                I."ItemName", T."OnHand"
            FROM "{{schema}}"."DRF1" L
            LEFT JOIN "{{schema}}"."OITM" I ON I."ItemCode" = L."ItemCode"
            LEFT JOIN "{{schema}}"."OITW" T
                ON T."ItemCode" = L."ItemCode" AND T."WhsCode" = L."WhsCode"
            WHERE L."DocEntry" IN ({placeholders})
            ORDER BY L."DocEntry", L."LineNum"
            """,
            tuple(entries),
        )
        result: dict[int, tuple[list, list]] = {}
        for (
            doc_entry, line_num, item_code, description,
            quantity, whs_code, tax_code, price,
            item_name, on_hand,
        ) in rows:
            doc_lines, fg_stock = result.setdefault(int(doc_entry), ([], []))
            qty = float(quantity) if quantity is not None else None
            doc_lines.append({
                "LineNum": int(line_num),
                "ItemCode": item_code,
                "ItemDescription": description or item_name,
                "Quantity": qty,
                "WarehouseCode": whs_code,
                "TaxCode": tax_code,
                "Price": float(price) if price is not None else None,
            })
            fg_stock.append({
                "line_num": int(line_num),
                "item_code": item_code,
                "item_name": item_name,
                "quantity": qty,
                "warehouse_code": whs_code,
                "warehouse_stock": float(on_hand) if on_hand is not None else None,
            })
        return result

    def _query(self, sql: str, params: tuple) -> list:
        conn = None
        cursor = None
        try:
            conn = self.connection.connect()
        except dbapi.Error as e:
            logger.error("SAP HANA connection failed while reading approvals: %s", e)
            raise SAPConnectionError("Unable to connect to SAP HANA.") from e

        try:
            cursor = conn.cursor()
            cursor.execute(sql.replace("{schema}", self.connection.schema), params)
            return cursor.fetchall()
        except dbapi.Error as e:
            logger.error("SAP HANA invoice-approval query failed: %s", e)
            raise SAPDataError("Failed to read invoice approvals from SAP.") from e
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
