"""
non_moving_rm/hana_reader.py

Executes SAP HANA queries for the Non-Moving Raw Material Dashboard.
Calls the stored procedure REPORT_BP_NON_MOVING_RM and reads item groups from OITB.
"""

import logging
from typing import Any, Dict, List

from hdbcli import dbapi

from sap_client.hana.connection import HanaConnection
from sap_client.exceptions import SAPConnectionError, SAPDataError

logger = logging.getLogger(__name__)


class HanaNonMovingRMReader:
    """
    Reads non-moving raw material data from SAP HANA.

    Provides two queries:
      1. get_non_moving_report() — calls REPORT_BP_NON_MOVING_RM procedure
      2. get_item_groups()       — reads item groups from OITB for dropdown
    """

    def __init__(self, context):
        self.connection = HanaConnection(context.hana)

    # ------------------------------------------------------------------
    # Public Methods
    # ------------------------------------------------------------------

    def get_non_moving_report(self, age: int, item_group: int) -> List[Dict]:
        """
        Calls the REPORT_BP_NON_MOVING_RM stored procedure.

        Args:
            age: Number of days since last movement (e.g. 45)
            item_group: Item group code from OITB (e.g. 105)

        Returns:
            List of dicts with non-moving item details.
        """
        schema = self.connection.schema
        query = f'CALL "{schema}"."REPORT_BP_NON_MOVING_RM1"(?, ?)'
        params = [age, item_group]

        rows = self._execute(query, params)
        return [self._map_report_row(r) for r in rows]

    def get_item_groups(self) -> List[Dict]:
        """
        Reads all item groups from OITB table for the dropdown filter.

        Returns:
            List of dicts with ItmsGrpCod and ItmsGrpNam.
        """
        schema = self.connection.schema
        query = f'SELECT "ItmsGrpCod", "ItmsGrpNam" FROM "{schema}"."OITB" ORDER BY "ItmsGrpNam"'
        rows = self._execute(query, [])
        return [self._map_item_group_row(r) for r in rows]

    # ------------------------------------------------------------------
    # Row Mappers
    # ------------------------------------------------------------------

    def _map_report_row(self, row) -> Dict:
        return {
            "branch": row[0] or "",
            "item_code": row[1] or "",
            "item_name": row[2] or "",
            "item_group_name": row[3] or "",
            "quantity": float(row[4] or 0),
            "litres": float(row[5] or 0),
            "sub_group": row[6] or "",
            "value": float(row[7] or 0),
            "last_movement_date": row[8].strftime("%Y-%m-%d %H:%M:%S") if row[8] else None,
            "days_since_last_movement": int(row[9] or 0),
            "consumption_ratio": float(row[10] or 0),
        }

    def _map_item_group_row(self, row) -> Dict:
        return {
            "item_group_code": int(row[0]),
            "item_group_name": row[1] or "",
        }

    # ------------------------------------------------------------------
    # Execution Helper
    # ------------------------------------------------------------------

    def _execute(self, query: str, params: List) -> List:
        conn = None
        cursor = None

        try:
            conn = self.connection.connect()
        except dbapi.Error as e:
            logger.error(f"SAP HANA connection failed: {e}")
            raise SAPConnectionError(
                "Unable to connect to SAP HANA. Please try again later."
            ) from e

        try:
            cursor = conn.cursor()
            cursor.execute(query, params)
            return cursor.fetchall()

        except dbapi.ProgrammingError as e:
            logger.error(f"SAP HANA query error in non-moving RM: {e}")
            raise SAPDataError(
                "Failed to retrieve non-moving RM data from SAP. Invalid query."
            ) from e
        except dbapi.Error as e:
            logger.error(f"SAP HANA data error in non-moving RM: {e}")
            raise SAPDataError(
                "Failed to retrieve non-moving RM data from SAP. Please try again."
            ) from e
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
