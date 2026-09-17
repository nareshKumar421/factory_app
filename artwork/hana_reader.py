"""
Reads the label and carton items out of the SAP item master.

Deliberately not ``WMSHanaReader.search_items_in_group``: that one filters on
the item group alone and caps at a screenful, and this page needs the *whole*
list. "For every item" is the requirement -- the register has to be able to
show the items with no artwork on file, and it cannot do that from a list that
stops at the first fifty.

The volume makes that safe. There are 593 artwork items in Oil, 171 in
Beverages and 33 in Mart, so the full list is one small query rather than a
paged read. See ``constants`` for the evidence behind the filter.
"""

import logging
from typing import Dict, List, Optional, Set

from hdbcli import dbapi

from sap_client.context import CompanyContext
from sap_client.exceptions import SAPConnectionError, SAPDataError
from sap_client.hana.connection import HanaConnection

from .constants import ARTWORK_SUB_GROUPS, PACKAGING_ITEM_GROUP_CODE

logger = logging.getLogger(__name__)


class ArtworkItemReader:
    """The label and carton items of one company."""

    def __init__(self, company_code: str):
        self.context = CompanyContext(company_code)
        self.connection = HanaConnection(self.context.hana)
        self.schema = self.connection.schema

    # ------------------------------------------------------------------
    # Items
    # ------------------------------------------------------------------

    def list_artwork_items(
        self,
        *,
        sub_group: Optional[str] = None,
        search: str = "",
        limit: int = 2000,
    ) -> List[Dict]:
        """Every LABEL / CARTON item in this company, oldest code first.

        ``sub_group`` narrows to one of the two kinds; omitted, both come back.
        ``search`` matches item code or name. ``limit`` is a backstop against a
        schema that has grown far past the counts in ``constants`` -- it is set
        well above the real figures rather than as a page size.
        """
        self._require_sub_group_column()

        kinds = (
            [sub_group.strip().upper()]
            if sub_group
            else list(ARTWORK_SUB_GROUPS)
        )
        unknown = [k for k in kinds if k not in ARTWORK_SUB_GROUPS]
        if unknown:
            raise SAPDataError(
                f"Not an artwork sub-group: {', '.join(unknown)}. "
                f"Expected one of {', '.join(ARTWORK_SUB_GROUPS)}."
            )

        placeholders = ", ".join(["?"] * len(kinds))
        clauses = [
            'T0."validFor" = ?',
            'T0."ItmsGrpCod" = ?',
            f'UPPER(T0."U_Sub_Group") IN ({placeholders})',
        ]
        params: List = ["Y", PACKAGING_ITEM_GROUP_CODE, *kinds]

        term = (search or "").strip()
        if term:
            like = f"%{term.upper()}%"
            clauses.append(
                '(UPPER(T0."ItemCode") LIKE ? OR UPPER(T0."ItemName") LIKE ?)'
            )
            params.extend([like, like])

        query = f"""
            SELECT TOP {int(limit)}
                T0."ItemCode",
                T0."ItemName",
                UPPER(T0."U_Sub_Group") AS "SubGroup",
                IFNULL(T0."InvntryUom", '') AS "UoM"
            FROM "{self.schema}"."OITM" T0
            WHERE {' AND '.join(clauses)}
            ORDER BY UPPER(T0."U_Sub_Group") ASC, T0."ItemCode" ASC
        """
        return [
            {
                "item_code": row[0] or "",
                "item_name": row[1] or "",
                "sub_group": row[2] or "",
                "uom": row[3] or "",
            }
            for row in self._execute(query, params)
        ]

    def get_item(self, item_code: str) -> Optional[Dict]:
        """One item, or ``None`` if SAP has no artwork item by that code.

        Used on the write path so a record cannot be filed against an item code
        that is not a label or a carton -- a typed code reaching the register
        unchecked is how a row ends up that nothing will ever read back.
        """
        code = (item_code or "").strip()
        if not code:
            return None
        matches = self.list_artwork_items(search=code, limit=50)
        for row in matches:
            if row["item_code"].upper() == code.upper():
                return row
        return None

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _require_sub_group_column(self) -> None:
        """Fail with a sentence rather than an SQL error if the UDF is gone.

        ``U_Sub_Group`` is a user-defined field. It is present in all three
        schemas today, but a company that dropped it would otherwise turn every
        request on this page into an opaque 500.
        """
        if "U_Sub_Group" in self._oitm_columns():
            return
        raise SAPDataError(
            "SAP item master has no U_Sub_Group field in "
            f"{self.schema}, so labels and cartons cannot be told apart. "
            "The field has to be restored on OITM before this register works."
        )

    def _oitm_columns(self) -> Set[str]:
        rows = self._execute(
            """
                SELECT "COLUMN_NAME"
                FROM "SYS"."TABLE_COLUMNS"
                WHERE "SCHEMA_NAME" = ? AND "TABLE_NAME" = ?
            """,
            [self.schema, "OITM"],
        )
        return {row[0] for row in rows}

    def _execute(self, query: str, params: List) -> List:
        conn = None
        cursor = None
        try:
            conn = self.connection.connect()
        except dbapi.Error as exc:
            logger.error("[Artwork] SAP HANA connection failed: %s", exc)
            raise SAPConnectionError(
                "Unable to reach SAP. The item list is unavailable; artwork "
                "already on file is still readable."
            ) from exc

        try:
            cursor = conn.cursor()
            cursor.execute(query, params)
            return cursor.fetchall()
        except dbapi.Error as exc:
            logger.error("[Artwork] SAP HANA query failed: %s", exc)
            raise SAPDataError("Failed to read the item master from SAP.") from exc
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
