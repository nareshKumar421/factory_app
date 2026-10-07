"""
Reads what an expense is filed under out of one company's SAP.

* **Budgets** -- dimension 3 of SAP's cost accounting (``OOCR``, ``DimCode =
  3``): Factory, Back Office, Sales, Transport... A short list, sent whole.
* **Expense G/L accounts** -- the chart of accounts (``OACT``), postable and
  of the expense type only (``ActType = 'E'``): about 235 in Oil, 180 in
  Beverages, 250 in Mart. Searched on the server as the user types.

Code and name are both snapshotted onto the expense by the caller, so the list
reads back when SAP is down.
"""

import logging
from typing import Dict, List

from hdbcli import dbapi

from sap_client.context import CompanyContext
from sap_client.exceptions import SAPConnectionError, SAPDataError
from sap_client.hana.connection import HanaConnection

logger = logging.getLogger(__name__)

#: SAP's cost-accounting dimension that holds the budget heads.
BUDGET_DIMENSION = 3


class ExpenseSapReader:
    """One company's budgets and expense accounts."""

    def __init__(self, company_code: str):
        self.context = CompanyContext(company_code)
        self.connection = HanaConnection(self.context.hana)
        self.schema = self.connection.schema

    # --- Budgets -----------------------------------------------------------

    def budgets(self) -> List[Dict]:
        """Every active budget, by name."""
        rows = self._execute(
            f"""
                SELECT "OcrCode", IFNULL("OcrName", '') AS "OcrName"
                FROM "{self.schema}"."OOCR"
                WHERE "DimCode" = ? AND IFNULL("Active", 'Y') = 'Y'
                ORDER BY CASE WHEN IFNULL("OcrName", '') = '' THEN "OcrCode" ELSE "OcrName" END
            """,
            [BUDGET_DIMENSION],
        )
        return [{"budget_code": row[0], "budget_name": row[1] or row[0]} for row in rows]

    def budget(self, code: str) -> Dict:
        """One active budget by its code, or :class:`SAPDataError`."""
        rows = self._execute(
            f"""
                SELECT "OcrCode", IFNULL("OcrName", '') AS "OcrName"
                FROM "{self.schema}"."OOCR"
                WHERE "DimCode" = ? AND "OcrCode" = ? AND IFNULL("Active", 'Y') = 'Y'
            """,
            [BUDGET_DIMENSION, (code or "").strip()],
        )
        if not rows:
            raise SAPDataError(f"{code} is not an active budget in SAP.")
        return {"budget_code": rows[0][0], "budget_name": rows[0][1] or rows[0][0]}

    # --- Expense G/L accounts ---------------------------------------------

    def expense_accounts(self, term: str = "", limit: int = 50) -> List[Dict]:
        """Postable expense accounts whose code or name matches ``term``."""
        clauses = ['"Postable" = ?', '"ActType" = ?']
        params: List = ["Y", "E"]
        needle = (term or "").strip()
        if needle:
            like = f"%{needle.upper()}%"
            clauses.append('(UPPER("AcctCode") LIKE ? OR UPPER("AcctName") LIKE ?)')
            params.extend([like, like])
        rows = self._execute(
            f"""
                SELECT TOP {int(limit)} "AcctCode", IFNULL("AcctName", '') AS "AcctName"
                FROM "{self.schema}"."OACT"
                WHERE {' AND '.join(clauses)}
                ORDER BY "AcctCode"
            """,
            params,
        )
        return [{"account_code": row[0], "account_name": row[1] or row[0]} for row in rows]

    def expense_account(self, code: str) -> Dict:
        """One postable expense account by its code, or :class:`SAPDataError`."""
        rows = self._execute(
            f"""
                SELECT "AcctCode", IFNULL("AcctName", '') AS "AcctName"
                FROM "{self.schema}"."OACT"
                WHERE "AcctCode" = ? AND "Postable" = ? AND "ActType" = ?
            """,
            [(code or "").strip(), "Y", "E"],
        )
        if not rows:
            raise SAPDataError(f"{code} is not a postable expense account in SAP.")
        return {"account_code": rows[0][0], "account_name": rows[0][1] or rows[0][0]}

    # --- Internals ---------------------------------------------------------

    def _execute(self, query: str, params: List) -> List:
        conn = None
        cursor = None
        try:
            conn = self.connection.connect()
        except dbapi.Error as exc:
            logger.error("[Expense claims] SAP HANA connection failed: %s", exc)
            raise SAPConnectionError("Unable to reach SAP right now.") from exc

        try:
            cursor = conn.cursor()
            cursor.execute(query, params)
            return cursor.fetchall()
        except dbapi.Error as exc:
            logger.error("[Expense claims] SAP HANA query failed: %s", exc)
            raise SAPDataError("Failed to read from SAP.") from exc
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
