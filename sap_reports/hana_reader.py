"""
sap_reports/hana_reader.py

Every HANA round-trip this module makes.

Three kinds of read live here:

* the **catalogue** -- SAP's own query categories (``OQCN``) and the saved
  queries filed under them (``OUQR``);
* a **report run** -- the saved query itself, executed with bound values;
* **lookups** -- the small master-data lists that fill a report's dropdowns
  (warehouses, item groups, fiscal periods) and the item / business-partner
  searches behind its type-ahead fields.

A saved query is written to run inside SAP, where the company database is the
current schema, so its tables are unqualified (``FROM OINM T0``). Each report
connection therefore issues ``SET SCHEMA`` first; without it every report would
fail on an unresolvable table name.
"""

import logging
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence, Tuple

from django.utils import timezone
from hdbcli import dbapi

from sap_client.exceptions import SAPConnectionError, SAPDataError
from sap_client.hana.connection import HanaConnection

logger = logging.getLogger(__name__)

# A report is a human waiting at a screen, but some of these queries scan years
# of OINM. Allow longer than a normal API read, and shorter than forever.
REPORT_COMMUNICATION_TIMEOUT_MS = 300000

LOOKUP_LIMIT = 100


class HanaSapReportReader:
    """Reads SAP's saved-query catalogue and runs the queries it holds."""

    def __init__(self, context):
        self.connection = HanaConnection(context.hana)
        self.schema = self.connection.schema

    # ------------------------------------------------------------------
    # Catalogue
    # ------------------------------------------------------------------

    def list_categories(self) -> List[Dict[str, Any]]:
        """Every SAP query category in this company database, with its query count."""
        query = f"""
            SELECT
                C."CategoryId",
                C."CatName",
                COUNT(Q."IntrnalKey") AS "QueryCount"
            FROM "{self.schema}"."OQCN" C
            LEFT JOIN "{self.schema}"."OUQR" Q ON Q."QCategory" = C."CategoryId"
            GROUP BY C."CategoryId", C."CatName"
            ORDER BY C."CatName"
        """
        return [
            {
                "category_id": int(row[0]),
                "category_name": self._text(row[1]),
                "query_count": int(row[2] or 0),
            }
            for row in self._read(query, [])
        ]

    def list_saved_queries(self, category_name: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        The saved queries in one category, or in all of them when none is named.

        Matching is on the category *name* rather than its id: the ids are
        per-database, so "Factory" is 22 in one company and something else in
        the next.
        """
        query = f"""
            SELECT
                Q."IntrnalKey",
                Q."QName",
                Q."QCategory",
                C."CatName",
                Q."QString",
                Q."UpdateDate"
            FROM "{self.schema}"."OUQR" Q
            LEFT JOIN "{self.schema}"."OQCN" C ON C."CategoryId" = Q."QCategory"
        """
        params: List[Any] = []
        if category_name:
            query += ' WHERE UPPER(C."CatName") = UPPER(?)'
            params.append(category_name)
        query += ' ORDER BY Q."QName"'

        return [
            {
                "sap_internal_key": int(row[0]),
                "sap_name": self._text(row[1]),
                "sap_category_id": int(row[2]) if row[2] is not None else 0,
                "sap_category_name": self._text(row[3]),
                "sql_text": self._text(row[4]),
                "sap_changed_at": self._aware(row[5]),
            }
            for row in self._read(query, params)
        ]

    # ------------------------------------------------------------------
    # Running a report
    # ------------------------------------------------------------------

    def run_statement(
        self,
        statement: str,
        params: Sequence[Any],
        *,
        row_limit: int,
    ) -> Tuple[List[Dict[str, Any]], List[List[Any]], bool]:
        """
        Executes one already-bound report statement.

        Returns ``(columns, rows, was_truncated)``. One row beyond the ceiling is
        fetched so the caller can honestly say the result was cut short rather
        than quietly showing a partial answer as if it were complete.
        """
        conn = self._connect(communication_timeout_ms=REPORT_COMMUNICATION_TIMEOUT_MS)
        cursor = None
        try:
            cursor = conn.cursor()
            # Saved queries assume the company database is the current schema.
            cursor.execute(f'SET SCHEMA "{self.schema}"')

            if params:
                cursor.execute(statement, list(params))
            else:
                cursor.execute(statement)

            columns = self._describe(cursor)
            fetched = cursor.fetchmany(row_limit + 1)
            was_truncated = len(fetched) > row_limit
            rows = [
                [self._json_safe(value) for value in row]
                for row in fetched[:row_limit]
            ]
            return columns, rows, was_truncated
        except dbapi.ProgrammingError as error:
            logger.warning("SAP report SQL rejected by HANA: %s", error)
            raise SAPDataError(self._readable_error(error)) from error
        except dbapi.Error as error:
            logger.error("SAP report execution failed: %s", error)
            raise SAPDataError(self._readable_error(error)) from error
        finally:
            self._close(cursor, conn)

    # ------------------------------------------------------------------
    # Lookups for parameter dropdowns
    # ------------------------------------------------------------------

    def list_warehouses(self, search: str = "") -> List[Dict[str, str]]:
        query = f"""
            SELECT "WhsCode", "WhsName"
            FROM "{self.schema}"."OWHS"
            WHERE "Inactive" = 'N'
        """
        params: List[Any] = []
        if search:
            query += ' AND (UPPER("WhsCode") LIKE ? OR UPPER("WhsName") LIKE ?)'
            params += [self._contains(search)] * 2
        query += ' ORDER BY "WhsCode"'
        return self._as_options(self._read(query, params, limit=LOOKUP_LIMIT))

    def list_item_groups(self, search: str = "") -> List[Dict[str, str]]:
        # Value and label are the same column here, so both need an alias --
        # HANA rejects a result set with two identically named columns.
        query = f"""
            SELECT "ItmsGrpNam" AS "OptionValue", "ItmsGrpNam" AS "OptionLabel"
            FROM "{self.schema}"."OITB"
        """
        params: List[Any] = []
        if search:
            query += ' WHERE UPPER("ItmsGrpNam") LIKE ?'
            params.append(self._contains(search))
        query += ' ORDER BY "ItmsGrpNam"'
        return self._as_options(self._read(query, params, limit=LOOKUP_LIMIT))

    def list_periods(self, search: str = "") -> List[Dict[str, str]]:
        """Fiscal periods from ``OFCT`` — what the order-recommendation calls want."""
        query = f"""
            SELECT "Name" AS "OptionValue", "Name" AS "OptionLabel"
            FROM "{self.schema}"."OFCT"
        """
        params: List[Any] = []
        if search:
            query += ' WHERE UPPER("Name") LIKE ?'
            params.append(self._contains(search))
        query += ' ORDER BY "Name" DESC'
        return self._as_options(self._read(query, params, limit=LOOKUP_LIMIT))

    def search_items(self, search: str = "") -> List[Dict[str, str]]:
        query = f"""
            SELECT "ItemCode", "ItemName"
            FROM "{self.schema}"."OITM"
        """
        params: List[Any] = []
        if search:
            query += ' WHERE UPPER("ItemCode") LIKE ? OR UPPER("ItemName") LIKE ?'
            params += [self._contains(search)] * 2
        query += ' ORDER BY "ItemCode"'
        return self._as_options(self._read(query, params, limit=LOOKUP_LIMIT))

    def search_business_partners(self, search: str = "") -> List[Dict[str, str]]:
        query = f"""
            SELECT "CardCode", "CardName"
            FROM "{self.schema}"."OCRD"
        """
        params: List[Any] = []
        if search:
            query += ' WHERE UPPER("CardCode") LIKE ? OR UPPER("CardName") LIKE ?'
            params += [self._contains(search)] * 2
        query += ' ORDER BY "CardName"'
        return self._as_options(self._read(query, params, limit=LOOKUP_LIMIT))

    # ------------------------------------------------------------------
    # Plumbing
    # ------------------------------------------------------------------

    def _connect(self, communication_timeout_ms: Optional[int] = None):
        try:
            if communication_timeout_ms is None:
                return self.connection.connect()
            return dbapi.connect(
                address=self.connection.hana["host"],
                port=self.connection.hana["port"],
                user=self.connection.hana["user"],
                password=self.connection.hana["password"],
                connectTimeout=HanaConnection.CONNECT_TIMEOUT_MS,
                communicationTimeout=communication_timeout_ms,
            )
        except dbapi.Error as error:
            logger.error("SAP HANA connection failed for SAP reports: %s", error)
            raise SAPConnectionError(
                "Unable to connect to SAP HANA. Please try again later."
            ) from error

    def _read(
        self,
        query: str,
        params: Sequence[Any],
        *,
        limit: Optional[int] = None,
    ) -> List[Sequence[Any]]:
        """Runs one of *our* queries — catalogue or lookup — never a saved query."""
        conn = self._connect()
        cursor = None
        try:
            cursor = conn.cursor()
            if params:
                cursor.execute(query, list(params))
            else:
                cursor.execute(query)
            return cursor.fetchmany(limit) if limit else cursor.fetchall()
        except dbapi.Error as error:
            logger.error("SAP reports read failed: %s", error)
            raise SAPDataError(
                self._readable_error(error, subject="this request")
            ) from error
        finally:
            self._close(cursor, conn)

    @staticmethod
    def _close(cursor, conn) -> None:
        for handle in (cursor, conn):
            if handle is None:
                continue
            try:
                handle.close()
            except Exception:  # pragma: no cover - cleanup only
                pass

    @staticmethod
    def _describe(cursor) -> List[Dict[str, Any]]:
        """
        Column metadata for a result set, with keys guaranteed unique.

        SAP reports happily select the same alias twice ("DOLLY MAM BST" returns
        two "INVOICE Taxable Value" columns), which would collide the moment the
        rows are keyed by name on a screen, so a suffix is added and the original
        heading is kept as the label.
        """
        description = cursor.description or []
        columns: List[Dict[str, Any]] = []
        used: Dict[str, int] = {}

        for index, column in enumerate(description):
            label = str(column[0]) if column[0] is not None else f"Column {index + 1}"
            key = label.strip() or f"column_{index + 1}"
            used[key] = used.get(key, 0) + 1
            if used[key] > 1:
                key = f"{key} ({used[key]})"
            columns.append({"key": key, "label": label, "type": _column_type(column)})

        return columns

    @staticmethod
    def _json_safe(value: Any) -> Any:
        """Turns a HANA value into something DRF can serialise."""
        if value is None or isinstance(value, (bool, int, float, str)):
            return value
        if isinstance(value, Decimal):
            return float(value)
        if isinstance(value, datetime):
            # SAP dates arrive as midnight timestamps; show them as plain dates.
            if (value.hour, value.minute, value.second, value.microsecond) == (0, 0, 0, 0):
                return value.date().isoformat()
            return value.isoformat(sep=" ")
        if isinstance(value, date):
            return value.isoformat()
        if isinstance(value, (bytes, bytearray, memoryview)):
            return bytes(value).hex()
        return str(value)

    @staticmethod
    def _text(value: Any) -> str:
        if value is None:
            return ""
        return str(value)

    @staticmethod
    def _aware(value):
        """
        Stamps a HANA timestamp with the project timezone.

        SAP records these in the company's local wall-clock time, which is the
        same zone this project runs in, so the naive value is simply localised.
        """
        if not isinstance(value, datetime):
            return value
        if timezone.is_aware(value):
            return value
        return timezone.make_aware(value, timezone.get_default_timezone())

    @staticmethod
    def _contains(search: str) -> str:
        return f"%{search.strip().upper()}%"

    @classmethod
    def _as_options(cls, rows: Sequence[Sequence[Any]]) -> List[Dict[str, str]]:
        options = []
        for row in rows:
            # SAP master data carries the odd blank or whitespace-only code;
            # an unpickable option is worse than a shorter list.
            value = cls._text(row[0]).strip()
            if not value:
                continue
            options.append({"value": value, "label": cls._text(row[1]).strip() or value})
        return options

    @staticmethod
    def _readable_error(error: Exception, subject: str = "this report") -> str:
        """
        A HANA error a report user can act on.

        These reports are authored in SAP by people outside this app, so a broken
        one is a routine event: the message has to say what SAP objected to, not
        just that "something failed".
        """
        detail = str(error).strip()
        if len(detail) > 400:
            detail = detail[:400] + "..."
        message = f"SAP rejected {subject}"
        return f"{message}: {detail}" if detail else f"{message}."


# hdbcli reports a column's type as a numeric HANA type code (NVARCHAR is 11,
# DECIMAL 5, TIMESTAMP 16). A report screen only needs to know whether to
# right-align the column and how to format it, so the codes collapse to three
# buckets. A driver that ever hands back type *names* instead still works.
NUMERIC_TYPE_CODES = {1, 2, 3, 4, 5, 6, 7, 47, 71, 72, 73}
DATE_TYPE_CODES = {14, 15, 16, 17, 18, 19, 20, 63, 64, 65, 66, 74}

NUMERIC_TYPE_NAMES = ("INT", "DECIMAL", "REAL", "DOUBLE", "FLOAT", "NUMERIC")
DATE_TYPE_NAMES = ("DATE", "TIME", "TIMESTAMP")


def _column_type(column: Sequence[Any]) -> str:
    """``"number"`` / ``"date"`` / ``"text"`` for one ``cursor.description`` entry."""
    type_code = column[1] if len(column) > 1 else None

    if isinstance(type_code, int):
        if type_code in NUMERIC_TYPE_CODES:
            return "number"
        if type_code in DATE_TYPE_CODES:
            return "date"
        return "text"

    type_name = str(type_code or "").upper()
    if any(candidate in type_name for candidate in NUMERIC_TYPE_NAMES):
        return "number"
    if any(candidate in type_name for candidate in DATE_TYPE_NAMES):
        return "date"
    return "text"
