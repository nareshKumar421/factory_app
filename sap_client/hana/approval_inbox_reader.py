"""HANA reads for the general SAP approval inbox (ported from SAP Portal).

JI's three approval queues (A/R invoice, transfer, credit note) each serve one
document family and list it company-wide. SAP Portal's approvals screen was the
other shape: every approval request SAP holds, of every object type, narrowed to
the ones that involve the person looking — requests they raised, and requests
with a decision line of theirs. This reader is that screen's data, rebuilt on
JI's rules (``backend_v1/services/sapApprovals.js`` and ``routes/sap.js``
``/approval-requests``).

Where the requests live (same facts as the queue readers beside this one):

* ``OWDD`` — one row per approval request; ``WDD1`` — one row per stage and
  authorizer, ``StepCode`` = ``OWDD.CurrStep`` for the stage now waiting.
* ``OWDD.DraftEntry`` points at the draft: ``ODRF`` for documents, ``OPDF`` for
  payment drafts (object types 24 and 46). ``approval_status.draft_join_sql``
  joins both, as the portal did.
* Who a request involves is decided by ``SapApproverIdentity`` only — the
  caller's mapped ``OUSR.USER_CODE`` resolved to its ``USERID`` here. The
  portal also matched the portal login's user name and display name against
  the decision lines (``sapApprovals.js:83-89``); that is not ported. A name
  match is not an identity.

When a request is really pending
--------------------------------

Two rules, both SAP's, applied together:

1. **The draft decides** (``approval_status.effective_status``, the portal's
   ``effectiveOwddStatus``): SAP often leaves ``OWDD.Status = 'W'`` after the
   draft's approval ended, so a request is pending only while its draft says
   ``'W'`` too, and a leftover takes its draft's outcome.
2. **Only the newest request per (draft, template) is live**, the rule JI's
   queue readers apply (``_LATEST_REQUEST``). Editing a draft opens a new
   request and leaves the old header at ``'W'`` — while the draft itself is
   back at ``'W'`` for the new one, so rule 1 alone keeps the superseded
   request pending. The portal had exactly that gap. A superseded request
   reads as cancelled. A draft that matches two templates still holds two live
   requests, one per template.

:func:`inbox_status` is the rule in Python and :data:`EFFECTIVE_STATUS_SQL` in
SQL; ``sap_client.tests_approval_inbox`` pins the two against the same cases.

Siblings and duplicates
-----------------------

Both ported from the portal (``attachSiblingApprovalInfo`` /
``attachDuplicateDocumentInfo``), batched for a whole page on one connection:

* **Siblings** — every live request on the same draft. SAP releases the draft
  only when all of them are approved, so a row says how many there are and how
  many are still open. The portal grouped by ``DraftEntry`` alone, which mixes a
  document draft with a payment draft of the same number (``ODRF`` and ``OPDF``
  number separately); this groups by (draft, object type) and counts by the
  effective status, so leftovers and superseded requests do not inflate it.
* **Duplicates** — SAP lets the same document be captured twice; the copy
  approved first posts, the other stays pending for ever, and approving it
  posts the document again. Three shapes, exactly the portal's: a twin credit
  note draft that already posted (14/19 only), the same document posted
  directly (same party, date and total AND the same reference or remarks), or
  this very draft already posted. Only pending requests are checked.

On a list the two are decoration: a failure logs and costs the flags, never
the list. On :meth:`HanaApprovalInboxReader.current_stage` — which gates the
approve button's write — the duplicate read raises instead.
"""

import logging
from contextlib import contextmanager
from decimal import Decimal

from hdbcli import dbapi

from .. import approval_status as rule
from ..exceptions import SAPConnectionError, SAPDataError, SAPValidationError
from .connection import HanaConnection

logger = logging.getLogger(__name__)

# The portal's OBJ_TYPE_MAP (sapApprovals.js:12), in JI's words. Two changes:
# the portal had 59 and 60 the wrong way round (59 is oInventoryGenEntry, the
# goods RECEIPT — its own OINM report reads 59 as stock in), and '1470000113'
# is dropped: the portal kept it only so an old saved filter rendered, and it
# matches no OWDD row. 24 (incoming payment) had no label there.
OBJECT_TYPE_LABELS = {
    "13": "A/R Invoice",
    "14": "A/R Credit Note",
    "15": "Delivery",
    "16": "A/R Return",
    "17": "Sales Order",
    "18": "A/P Invoice",
    "19": "A/P Credit Note",
    "20": "Goods Receipt PO",
    "21": "Goods Return",
    "22": "Purchase Order",
    "24": "Incoming Payment",
    "46": "Outgoing Payment",
    "59": "Goods Receipt",
    "60": "Goods Issue",
    "67": "Stock Transfer",
    "112": "Draft",
    "202": "Production Order",
    "1250000001": "Transfer Request",
}

# Posted-document header table per object type, for the duplicate checks
# (sapApprovals.js DRAFT_POSTED_TABLE). A fixed whitelist: these names are the
# only thing ever formatted into the duplicate SQL.
POSTED_TABLE = {
    "13": "OINV",
    "14": "ORIN",
    "15": "ODLN",
    "16": "ORDN",
    "17": "ORDR",
    "18": "OPCH",
    "19": "ORPC",
    "20": "OPDN",
    "21": "ORPD",
    "22": "OPOR",
}
# A different draft that already posted counts only for credit notes
# (sapApprovals.js TWIN_DRAFT_TYPES): equal totals on one day are routine for
# invoices and receipts.
TWIN_DRAFT_TYPES = ("14", "19")

