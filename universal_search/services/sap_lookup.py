"""
universal_search/services/sap_lookup.py

Every HANA read the universal search makes, against ONE company database.

Three lookups live here:

* **documents** -- one statement per company that asks all fifteen document
  tables at once whether they hold this ``DocNum``. Fifteen index seeks in a
  single round trip beats fifteen round trips, and a search has to feel like
  a search;
* **document detail** -- the header again plus its lines, read when the user
  opens a hit rather than for every hit on the way past;
* **items and batches** -- an item code resolves to its master row and its
  stock by warehouse, a batch number to the stock standing under it.

Nothing here writes, and nothing here is company-aware beyond the schema it
was handed: the caller decides which companies to ask.
"""

import logging
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from hdbcli import dbapi

from sap_client.exceptions import SAPConnectionError, SAPDataError
from sap_client.hana.connection import HanaConnection

from ..documents import (
    DOC_TYPES,
    DOC_TYPES_BY_KIND,
    SHAPE_PRODUCTION,
    SapDocType,
    status_label,
)

logger = logging.getLogger(__name__)

#: A person is waiting on this with a modal open, so it gives up quickly rather
#: than holding the screen. Well under the report timeout, which serves a
#: different kind of patience.
SEARCH_COMMUNICATION_TIMEOUT_MS = 30000

#: Lines shown for one document. The modal is a summary, not the document.
MAX_LINES = 200

#: Items or batches offered for one term.
MAX_MASTER_HITS = 25

#: SAP keeps DocNum in a 32-bit integer column. A longer run of digits is a
#: barcode or a phone number, not a document, and asking costs a round trip.
MAX_DOC_NUM = 2147483647


def is_doc_num(term: str) -> bool:
    """Whether a term could be a ``DocNum`` at all."""
    return term.isdigit() and 0 < int(term) <= MAX_DOC_NUM


# The fourteen standard types all answer this shape.
_HEADER_MARKETING = """
    SELECT '{kind}' AS "Kind", T."DocEntry", T."DocNum", T."DocDate",
           T."CardCode", T."CardName", T."DocTotal", T."DocCur",
           T."DocStatus", T."CANCELED", T."NumAtCard"
    FROM "{schema}"."{table}" T
    WHERE T.{key} = ?
"""

# OWOR has no customer, no total and no cancel flag; the casts keep its branch
# union-compatible with the others rather than letting HANA guess a type.
_HEADER_PRODUCTION = """
    SELECT '{kind}' AS "Kind", T."DocEntry", T."DocNum", T."PostDate",
           CAST(T."ItemCode" AS NVARCHAR(254)), CAST(NULL AS NVARCHAR(254)),
           CAST(NULL AS DECIMAL(19,6)), CAST(NULL AS NVARCHAR(20)),
           T."Status", CAST('N' AS NVARCHAR(1)), CAST(NULL AS NVARCHAR(254))
    FROM "{schema}"."{table}" T
    WHERE T.{key} = ?
"""

_KEY_DOC_NUM = '"DocNum"'
_KEY_DOC_ENTRY = '"DocEntry"'


