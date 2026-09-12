"""Read inventory-transfer DRAFTS that are approved but never added (ODRF/DRF1).

An inventory transfer keyed in the SAP client on a route an approval template
covers is not saved as a document: SAP saves it as a **draft** (``ODRF`` with
``ObjType = '67'``) and opens an approval request on it. Approving that request
moves nothing either — somebody must still open the draft and press **Add**.
Until they do, the stock has not moved and no ``OWTR`` exists, so the document
is invisible to every other read in this app: it is not a transfer request
(``OWTQ``), and the approval queue drops it the moment it stops being pending.

Live shapes this reader relies on, verified across all three company databases:

* The draft's source warehouse is ``ODRF."Filler"``; the destination is
  ``ODRF."ToWhsCode"``. There is no ``FromWhsCode`` column. On the lines
  (``DRF1``) the sense is reversed from a sales line — ``FromWhsCod`` is the
  source and ``WhsCode`` the destination.
* **Added** drafts are not deleted. SAP closes them: ``DocStatus = 'C'`` with
  ``WddStatus = '-'``, and the posted ``OWTR`` points back through
  ``OWTR."draftKey"``. So a still-to-add draft is ``DocStatus = 'O'`` and the
  approved ones carry ``WddStatus = 'Y'`` (2,166 closed vs 3 open in Beverages
  when this was written; 33 open across the estate, the oldest 612 days).
* Unlike an A/R invoice draft, a transfer draft **does** carry its batch
  allocations (``DRF16``, keyed ``AbsEntry``/``LineNum``) — every batch-managed
  line of all 33 had them. So the add must post the operator's own batches and
  must never re-allocate them.
"""

import logging
from typing import Optional

from hdbcli import dbapi

from .connection import HanaConnection
from ..exceptions import SAPConnectionError, SAPDataError

logger = logging.getLogger(__name__)

# ODRF.ObjType for an inventory transfer draft.
OBJ_TYPE_TRANSFER = "67"

# ODRF.WddStatus — where the draft stands with SAP's approval procedure.
WDD_APPROVED = "Y"
WDD_PENDING = "W"
WDD_REJECTED = "N"
WDD_CANCELLED = "C"
# '-' means no approval procedure ever applied to this draft.
WDD_NONE = "-"

_WDD_LABELS = {
    WDD_APPROVED: "approved",
    WDD_PENDING: "waiting for approval",
    WDD_REJECTED: "rejected",
    WDD_CANCELLED: "cancelled",
    WDD_NONE: "not routed for approval",
}


def _clean(value) -> str:
    return (value or "").strip()


def _date(value) -> Optional[str]:
    return value.strftime("%Y-%m-%d") if value is not None else None


