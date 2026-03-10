import logging
from typing import List, Optional

from hdbcli import dbapi

from sap_client.hana.connection import HanaConnection
from sap_client.dtos import ItemDTO, UoMDTO
from sap_client.exceptions import SAPConnectionError, SAPDataError

logger = logging.getLogger(__name__)


class HanaItemReader:
    """
    Reads item master data, UoM, and warehouse data from SAP HANA
    for dropdown population in the Production Planning module.
    """

    def __init__(self, context):
        self.connection = HanaConnection(context.hana)

    # ------------------------------------------------------------------
    # Items (OITM)
    # ------------------------------------------------------------------

    def get_finished_goods(self, search: Optional[str] = None) -> List[ItemDTO]:
        """
        Fetch finished goods — items that have a production BOM defined in SAP (OITT).
        This ensures the selected item will always have BOM components available.
        """
        return self._get_items(item_type='finished', search=search)

    def get_raw_materials(self, search: Optional[str] = None) -> List[ItemDTO]:
        """
        Fetch raw material items — items where PrchseItem = 'Y'.
        Optionally filter by code or name.
        """
        return self._get_items(item_type='raw', search=search)

    def get_all_items(self, search: Optional[str] = None) -> List[ItemDTO]:
        """
        Fetch all inventory items (both finished and raw).
        """
        return self._get_items(item_type=None, search=search)

    def _get_items(self, item_type: Optional[str], search: Optional[str]) -> List[ItemDTO]:
        conn = None
        cursor = None

        try:
            conn = self.connection.connect()
        except dbapi.Error as e:
            logger.error(f"SAP HANA connection failed: {e}")
            raise SAPConnectionError("Unable to connect to SAP HANA.") from e

        try:
            cursor = conn.cursor()
            schema = self.connection.schema

            conditions = [
                'T0."InvntItem" = \'Y\'',
                'T0."Canceled" = \'N\'',
            ]
            params = []

            if item_type == 'finished':
                # Only items that have a production BOM defined in OITT
                conditions.append(
                    f'EXISTS (SELECT 1 FROM "{schema}"."OITT" B WHERE B."Code" = T0."ItemCode")'
                )
            elif item_type == 'raw':
                conditions.append('T0."PrchseItem" = \'Y\'')

            if search:
                conditions.append(
                    '(UPPER(T0."ItemCode") LIKE UPPER(?) OR UPPER(T0."ItemName") LIKE UPPER(?))'
                )
                wildcard = f'%{search}%'
                params.extend([wildcard, wildcard])

            where_clause = ' AND '.join(conditions)

            query = f"""
                SELECT
                    T0."ItemCode"                   AS item_code,
                    T0."ItemName"                   AS item_name,
                    IFNULL(T0."InvntryUom", '')     AS uom,
                    IFNULL(T1."ItmsGrpNam", '')     AS item_group,
                    CASE WHEN EXISTS (SELECT 1 FROM "{schema}"."OITT" B WHERE B."Code" = T0."ItemCode") THEN 'Y' ELSE 'N' END AS make_item,
                    CASE WHEN T0."PrchseItem"  = 'Y' THEN 'Y' ELSE 'N' END AS purchase_item
                FROM "{schema}"."OITM" T0
                LEFT JOIN "{schema}"."OITB" T1 ON T0."ItmsGrpCod" = T1."ItmsGrpCod"
                WHERE {where_clause}
                ORDER BY T0."ItemCode"
                LIMIT 200
            """

            cursor.execute(query, params)
            rows = cursor.fetchall()

            return [
                ItemDTO(
                    item_code=row[0],
                    item_name=row[1],
                    uom=row[2],
                    item_group=row[3],
                    make_item=row[4] == 'Y',
                    purchase_item=row[5] == 'Y',
                )
                for row in rows
            ]

        except dbapi.ProgrammingError as e:
            logger.error(f"SAP HANA query error fetching items: {e}")
            raise SAPDataError("Failed to retrieve items from SAP.") from e
        except dbapi.Error as e:
            logger.error(f"SAP HANA data error fetching items: {e}")
            raise SAPDataError("Failed to retrieve items from SAP. Please try again.") from e
        finally:
            self._close(cursor, conn)

    # ------------------------------------------------------------------
    # Units of Measure (OUOM)
    # ------------------------------------------------------------------

    def get_uom_list(self) -> List[UoMDTO]:
        """Fetch all units of measure from SAP HANA."""
        conn = None
        cursor = None

        try:
            conn = self.connection.connect()
        except dbapi.Error as e:
            logger.error(f"SAP HANA connection failed: {e}")
            raise SAPConnectionError("Unable to connect to SAP HANA.") from e

        try:
            cursor = conn.cursor()
            schema = self.connection.schema

            query = f"""
                SELECT
                    "UomCode" AS uom_code,
                    "UomName" AS uom_name
                FROM "{schema}"."OUOM"
                ORDER BY "UomCode"
            """

            cursor.execute(query)
            rows = cursor.fetchall()

            return [UoMDTO(uom_code=row[0], uom_name=row[1]) for row in rows]

        except dbapi.ProgrammingError as e:
            logger.error(f"SAP HANA query error fetching UoM: {e}")
            raise SAPDataError("Failed to retrieve UoM list from SAP.") from e
        except dbapi.Error as e:
            logger.error(f"SAP HANA data error fetching UoM: {e}")
            raise SAPDataError("Failed to retrieve UoM from SAP. Please try again.") from e
        finally:
            self._close(cursor, conn)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _close(cursor, conn):
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
