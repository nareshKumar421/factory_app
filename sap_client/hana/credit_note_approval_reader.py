"""HANA reads for the SAP credit-note approval queue.

Credit notes raised in the SAP client and routed into an approval procedure
land as **drafts** (``ODRF``) whose procedure opens an **approval request**
(``OWDD``) with per-authorizer stage lines (``WDD1``). Nothing about them is
visible to anyone who is not sitting in SAP, so they stall — this reader lists
them so they can be decided from the app instead.

Two object types travel together because the queue is one queue to whoever
works it: ``14`` the A/R credit note (a customer is credited, and on an item
credit note the stock comes BACK into the warehouse) and ``19`` the A/P credit
note (a vendor is debited, and the stock goes back OUT to them). Each row says
which it is, and which way its stock moves.

Key data facts (verified live against all three company databases):

* ``OWDD.DraftEntry`` — not ``DocEntry`` — is the FK to ``ODRF.DocEntry``.
* Editing a draft cancels its request and opens a new one, so stale ``OWDD``
  rows with ``Status = 'W'`` point at drafts whose ``WddStatus`` is ``'C'`` or
  ``'N'``. Only the LATEST request per draft AND TEMPLATE is live (next
  bullet), and PENDING further
  requires the draft itself to say ``WddStatus = 'W'`` and ``DocStatus = 'O'``.
  Without that filter Oil shows 88 "pending" A/R credit notes where 25 are real.
* **One draft can hold SEVERAL live requests at once.** A draft that matches
  two approval templates opens one request per template (``OWDD.WtmCode``),
  both waiting, each on its own authorizer: Oil draft 57272 waits on USER26
  (template 73, stage 6) *and* USER30 (template 106, stage 19). 11 of Oil's 26
  pending A/R credit-note drafts are like that (115 of 1,475 ever on ObjType
  14, 112 of 1,765 on 19). So "latest request" is per (draft, template) — such
  a draft legitimately shows as two rows, deciding one does not release the
  other, and the draft only leaves the queue once every request is approved.
* ``OWDD.CurrStep`` is the ``WstCode`` of the stage now waiting, and the one
  ``WDD1`` row at that step names the single user SAP will accept a decision
  from. Once decided, that same row holds the decision and its
  ``UpdateDate``/``UpdateTime`` is when it was taken.
* **A credit note is not always an item document.** ``ODRF.DocType`` is ``'I'``
  for an item credit note and ``'S'`` for a service one — 591 of Oil's 1,587
  are service. A service line has no ``ItemCode``, no ``WhsCode`` and
  ``Quantity = 0``; what it carries is a G/L account (``DRF1.AcctCode``) and an
  amount. Both are listed, and a line is rendered by what it actually holds.
* **A draft's ``DocNum`` is not the number the document ends up with.** It is
  the series' next number as at the save, so open drafts share it (three Oil
  credit-note drafts here carry 626042613 between them) and the add takes
  whatever is next *then*. The link that holds is the draft entry —
  ``ORIN."draftKey"`` / ``ORPC."draftKey"`` — which is how ``posted_doc_num``
  below is resolved, and it is the only number worth quoting to an operator.
"""

import logging
from decimal import Decimal

from hdbcli import dbapi

from .connection import HanaConnection
from ..exceptions import SAPConnectionError, SAPDataError, SAPValidationError

logger = logging.getLogger(__name__)

# The two credit-note families this queue covers, and how to label them.
OBJ_TYPE_AR_CREDIT_NOTE = "14"
OBJ_TYPE_AP_CREDIT_NOTE = "19"
OBJ_TYPES = (OBJ_TYPE_AR_CREDIT_NOTE, OBJ_TYPE_AP_CREDIT_NOTE)
_OBJ_TYPE_LABELS = {
    OBJ_TYPE_AR_CREDIT_NOTE: "A/R Credit Note",
    OBJ_TYPE_AP_CREDIT_NOTE: "A/P Credit Note",
}
# Which way an ITEM credit note moves stock. A service credit note moves none,
# whichever family it belongs to.
_OBJ_TYPE_STOCK_DIRECTION = {
    OBJ_TYPE_AR_CREDIT_NOTE: "IN",
    OBJ_TYPE_AP_CREDIT_NOTE: "OUT",
}

