"""Read SAP back before writing: has this document already been made?

Every imprest document carries a **marker** in its ``Comments`` --
``[JI-CB PO 42]``, ``[JI-CB G77]``, ``[JI-CB API 9]``, ``[JI-CB APS 10]``, with
``-{round}`` after a cancel-and-re-raise (spec D7). A post that timed out may
have committed anyway, so before every write the handler asks SAP two
questions: is there a posted document with this marker, and is there an open
draft (an approval draft, or the A/P draft itself) with it? Only when both say
no does it write.

``NumAtCard`` cannot be that key: SAP users suffix it with ``.`` to dodge the
duplicate check, and amounts and dates collide between vouchers.

Rules every query here follows
------------------------------
* **Schema-qualified, always.** HANA has no current schema for this login, so
  every table is ``"{schema}"."<TABLE>"`` -- ``ODRF`` and ``OITM`` included --
  with ``schema = HanaConnection.schema``, the same rule as
  ``GRPOService.find_app_posted_grpo``.
* **Table names come only from** ``OBJECT_TABLES``. An unknown object code is
  a programming error and raises ``ValueError`` before any SQL is built.
* **Never fail open.** Not knowing whether a document is in SAP must never be
  read as "it is not". A connect failure or any ``dbapi.Error`` raises
  ``SAPUnavailable`` -- the handler waits and tries again -- and two documents
  with one marker raise ``SAPDataError`` for a person to sort out in SAP.

Line read-backs
---------------
After a PO is created, ``document_lines("22", e)`` plus ``map_lines_by_free_text``
give each voucher its PO ``LineNum`` (the PO line's ``FreeText`` is
``po_line_free_text(entry_id)`` = ``"JI V{entry_id}"``, stored by SAP in
``POR1.FreeTxt``). After a GRPO, ``document_lines("20", e)`` plus
``map_lines_by_base_line`` give each voucher its GRPO ``LineNum`` through the
GRPO line's ``BaseLine`` (= the voucher's PO line). The A/P item draft then
bases its lines on those.
"""

import logging
from typing import Dict, Iterable, List, Mapping, Optional

from hdbcli import dbapi

from sap_client.context import CompanyContext
from sap_client.exceptions import SAPDataError, SAPUnavailable
from sap_client.hana.connection import HanaConnection

logger = logging.getLogger(__name__)

#: ObjType -> (header table, line table). The only source of table names.
OBJECT_TABLES = {
    "22": ("OPOR", "POR1"),
    "20": ("OPDN", "PDN1"),
    "18": ("OPCH", "PCH1"),
}

#: What a PO line's FreeText says, so the line can be found again by voucher.
PO_LINE_FREE_TEXT = "JI V{entry_id}"

#: ODRF.WddStatus -> what the imprest handlers do with it (spec D10).
DRAFT_IN_APPROVAL = "IN_APPROVAL"   # 'W': waiting for an approver
DRAFT_APPROVED = "APPROVED"         # 'Y': approved, add it (SaveDraftToDocument)
DRAFT_REJECTED = "REJECTED"         # 'N': rejected in SAP approval
DRAFT_OTHER = "OTHER"               # '-', 'P', 'A', 'C', ...: held as a draft, no approval to wait for


def po_line_free_text(entry_id) -> str:
    """The ``FreeText`` an imprest PO line carries for cash entry ``entry_id``."""
    return PO_LINE_FREE_TEXT.format(entry_id=entry_id)


def classify_draft(wdd_status) -> str:
    """Map an ODRF ``WddStatus`` to one of the ``DRAFT_*`` words.

    ``'-'`` (no approval procedure) is ``DRAFT_OTHER``: SAP holds a plain
    draft that someone must add or delete in SAP. ``approval_status.py``
    explains why ODRF's own WddStatus, not OWDD's, is the one to trust.
    """
    value = (wdd_status or "").strip().upper()
    if value == "W":
        return DRAFT_IN_APPROVAL
    if value == "Y":
        return DRAFT_APPROVED
    if value == "N":
        return DRAFT_REJECTED
    return DRAFT_OTHER