class HanaTransferDraftReader:
    """List and inspect inventory-transfer drafts still waiting to be added."""

    def __init__(self, context):
        self.connection = HanaConnection(context.hana)

    # ------------------------------------------------------------------
    # List
    # ------------------------------------------------------------------

    def list_unposted(self, limit: int = 100) -> list[dict]:
        """Approved transfer drafts nobody has added yet, newest first.

        Deliberately only the **approved** ones. A draft SAP never routed for
        approval (``WddStatus = '-'``) is just as unposted, but it is also where
        a half-keyed document sits — adding one from here would post work its
        author had not finished.
        """
        rows = self._query(
            f"""
            SELECT
                D."DocEntry", D."DocNum", TO_DATE(D."DocDate"),
                IFNULL(D."Filler", ''), IFNULL(D."ToWhsCode", ''),
                IFNULL(D."Comments", ''), IFNULL(D."JrnlMemo", ''),
                D."BPLId", IFNULL(U."U_NAME", ''),
                DAYS_BETWEEN(D."DocDate", CURRENT_DATE) AS age_days,
                IFNULL(D."WddStatus", '')
            FROM "{{schema}}"."ODRF" D
            LEFT JOIN "{{schema}}"."OUSR" U ON U."USERID" = D."UserSign"
            WHERE D."ObjType" = ?
              AND D."DocStatus" = 'O'
              AND D."WddStatus" = ?
              AND IFNULL(D."CANCELED", 'N') = 'N'
            ORDER BY D."DocDate" DESC, D."DocEntry" DESC
            LIMIT {max(1, min(int(limit or 100), 500))}
            """,
            (OBJ_TYPE_TRANSFER, WDD_APPROVED),
        )
        if not rows:
            return []

        lines_by_draft = self._lines([int(row[0]) for row in rows])
        return [
            {
                "draft_entry": int(row[0]),
                # Provisional, and NOT the number the add ends up with: it is
                # the series' next number as at the save, so open drafts share
                # it and the add takes whatever is next then. Across every
                # draft-linked transfer, 4,635 of 11,309 Oil and 878 of 2,168
                # Beverages documents differ from their draft's. Shown as the
                # draft's own number; the real one is read back after the add.
                "doc_num": int(row[1]) if row[1] is not None else None,
                "doc_date": _date(row[2]),
                "from_warehouse": _clean(row[3]),
                "to_warehouse": _clean(row[4]),
                "comments": _clean(row[5]) or None,
                "journal_memo": _clean(row[6]) or None,
                "branch_id": int(row[7]) if row[7] is not None else None,
                "created_by": _clean(row[8]) or None,
                "age_days": int(row[9] or 0),
                "approval_status": _clean(row[10]),
                "lines": lines_by_draft.get(int(row[0]), []),
            }
            for row in rows
        ]

    def unposted_count(self) -> int:
        rows = self._query(
            """
            SELECT COUNT(*)
            FROM "{schema}"."ODRF" D
            WHERE D."ObjType" = ?
              AND D."DocStatus" = 'O'
              AND D."WddStatus" = ?
              AND IFNULL(D."CANCELED", 'N') = 'N'
            """,
            (OBJ_TYPE_TRANSFER, WDD_APPROVED),
        )
        return int(rows[0][0]) if rows else 0

    # ------------------------------------------------------------------
    # One draft
    # ------------------------------------------------------------------

    def get_draft(self, draft_entry: int) -> Optional[dict]:
        """One transfer draft with its lines and where it stands.

        Returns every draft, addable or not — the caller decides, and needs the
        state to say why rather than "not found".
        """
        rows = self._query(
            """
            SELECT
                D."DocEntry", D."DocNum", TO_DATE(D."DocDate"),
                IFNULL(D."Filler", ''), IFNULL(D."ToWhsCode", ''),
                IFNULL(D."Comments", ''), IFNULL(D."JrnlMemo", ''),
                D."BPLId", IFNULL(U."U_NAME", ''),
                DAYS_BETWEEN(D."DocDate", CURRENT_DATE),
                IFNULL(D."WddStatus", ''), IFNULL(D."DocStatus", ''),
                IFNULL(D."CANCELED", 'N'), D."ObjType"
            FROM "{schema}"."ODRF" D
            LEFT JOIN "{schema}"."OUSR" U ON U."USERID" = D."UserSign"
            WHERE D."DocEntry" = ?
            """,
            (int(draft_entry),),
        )
        if not rows:
            return None

        row = rows[0]
        return {
            "draft_entry": int(row[0]),
            "doc_num": int(row[1]) if row[1] is not None else None,
            "doc_date": _date(row[2]),
            "from_warehouse": _clean(row[3]),
            "to_warehouse": _clean(row[4]),
            "comments": _clean(row[5]) or None,
            "journal_memo": _clean(row[6]) or None,
            "branch_id": int(row[7]) if row[7] is not None else None,
            "created_by": _clean(row[8]) or None,
            "age_days": int(row[9] or 0),
            "approval_status": _clean(row[10]),
            "approval_label": _WDD_LABELS.get(_clean(row[10]), _clean(row[10])),
            "doc_status": _clean(row[11]),
            "cancelled": _clean(row[12]) == "Y",
            "obj_type": str(row[13]),
            "is_transfer": str(row[13]) == OBJ_TYPE_TRANSFER,
            # 'O' is SAP's "still a draft"; an added draft is closed to 'C'.
            "is_open": _clean(row[11]) == "O",
            "is_approved": _clean(row[10]) == WDD_APPROVED,
            "lines": self._lines([int(draft_entry)]).get(int(draft_entry), []),
        }

    def posted_document(self, draft_entry: int) -> Optional[dict]:
        """The ``OWTR`` this draft was already added as, if it was.

        The idempotence guard on the add: a retry after a timeout, or a second
        click, must report the document SAP already holds instead of posting the
        stock twice.
        """
        rows = self._query(
            """
            SELECT T."DocEntry", T."DocNum", TO_DATE(T."DocDate"),
                   IFNULL(T."CANCELED", 'N')
            FROM "{schema}"."OWTR" T
            WHERE T."draftKey" = ?
            ORDER BY T."DocEntry"
            """,
            (int(draft_entry),),
        )
        for row in rows:
            if _clean(row[3]) != "Y":
                return {
                    "doc_entry": int(row[0]),
                    "doc_num": int(row[1]) if row[1] is not None else None,
                    "doc_date": _date(row[2]),
                }
        return None

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _lines(self, draft_entries: list[int]) -> dict[int, list]:
        """Draft lines for a whole page in one query.

        ``source_stock`` joins ``OITW`` on the line's SOURCE warehouse: what an
        operator about to add this needs to know is whether the sending side
        still holds the quantity — the draft may be months old.
        ``batches_allocated`` counts the draft's own ``DRF16`` rows, so a
        batch-managed line missing them can be named before SAP refuses the add
        with -4014.
        """
        entries = sorted({int(e) for e in draft_entries})
        if not entries:
            return {}

        placeholders = ", ".join(["?"] * len(entries))
        rows = self._query(
            f"""
            SELECT
                L."DocEntry", L."LineNum", L."ItemCode",
                IFNULL(L."Dscription", ''), IFNULL(I."ItemName", ''),
                L."Quantity", IFNULL(L."unitMsr", ''),
                IFNULL(L."FromWhsCod", ''), IFNULL(L."WhsCode", ''),
                W."OnHand",
                IFNULL(I."ManBtchNum", 'N'),
                (SELECT COUNT(*) FROM "{{schema}}"."DRF16" B
                  WHERE B."AbsEntry" = L."DocEntry" AND B."LineNum" = L."LineNum")
            FROM "{{schema}}"."DRF1" L
            LEFT JOIN "{{schema}}"."OITM" I ON I."ItemCode" = L."ItemCode"
            LEFT JOIN "{{schema}}"."OITW" W
                ON W."ItemCode" = L."ItemCode" AND W."WhsCode" = L."FromWhsCod"
            WHERE L."DocEntry" IN ({placeholders})
            ORDER BY L."DocEntry", L."LineNum"
            """,
            tuple(entries),
        )

        result: dict[int, list] = {}
        for row in rows:
            quantity = row[5]
            on_hand = row[9]
            batch_managed = _clean(row[10]) == "Y"
            allocated = int(row[11] or 0)
            result.setdefault(int(row[0]), []).append({
                "line_num": int(row[1]),
                "item_code": _clean(row[2]),
                "item_name": _clean(row[3]) or _clean(row[4]),
                "quantity": str(quantity) if quantity is not None else "0",
                "uom": _clean(row[6]),
                "from_warehouse": _clean(row[7]),
                "to_warehouse": _clean(row[8]),
                "source_stock": str(on_hand) if on_hand is not None else None,
                "short": (
                    on_hand is not None
                    and quantity is not None
                    and on_hand < quantity
                ),
                "batch_managed": batch_managed,
                "batches_allocated": allocated,
                # SAP refuses the add without an allocation on such a line.
                "batches_missing": batch_managed and allocated == 0,
            })
        return result

    def _query(self, sql: str, params: tuple) -> list:
        conn = None
        cursor = None
        try:
            conn = self.connection.connect()
        except dbapi.Error as e:
            logger.error(
                "SAP HANA connection failed while reading transfer drafts: %s", e
            )
            raise SAPConnectionError("Unable to connect to SAP HANA.") from e

        try:
            cursor = conn.cursor()
            cursor.execute(sql.replace("{schema}", self.connection.schema), params)
            return cursor.fetchall()
        except dbapi.Error as e:
            logger.error("SAP HANA transfer-draft query failed: %s", e)
            raise SAPDataError("Failed to read transfer drafts from SAP.") from e
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
