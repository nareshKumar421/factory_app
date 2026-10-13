"""The SAP numbering series for an imprest document: object, date and branch.

``sap_client.hana.series_reader.HanaSeriesReader.resolve`` answers "which
series covers this date for this object" with the first row of the narrowest
period. That is right for stock transfers, which have one series a month. It
is not enough here:

* **Branches.** Purchasing objects at JIVO have a series per branch per month
  (``NNM1."BPLId"``) -- Bhiwadi and Delhi imprest A/Ps number differently
  (``6YYMM4NNN`` vs ``7YYMM31NN``). Taking the first row would put a Delhi
  voucher into a Bhiwadi series.
* **Backlog.** Vouchers post into past months, so the user's default series in
  SAP would be wrong; every cash-book document sends ``Series`` resolved for
  its own ``DocDate`` (spec D6).

``HanaSeriesReader`` cannot filter by branch and its source may not change, so
``BranchSeriesReader`` subclasses it -- keeping its connection and its
``resolve``/``name_for`` -- and adds ``candidates`` (the same OFPR join NNM1
query plus ``BPLId``, ``BeginStr`` and ``Locked``, every row of the narrowest
period) and ``resolve_for_branch`` (spec §2.1.3):

1. If ``bpl_id`` is given, keep rows for that branch when any exist, otherwise
   rows with no branch.
2. If ``name_hint`` is set, keep rows whose ``SeriesName + " " + BeginStr``
   contains it (case-insensitive). The hint is the per-routing setting that
   picks between two series that both fit.
3. Drop locked rows (``Locked = 'Y'``).
4. Drop exhausted rows (``NextNumber > LastNum``).

The order matters, and it is the base reader's order: narrowest period first,
then branch, and only then "is it locked". Locked rows are not filtered in SQL.
If they were, a month whose series are all locked would leave the year row
(OFPR holds those too, with NNM1 entries) as the "narrowest" survivor, and a
branch whose series are all locked would fall back to a branchless series --
either way a cash-book document would quietly land in the wrong series (D6).
Kept until step 3, a locked month or branch series ends in "No open SAP
series", as ``HanaSeriesReader.resolve`` ends in "is locked".

Exactly one row left is the answer. None, or more than one, is a
``SAPDataError`` with a message a person can act on.

Errors are those of ``HanaSeriesReader._fetch``: HANA unreachable ->
``SAPConnectionError`` (the handler waits); a refused query -> ``SAPDataError``.
"""

import logging
from datetime import date, datetime
from typing import List, Optional

from hdbcli import dbapi

from sap_client.context import CompanyContext
from sap_client.exceptions import SAPConnectionError, SAPDataError
from sap_client.hana.series_reader import HanaSeriesReader

logger = logging.getLogger(__name__)