def _tables(object_code) -> tuple:
    try:
        return OBJECT_TABLES[str(object_code)]
    except KeyError:
        raise ValueError(
            f"Unknown SAP purchasing object {object_code!r}; expected one of "
            f"{', '.join(sorted(OBJECT_TABLES))}."
        ) from None


def _like(marker: str) -> str:
    marker = (marker or "").strip()
    if not marker:
        raise ValueError("A read-back marker cannot be blank.")
    return f"%{marker}%"


def _int(value) -> Optional[int]:
    return int(value) if value is not None else None


def _float(value) -> Optional[float]:
    return float(value) if value is not None else None


def _iso(value):
    if value is None:
        return None
    if hasattr(value, "date") and callable(value.date):
        value = value.date()
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


class PurchasingReadback:
    """HANA read-back for OPOR / OPDN / OPCH and ODRF in one company.

    ``PurchasingReadback("JIVO_OIL")``. Every method opens its own
    connection and closes it, like the sap_client readers.
    """

    def __init__(self, company_code: str):
        self.company_code = company_code
        self.connection = HanaConnection(CompanyContext(company_code).hana)

    @property
    def schema(self) -> str:
        return self.connection.schema

    # -- plumbing ------------------------------------------------------------

    def _run(self, statements):
        """Run ``[(sql, params), ...]`` on one connection; list of fetchall()s.

        Any failure to connect or to read is ``SAPUnavailable``: the caller
        must not take "could not read" for "not there".
        """
        conn = cursor = None
        try:
            try:
                conn = self.connection.connect()
            except dbapi.Error as e:
                logger.error("SAP HANA unreachable for purchasing read-back: %s", e)
                raise SAPUnavailable(
                    f"SAP HANA could not be read to check what is already in SAP: {e}"
                ) from e
            cursor = conn.cursor()
            results = []
            for sql, params in statements:
                cursor.execute(sql, params)
                results.append(cursor.fetchall())
            return results
        except dbapi.Error as e:
            logger.error("SAP HANA purchasing read-back failed: %s", e)
            raise SAPUnavailable(
                f"SAP HANA could not be read to check what is already in SAP: {e}"
            ) from e
        finally:
            for handle in (cursor, conn):
                if handle is not None:
                    try:
                        handle.close()
                    except Exception:
                        pass

    def _one(self, sql, params):
        return self._run([(sql, params)])[0]

    # -- posted documents ----------------------------------------------------

    def find_by_marker(self, object_code, card_code, marker) -> Optional[dict]:
        """The one non-cancelled posted document carrying ``marker``, or None.

        Returns ``{"doc_entry", "doc_num", "doc_total", "doc_date", "draft_key"}``
        (``doc_date`` ISO string; ``draft_key`` the ODRF entry it was added
        from, or None). Two documents -> ``SAPDataError`` naming both DocNums.
        """
        header, _ = _tables(object_code)
        rows = self._one(
            f'''SELECT TOP 2 "DocEntry", "DocNum", "DocTotal", "DocDate", "draftKey"
                FROM "{self.schema}"."{header}"
                WHERE "CardCode" = ? AND "CANCELED" = 'N' AND "Comments" LIKE ?
                ORDER BY "DocEntry"''',
            [card_code, _like(marker)],
        )
        if not rows:
            return None
        if len(rows) > 1:
            nums = ", ".join(str(r[1]) for r in rows)
            raise SAPDataError(
                f"Two SAP documents carry {marker}: DocNum {nums} -- cancel one in SAP."
            )
        doc_entry, doc_num, doc_total, doc_date, draft_key = rows[0]
        return {
            "doc_entry": int(doc_entry),
            "doc_num": _int(doc_num),
            "doc_total": _float(doc_total),
            "doc_date": _iso(doc_date),
            "draft_key": _int(draft_key),
        }

    def find_by_draft_key(self, object_code, draft_entry) -> Optional[dict]:
        """The non-cancelled document added from ODRF draft ``draft_entry``, or None.

        The other way to find a document after ``SaveDraftToDocument`` (which
        answers with no body). Same return shape as ``find_by_marker``.
        """
        header, _ = _tables(object_code)
        rows = self._one(
            f'''SELECT TOP 2 "DocEntry", "DocNum", "DocTotal", "DocDate", "draftKey"
                FROM "{self.schema}"."{header}"
                WHERE "draftKey" = ? AND "CANCELED" = 'N'
                ORDER BY "DocEntry"''',
            [int(draft_entry)],
        )
        if not rows:
            return None
        doc_entry, doc_num, doc_total, doc_date, draft_key = rows[0]
        return {
            "doc_entry": int(doc_entry),
            "doc_num": _int(doc_num),
            "doc_total": _float(doc_total),
            "doc_date": _iso(doc_date),
            "draft_key": _int(draft_key),
        }

    # -- drafts --------------------------------------------------------------

    def find_draft_by_marker(self, object_code, card_code, marker) -> Optional[dict]:
        """The one OPEN ODRF draft of this object carrying ``marker``, or None.

        Returns ``{"draft_entry", "wdd_status", "doc_total", "outcome"}`` where
        ``wdd_status`` is ODRF's own (``'-'`` when null) and ``outcome`` is
        ``classify_draft(wdd_status)``. Closed drafts (``DocStatus='C'``: added
        or deleted) are ignored. Two open drafts -> ``SAPDataError``.
        """
        _tables(object_code)
        rows = self._one(
            f'''SELECT TOP 2 "DocEntry", "DocStatus", IFNULL("WddStatus", '-'), "DocTotal"
                FROM "{self.schema}"."ODRF"
                WHERE "ObjType" = ? AND "CardCode" = ? AND "DocStatus" = 'O'
                  AND "Comments" LIKE ?
                ORDER BY "DocEntry"''',
            [str(object_code), card_code, _like(marker)],
        )
        if not rows:
            return None
        if len(rows) > 1:
            entries = ", ".join(str(r[0]) for r in rows)
            raise SAPDataError(
                f"Two open SAP drafts carry {marker}: draft {entries} -- delete one in SAP."
            )
        draft_entry, _status, wdd_status, doc_total = rows[0]
        wdd_status = wdd_status or "-"
        return {
            "draft_entry": int(draft_entry),
            "wdd_status": wdd_status,
            "doc_total": _float(doc_total),
            "outcome": classify_draft(wdd_status),
        }

    # -- one document --------------------------------------------------------

    def document_lines(self, object_code, doc_entry) -> List[dict]:
        """Every line of a posted document, by ``LineNum``.

        Each: ``{"line_num", "item_code", "quantity", "open_qty", "price",
        "line_total", "free_text", "base_entry", "base_line", "line_status"}``.
        A missing document gives ``[]``.
        """
        _, lines = _tables(object_code)
        rows = self._one(
            f'''SELECT "LineNum", "ItemCode", "Quantity", "OpenQty", "Price", "LineTotal",
                       "FreeTxt", "BaseEntry", "BaseLine", "LineStatus"
                FROM "{self.schema}"."{lines}"
                WHERE "DocEntry" = ?
                ORDER BY "LineNum"''',
            [int(doc_entry)],
        )
        return [
            {
                "line_num": int(r[0]),
                "item_code": r[1] or "",
                "quantity": _float(r[2]),
                "open_qty": _float(r[3]),
                "price": _float(r[4]),
                "line_total": _float(r[5]),
                "free_text": r[6] or "",
                "base_entry": _int(r[7]),
                "base_line": _int(r[8]),
                "line_status": r[9] or "",
            }
            for r in rows
        ]

    def document_state(self, object_code, doc_entry) -> Optional[dict]:
        """``{"doc_num", "canceled", "doc_status"}`` of one document, or None if missing.

        ``canceled``: ``'Y'`` cancelled, ``'C'`` the cancellation document
        itself, ``'N'`` live. ``doc_status``: ``'O'`` open, ``'C'`` closed.
        """
        header, _ = _tables(object_code)
        rows = self._one(
            f'''SELECT "DocNum", IFNULL("CANCELED", 'N'), IFNULL("DocStatus", 'O')
                FROM "{self.schema}"."{header}"
                WHERE "DocEntry" = ?''',
            [int(doc_entry)],
        )
        if not rows:
            return None
        doc_num, canceled, doc_status = rows[0]
        return {"doc_num": _int(doc_num), "canceled": canceled, "doc_status": doc_status}

    def num_at_card_taken(self, object_code, card_code, num_at_card) -> bool:
        """Is this vendor reference already used by a live document or an open draft?

        True if the header table has ``CANCELED='N'`` with this CardCode and
        NumAtCard, **or** ODRF has an open draft of this object with them.
        Exact comparison: the ``.`` suffixes are what keep them apart.
        """
        header, _ = _tables(object_code)
        posted, drafts = self._run([
            (
                f'''SELECT TOP 1 "DocEntry" FROM "{self.schema}"."{header}"
                    WHERE "CANCELED" = 'N' AND "CardCode" = ? AND "NumAtCard" = ?''',
                [card_code, num_at_card],
            ),
            (
                f'''SELECT TOP 1 "DocEntry" FROM "{self.schema}"."ODRF"
                    WHERE "ObjType" = ? AND "DocStatus" = 'O'
                      AND "CardCode" = ? AND "NumAtCard" = ?''',
                [str(object_code), card_code, num_at_card],
            ),
        ])
        return bool(posted) or bool(drafts)

    # -- item master ---------------------------------------------------------

    def item_flags(self, item_codes: Iterable[str]) -> Dict[str, dict]:
        """OITM flags for the given codes; codes SAP does not know are absent.

        ``{code: {"item_name", "group", "inventory", "purchase", "valid",
        "frozen"}}`` with the four flags as booleans (``'Y'`` -> True).
        """
        codes = sorted({(c or "").strip() for c in item_codes if (c or "").strip()})
        if not codes:
            return {}
        placeholders = ", ".join("?" for _ in codes)
        rows = self._one(
            f'''SELECT "ItemCode", "ItemName", "ItmsGrpCod", "InvntItem", "PrchseItem",
                       "validFor", "frozenFor"
                FROM "{self.schema}"."OITM"
                WHERE "ItemCode" IN ({placeholders})''',
            codes,
        )
        return {
            r[0]: {
                "item_name": r[1] or "",
                "group": _int(r[2]),
                "inventory": r[3] == "Y",
                "purchase": r[4] == "Y",
                "valid": r[5] == "Y",
                "frozen": r[6] == "Y",
            }
            for r in rows
        }


