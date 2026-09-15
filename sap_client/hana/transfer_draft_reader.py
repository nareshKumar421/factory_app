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
* An allocation names a batch through ``DRF16."ObjAbs"`` → ``OBTN."AbsEntry"``,
  and what that batch holds today is ``OIBT`` for the same item and
  ``SysNumber`` in the allocation's own warehouse. SAP refuses the add **per
  allocated batch** — ``10001153 - Insufficient quantity for item FG0000296
  with batch LS1103`` — so a line can be fine against ``OITW`` and still be
  refused. Both are read here.
* ``OINM`` is what says where the stock went instead. A draft that sat for two
  months is usually not merely short: its quantity left the warehouse whole on
  a later document (the July Beverages one was superseded by transfer
  726678123, same items, same quantities, sent to BH-WST rather than BH-GR).
  The last outgoing document per item and warehouse is read for exactly that,
  and only for lines that are already short.
"""

import logging
from decimal import Decimal
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

# OINM.TransType — the SAP object that moved the stock. Only the kinds that
# actually take finished or raw stock OUT of a warehouse are named; anything
# else is reported as a document, which is still better than a bare number.
_TRANS_TYPE_LABELS = {
    13: "invoice",
    14: "credit note",
    15: "delivery",
    16: "return",
    18: "A/P invoice",
    20: "goods receipt PO",
    21: "goods return",
    59: "goods receipt",
    60: "goods issue",
    67: "inventory transfer",
    162: "inventory revaluation",
    202: "production order",
    1250000001: "inventory transfer request",
}

_WDD_LABELS = {
    WDD_APPROVED: "approved",
    WDD_PENDING: "waiting for approval",
    WDD_REJECTED: "rejected",
    WDD_CANCELLED: "cancelled",
    WDD_NONE: "not routed for approval",
}


def _clean(value) -> str:
    return (value or "").strip()


def _decimal(value) -> Decimal:
    """HANA hands decimals back as Decimal or as str, depending on the driver."""
    if value is None:
        return Decimal(0)
    return value if isinstance(value, Decimal) else Decimal(str(value))


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
        """Draft lines for a whole page, with everything SAP would refuse on.

        ``source_stock`` joins ``OITW`` on the line's SOURCE warehouse: what an
        operator about to add this needs to know is whether the sending side
        still holds the quantity — the draft may be months old.

        The batch allocations come with it, because the item total in ``OITW``
        is not what SAP checks: it refuses per allocated batch. Three queries
        at most, each for the whole page rather than one per line.
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
                IFNULL(I."ManBtchNum", 'N')
            FROM "{{schema}}"."DRF1" L
            LEFT JOIN "{{schema}}"."OITM" I ON I."ItemCode" = L."ItemCode"
            LEFT JOIN "{{schema}}"."OITW" W
                ON W."ItemCode" = L."ItemCode" AND W."WhsCode" = L."FromWhsCod"
            WHERE L."DocEntry" IN ({placeholders})
            ORDER BY L."DocEntry", L."LineNum"
            """,
            tuple(entries),
        )

        allocations = self._allocations(entries)

        result: dict[int, list] = {}
        short_pairs = set()
        for row in rows:
            entry, line_num = int(row[0]), int(row[1])
            quantity = _decimal(row[5]) if row[5] is not None else None
            on_hand = row[9]
            batch_managed = _clean(row[10]) == "Y"
            batches = allocations.get((entry, line_num), [])
            allocated = sum((b["quantity"] for b in batches), Decimal(0))
            item_code = _clean(row[2])
            source = _clean(row[7])
            short = (
                on_hand is not None
                and quantity is not None
                and _decimal(on_hand) < quantity
            )
            if short and item_code and source:
                short_pairs.add((item_code, source))
            result.setdefault(entry, []).append({
                "line_num": line_num,
                "item_code": item_code,
                "item_name": _clean(row[3]) or _clean(row[4]),
                "quantity": str(row[5]) if row[5] is not None else "0",
                "uom": _clean(row[6]),
                "from_warehouse": source,
                "to_warehouse": _clean(row[8]),
                "source_stock": str(on_hand) if on_hand is not None else None,
                "short": short,
                # Said apart from `short`: an empty warehouse is a draft whose
                # stock has gone, which no retry fixes, while a partial
                # shortfall may just be waiting on today's production.
                "source_empty": on_hand is not None and _decimal(on_hand) == 0,
                "batch_managed": batch_managed,
                "batches_allocated": len(batches),
                "allocated_quantity": str(allocated),
                # The two allocation faults SAP refuses: none at all (-4014),
                # and fewer pieces allocated than the line moves.
                "batches_missing": batch_managed and not batches,
                "allocation_partial": bool(
                    batches and quantity is not None and allocated < quantity
                ),
                # The named refusal: 20 allocated of a batch that holds 0.
                "batches_short": [
                    {
                        "batch": b["batch"],
                        "allocated": str(b["quantity"]),
                        "in_stock": str(b["in_stock"]),
                    }
                    for b in batches
                    if b["in_stock"] < b["quantity"]
                ],
            })

        # Only for lines already short: on a healthy draft this answers a
        # question nobody asked, and OINM is the biggest table in the database.
        issues = self._last_issues(short_pairs)
        for lines in result.values():
            for line in lines:
                line["last_issue"] = issues.get(
                    (line["item_code"], line["from_warehouse"])
                )
        return result

    def _allocations(self, entries: list[int]) -> dict:
        """``(DocEntry, LineNum)`` -> the draft's batches and what each holds.

        ``DRF16."WhsCode"`` is the warehouse the allocation draws from, so the
        comparison is made there rather than against the line's source. They
        agree on every draft seen; if one ever disagrees, SAP checks the
        allocation's own warehouse, so that is the one to read.
        """
        if not entries:
            return {}
        placeholders = ", ".join(["?"] * len(entries))
        rows = self._query(
            f"""
            SELECT
                B."AbsEntry", B."LineNum", IFNULL(N."DistNumber", ''),
                B."Quantity",
                (SELECT IFNULL(SUM(T."Quantity"), 0)
                   FROM "{{schema}}"."OIBT" T
                  WHERE T."ItemCode" = B."ItemCode"
                    AND T."SysNumber" = N."SysNumber"
                    AND T."WhsCode" = B."WhsCode")
            FROM "{{schema}}"."DRF16" B
            LEFT JOIN "{{schema}}"."OBTN" N ON N."AbsEntry" = B."ObjAbs"
            WHERE B."AbsEntry" IN ({placeholders})
            ORDER BY B."AbsEntry", B."LineNum", N."DistNumber"
            """,
            tuple(entries),
        )
        out: dict = {}
        for row in rows:
            out.setdefault((int(row[0]), int(row[1])), []).append({
                "batch": _clean(row[2]),
                "quantity": _decimal(row[3]),
                "in_stock": _decimal(row[4]),
            })
        return out

    def _last_issues(self, pairs: set) -> dict:
        """``(item, warehouse)`` -> the last document that took stock out.

        Grouped by document before the latest is picked, because one transfer
        writes an ``OINM`` row per batch and its newest row is usually its
        smallest fragment. What the operator needs is "1,620 left on 25 Jul on
        inventory transfer 726678123", not 92 pieces of it.
        """
        if not pairs:
            return {}
        items = sorted({item for item, _ in pairs})
        warehouses = sorted({whs for _, whs in pairs})
        item_ph = ", ".join(["?"] * len(items))
        whs_ph = ", ".join(["?"] * len(warehouses))
        rows = self._query(
            f"""
            SELECT "ItemCode", "Warehouse", "BASE_REF", "TransType",
                   DOC_DATE, OUT_QTY
            FROM (
                SELECT D.*, ROW_NUMBER() OVER (
                    PARTITION BY D."ItemCode", D."Warehouse"
                    ORDER BY D.DOC_DATE DESC, D.LAST_TRANS DESC
                ) AS RN
                FROM (
                    SELECT M."ItemCode", M."Warehouse",
                           IFNULL(M."BASE_REF", '') AS "BASE_REF",
                           M."TransType",
                           TO_DATE(M."DocDate") AS DOC_DATE,
                           SUM(M."OutQty") AS OUT_QTY,
                           MAX(M."TransNum") AS LAST_TRANS
                    FROM "{{schema}}"."OINM" M
                    WHERE M."OutQty" > 0
                      AND M."ItemCode" IN ({item_ph})
                      AND M."Warehouse" IN ({whs_ph})
                    GROUP BY M."ItemCode", M."Warehouse",
                             IFNULL(M."BASE_REF", ''), M."TransType",
                             TO_DATE(M."DocDate")
                ) D
            )
            WHERE RN = 1
            """,
            tuple(items) + tuple(warehouses),
        )
        out = {}
        for row in rows:
            pair = (_clean(row[0]), _clean(row[1]))
            # The two IN lists are a cross product of the pairs asked for, so
            # rows pairing one item with another item's warehouse come back too.
            if pair not in pairs:
                continue
            trans_type = int(row[3]) if row[3] is not None else 0
            out[pair] = {
                "doc_num": _clean(row[2]) or None,
                "doc_type": _TRANS_TYPE_LABELS.get(trans_type, "document"),
                "doc_date": _date(row[4]),
                "quantity": str(_decimal(row[5])),
            }
        return out

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
