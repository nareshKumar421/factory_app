"""
Reads the SAP branches a claim is filed under, out of ``OBPL``.

The G/L accounts come from :class:`cash_book.hana_reader.GLAccountReader` --
the same chart of accounts, searched the same way, so the two screens cannot
disagree about which accounts are postable.

Unlike the chart of accounts the branch list is short (a handful per company),
so it is sent whole rather than searched.
"""

import logging
from typing import Dict, List

from hdbcli import dbapi

from sap_client.context import CompanyContext
from sap_client.exceptions import SAPConnectionError, SAPDataError
from sap_client.hana.connection import HanaConnection

logger = logging.getLogger(__name__)


class BranchReader:
    """One company's active SAP branches (business places)."""

    def __init__(self, company_code: str):
        self.context = CompanyContext(company_code)
        self.connection = HanaConnection(self.context.hana)
        self.schema = self.connection.schema

    def list(self) -> List[Dict]:
        """Every branch SAP will take a posting on, lowest id first."""
        rows = self._execute(
            f"""
                SELECT "BPLId", IFNULL("BPLName", '') AS "BPLName"
                FROM "{self.schema}"."OBPL"
                WHERE IFNULL("Disabled", 'N') = 'N'
                ORDER BY "BPLId"
            """,
            [],
        )
        return [
            {"branch_id": int(row[0]), "branch_name": row[1] or str(row[0])}
            for row in rows
        ]

    def resolve(self, branch_id: int) -> Dict:
        """One branch by its id, so a sent claim snapshots a real name.

        Raises :class:`SAPDataError` if SAP has no such branch, or has it
        disabled.
        """
        rows = self._execute(
            f"""
                SELECT "BPLId", IFNULL("BPLName", '') AS "BPLName"
                FROM "{self.schema}"."OBPL"
                WHERE "BPLId" = ? AND IFNULL("Disabled", 'N') = 'N'
            """,
            [int(branch_id)],
        )
        if not rows:
            raise SAPDataError(f"Branch {branch_id} is not an active branch in SAP.")
        return {"branch_id": int(rows[0][0]), "branch_name": rows[0][1] or str(rows[0][0])}

    def _execute(self, query: str, params: List) -> List:
        conn = None
        cursor = None
        try:
            conn = self.connection.connect()
        except dbapi.Error as exc:
            logger.error("[Expense claims] SAP HANA connection failed: %s", exc)
            raise SAPConnectionError(
                "Unable to reach SAP, so its branches cannot be read."
            ) from exc

        try:
            cursor = conn.cursor()
            cursor.execute(query, params)
            return cursor.fetchall()
        except dbapi.Error as exc:
            logger.error("[Expense claims] SAP HANA query failed: %s", exc)
            raise SAPDataError("Failed to read the branches from SAP.") from exc
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
