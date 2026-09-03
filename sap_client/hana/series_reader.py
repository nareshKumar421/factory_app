"""Resolve the SAP numbering series that applies to a posting date.

SAP B1 at JIVO uses **month-specific** series: `NNM1` holds one row per
(object, month) keyed by `Indicator`, and `Indicator` joins `OFPR` — the posting
period — for the date range it covers. August 2026 is series 2660 for an
inventory transfer (object 67) and 2648 for a transfer request (1250000001);
September is a different pair. A posted document must carry the series for its
own month, so this has to be resolved per post and never hardcoded.
"""

import logging
from datetime import date
from typing import Optional

from hdbcli import dbapi

from .connection import HanaConnection
from ..exceptions import SAPConnectionError, SAPDataError

logger = logging.getLogger(__name__)


class HanaSeriesReader:
    """Read document numbering series from NNM1, scoped by posting period."""

    # SAP object codes (NNM1."ObjectCode")
    OBJ_STOCK_TRANSFER = "67"
    OBJ_TRANSFER_REQUEST = "1250000001"

    def __init__(self, context):
        self.connection = HanaConnection(context.hana)

    def resolve(self, object_code: str, posting_date: date) -> dict:
        """Return the series that applies to `posting_date` for `object_code`.

        Raises SAPDataError when no series covers the date, when the series is
        locked, or when its number range is exhausted — all three would
        otherwise surface as an opaque SAP rejection at post time.
        """
        row = self._fetch(object_code, posting_date)

        if not row:
            raise SAPDataError(
                f"No SAP numbering series is defined for object {object_code} "
                f"on {posting_date:%Y-%m-%d}. Ask the SAP team to open the "
                f"series for that month before posting."
            )

        if row["locked"] == "Y":
            raise SAPDataError(
                f"SAP series {row['series_name']} ({row['series']}) is locked, "
                f"so nothing can be posted into {row['indicator']}."
            )

        if row["next_number"] is not None and row["last_number"] is not None:
            if row["next_number"] > row["last_number"]:
                raise SAPDataError(
                    f"SAP series {row['series_name']} ({row['series']}) is "
                    f"exhausted — it ends at {row['last_number']}. The SAP team "
                    f"needs to extend the range."
                )

        return row

    def resolve_stock_transfer(self, posting_date: date) -> int:
        return self.resolve(self.OBJ_STOCK_TRANSFER, posting_date)["series"]

    def resolve_transfer_request(self, posting_date: date) -> int:
        return self.resolve(self.OBJ_TRANSFER_REQUEST, posting_date)["series"]

    def remaining_capacity(self, object_code: str, posting_date: date) -> Optional[int]:
        """How many document numbers the month's series still has.

        Worth surfacing because cancelling a transfer burns two numbers (the
        original plus its reversal), so a cancel-heavy month consumes the range
        faster than the document count suggests.
        """
        row = self._fetch(object_code, posting_date)
        if not row or row["next_number"] is None or row["last_number"] is None:
            return None
        return max(0, row["last_number"] - row["next_number"] + 1)

    def name_for(self, series) -> str:
        """The printed name of a series SAP has already stamped on a document.

        ``resolve`` answers "which series should this post use?". A printed
        document asks the opposite question: it already carries ``Series`` 2094
        and has to show ``DELG0926``, which is what the SAP layout prints.

        Keyed on the series id alone — the document's own period is whatever
        period the series belongs to, so no date is needed or wanted here.
        """
        if not series:
            return ""

        conn = None
        cursor = None
        try:
            conn = self.connection.connect()
            cursor = conn.cursor()
            cursor.execute(
                f'''SELECT IFNULL("SeriesName", '')
                    FROM "{self.connection.schema}"."NNM1"
                    WHERE "Series" = ?''',
                (int(series),),
            )
            row = cursor.fetchone()
            return (row[0] or "") if row else ""
        except dbapi.Error as e:
            logger.error("SAP HANA series-name lookup failed: %s", e)
            raise SAPDataError("Failed to read the SAP numbering series name.") from e
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

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _fetch(self, object_code: str, posting_date: date) -> Optional[dict]:
        conn = None
        cursor = None

        try:
            conn = self.connection.connect()
        except dbapi.Error as e:
            logger.error("SAP HANA connection failed while resolving series: %s", e)
            raise SAPConnectionError("Unable to connect to SAP HANA.") from e

        try:
            cursor = conn.cursor()
            schema = self.connection.schema

            # OFPR can hold both month rows and wider (year) rows whose
            # Indicator also has an NNM1 entry, so prefer the narrowest period
            # covering the date — that is the month the document belongs to.
            cursor.execute(
                f"""
                SELECT
                    S."Series",
                    IFNULL(S."SeriesName", ''),
                    S."NextNumber",
                    S."LastNum",
                    IFNULL(S."Locked", 'N'),
                    IFNULL(P."Indicator", ''),
                    IFNULL(P."PeriodStat", ''),
                    DAYS_BETWEEN(P."F_RefDate", P."T_RefDate") AS span_days
                FROM "{schema}"."OFPR" P
                JOIN "{schema}"."NNM1" S
                    ON S."Indicator" = P."Indicator"
                   AND S."ObjectCode" = ?
                WHERE ? BETWEEN TO_DATE(P."F_RefDate") AND TO_DATE(P."T_RefDate")
                ORDER BY span_days ASC
                """,
                (str(object_code), posting_date),
            )
            row = cursor.fetchone()
            if not row:
                return None

            return {
                "series": int(row[0]),
                "series_name": row[1] or "",
                "next_number": int(row[2]) if row[2] is not None else None,
                "last_number": int(row[3]) if row[3] is not None else None,
                "locked": row[4] or "N",
                "indicator": row[5] or "",
                "period_status": row[6] or "",
            }
        except dbapi.Error as e:
            logger.error("SAP HANA series lookup failed: %s", e)
            raise SAPDataError("Failed to resolve the SAP numbering series.") from e
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