SCOPES = ("waiting_on_me", "raised_by_me", "all")
STATUSES = tuple(rule.APP_FILTER_TO_OWDD)

_STAGE_STATUS = {"W": "PENDING", "Y": "APPROVED", "N": "REJECTED"}

# ---------------------------------------------------------------------------
# The pending rule (see the module docstring)
# ---------------------------------------------------------------------------

# The newest request this draft holds under this request's template.
_LATEST_OF_TEMPLATE = """(SELECT MAX(W2."WddCode") FROM "{schema}"."OWDD" W2
    WHERE W2."DraftEntry" = W."DraftEntry" AND W2."ObjType" = W."ObjType"
      AND W2."WtmCode" = W."WtmCode")"""

_SUPERSEDED_SQL = (
    f"""(W."Status" = 'W' AND W."IsDraft" = 'Y' AND W."WddCode" < {_LATEST_OF_TEMPLATE})"""
)

#: :func:`inbox_status` over OWDD alias ``W`` and the ``DR``/``PD`` draft joins.
EFFECTIVE_STATUS_SQL = (
    f"(CASE WHEN {_SUPERSEDED_SQL} THEN 'C' "
    f"ELSE {rule.effective_status_sql('W')} END)"
)

_DRAFT_JOINS = rule.draft_join_sql("W")


def inbox_status(owdd_status, is_draft, draft_status, superseded: bool) -> str:
    """The OWDD code SAP acts on: the draft's say, and superseded means cancelled.

    >>> inbox_status("W", "Y", "W", superseded=False)
    'W'
    >>> inbox_status("W", "Y", "W", superseded=True)
    'C'
    >>> inbox_status("W", "Y", "N", superseded=False)
    'N'
    >>> inbox_status("Y", "Y", "W", superseded=True)
    'Y'
    """
    if superseded and owdd_status == "W" and rule.is_draft_flag(is_draft):
        return "C"
    return rule.effective_status(owdd_status, is_draft, draft_status)


def stale_message(stage: dict) -> str:
    """What the decision and withdraw endpoints say about a request no longer pending.

    The portal's ``staleRequestMessage`` for a leftover, plus the plain case of
    a request that was genuinely decided while the page was open.
    """
    code = stage.get("wdd_code")
    status = stage.get("status")
    if stage.get("superseded"):
        return (
            f"SAP still lists approval request #{code} as pending, but its draft was "
            "edited since and SAP opened a newer request for it — decide that one "
            "instead. This one no longer appears under Pending."
        )
    if stage.get("stale_pending"):
        outcome = {
            "CANCELLED": "was cancelled",
            "REJECTED": "was rejected",
            "APPROVED": "was approved",
            "GENERATED": "has already been posted",
        }.get(status, "is no longer pending")
        return (
            f"SAP still lists approval request #{code} as pending, but the approval of "
            f"its draft {outcome} in SAP, so there is nothing left to approve or "
            "reject. It no longer appears under Pending."
        )
    return (
        f"Approval request #{code} is already {str(status or 'decided').lower()} in "
        "SAP; it can no longer be decided or withdrawn."
    )


def posted_duplicates(row: dict) -> list[dict]:
    """Every already-posted copy approving ``row`` would duplicate (portal
    ``postedDuplicatesOf``). Empty means safe to approve.

    The request's OWN posted document counts only while the request is still
    pending: every normally approved request has one — that is what approval
    produces — and counting it would flag the whole Approved tab.
    """
    out = []
    own = row.get("already_posted_as")
    if own and row.get("status", "PENDING") == "PENDING":
        out.append(own)
    for doc in row.get("duplicate_of_posted") or []:
        if not any(p["doc_entry"] == doc["doc_entry"] for p in out):
            out.append(doc)
    return out


# ---------------------------------------------------------------------------
# SQL fragments over OWDD alias W
# ---------------------------------------------------------------------------

# The authorizer the current stage waits on, for display. A stage names one
# user in this estate (WST1 MaxReqr = 1); MIN keeps a two-user stage from
# fanning the row out. Who MAY decide is read separately (every undecided user
# at the stage), so a second authorizer is never refused because of MIN.
_CURRENT_APPROVER = """(
    SELECT MIN(AU."{column}") FROM "{{schema}}"."WDD1" S
    LEFT JOIN "{{schema}}"."OUSR" AU ON AU."USERID" = S."UserID"
    WHERE S."WddCode" = W."WddCode" AND S."StepCode" = W."CurrStep"
      AND S."Status" = 'W'
)"""


def _decided(column: str) -> str:
    """Once decided, the stage CurrStep points at holds the decision."""
    return f"""(
    SELECT MIN({column}) FROM "{{schema}}"."WDD1" S
    LEFT JOIN "{{schema}}"."OUSR" DU ON DU."USERID" = S."UserID"
    WHERE S."WddCode" = W."WddCode" AND S."StepCode" = W."CurrStep"
      AND S."Status" = W."Status" AND W."Status" <> 'W'
)"""