# ---------------------------------------------------------------------------
# Pure line mapping (no SAP)
# ---------------------------------------------------------------------------

def map_lines_by_free_text(lines: Iterable[dict], free_texts: Mapping) -> Dict:
    """``{key: LineNum}`` for each ``key -> FreeText`` found on exactly one line.

    ``lines`` is ``document_lines("22", e)``; ``free_texts`` is usually
    ``{entry_id: po_line_free_text(entry_id)}``. A key whose text is on no
    line, or on two, is left out: the caller reports "PO line not found"
    rather than guess.
    """
    by_text: Dict[str, List[int]] = {}
    for line in lines:
        text = (line.get("free_text") or "").strip()
        if text:
            by_text.setdefault(text, []).append(int(line["line_num"]))
    result = {}
    for key, text in free_texts.items():
        found = by_text.get((text or "").strip(), [])
        if len(found) == 1:
            result[key] = found[0]
    return result


def map_lines_by_base_line(lines: Iterable[dict], po_lines: Mapping,
                           base_entry: Optional[int] = None) -> Dict:
    """``{key: GRPO LineNum}`` via each GRPO line's ``BaseLine``.

    ``lines`` is ``document_lines("20", e)``; ``po_lines`` is
    ``{key: po_line_num}`` (e.g. voucher id -> its PO line). When
    ``base_entry`` is given only lines based on that PO count. A key whose PO
    line is on no GRPO line, or on two, is left out.
    """
    by_base: Dict[int, List[int]] = {}
    for line in lines:
        if line.get("base_line") is None:
            continue
        if base_entry is not None and line.get("base_entry") != int(base_entry):
            continue
        by_base.setdefault(int(line["base_line"]), []).append(int(line["line_num"]))
    result = {}
    for key, po_line in po_lines.items():
        if po_line is None:
            continue
        found = by_base.get(int(po_line), [])
        if len(found) == 1:
            result[key] = found[0]
    return result