class UniversalSapReader:
    """Reads one company database for the universal search."""

    def __init__(self, context):
        self.connection = HanaConnection(context.hana)
        self.schema = self.connection.schema
        self._session = None

    @contextmanager
    def session(self):
        """Hold one connection open across several reads.

        A search asks each company three questions -- documents, items,
        batches -- and opening a connection costs about half as much as the
        query it carries. Under this, the three share one.
        """
        conn = self._connect()
        self._session = conn
        try:
            yield self
        finally:
            self._session = None
            try:
                conn.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Documents
    # ------------------------------------------------------------------

    def search_documents(self, doc_num: int) -> list[dict[str, Any]]:
        """Every document in this company whose ``DocNum`` is the number given."""
        branches = [self._header_sql(doc, _KEY_DOC_NUM) for doc in DOC_TYPES]
        sql = "\nUNION ALL\n".join(branches)
        rows = self._read(sql, [doc_num] * len(branches))
        return [self._header_row(row) for row in rows]

    def document_detail(self, kind: str, doc_entry: int) -> dict[str, Any] | None:
        """One document's header and its lines, or ``None`` if it is not there."""
        doc = DOC_TYPES_BY_KIND.get(kind)
        if doc is None:
            return None

        # The search matched on DocNum; the detail is fetched by DocEntry, which
        # is the actual key -- DocNum repeats across numbering series.
        rows = self._read(self._header_sql(doc, _KEY_DOC_ENTRY), [doc_entry])
        if not rows:
            return None

        header = self._header_row(rows[0])
        header["lines"] = self._lines(doc, doc_entry)
        return header

    def _header_sql(self, doc: SapDocType, key: str) -> str:
        template = (
            _HEADER_PRODUCTION if doc.shape == SHAPE_PRODUCTION else _HEADER_MARKETING
        )
        return template.format(
            kind=doc.kind, schema=self.schema, table=doc.header, key=key
        )

    def _lines(self, doc: SapDocType, doc_entry: int) -> list[dict[str, Any]]:
        if doc.shape == SHAPE_PRODUCTION:
            sql = f"""
                SELECT L."LineNum", L."ItemCode", L."ItemName", L."PlannedQty",
                       L."wareHouse", L."IssuedQty"
                FROM "{self.schema}"."{doc.lines}" L
                WHERE L."DocEntry" = ?
                ORDER BY L."LineNum"
                LIMIT {MAX_LINES}
            """
            return [
                {
                    "line_num": self._int(row[0]),
                    "item_code": self._text(row[1]),
                    "description": self._text(row[2]),
                    "quantity": self._number(row[3]),
                    "warehouse": self._text(row[4]),
                    "unit": "",
                    "price": None,
                    "line_total": None,
                    # A component line's "open" amount is what is still to be
                    # issued against the order.
                    "open_quantity": self._number(row[5]),
                }
                for row in self._read(sql, [doc_entry])
            ]

        sql = f"""
            SELECT L."LineNum", L."ItemCode", L."Dscription", L."Quantity",
                   L."unitMsr", L."WhsCode", L."Price", L."LineTotal", L."OpenQty"
            FROM "{self.schema}"."{doc.lines}" L
            WHERE L."DocEntry" = ?
            ORDER BY L."LineNum"
            LIMIT {MAX_LINES}
        """
        return [
            {
                "line_num": self._int(row[0]),
                "item_code": self._text(row[1]),
                "description": self._text(row[2]),
                "quantity": self._number(row[3]),
                "unit": self._text(row[4]),
                "warehouse": self._text(row[5]),
                "price": self._number(row[6]),
                "line_total": self._number(row[7]),
                "open_quantity": self._number(row[8]),
            }
            for row in self._read(sql, [doc_entry])
        ]

    def _header_row(self, row) -> dict[str, Any]:
        doc = DOC_TYPES_BY_KIND[self._text(row[0])]
        return {
            "kind": doc.kind,
            "label": doc.label,
            "obj_type": doc.obj_type,
            "doc_entry": self._int(row[1]),
            "doc_num": self._int(row[2]),
            "doc_date": self._date(row[3]),
            "card_code": self._text(row[4]),
            "card_name": self._text(row[5]),
            "partner_label": doc.partner_label,
            "doc_total": self._number(row[6]),
            "currency": self._text(row[7]),
            "status": status_label(doc, self._text(row[8])),
            "is_cancelled": self._text(row[9]) == "Y",
            "ref_no": self._text(row[10]),
        }

    # ------------------------------------------------------------------
    # Item master and batches
    # ------------------------------------------------------------------

    def search_items(self, term: str) -> list[dict[str, Any]]:
        """Items whose code is the term, or begins with it.

        Exact first: someone who types a full code wants that item at the top,
        not whichever of its longer siblings sorts first.
        """
        sql = f"""
            SELECT I."ItemCode", I."ItemName", I."ItmsGrpCod", G."ItmsGrpNam",
                   I."InvntryUom", I."OnHand", I."IsCommited", I."U_TYPE",
                   I."U_Sub_Group", I."SalFactor2"
            FROM "{self.schema}"."OITM" I
            LEFT JOIN "{self.schema}"."OITB" G ON G."ItmsGrpCod" = I."ItmsGrpCod"
            WHERE UPPER(I."ItemCode") = ? OR UPPER(I."ItemCode") LIKE ?
            ORDER BY CASE WHEN UPPER(I."ItemCode") = ? THEN 0 ELSE 1 END, I."ItemCode"
            LIMIT {MAX_MASTER_HITS}
        """
        upper = term.upper()
        return [
            {
                "item_code": self._text(row[0]),
                "item_name": self._text(row[1]),
                "item_group": self._text(row[3]),
                "uom": self._text(row[4]),
                "on_hand": self._number(row[5]),
                "committed": self._number(row[6]),
                "type": self._text(row[7]),
                "variety": self._text(row[8]),
                # Pieces per box, straight from the master -- never parsed out
                # of the item name.
                "pieces_per_box": self._number(row[9]),
            }
            for row in self._read(sql, [upper, f"{upper}%", upper])
        ]

    def item_stock(self, item_code: str) -> list[dict[str, Any]]:
        """Where an item's stock is standing, warehouse by warehouse."""
        sql = f"""
            SELECT W."WhsCode", W."OnHand", W."IsCommited", W."OnOrder"
            FROM "{self.schema}"."OITW" W
            WHERE W."ItemCode" = ? AND (W."OnHand" <> 0 OR W."IsCommited" <> 0)
            ORDER BY W."WhsCode"
        """
        return [
            {
                "warehouse": self._text(row[0]),
                "on_hand": self._number(row[1]),
                "committed": self._number(row[2]),
                "on_order": self._number(row[3]),
            }
            for row in self._read(sql, [item_code])
        ]

    def search_batches(self, term: str) -> list[dict[str, Any]]:
        """Batches whose number is the term, with the stock still under them.

        ``OIBT`` holds a row per batch per warehouse; a batch that has moved
        around has several, so they are summed. Emptied warehouses are dropped
        -- a zero row is history, not stock.
        """
        sql = f"""
            SELECT B."ItemCode", B."BatchNum", B."WhsCode", SUM(B."Quantity"),
                   MAX(B."ItemName"), MIN(B."ExpDate"), MIN(B."InDate")
            FROM "{self.schema}"."OIBT" B
            WHERE UPPER(B."BatchNum") = ?
            GROUP BY B."ItemCode", B."BatchNum", B."WhsCode"
            HAVING SUM(B."Quantity") <> 0
            ORDER BY B."ItemCode", B."WhsCode"
            LIMIT {MAX_MASTER_HITS}
        """
        return [
            {
                "item_code": self._text(row[0]),
                "batch_num": self._text(row[1]),
                "warehouse": self._text(row[2]),
                "quantity": self._number(row[3]),
                "item_name": self._text(row[4]),
                "expiry_date": self._date(row[5]),
                "received_date": self._date(row[6]),
            }
            for row in self._read(sql, [term.upper()])
        ]

    # ------------------------------------------------------------------
    # Plumbing
    # ------------------------------------------------------------------

    def _read(self, sql: str, params: list) -> list:
        # Reaching SAP and being refused by it are different failures with
        # different answers -- "try again in a moment" against "that query is
        # wrong" -- and hdbcli reports both as dbapi.Error, so the two phases
        # are separated here rather than by reading the message.
        shared = self._session
        conn = shared or self._connect()
        try:
            cursor = conn.cursor()
            try:
                cursor.execute(sql, params)
                return cursor.fetchall()
            finally:
                cursor.close()
        except dbapi.Error as exc:
            logger.warning("Universal search read failed on %s: %s", self.schema, exc)
            raise SAPDataError(f"SAP refused the search: {exc}") from exc
        finally:
            # A session's connection belongs to whoever opened it.
            if shared is None:
                try:
                    conn.close()
                except Exception:
                    pass

    def _connect(self):
        try:
            conn = self.connection.connect()
            conn.setclientinfo(
                "COMMUNICATIONTIMEOUT", str(SEARCH_COMMUNICATION_TIMEOUT_MS)
            )
            return conn
        except Exception as exc:
            logger.warning("Universal search could not reach %s: %s", self.schema, exc)
            raise SAPConnectionError(str(exc)) from exc

    @staticmethod
    def _text(value) -> str:
        return "" if value is None else str(value).strip()

    @staticmethod
    def _int(value) -> int | None:
        return None if value is None else int(value)

    @staticmethod
    def _number(value) -> float | None:
        """A quantity or amount as JSON can carry it.

        ``Decimal`` is not JSON-serialisable and the frontend formats these
        itself, so they go over as floats.
        """
        if value is None:
            return None
        if isinstance(value, Decimal):
            return float(value)
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _date(value) -> str | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value.date().isoformat()
        if isinstance(value, date):
            return value.isoformat()
        return str(value)[:10]