# App-facing families (the FE filter) -> the object types they cover.
FAMILIES = {
    "AR": (OBJ_TYPE_AR_CREDIT_NOTE,),
    "AP": (OBJ_TYPE_AP_CREDIT_NOTE,),
    "ALL": OBJ_TYPES,
}

# ODRF.DocType -> what the draft's lines actually are.
DOC_TYPE_ITEM = "I"
DOC_TYPE_SERVICE = "S"
_DOC_TYPE_LABELS = {DOC_TYPE_ITEM: "Item", DOC_TYPE_SERVICE: "Service"}

# App-facing statuses (the FE tabs) -> filter over OWDD/ODRF.
STATUS_FILTERS = {
    "PENDING": """W."Status" = 'W' AND D."DocStatus" = 'O' AND D."WddStatus" = 'W'""",
    "APPROVED": """W."Status" = 'Y'""",
    "REJECTED": """W."Status" = 'N'""",
}

_OWDD_STATUS_TO_APP = {"W": "PENDING", "Y": "APPROVED", "N": "REJECTED"}

# The live request(s) for a draft. Editing a draft supersedes its request, so
# the newest WddCode wins — but a draft that fires SEVERAL approval templates
# holds one CONCURRENT request per template, each with its own authorizer and
# each needing its own decision. "Latest" is therefore per TEMPLATE
# (``WtmCode``), never per draft: scoping it per draft dropped the lower
# WddCode of every such pair, hiding live requests from the only user SAP
# would accept a decision from.
_LATEST_REQUEST = """W."WddCode" = (
    SELECT MAX(W2."WddCode") FROM "{schema}"."OWDD" W2
    WHERE W2."DraftEntry" = W."DraftEntry" AND W2."ObjType" = W."ObjType"
      AND W2."WtmCode" = W."WtmCode"
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


def _decided(column: str) -> str:
    """Once decided, the stage CurrStep points at holds the decision.

    Same shape as the two above but matching the header's own Y/N instead of
    'W'. HANA refuses ORDER BY inside a correlated subquery, hence MIN over a
    stage that holds one user anyway.
    """
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
# number cannot be trusted. NULL while the draft is still a draft.
_POSTED = """(CASE W."ObjType"
    WHEN '14' THEN (
        SELECT MIN(R.{column}) FROM "{{schema}}"."ORIN" R
        WHERE R."draftKey" = W."DraftEntry" AND IFNULL(R."CANCELED", 'N') = 'N')
    WHEN '19' THEN (
        SELECT MIN(P.{column}) FROM "{{schema}}"."ORPC" P
        WHERE P."draftKey" = W."DraftEntry" AND IFNULL(P."CANCELED", 'N') = 'N')
END)"""
_POSTED_DOC_ENTRY = _POSTED.format(column='"DocEntry"')
_POSTED_DOC_NUM = _POSTED.format(column='"DocNum"')


def _amount(value) -> str | None:
    """Money as a decimal STRING — a float would round paise off a total."""
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


def _clean(value) -> str:
    return (value or "").strip()


class HanaCreditNoteApprovalReader:
    """List/inspect SAP approval requests on credit-note drafts."""

    def __init__(self, context):
        self.connection = HanaConnection(context.hana)

    # ------------------------------------------------------------------
    # List + count
    # ------------------------------------------------------------------

    def list_approvals(
        self,
        status: str | None = "PENDING",
        family: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """Credit-note approval requests, newest first, with their draft lines.

        Not warehouse-scoped, for the same reason the transfer queue is not: the
        authorizer SAP names is rarely a warehouse manager, a service credit
        note names no warehouse at all, and hiding rows by warehouse would
        leave a stalled credit note that nobody can then find. The queue is
        company-wide and every row names its authorizer.
        """
        if status and status not in STATUS_FILTERS:
            raise SAPValidationError(f"Unknown approval status: {status}")
        obj_types = self._obj_types(family)

        clauses = [_LATEST_REQUEST]
        if status:
            clauses.append(STATUS_FILTERS[status])
        where = " AND ".join(clauses)
        obj_type_list = ", ".join(f"'{t}'" for t in obj_types)

        headers = self._query(
            f"""
            SELECT
                W."WddCode", W."ObjType", W."Status", W."CurrStep",
                W."CreateDate", W."CreateTime",
                D."DocEntry", D."DocNum", D."DocType",
                D."CardCode", D."CardName", D."DocTotal", D."VatSum", D."DocCur",
                D."DocDate", D."BPLName", D."Comments", D."NumAtCard",
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
            WHERE W."ObjType" IN ({obj_type_list})
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
            doc_entry, doc_num, doc_type,
            card_code, card_name, doc_total, vat_sum, currency,
            doc_date, branch, comments, ref_number,
            owner_name, approver_code, approver_name, reject_remarks,
            decided_by, decided_by_name, decided_date, decided_time,
            posted_entry, posted_doc_num,
        ) in headers:
            obj_type = str(obj_type)
            doc_type = _clean(doc_type) or DOC_TYPE_ITEM
            lines = lines_by_doc.get(int(doc_entry), [])
            rows.append({
                # WddCode is the id the decision endpoint acts on.
                "id": int(wdd_code),
                "obj_type": obj_type,
                "doc_type_label": _OBJ_TYPE_LABELS.get(obj_type, obj_type),
                # AR credits a customer, AP debits a vendor — the FE filter.
                "family": "AP" if obj_type == OBJ_TYPE_AP_CREDIT_NOTE else "AR",
                # 'I' item / 'S' service: a service credit note has no items,
                # no warehouse and moves no stock, only money against a G/L
                # account. It is a different document to read.
                "line_type": doc_type,
                "line_type_label": _DOC_TYPE_LABELS.get(doc_type, doc_type),
                "moves_stock": doc_type == DOC_TYPE_ITEM,
                "stock_direction": (
                    _OBJ_TYPE_STOCK_DIRECTION.get(obj_type)
                    if doc_type == DOC_TYPE_ITEM
                    else None
                ),
                "draft_entry": int(doc_entry),
                # The DRAFT's number: provisional, shared with other open
                # drafts, and often not the one the document keeps. Never the
                # number to hand an operator — `posted_doc_num` is.
                "doc_num": int(doc_num) if doc_num is not None else None,
                "posted_doc_entry": int(posted_entry) if posted_entry is not None else None,
                "posted_doc_num": int(posted_doc_num) if posted_doc_num is not None else None,
                "card_code": _clean(card_code),
                "party_name": _clean(card_name) or _clean(card_code),
                "total_amount": _amount(doc_total),
                "tax_amount": _amount(vat_sum),
                "currency": _clean(currency) or None,
                "branch": _clean(branch) or None,
                "doc_date": _date(doc_date),
                "comments": _clean(comments) or None,
                # The party's own reference on the document, if they gave one.
                "reference": _clean(ref_number) or None,
                # What this credit note was raised AGAINST, from the lines.
                "base_documents": self._base_documents(lines),
                "warehouses": sorted(
                    {line["warehouse"] for line in lines if line["warehouse"]}
                ),
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
                "lines": lines,
                "created_at": _iso(create_date, create_time),
                "created_by": _clean(owner_name) or None,
            })
        return rows

    def pending_count(self, family: str | None = None) -> int:
        obj_types = ", ".join(f"'{t}'" for t in self._obj_types(family))
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
                D."DocNum", D."CardCode", D."CardName", D."DocTotal",
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
                f"Credit-note approval request {wdd_code} was not found in SAP."
            )
        (
            code, obj_type, owdd_status, curr_step, draft_entry,
            doc_num, card_code, card_name, doc_total,
            approver_code, approver_name,
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
            "card_code": _clean(card_code),
            "party_name": _clean(card_name) or _clean(card_code) or None,
            "total_amount": _amount(doc_total),
            "approver_code": _clean(approver_code) or None,
            "approver_name": _clean(approver_name) or None,
        }

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    @staticmethod
    def _obj_types(family: str | None) -> tuple:
        """'AR' / 'AP' / 'ALL' (or nothing) -> the OWDD object types to read."""
        key = (family or "ALL").strip().upper()
        if key not in FAMILIES:
            raise SAPValidationError(
                f"Unknown credit-note family: {family}. Use AR, AP or ALL."
            )
        return FAMILIES[key]

    @staticmethod
    def _base_documents(lines: list[dict]) -> list[str]:
        """The documents the credit note's lines were copied from, deduplicated.

        A credit note is usually raised against an invoice or a return, and
        which one it is is the first thing an approver checks. It lives on the
        LINES (``DRF1.BaseType``/``BaseRef``), not the header, and a standalone
        credit note has none at all.
        """
        seen = {}
        for line in lines:
            ref = line.get("base_ref")
            if not ref:
                continue
            seen.setdefault(f"{line.get('base_type_label') or ''} {ref}".strip(), None)
        return list(seen)

    def _draft_lines(self, doc_entries: list[int]) -> dict[int, list]:
        """DocEntry -> the draft's lines, for a whole page in one query.

        Handles both shapes a credit note comes in. An ITEM line has an item, a
        quantity and a warehouse, and ``warehouse_stock`` says what that
        warehouse holds right now — the number that decides whether an A/P
        credit note can actually send the goods back. A SERVICE line has none
        of that: it has a G/L account and an amount, and both are carried here
        so the row can render itself from what it actually holds.
        """
        entries = sorted({int(e) for e in doc_entries})
        if not entries:
            return {}
        placeholders = ", ".join(["?"] * len(entries))
        rows = self._query(
            f"""
            SELECT
                L."DocEntry", L."LineNum", L."ItemCode", L."Dscription",
                L."Quantity", L."WhsCode", L."Price", L."LineTotal",
                L."AcctCode", L."BaseType", L."BaseRef",
                I."ItemName", T."OnHand", A."AcctName"
            FROM "{{schema}}"."DRF1" L
            LEFT JOIN "{{schema}}"."OITM" I ON I."ItemCode" = L."ItemCode"
            LEFT JOIN "{{schema}}"."OITW" T
                ON T."ItemCode" = L."ItemCode" AND T."WhsCode" = L."WhsCode"
            LEFT JOIN "{{schema}}"."OACT" A ON A."AcctCode" = L."AcctCode"
            WHERE L."DocEntry" IN ({placeholders})
            ORDER BY L."DocEntry", L."LineNum"
            """,
            tuple(entries),
        )
        result: dict[int, list] = {}
        for (
            doc_entry, line_num, item_code, description,
            quantity, whs_code, price, line_total,
            account_code, base_type, base_ref,
            item_name, on_hand, account_name,
        ) in rows:
            item_code = _clean(item_code)
            result.setdefault(int(doc_entry), []).append({
                "line_num": int(line_num),
                # Empty on a service line; the account below is what it has.
                "item_code": item_code or None,
                "description": _clean(description) or _clean(item_name) or None,
                "quantity": float(quantity) if quantity is not None else None,
                "warehouse": _clean(whs_code) or None,
                "price": _amount(price),
                "line_total": _amount(line_total),
                # Service lines: where the money lands.
                "account_code": _clean(account_code) or None,
                "account_name": _clean(account_name) or None,
                # On hand at the line's own warehouse — null for a service line.
                "warehouse_stock": float(on_hand) if on_hand is not None else None,
                "base_ref": _clean(base_ref) or None,
                "base_type_label": _BASE_TYPE_LABELS.get(
                    int(base_type) if base_type is not None else None
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
                "SAP HANA connection failed while reading credit-note approvals: %s", e
            )
            raise SAPConnectionError("Unable to connect to SAP HANA.") from e

        try:
            cursor = conn.cursor()
            cursor.execute(sql.replace("{schema}", self.connection.schema), params)
            return cursor.fetchall()
        except dbapi.Error as e:
            logger.error("SAP HANA credit-note-approval query failed: %s", e)
            raise SAPDataError("Failed to read credit-note approvals from SAP.") from e
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


# What a credit-note line was copied from. -1 is SAP's "nothing" — a standalone
# credit note typed from scratch, which is most of the service ones.
_BASE_TYPE_LABELS = {
    13: "A/R Invoice",
    14: "A/R Credit Note",
    15: "Delivery",
    16: "A/R Return",
    18: "A/P Invoice",
    19: "A/P Credit Note",
    20: "GRPO",
    21: "Goods Return",
}
