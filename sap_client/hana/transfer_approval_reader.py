"""HANA reads for the SAP branch-stock-transfer approval queue.

Inventory transfers routed into SAP's approval procedure land as **drafts**
(``ODRF``) whose procedure opens an **approval request** (``OWDD``) with
per-authorizer stage lines (``WDD1``). This reader lists those requests so the
transfer-requests page can show — and decide — them alongside the app's own
transfer requests.

Two object types travel together here because operators treat them as one
queue: ``67`` the inventory transfer itself (the stock actually moves on
approval) and ``1250000001`` the transfer *request* that precedes it. Each row
says which it is.

Key data facts (verified live against all three company databases):

* ``OWDD.DraftEntry`` — not ``DocEntry`` — is the FK to ``ODRF.DocEntry``.
* On a transfer draft the source warehouse is ``ODRF.Filler`` and the
  destination is ``ODRF.ToWhsCode``; there is no ``FromWhsCode`` column.
* Editing a draft cancels its request and opens a new one, so stale ``OWDD``
  rows with ``Status = 'W'`` point at drafts whose ``WddStatus`` is ``'C'`` or
  ``'N'``. Only the LATEST request per draft is live, and PENDING further
  requires the draft itself to say ``WddStatus = 'W'`` and ``DocStatus = 'O'``.
  Without that filter Oil shows 292 "pending" transfers where only 4 are real.
* ``OWDD.CurrStep`` is the ``WstCode`` of the stage now waiting, and the one
  ``WDD1`` row at that step names the single user SAP will accept a decision
  from (every stage in this estate has ``MaxReqr = 1``). Once the request is
  decided that same step holds the decision: the ``WDD1`` row's ``Status``
  matches the header's, and its ``UpdateDate``/``UpdateTime`` is when it was
  taken. That is where the history below reads "who decided it, and when".
* On the *lines* (``DRF1``) the sense of the warehouse columns is reversed from
  a sales line: ``FromWhsCod`` is the source and ``WhsCode`` the destination.
* **A draft's ``DocNum`` is not the number the document ends up with.** It is
  only the series' next number at the moment the draft was saved, so open
  drafts share it (one Oil number is on seven at once) and the add takes
  whatever is next *then*. Measured over every draft-linked transfer: 4,635 of
  11,309 differ from their draft's in Oil, 878 of 2,168 in Beverages, 74 of
  1,324 in Mart. Worse, that provisional number frequently already belongs to
  some *other* posted document, so searching for it lands on the wrong one.
  The link that holds is the draft entry — ``OWTR."draftKey"`` /
  ``OWTQ."draftKey"`` — which is how ``posted_doc_num`` below is resolved. It
  is the only number worth quoting to an operator hunting the transfer.
"""

import logging

from hdbcli import dbapi

from .connection import HanaConnection
from ..exceptions import SAPConnectionError, SAPDataError, SAPValidationError

logger = logging.getLogger(__name__)