# Every column a row is built from. Payment drafts (OPDF) are read for party,
# number, date, total and remarks only: their currency and reference columns
# are named differently from ODRF's and were never read by the portal.
_HEADER_COLUMNS = f"""
    W."WddCode" AS "WddCode", W."ObjType" AS "ObjType", W."DraftEntry" AS "DraftEntry",
    W."OwnerID" AS "OwnerID", W."CurrStep" AS "CurrStep", W."IsDraft" AS "IsDraft",
    W."WtmCode" AS "WtmCode", W."Status" AS "OwddStatus",
    W."CreateDate" AS "CreateDate", W."CreateTime" AS "CreateTime", W."Remarks" AS "Remarks",
    {rule.DRAFT_STATUS_SQL} AS "DraftStatus",
    {EFFECTIVE_STATUS_SQL} AS "EffStatus",
    CASE WHEN {_SUPERSEDED_SQL} THEN 1 ELSE 0 END AS "Superseded",
    DR."DocEntry" AS "OdrfEntry", DR."DocType" AS "DocType",
    COALESCE(DR."DocNum", PD."DocNum") AS "DocNum",
    COALESCE(DR."CardCode", PD."CardCode") AS "CardCode",
    COALESCE(DR."CardName", PD."CardName") AS "CardName",
    DR."NumAtCard" AS "NumAtCard",
    COALESCE(DR."DocTotal", PD."DocTotal") AS "DocTotal",
    DR."DocCur" AS "DocCur",
    COALESCE(DR."DocDate", PD."DocDate") AS "DocDate",
    COALESCE(DR."Comments", PD."Comments") AS "Comments",
    O."USER_CODE" AS "OwnerCode", O."U_NAME" AS "OwnerName",
    M."Name" AS "TemplateName",
    {_CURRENT_APPROVER.format(column="USER_CODE")} AS "ApproverCode",
    {_CURRENT_APPROVER.format(column="U_NAME")} AS "ApproverName",
    (SELECT MAX(S2."Remarks") FROM "{{schema}}"."WDD1" S2
     WHERE S2."WddCode" = W."WddCode" AND S2."Status" = 'N') AS "RejectRemarks",
    {_decided('DU."USER_CODE"')} AS "DecidedBy",
    {_decided('DU."U_NAME"')} AS "DecidedByName",
    {_decided('S."UpdateDate"')} AS "DecidedDate",
    {_decided('S."UpdateTime"')} AS "DecidedTime"
"""

_HEADER_FROM = f"""
    FROM "{{schema}}"."OWDD" W
    {_DRAFT_JOINS}
    LEFT JOIN "{{schema}}"."OUSR" O ON O."USERID" = W."OwnerID"
    LEFT JOIN "{{schema}}"."OWTM" M ON M."WtmCode" = W."WtmCode"
"""

# The caller holds an undecided line at the stage now waiting.
_WAITING_ON = """EXISTS (SELECT 1 FROM "{schema}"."WDD1" L
    WHERE L."WddCode" = x."WddCode" AND L."UserID" = ?
      AND L."Status" = 'W' AND L."StepCode" = x."CurrStep")"""

# The portal's visibility rule (listVisibleApprovalRequestsHana): you raised it,
# or you sit on a decision line — and while it is pending, that line must be
# undecided and at the current stage.
_VISIBLE = """(x."OwnerID" = ? OR EXISTS (SELECT 1 FROM "{schema}"."WDD1" L
    WHERE L."WddCode" = x."WddCode" AND L."UserID" = ?
      AND (x."EffStatus" <> 'W' OR (L."Status" = 'W' AND L."StepCode" = x."CurrStep"))))"""

# A superset of _VISIBLE that needs no effective status, applied to the raw
# OWDD scan so the correlated status expression only runs over requests that
# involve the caller at all.
_INVOLVES = """(W."OwnerID" = ? OR EXISTS (SELECT 1 FROM "{schema}"."WDD1" L0
    WHERE L0."WddCode" = W."WddCode" AND L0."UserID" = ?))"""

_WAITING_ON_RAW = """(W."Status" = 'W' AND EXISTS (SELECT 1 FROM "{schema}"."WDD1" L0
    WHERE L0."WddCode" = W."WddCode" AND L0."UserID" = ?
      AND L0."Status" = 'W' AND L0."StepCode" = W."CurrStep"))"""


# ---------------------------------------------------------------------------
# Value helpers
# ---------------------------------------------------------------------------


def _clean(value) -> str:
    return str(value).strip() if value is not None else ""


def _int(value):
    return int(value) if value is not None else None


def _amount(value) -> str | None:
    """Money as a decimal STRING — a float would round paise off a total."""
    if value is None:
        return None
    return f"{Decimal(str(value)):.2f}"


def _date(value) -> str | None:
    if value is None:
        return None
    return value.strftime("%Y-%m-%d") if hasattr(value, "strftime") else str(value)[:10]


def _iso(stamp_date, stamp_time) -> str | None:
    """SAP stamps a date and an HHMM smallint separately; one ISO string."""
    if stamp_date is None:
        return None
    hhmm = int(stamp_time or 0)
    return f"{_date(stamp_date)}T{hhmm // 100:02d}:{hhmm % 100:02d}:00"


