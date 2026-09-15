"""
Reads the G/L heads the cash book picks from out of SAP's chart of accounts.

A type-ahead, not a list. There are 1,318 postable accounts in Oil, 1,180 in
Mart and 660 in Beverages (15 September 2026), so the search runs on HANA and
comes back capped -- see ``constants.GL_ACCOUNT_SEARCH_LIMIT``.

The account's code and name are snapshotted onto the entry by the caller, so
the register reads back in full when SAP is unreachable. Only the *picker*
needs SAP to be up.
"""

import logging
from typing import Dict, List

from hdbcli import dbapi

from sap_client.context import CompanyContext
from sap_client.exceptions import SAPConnectionError, SAPDataError
from sap_client.hana.connection import HanaConnection

from .constants import GL_ACCOUNT_SEARCH_LIMIT

logger = logging.getLogger(__name__)


class GLAccountReader:
    """One company's postable G/L accounts."""

    def __init__(self, company_code: str):
        self.context = CompanyContext(company_code)
        self.connection = HanaConnection(self.context.hana)
        self.schema = self.connection.schema

    def search(self, term: str = "", limit: int = GL_ACCOUNT_SEARCH_LIMIT) -> List[Dict]:
        """Accounts whose code or name matches ``term``, lowest code first.

        A blank term returns the first ``limit`` accounts, which is what the
        picker shows before anybody types.

        Not narrowed to expense accounts: petty cash pays advances against
        salary as well as expenses, and an advance lands on a debtor account.
        """
        clauses = ['"Postable" = ?']
        params: List = ["Y"]

        needle = (term or "").strip()
        if needle:
            like = f"%{needle.upper()}%"
            clauses.append('(UPPER("AcctCode") LIKE ? OR UPPER("AcctName") LIKE ?)')
            params.extend([like, like])

        query = f"""
            SELECT TOP {int(limit)}
                "AcctCode",
                IFNULL("AcctName", '') AS "AcctName",
                IFNULL("ActType", '') AS "ActType"
            FROM "{self.schema}"."OACT"
            WHERE {' AND '.join(clauses)}
            ORDER BY "AcctCode"
        """

        return [
            {
                "account_code": row[0] or "",
                "account_name": row[1] or row[0] or "",
                "account_type": row[2] or "",
            }
            for row in self._execute(query, params)
        ]

    def resolve(self, account_code: str) -> Dict:
        """One account by its code, so a recorded entry snapshots a real name.

        Raises :class:`SAPDataError` if SAP does not have it or will not take a
        posting on it -- an entry filed against an account nothing can be
        posted to is one accounts will have to unpick later.
        """
        code = (account_code or "").strip()
        if not code:
            raise SAPDataError("No G/L account code given.")

        rows = self._execute(
            f"""
                SELECT "AcctCode", IFNULL("AcctName", '') AS "AcctName"
                FROM "{self.schema}"."OACT"
                WHERE "AcctCode" = ? AND "Postable" = ?
            """,
            [code, "Y"],
        )
        if not rows:
            raise SAPDataError(
                f"{code} is not a postable G/L account in this company's chart "
                f"of accounts."
            )
        return {"account_code": rows[0][0], "account_name": rows[0][1] or rows[0][0]}

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _execute(self, query: str, params: List) -> List:
        conn = None
        cursor = None
        try:
            conn = self.connection.connect()
        except dbapi.Error as exc:
            logger.error("[Cash book] SAP HANA connection failed: %s", exc)
            raise SAPConnectionError(
                "Unable to reach SAP, so the chart of accounts cannot be "
                "searched. Entries already in the book still read in full."
            ) from exc

        try:
            cursor = conn.cursor()
            cursor.execute(query, params)
            return cursor.fetchall()
        except dbapi.Error as exc:
            logger.error("[Cash book] SAP HANA query failed: %s", exc)
            raise SAPDataError("Failed to read the chart of accounts from SAP.") from exc
        finally:
            if cursor is not None:
                try:
                    cursor.close()
                except dbapi.Error:
                    pass
            if conn is not None:
                try:
                    conn.close()
                except dbapi.Error:
                    pass


__all__ = ["GLAccountReader", "SAPConnectionError", "SAPDataError"]