# The two transfer families this queue covers, and how to label them.
OBJ_TYPE_TRANSFER = "67"
OBJ_TYPE_TRANSFER_REQUEST = "1250000001"
OBJ_TYPES = (OBJ_TYPE_TRANSFER, OBJ_TYPE_TRANSFER_REQUEST)
_OBJ_TYPE_LABELS = {
    OBJ_TYPE_TRANSFER: "Stock Transfer",
    OBJ_TYPE_TRANSFER_REQUEST: "Transfer Request",
}

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
    WHERE W2."DraftEntry" = W."DraftEntry" AND W2."ObjType" = W."ObjType"
)"""

# The one user waiting on the stage OWDD.CurrStep points at. Scalar subqueries
# rather than a join so a stage that somehow holds two approvers cannot fan the
# header row out into duplicates.
_CURRENT_APPROVER = """(
    SELECT MIN(AU."USER_CODE") FROM "{schema}"."WDD1" S
    LEFT JOIN "{schema}"."OUSR" AU ON AU."USERID" = S."UserID"
    WHERE S."WddCode" = W."WddCode" AND S."StepCode" = W."CurrStep"
      AND S."Status" = 'W'
)"""
_CURRENT_APPROVER_NAME = """(
    SELECT MIN(AU."U_NAME") FROM "{schema}"."WDD1" S
    LEFT JOIN "{schema}"."OUSR" AU ON AU."USERID" = S."UserID"
    WHERE S."WddCode" = W."WddCode" AND S."StepCode" = W."CurrStep"
      AND S."Status" = 'W'
)"""

# Once decided, the stage CurrStep points at holds the decision: same shape as
# the two above, but matching the header's own Y/N instead of 'W'. HANA refuses
# ORDER BY inside a correlated subquery, hence MIN over a stage that holds one
# user anyway.
def _decided(column: str) -> str:
    return f"""(
    SELECT MIN({column}) FROM "{{schema}}"."WDD1" S
    LEFT JOIN "{{schema}}"."OUSR" DU ON DU."USERID" = S."UserID"
    WHERE S."WddCode" = W."WddCode" AND S."StepCode" = W."CurrStep"
      AND S."Status" = W."Status" AND W."Status" <> 'W'
)"""


_DECIDED_BY = _decided('DU."USER_CODE"')
_DECIDED_BY_NAME = _decided('DU."U_NAME"')
_DECIDED_DATE = _decided('S."UpdateDate"')
_DECIDED_TIME = _decided('S."UpdateTime"')

# The document the draft actually became, found through the draft entry rather
# than through the draft's own DocNum — see the module docstring for why that
# number cannot be trusted. NULL while the draft is still a draft, which is
# exactly the "approved but nothing moved" backlog the page's other tab lists.
_POSTED = """(CASE W."ObjType"
    WHEN '67' THEN (
        SELECT MIN(T.{column}) FROM "{{schema}}"."OWTR" T
        WHERE T."draftKey" = W."DraftEntry" AND IFNULL(T."CANCELED", 'N') = 'N')
    WHEN '1250000001' THEN (
        SELECT MIN(Q.{column}) FROM "{{schema}}"."OWTQ" Q
        WHERE Q."draftKey" = W."DraftEntry" AND IFNULL(Q."CANCELED", 'N') = 'N')
END)"""
_POSTED_DOC_ENTRY = _POSTED.format(column='"DocEntry"')
_POSTED_DOC_NUM = _POSTED.format(column='"DocNum"')


def _iso(create_date, create_time) -> str | None:
    """OWDD stamps date and an HHMM smallint separately; merge into one ISO string."""
    if create_date is None:
        return None
    hhmm = int(create_time or 0)
    return f"{create_date.strftime('%Y-%m-%d')}T{hhmm // 100:02d}:{hhmm % 100:02d}:00"


def _date(value) -> str | None:
    return value.strftime("%Y-%m-%d") if value is not None else None


def _clean(value) -> str:
    return (value or "").strip()


class HanaTransferApprovalReader:
    """List/inspect SAP approval requests on inventory-transfer drafts."""

    def __init__(self, context):
        self.connection = HanaConnection(context.hana)

    # ------------------------------------------------------------------
    # List + count
    # ------------------------------------------------------------------

    def list_approvals(self, status: str | None = "PENDING", limit: int = 100) -> list[dict]:
        """Transfer approval requests, newest first, with their draft lines.

        Not warehouse-scoped: a transfer's two warehouses belong to different
        managers and the authorizer SAP names is often neither of them, so
        filtering by the caller's warehouses would hide rows nobody could then
        find. The queue is company-wide and each row names its authorizer.
        """
        if status and status not in STATUS_FILTERS:
            raise SAPValidationError(f"Unknown approval status: {status}")

        clauses = [_LATEST_REQUEST]
        if status:
            clauses.append(STATUS_FILTERS[status])
        where = " AND ".join(clauses)
        obj_types = ", ".join(f"'{t}'" for t in OBJ_TYPES)

        headers = self._query(
            f"""
            SELECT
                W."WddCode", W."ObjType", W."Status", W."CurrStep",
                W."CreateDate", W."CreateTime",
                D."DocEntry", D."DocNum", D."Filler", D."ToWhsCode",
                D."DocDate", D."Comments",
                O."U_NAME",
                {_CURRENT_APPROVER} AS "ApproverCode",
                {_CURRENT_APPROVER_NAME} AS "ApproverName",
                (SELECT MAX(S2."Remarks") FROM "{{schema}}"."WDD1" S2
                 WHERE S2."WddCode" = W."WddCode" AND S2."Status" = 'N') AS "RejectRemarks",
                {_DECIDED_BY} AS "DecidedBy",
                {_DECIDED_BY_NAME} AS "DecidedByName",
                {_DECIDED_DATE} AS "DecidedDate",
                {_DECIDED_TIME} AS "DecidedTime",
                {_POSTED_DOC_ENTRY} AS "PostedEntry",
                {_POSTED_DOC_NUM} AS "PostedDocNum"
            FROM "{{schema}}"."OWDD" W
            JOIN "{{schema}}"."ODRF" D
                ON D."DocEntry" = W."DraftEntry" AND D."ObjType" = W."ObjType"
            LEFT JOIN "{{schema}}"."OUSR" O ON O."USERID" = W."OwnerID"
            WHERE W."ObjType" IN ({obj_types})
              AND {where}
            ORDER BY W."WddCode" DESC
            LIMIT {int(limit)}
            """,
            (),
        )
        if not headers:
            return []

        lines_by_doc = self._draft_lines([row[6] for row in headers])

        rows = []
        for (
            wdd_code, obj_type, owdd_status, curr_step,
            create_date, create_time,
            doc_entry, doc_num, from_whs, to_whs,
            doc_date, comments,
            owner_name, approver_code, approver_name, reject_remarks,
            decided_by, decided_by_name, decided_date, decided_time,
            posted_entry, posted_doc_num,
        ) in headers:
            obj_type = str(obj_type)
            rows.append({
                # WddCode is the id the decision endpoint acts on.
                "id": int(wdd_code),
                "obj_type": obj_type,
                "doc_type_label": _OBJ_TYPE_LABELS.get(obj_type, obj_type),
                "draft_entry": int(doc_entry),
                # The DRAFT's number: provisional, shared with other open
                # drafts, and often not the one the document keeps. Never the
                # number to hand an operator — `posted_doc_num` is.
                "doc_num": int(doc_num) if doc_num is not None else None,
                # What the draft was added as, once it was. For a transfer
                # REQUEST this is the number to search the awaiting-transfer
                # queue with; for a stock transfer it is the movement itself.
                "posted_doc_entry": int(posted_entry) if posted_entry is not None else None,
                "posted_doc_num": int(posted_doc_num) if posted_doc_num is not None else None,
                "from_warehouse": _clean(from_whs),
                "to_warehouse": _clean(to_whs),
                "doc_date": _date(doc_date),
                "comments": _clean(comments) or None,
                "status": _OWDD_STATUS_TO_APP.get(owdd_status, owdd_status),
                "rejection_reason": _clean(reject_remarks) or None,
                # The single SAP user this request is now waiting on; signing as
                # anyone else is refused with -6006.
                "current_step": int(curr_step) if curr_step is not None else None,
                "approver_code": _clean(approver_code) or None,
                "approver_name": _clean(approver_name) or None,
                # Who actually signed it off in SAP, and when — empty while the
                # request is still pending.
                "decided_by": _clean(decided_by) or None,
                "decided_by_name": _clean(decided_by_name) or None,
                "decided_at": _iso(decided_date, decided_time),
                "lines": lines_by_doc.get(int(doc_entry), []),
                "created_at": _iso(create_date, create_time),
                "created_by": _clean(owner_name) or None,
            })
        return rows

    def pending_count(self) -> int:
        obj_types = ", ".join(f"'{t}'" for t in OBJ_TYPES)
        rows = self._query(
            f"""
            SELECT COUNT(*)
            FROM "{{schema}}"."OWDD" W
            JOIN "{{schema}}"."ODRF" D
                ON D."DocEntry" = W."DraftEntry" AND D."ObjType" = W."ObjType"
            WHERE W."ObjType" IN ({obj_types})
              AND {_LATEST_REQUEST}
              AND {STATUS_FILTERS["PENDING"]}
            """,
            (),
        )
        return int(rows[0][0]) if rows else 0

    # ------------------------------------------------------------------
    # One request
    # ------------------------------------------------------------------

    def current_stage(self, wdd_code: int) -> dict:
        """The stage one request is waiting on, and the user who must decide it.

        The decision endpoint reads this rather than trusting the browser: the
        page may have been open while somebody advanced the request in SAP, and
        signing as a stale stage's authorizer would be refused.
        """
        obj_types = ", ".join(f"'{t}'" for t in OBJ_TYPES)
        rows = self._query(
            f"""
            SELECT
                W."WddCode", W."ObjType", W."Status", W."CurrStep", W."DraftEntry",
                D."DocNum", D."Filler", D."ToWhsCode",
                {_CURRENT_APPROVER} AS "ApproverCode",
                {_CURRENT_APPROVER_NAME} AS "ApproverName"
            FROM "{{schema}}"."OWDD" W
            LEFT JOIN "{{schema}}"."ODRF" D
                ON D."DocEntry" = W."DraftEntry" AND D."ObjType" = W."ObjType"
            WHERE W."WddCode" = ? AND W."ObjType" IN ({obj_types})
            """,
            (int(wdd_code),),
        )
        if not rows:
            raise SAPValidationError(
                f"Transfer approval request {wdd_code} was not found in SAP."
            )
        (
            code, obj_type, owdd_status, curr_step, draft_entry,
            doc_num, from_whs, to_whs, approver_code, approver_name,
        ) = rows[0]
        obj_type = str(obj_type)
        return {
            "id": int(code),
            "obj_type": obj_type,
            "doc_type_label": _OBJ_TYPE_LABELS.get(obj_type, obj_type),
            "status": _OWDD_STATUS_TO_APP.get(owdd_status, owdd_status),
            "current_step": int(curr_step) if curr_step is not None else None,
            "draft_entry": int(draft_entry) if draft_entry is not None else None,
            "doc_num": int(doc_num) if doc_num is not None else None,
            "from_warehouse": _clean(from_whs),
            "to_warehouse": _clean(to_whs),
            "approver_code": _clean(approver_code) or None,
            "approver_name": _clean(approver_name) or None,
        }

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _draft_lines(self, doc_entries: list[int]) -> dict[int, list]:
        """DocEntry -> the draft's item lines, for a whole page in one query.

        On a transfer line ``DRF1.FromWhsCod`` is the source and ``WhsCode`` the
        destination (the opposite way round from a sales line). ``source_stock``
        therefore joins OITW on ``FromWhsCod``: what the approver needs to know
        is whether the *sending* warehouse actually holds the quantity.
        """
        entries = sorted({int(e) for e in doc_entries})
        if not entries:
            return {}
        placeholders = ", ".join(["?"] * len(entries))
        rows = self._query(
            f"""
            SELECT
                L."DocEntry", L."LineNum", L."ItemCode", L."Dscription",
                L."Quantity", L."FromWhsCod", L."WhsCode", I."ItemName", T."OnHand"
            FROM "{{schema}}"."DRF1" L
            LEFT JOIN "{{schema}}"."OITM" I ON I."ItemCode" = L."ItemCode"
            LEFT JOIN "{{schema}}"."OITW" T
                ON T."ItemCode" = L."ItemCode" AND T."WhsCode" = L."FromWhsCod"
            WHERE L."DocEntry" IN ({placeholders})
            ORDER BY L."DocEntry", L."LineNum"
            """,
            tuple(entries),
        )
        result: dict[int, list] = {}
        for (
            doc_entry, line_num, item_code, description,
            quantity, from_whs, to_whs, item_name, source_on_hand,
        ) in rows:
            result.setdefault(int(doc_entry), []).append({
                "line_num": int(line_num),
                "item_code": item_code,
                "item_name": _clean(description) or _clean(item_name),
                "quantity": float(quantity) if quantity is not None else None,
                "from_warehouse": _clean(from_whs),
                "to_warehouse": _clean(to_whs),
                "source_stock": (
                    float(source_on_hand) if source_on_hand is not None else None
                ),
            })
        return result

    def _query(self, sql: str, params: tuple) -> list:
        conn = None
        cursor = None
        try:
            conn = self.connection.connect()
        except dbapi.Error as e:
            logger.error(
                "SAP HANA connection failed while reading transfer approvals: %s", e
            )
            raise SAPConnectionError("Unable to connect to SAP HANA.") from e

        try:
            cursor = conn.cursor()
            cursor.execute(sql.replace("{schema}", self.connection.schema), params)
            return cursor.fetchall()
        except dbapi.Error as e:
            logger.error("SAP HANA transfer-approval query failed: %s", e)
            raise SAPDataError("Failed to read transfer approvals from SAP.") from e
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