def _as_date(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


class BranchSeriesReader(HanaSeriesReader):
    """``HanaSeriesReader`` plus branch-aware candidates. Build with a ``CompanyContext``."""

    def candidates(self, object_code: str, posting_date) -> List[dict]:
        """Every series, locked or not, of the narrowest period covering ``posting_date``.

        Each row: ``{series, series_name, begin_str, bpl_id, next_number,
        last_number, locked, indicator, period_status}``. ``bpl_id`` is None
        for a series not tied to a branch; ``locked`` is ``"Y"`` or ``"N"``.
        An empty list means SAP has no series at all for that date. Locked
        rows are returned on purpose (see the module docstring):
        ``pick_series`` drops them after the period and branch are chosen.
        """
        posting_date = _as_date(posting_date)
        conn = None
        cursor = None
        try:
            conn = self.connection.connect()
        except dbapi.Error as e:
            logger.error("SAP HANA connection failed while listing series: %s", e)
            raise SAPConnectionError("Unable to connect to SAP HANA.") from e

        try:
            cursor = conn.cursor()
            schema = self.connection.schema
            cursor.execute(
                f"""
                SELECT
                    S."Series",
                    IFNULL(S."SeriesName", ''),
                    IFNULL(S."BeginStr", ''),
                    S."BPLId",
                    S."NextNumber",
                    S."LastNum",
                    IFNULL(P."Indicator", ''),
                    IFNULL(P."PeriodStat", ''),
                    DAYS_BETWEEN(P."F_RefDate", P."T_RefDate") AS span_days,
                    IFNULL(S."Locked", 'N')
                FROM "{schema}"."OFPR" P
                JOIN "{schema}"."NNM1" S
                    ON S."Indicator" = P."Indicator"
                   AND S."ObjectCode" = ?
                WHERE ? BETWEEN TO_DATE(P."F_RefDate") AND TO_DATE(P."T_RefDate")
                ORDER BY span_days ASC, S."Series" ASC
                """,
                (str(object_code), posting_date),
            )
            rows = cursor.fetchall()
        except dbapi.Error as e:
            logger.error("SAP HANA series listing failed: %s", e)
            raise SAPDataError("Failed to resolve the SAP numbering series.") from e
        finally:
            for handle in (cursor, conn):
                if handle is not None:
                    try:
                        handle.close()
                    except Exception:
                        pass

        if not rows:
            return []
        # The month row is the narrowest period; a year row covering the same
        # date can also have NNM1 entries and must not compete with it --
        # including when every month series is locked, which is why the span
        # is worked out over locked rows too.
        spans = [r[8] for r in rows if r[8] is not None]
        narrowest = min(spans) if spans else None
        return [
            {
                "series": int(r[0]),
                "series_name": r[1] or "",
                "begin_str": r[2] or "",
                "bpl_id": int(r[3]) if r[3] is not None else None,
                "next_number": int(r[4]) if r[4] is not None else None,
                "last_number": int(r[5]) if r[5] is not None else None,
                "locked": "Y" if (r[9] or "N") == "Y" else "N",
                "indicator": r[6] or "",
                "period_status": r[7] or "",
            }
            for r in rows
            if narrowest is None or r[8] == narrowest
        ]

    def resolve_for_branch(self, object_code: str, posting_date, bpl_id: Optional[int] = None,
                           name_hint: str = "") -> dict:
        """The one series for (object, date, branch[, hint]); see the module rules."""
        posting_date = _as_date(posting_date)
        return pick_series(self.candidates(object_code, posting_date), object_code,
                           posting_date, bpl_id=bpl_id, name_hint=name_hint)


def pick_series(rows: List[dict], object_code: str, posting_date, bpl_id: Optional[int] = None,
                name_hint: str = "") -> dict:
    """Apply the branch, hint, locked and exhausted rules to ``candidates`` rows (pure).

    The branch rule looks at locked rows too, so a branch whose own series are
    all locked gets "No open SAP series" rather than a branchless series. A
    row without a ``locked`` key counts as unlocked.
    """
    posting_date = _as_date(posting_date)
    if bpl_id is not None:
        bpl_id = int(bpl_id)
        own = [r for r in rows if r["bpl_id"] == bpl_id]
        rows = own if own else [r for r in rows if r["bpl_id"] is None]
    hint = (name_hint or "").strip().upper()
    if hint:
        rows = [r for r in rows
                if hint in f"{r['series_name']} {r['begin_str']}".upper()]
    locked = [r for r in rows if r.get("locked", "N") == "Y"]
    rows = [r for r in rows if r.get("locked", "N") != "Y"]
    rows = [r for r in rows
            if r["next_number"] is None or r["last_number"] is None
            or r["next_number"] <= r["last_number"]]

    branch = bpl_id if bpl_id is not None else "any"
    if len(rows) == 1:
        return rows[0]
    if not rows:
        locked_note = ""
        if locked:
            names = ", ".join(f"{r['series_name']} ({r['series']})" for r in locked)
            locked_note = f" {names} {'is' if len(locked) == 1 else 'are'} locked."
        raise SAPDataError(
            f"No open SAP series for object {object_code} on {posting_date:%d %b %Y} "
            f"(branch {branch}).{locked_note} Ask the SAP team to open it."
        )
    names = ", ".join(f"{r['series_name']} ({r['series']})" for r in rows)
    raise SAPDataError(
        f"Several SAP series fit object {object_code} on {posting_date:%d %b %Y}: {names}. "
        f"Set the series hint in Cash book → SAP settings."
    )


def resolve_series_for_branch(company_code: str, object_code: str, posting_date,
                              bpl_id: Optional[int] = None, name_hint: str = "") -> dict:
    """``BranchSeriesReader(CompanyContext(company_code)).resolve_for_branch(...)``.

    Returns the chosen row; ``row["series"]`` is the number to send as ``Series``.
    """
    reader = BranchSeriesReader(CompanyContext(company_code))
    return reader.resolve_for_branch(object_code, posting_date, bpl_id=bpl_id,
                                     name_hint=name_hint)