def _placeholders(values) -> str:
    return ", ".join("?" for _ in values)


class _Session:
    """One HANA connection, several statements: a page is one round of reads."""

    def __init__(self, conn, schema: str):
        self.conn = conn
        self.schema = schema

    def rows(self, sql: str, params=()) -> list[dict]:
        cursor = None
        try:
            cursor = self.conn.cursor()
            cursor.execute(sql.replace("{schema}", self.schema), tuple(params))
            names = [column[0] for column in (cursor.description or ())]
            return [dict(zip(names, row)) for row in cursor.fetchall()]
        except dbapi.Error as e:
            logger.error("SAP HANA approval-inbox query failed: %s", e)
            raise SAPDataError("Failed to read SAP approval requests.") from e
        finally:
            if cursor is not None:
                try:
                    cursor.close()
                except Exception:
                    pass


class HanaApprovalInboxReader:
    """Approval requests of every object type, as they involve one SAP user."""

    def __init__(self, context):
        self.connection = HanaConnection(context.hana)

    # ------------------------------------------------------------------
    # List + count
    # ------------------------------------------------------------------

    def list_requests(
        self,
        sap_user_code: str,
        *,
        scope: str = "all",
        status: str | None = None,
        object_type: str | None = None,
        date_from=None,
        date_to=None,
        search: str = "",
        limit: int = 200,
        offset: int = 0,
    ) -> list[dict]:
        """Requests involving ``sap_user_code``, newest first; ``offset`` pages on.

        ``scope``: ``waiting_on_me`` (pending at a stage of theirs — the status
        filter does not apply), ``raised_by_me`` (``OWDD.OwnerID``) or ``all``
        (the portal's visibility rule). ``status`` is one of :data:`STATUSES`
        or None for every status. ``search`` matches the request, draft and
        document numbers, the party, its reference, the remarks, the
        originator and the draft's item lines.
        """
        if scope not in SCOPES:
            raise SAPValidationError(f"Unknown approvals scope: {scope}")
        if status is not None and status not in STATUSES:
            raise SAPValidationError(f"Unknown approval status: {status}")
        limit = max(1, min(int(limit), 500))
        offset = max(0, int(offset))

        with self._session() as session:
            user_id = self._user_id(session, sap_user_code)
            if user_id is None:
                return []

            inner, inner_params = [], []
            outer, outer_params = [], []
            if scope == "waiting_on_me":
                inner.append(_WAITING_ON_RAW)
                inner_params.append(user_id)
                outer.append(f"""x."EffStatus" = 'W' AND {_WAITING_ON}""")
                outer_params.append(user_id)
            elif scope == "raised_by_me":
                inner.append('W."OwnerID" = ?')
                inner_params.append(user_id)
            else:
                inner.append(_INVOLVES)
                inner_params.extend([user_id, user_id])
                outer.append(_VISIBLE)
                outer_params.extend([user_id, user_id])

            if status is not None and scope != "waiting_on_me":
                codes = rule.APP_FILTER_TO_OWDD[status]
                scan = rule.owdd_codes_to_scan(codes)
                inner.append(f'W."Status" IN ({_placeholders(scan)})')
                inner_params.extend(scan)
                outer.append(f'x."EffStatus" IN ({_placeholders(codes)})')
                outer_params.extend(codes)
            if object_type:
                inner.append('W."ObjType" = ?')
                inner_params.append(str(object_type))
            if date_from:
                inner.append('W."CreateDate" >= ?')
                inner_params.append(date_from)
            if date_to:
                inner.append('W."CreateDate" <= ?')
                inner_params.append(date_to)
            text = (search or "").strip()
            if text:
                clause, params = self._search_clause(text)
                inner.append(clause)
                inner_params.extend(params)

            sql = f"""
                SELECT x.*,
                       CASE WHEN x."EffStatus" = 'W' AND {_WAITING_ON}
                            THEN 1 ELSE 0 END AS "WaitingOnMe"
                FROM (
                    SELECT {_HEADER_COLUMNS}
                    {_HEADER_FROM}
                    WHERE {' AND '.join(inner)}
                ) x
                {('WHERE ' + ' AND '.join(outer)) if outer else ''}
                ORDER BY x."WddCode" DESC
                LIMIT {limit} OFFSET {offset}
            """
            raw = session.rows(sql, [user_id, *inner_params, *outer_params])
            rows = [self._row(r) for r in raw]
            self._decorate(session, rows)
        return rows

    def waiting_count(self, sap_user_code: str) -> int:
        """Requests pending at a stage of ``sap_user_code`` — the sidebar badge."""
        with self._session() as session:
            user_id = self._user_id(session, sap_user_code)
            if user_id is None:
                return 0
            found = session.rows(
                f"""
                SELECT COUNT(*) AS "N" FROM (
                    SELECT W."WddCode", {EFFECTIVE_STATUS_SQL} AS "EffStatus"
                    FROM "{{schema}}"."OWDD" W
                    {_DRAFT_JOINS}
                    WHERE {_WAITING_ON_RAW}
                ) x
                WHERE x."EffStatus" = 'W'
                """,
                [user_id],
            )
        return int(found[0]["N"]) if found else 0

    # ------------------------------------------------------------------
    # One request
    # ------------------------------------------------------------------

    def detail(self, wdd_code: int, sap_user_code: str | None) -> dict | None:
        """One request with every stage and the draft's lines; None if SAP has none."""
        with self._session() as session:
            user_id = self._user_id(session, sap_user_code) if sap_user_code else None
            found = session.rows(
                f"""
                SELECT x.*,
                       CASE WHEN x."EffStatus" = 'W' AND {_WAITING_ON}
                            THEN 1 ELSE 0 END AS "WaitingOnMe"
                FROM (SELECT {_HEADER_COLUMNS} {_HEADER_FROM} WHERE W."WddCode" = ?) x
                """,
                [user_id if user_id is not None else -1, int(wdd_code)],
            )
            if not found:
                return None
            row = self._row(found[0])
            row["stages"] = self._stages(session, row)
            row["lines"] = self._lines(session, found[0].get("OdrfEntry"))
            # Payment drafts keep their lines elsewhere (not read here).
            row["lines_available"] = found[0].get("OdrfEntry") is not None
            self._decorate(session, [row])
        return row

    def current_stage(
        self,
        wdd_code: int,
        *,
        with_duplicates: bool = False,
        with_item_lines: bool = False,
    ) -> dict | None:
        """The request as the decision and withdraw endpoints must judge it.

        Read fresh at the moment of the write rather than taken from the page:
        somebody may have advanced or decided it in SAP meanwhile. The status is
        computed in Python (:func:`inbox_status`) from the raw columns, and
        ``authorizer_codes`` lists every user still undecided at the current
        stage — the accounts SAP will take a decision from.

        ``with_duplicates`` adds ``posted_duplicates`` for a pending request and,
        unlike on a list, raises if that read fails: it gates an approval.
        ``with_item_lines`` adds the draft's item lines with their
        Without Qty Posting flag (``DRF1.NoInvtryMv``).
        """
        with self._session() as session:
            found = session.rows(
                f"""
                SELECT {_HEADER_COLUMNS}, {_LATEST_OF_TEMPLATE} AS "LatestCode"
                {_HEADER_FROM}
                WHERE W."WddCode" = ?
                """,
                [int(wdd_code)],
            )
            if not found:
                return None
            raw = found[0]
            stage = self._row(raw)
            superseded = (
                raw.get("LatestCode") is not None
                and int(raw["WddCode"]) < int(raw["LatestCode"])
            )
            code = inbox_status(
                raw.get("OwddStatus"), raw.get("IsDraft"), raw.get("DraftStatus"), superseded
            )
            stage["status"] = rule.OWDD_TO_APP.get(code, code)
            stage["stale_pending"] = raw.get("OwddStatus") == "W" and code != "W"
            stage["superseded"] = bool(superseded and stage["stale_pending"])

            authorizers = []
            if raw.get("CurrStep") is not None:
                authorizers = session.rows(
                    """
                    SELECT U."USER_CODE" AS "Code", U."U_NAME" AS "Name"
                    FROM "{schema}"."WDD1" S
                    LEFT JOIN "{schema}"."OUSR" U ON U."USERID" = S."UserID"
                    WHERE S."WddCode" = ? AND S."StepCode" = ? AND S."Status" = 'W'
                    ORDER BY U."USER_CODE"
                    """,
                    [int(wdd_code), int(raw["CurrStep"])],
                )
            stage["authorizer_codes"] = [
                _clean(a["Code"]) for a in authorizers if _clean(a.get("Code"))
            ]

            if with_item_lines:
                entry = raw.get("OdrfEntry")
                stage["item_lines"] = [] if entry is None else [
                    {
                        "line_num": int(line["LineNum"]),
                        "item_code": _clean(line["ItemCode"]),
                        "without_qty_posting": _clean(line.get("NoInvtryMv")) == "Y",
                    }
                    for line in session.rows(
                        """
                        SELECT "LineNum", "ItemCode", "NoInvtryMv"
                        FROM "{schema}"."DRF1"
                        WHERE "DocEntry" = ? AND "ItemCode" IS NOT NULL
                        ORDER BY "LineNum"
                        """,
                        [int(entry)],
                    )
                ]

            stage["posted_duplicates"] = []
            if with_duplicates and stage["status"] == "PENDING":
                # No try/except: a check that gates the approval fails closed.
                self._attach_duplicates(session, [stage])
                stage["posted_duplicates"] = posted_duplicates(stage)
                stage["is_duplicate"] = bool(stage["posted_duplicates"])
        return stage

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    @contextmanager
    def _session(self):
        try:
            conn = self.connection.connect()
        except dbapi.Error as e:
            logger.error("SAP HANA connection failed while reading approval requests: %s", e)
            raise SAPConnectionError("Unable to connect to SAP HANA.") from e
        try:
            yield _Session(conn, self.connection.schema)
        finally:
            try:
                conn.close()
            except Exception:
                pass

    @staticmethod
    def _user_id(session: _Session, sap_user_code: str | None):
        """``OUSR.USERID`` of a mapped code; None if SAP has no such user."""
        code = _clean(sap_user_code).upper()
        if not code:
            return None
        found = session.rows(
            'SELECT "USERID" AS "UserID" FROM "{schema}"."OUSR" WHERE UPPER("USER_CODE") = ?',
            [code],
        )
        return int(found[0]["UserID"]) if found else None

    @staticmethod
    def _search_clause(text: str) -> tuple[str, list]:
        """One box, bound values only. Numbers also match the request, draft and document."""
        like = f"%{text[:100].upper()}%"
        parts = [
            'UPPER(COALESCE(DR."CardCode", PD."CardCode", \'\')) LIKE ?',
            'UPPER(COALESCE(DR."CardName", PD."CardName", \'\')) LIKE ?',
            'UPPER(COALESCE(DR."NumAtCard", \'\')) LIKE ?',
            'UPPER(COALESCE(DR."Comments", PD."Comments", \'\')) LIKE ?',
            'UPPER(COALESCE(O."U_NAME", \'\')) LIKE ?',
            """EXISTS (SELECT 1 FROM "{schema}"."DRF1" SL WHERE SL."DocEntry" = DR."DocEntry"
                AND (UPPER(SL."ItemCode") LIKE ? OR UPPER(SL."Dscription") LIKE ?))""",
        ]
        params = [like] * 7
        if text.isdigit() and len(text) <= 12:
            number = int(text)
            parts.extend([
                'W."WddCode" = ?',
                'W."DraftEntry" = ?',
                'COALESCE(DR."DocNum", PD."DocNum") = ?',
            ])
            params.extend([number, number, number])
        return f"({' OR '.join(parts)})", params

    @staticmethod
    def _row(r: dict) -> dict:
        eff = r.get("EffStatus")
        obj_type = _clean(r.get("ObjType"))
        stale = r.get("OwddStatus") == "W" and eff != "W"
        return {
            # The id every endpoint acts on.
            "wdd_code": int(r["WddCode"]),
            "object_type": obj_type,
            "object_type_label": OBJECT_TYPE_LABELS.get(obj_type, f"Object {obj_type}"),
            "draft_entry": _int(r.get("DraftEntry")),
            "is_draft": rule.is_draft_flag(r.get("IsDraft")),
            "status": rule.OWDD_TO_APP.get(eff, eff),
            # SAP still says 'W', but the draft (or a newer request) says otherwise.
            "stale_pending": stale,
            "superseded": bool(stale and r.get("Superseded")),
            "current_step": _int(r.get("CurrStep")),
            "template_code": _int(r.get("WtmCode")),
            "template_name": _clean(r.get("TemplateName")) or None,
            "remarks": _clean(r.get("Remarks")) or None,
            "created_at": _iso(r.get("CreateDate"), r.get("CreateTime")),
            "originator_code": _clean(r.get("OwnerCode")) or None,
            "originator_name": _clean(r.get("OwnerName")) or None,
            # The authorizer the current stage waits on — pending rows only.
            "approver_code": (_clean(r.get("ApproverCode")) or None) if eff == "W" else None,
            "approver_name": (_clean(r.get("ApproverName")) or None) if eff == "W" else None,
            "decided_by": _clean(r.get("DecidedBy")) or None,
            "decided_by_name": _clean(r.get("DecidedByName")) or None,
            "decided_at": _iso(r.get("DecidedDate"), r.get("DecidedTime")),
            "rejection_reason": _clean(r.get("RejectRemarks")) or None,
            "waiting_on_me": bool(r.get("WaitingOnMe")),
            # The document the request is about, as the draft holds it. A
            # draft's DocNum is provisional (shared by open drafts), so the
            # draft entry is the key; the number is only a hint.
            "document": {
                "doc_num": _int(r.get("DocNum")),
                "doc_type": _clean(r.get("DocType")) or None,
                "card_code": _clean(r.get("CardCode")) or None,
                "party_name": _clean(r.get("CardName")) or _clean(r.get("CardCode")) or None,
                "reference": _clean(r.get("NumAtCard")) or None,
                "total_amount": _amount(r.get("DocTotal")),
                "currency": _clean(r.get("DocCur")) or None,
                "doc_date": _date(r.get("DocDate")),
                "comments": _clean(r.get("Comments")) or None,
            },
            "request_count": 1,
            "pending_request_count": 1 if eff == "W" else 0,
            "sibling_requests": [],
            "already_posted_as": None,
            "duplicate_of_posted": [],
            "posted_duplicates": [],
            "is_duplicate": False,
        }

    def _decorate(self, session: _Session, rows: list[dict]) -> None:
        """Siblings and duplicates: best effort, never at the cost of the rows."""
        if not rows:
            return
        try:
            self._attach_siblings(session, rows)
        except SAPDataError:
            logger.warning("Approval inbox: sibling lookup skipped for %d rows", len(rows))
        pending = [r for r in rows if r["status"] == "PENDING"]
        try:
            self._attach_duplicates(session, pending)
        except SAPDataError:
            logger.warning("Approval inbox: duplicate lookup skipped for %d rows", len(pending))
            return
        for row in pending:
            row["posted_duplicates"] = posted_duplicates(row)
            row["is_duplicate"] = bool(row["posted_duplicates"])

    @staticmethod
    def _attach_siblings(session: _Session, rows: list[dict]) -> None:
        keys = {(r["draft_entry"], r["object_type"]) for r in rows if r.get("draft_entry")}
        if not keys:
            return
        drafts = sorted({draft for draft, _ in keys})
        found = session.rows(
            f"""
            SELECT W."DraftEntry" AS "DraftEntry", W."ObjType" AS "ObjType",
                   W."WddCode" AS "WddCode", W."WtmCode" AS "WtmCode",
                   M."Name" AS "TemplateName", W."CreateDate" AS "CreateDate",
                   W."CreateTime" AS "CreateTime",
                   {EFFECTIVE_STATUS_SQL} AS "EffStatus"
            FROM "{{schema}}"."OWDD" W
            {_DRAFT_JOINS}
            LEFT JOIN "{{schema}}"."OWTM" M ON M."WtmCode" = W."WtmCode"
            WHERE W."DraftEntry" IN ({_placeholders(drafts)}) AND W."Status" <> 'C'
            ORDER BY W."WddCode"
            """,
            drafts,
        )
        groups: dict[tuple, list] = {}
        for s in found:
            key = (int(s["DraftEntry"]), _clean(s["ObjType"]))
            # A cancelled request (a leftover, or superseded) holds nothing.
            if key in keys and s.get("EffStatus") != "C":
                groups.setdefault(key, []).append(s)
        for row in rows:
            group = groups.get((row.get("draft_entry"), row["object_type"])) or []
            if not group:
                continue
            row["request_count"] = max(len(group), 1)
            row["pending_request_count"] = sum(1 for g in group if g.get("EffStatus") == "W")
            row["sibling_requests"] = [
                {
                    "wdd_code": int(g["WddCode"]),
                    "status": rule.OWDD_TO_APP.get(g.get("EffStatus"), g.get("EffStatus")),
                    "template_code": _int(g.get("WtmCode")),
                    "template_name": _clean(g.get("TemplateName")) or None,
                    "created_at": _iso(g.get("CreateDate"), g.get("CreateTime")),
                }
                for g in group
                if int(g["WddCode"]) != row["wdd_code"]
            ]

    @staticmethod
    def _attach_duplicates(session: _Session, rows: list[dict]) -> None:
        """The portal's three duplicate shapes, every object type in one statement."""
        by_type: dict[str, dict[int, list]] = {}
        for row in rows:
            obj_type, draft = row["object_type"], row.get("draft_entry")
            if obj_type in POSTED_TABLE and draft:
                by_type.setdefault(obj_type, {}).setdefault(int(draft), []).append(row)
        if not by_type:
            return

        selects, params = [], []
        posted_columns = (
            'p."DocEntry" AS "PostedEntry", p."DocNum" AS "PostedDocNum", '
            'p."DocTotal" AS "PostedTotal", p."DocCur" AS "PostedCurrency", '
            'p."DocDate" AS "PostedDate"'
        )
        for obj_type, drafts in by_type.items():
            table = POSTED_TABLE[obj_type]
            entries = sorted(drafts)
            marks = _placeholders(entries)
            if obj_type in TWIN_DRAFT_TYPES:
                # (a) a DIFFERENT draft, same partner/date/total, already posted.
                selects.append(f"""
                    SELECT CAST('TWIN' AS NVARCHAR(10)) AS "Kind", d."ObjType" AS "ObjType", d."DocEntry" AS "DraftEntry",
                           t."DocEntry" AS "FromDraft", {posted_columns}
                    FROM "{{schema}}"."ODRF" d
                    JOIN "{{schema}}"."ODRF" t ON t."DocEntry" <> d."DocEntry"
                         AND t."ObjType" = d."ObjType" AND t."CardCode" = d."CardCode"
                         AND t."DocTotal" = d."DocTotal" AND t."DocDate" = d."DocDate"
                    JOIN "{{schema}}"."{table}" p ON p."draftKey" = t."DocEntry" AND p."CANCELED" = 'N'
                    WHERE d."ObjType" = ? AND d."DocEntry" IN ({marks})""")
                params.extend([obj_type, *entries])
            # (c) the same document posted directly — same partner, date and
            # total AND the same reference or remarks, because equal amounts
            # on one day are routine for some partners.
            selects.append(f"""
                SELECT CAST('POSTED' AS NVARCHAR(10)) AS "Kind", d."ObjType" AS "ObjType", d."DocEntry" AS "DraftEntry",
                       p."draftKey" AS "FromDraft", {posted_columns}
                FROM "{{schema}}"."ODRF" d
                JOIN "{{schema}}"."{table}" p ON p."CardCode" = d."CardCode"
                     AND p."DocDate" = d."DocDate" AND p."DocTotal" = d."DocTotal"
                     AND p."CANCELED" = 'N' AND COALESCE(p."draftKey", -1) <> d."DocEntry"
                     AND ((COALESCE(d."NumAtCard", '') <> '' AND p."NumAtCard" = d."NumAtCard")
                       OR (COALESCE(d."Comments", '') <> '' AND p."Comments" = d."Comments"))
                WHERE d."ObjType" = ? AND d."DocEntry" IN ({marks})""")
            params.extend([obj_type, *entries])
            # (b) this very draft already produced a posted document.
            selects.append(f"""
                SELECT CAST('SELF' AS NVARCHAR(10)) AS "Kind", p."ObjType" AS "ObjType", p."draftKey" AS "DraftEntry",
                       p."draftKey" AS "FromDraft", {posted_columns}
                FROM "{{schema}}"."{table}" p
                WHERE p."CANCELED" = 'N' AND p."draftKey" IN ({marks})""")
            params.extend(entries)

        found = session.rows(" UNION ALL ".join(selects), params)
        for hit in found:
            obj_type = _clean(hit.get("ObjType"))
            targets = by_type.get(obj_type, {}).get(_int(hit.get("DraftEntry")))
            if not targets:
                continue
            doc = {
                "doc_entry": int(hit["PostedEntry"]),
                "doc_num": _int(hit.get("PostedDocNum")),
                "doc_date": _date(hit.get("PostedDate")),
                "total_amount": _amount(hit.get("PostedTotal")),
                "currency": _clean(hit.get("PostedCurrency")) or None,
                "table": POSTED_TABLE[obj_type],
                "from_draft_entry": _int(hit.get("FromDraft")),
            }
            for row in targets:
                if hit["Kind"] == "SELF":
                    row["already_posted_as"] = doc
                elif not any(p["doc_entry"] == doc["doc_entry"] for p in row["duplicate_of_posted"]):
                    row["duplicate_of_posted"].append(doc)

    @staticmethod
    def _stages(session: _Session, row: dict) -> list[dict]:
        found = session.rows(
            """
            SELECT S."StepCode" AS "StepCode", S."Status" AS "Status", S."Remarks" AS "Remarks",
                   S."UpdateDate" AS "UpdateDate", S."UpdateTime" AS "UpdateTime",
                   U."USER_CODE" AS "UserCode", U."U_NAME" AS "UserName"
            FROM "{schema}"."WDD1" S
            LEFT JOIN "{schema}"."OUSR" U ON U."USERID" = S."UserID"
            WHERE S."WddCode" = ?
            ORDER BY S."StepCode", U."USER_CODE"
            """,
            [row["wdd_code"]],
        )
        names = {}
        steps = sorted({int(s["StepCode"]) for s in found if s.get("StepCode") is not None})
        if steps:
            try:
                names = {
                    int(n["WstCode"]): _clean(n.get("Name")) or None
                    for n in session.rows(
                        f"""SELECT "WstCode", "Name" FROM "{{schema}}"."OWST"
                            WHERE "WstCode" IN ({_placeholders(steps)})""",
                        steps,
                    )
                }
            except SAPDataError:
                logger.warning("Approval inbox: stage names skipped for %s", row["wdd_code"])
        stages = []
        for s in found:
            step = _int(s.get("StepCode"))
            decided = s.get("Status") in ("Y", "N")
            stages.append({
                "step_code": step,
                "stage_name": names.get(step),
                "user_code": _clean(s.get("UserCode")) or None,
                "user_name": _clean(s.get("UserName")) or None,
                "status": _STAGE_STATUS.get(s.get("Status"), _clean(s.get("Status")) or None),
                "remarks": _clean(s.get("Remarks")) or None,
                "decided_at": _iso(s.get("UpdateDate"), s.get("UpdateTime")) if decided else None,
                "is_current": step is not None and step == row["current_step"],
            })
        return stages

    @staticmethod
    def _lines(session: _Session, odrf_entry) -> list[dict]:
        if odrf_entry is None:
            return []
        found = session.rows(
            """
            SELECT L."LineNum" AS "LineNum", L."ItemCode" AS "ItemCode",
                   L."Dscription" AS "Dscription", L."Quantity" AS "Quantity",
                   L."unitMsr" AS "UnitMsr", L."Price" AS "Price",
                   L."LineTotal" AS "LineTotal", L."VatGroup" AS "VatGroup",
                   L."WhsCode" AS "WhsCode", L."AcctCode" AS "AcctCode",
                   L."BaseType" AS "BaseType", L."BaseRef" AS "BaseRef",
                   L."NoInvtryMv" AS "NoInvtryMv"
            FROM "{schema}"."DRF1" L
            WHERE L."DocEntry" = ?
            ORDER BY L."LineNum"
            """,
            [int(odrf_entry)],
        )
        lines = []
        for line in found:
            item_code = _clean(line.get("ItemCode")) or None
            base_type = _int(line.get("BaseType"))
            lines.append({
                "line_num": int(line["LineNum"]),
                "item_code": item_code,
                "description": _clean(line.get("Dscription")) or None,
                "quantity": float(line["Quantity"]) if line.get("Quantity") is not None else None,
                "unit": _clean(line.get("UnitMsr")) or None,
                "price": _amount(line.get("Price")),
                "line_total": _amount(line.get("LineTotal")),
                "tax_code": _clean(line.get("VatGroup")) or None,
                "warehouse": _clean(line.get("WhsCode")) or None,
                "account_code": _clean(line.get("AcctCode")) or None,
                "base_type_label": (
                    OBJECT_TYPE_LABELS.get(str(base_type))
                    if base_type is not None and base_type >= 0
                    else None
                ),
                "base_ref": _clean(line.get("BaseRef")) or None,
                # SAP's "Without Qty Posting": value only, no stock. Only an
                # item line can carry it.
                "without_qty_posting": bool(item_code) and _clean(line.get("NoInvtryMv")) == "Y",
            })
        return lines
