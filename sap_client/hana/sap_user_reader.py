"""Read the SAP B1 user list, for the SAP-identity admin page.

The page maps app users to SAP accounts, so it needs the real list of accounts
to pick from — typing a code by hand is how you get a mapping that silently
never matches an authorizer.

Each user also carries how many **active** approval templates name them as an
authorizer, which turns the page from a form into a worklist: those are exactly
the accounts whose passwords are worth collecting, and the ones a mapping
actually buys anything for.
"""

import logging

from hdbcli import dbapi

from .connection import HanaConnection
from ..exceptions import SAPConnectionError, SAPDataError

logger = logging.getLogger(__name__)

# The document families this app decides approvals for: A/R invoices, inventory
# transfers and inventory transfer requests.
_APPROVAL_OBJ_TYPES = ("13", "67", "1250000001")


class HanaSapUserReader:
    """List SAP B1 users (OUSR) with their approval-authorizer standing."""

    def __init__(self, context):
        self.connection = HanaConnection(context.hana)

    def list_users(self, include_locked: bool = False) -> list[dict]:
        """Every SAP user in the company database, code order.

        ``authorizing_templates`` counts distinct ACTIVE templates (``OWTM``)
        whose stages (``WTM2`` → ``WST1``) name this user, restricted to the
        document types this app decides. Zero means mapping them buys nothing
        today — they may still be a valid person, just not an authorizer.
        """
        obj_types = ", ".join(f"'{t}'" for t in _APPROVAL_OBJ_TYPES)
        locked_clause = "" if include_locked else """WHERE U."Locked" = 'N'"""
        rows = self._query(
            f"""
            SELECT
                U."USER_CODE", U."U_NAME", U."Locked",
                (SELECT COUNT(DISTINCT T."WtmCode")
                 FROM "{{schema}}"."WST1" S
                 JOIN "{{schema}}"."WTM2" M ON M."WstCode" = S."WstCode"
                 JOIN "{{schema}}"."OWTM" T
                     ON T."WtmCode" = M."WtmCode" AND T."Active" = 'Y'
                 JOIN "{{schema}}"."WTM3" D ON D."WtmCode" = T."WtmCode"
                 WHERE S."UserID" = U."USERID"
                   AND D."TransType" IN ({obj_types})) AS "AuthTemplates"
            FROM "{{schema}}"."OUSR" U
            {locked_clause}
            ORDER BY U."USER_CODE"
            """,
            (),
        )
        return [
            {
                "user_code": (code or "").strip(),
                "user_name": (name or "").strip(),
                "locked": locked == "Y",
                "authorizing_templates": int(templates or 0),
            }
            for code, name, locked, templates in rows
        ]

    def _query(self, sql: str, params: tuple) -> list:
        conn = None
        cursor = None
        try:
            conn = self.connection.connect()
        except dbapi.Error as e:
            logger.error("SAP HANA connection failed while reading SAP users: %s", e)
            raise SAPConnectionError("Unable to connect to SAP HANA.") from e

        try:
            cursor = conn.cursor()
            cursor.execute(sql.replace("{schema}", self.connection.schema), params)
            return cursor.fetchall()
        except dbapi.Error as e:
            logger.error("SAP HANA user query failed: %s", e)
            raise SAPDataError("Failed to read the SAP user list.") from e
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
