import logging
from typing import List

from hdbcli import dbapi

from .connection import HanaConnection
from ..dtos import WarehouseDTO
from ..exceptions import SAPConnectionError, SAPDataError

logger = logging.getLogger(__name__)


class HanaWarehouseReader:

    def __init__(self, context):
        self.connection = HanaConnection(context.hana)

    def get_active_warehouses(self) -> List[WarehouseDTO]:
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
            schema = self.connection.schema

            query = f"""
                SELECT
                    "WhsCode"  AS warehouse_code,
                    "WhsName"  AS warehouse_name
                FROM "{schema}"."OWHS"
                WHERE "Inactive" = 'N'
                ORDER BY "WhsCode"
            """

            cursor.execute(query)
            rows = cursor.fetchall()

            return [
                WarehouseDTO(
                    warehouse_code=row[0],
                    warehouse_name=row[1],
                )
                for row in rows
            ]

        except dbapi.ProgrammingError as e:
            logger.error(f"SAP HANA query error for warehouses: {e}")
            raise SAPDataError(
                "Failed to retrieve warehouse data from SAP. Invalid query or parameters."
            ) from e
        except dbapi.Error as e:
            logger.error(f"SAP HANA data error for warehouses: {e}")
            raise SAPDataError(
                "Failed to retrieve warehouse data from SAP. Please try again later."
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

    def get_return_warehouses(self) -> List[WarehouseDTO]:
        """Active goods-return warehouses (codes like ``<branch>-GR``/``-GRM``/``-RG``
        or names mentioning "return"). Used as the destination for A/R Returns."""
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
            schema = self.connection.schema

            query = f"""
                SELECT
                    "WhsCode"  AS warehouse_code,
                    "WhsName"  AS warehouse_name
                FROM "{schema}"."OWHS"
                WHERE "Inactive" = 'N'
                  AND (
                        "WhsCode" LIKE '%-GR'
                     OR "WhsCode" LIKE '%-GRM'
                     OR "WhsCode" LIKE '%-RG'
                     OR UPPER("WhsName") LIKE '%RETURN%'
                  )
                ORDER BY "WhsCode"
            """

            cursor.execute(query)
            rows = cursor.fetchall()

            return [
                WarehouseDTO(
                    warehouse_code=row[0],
                    warehouse_name=row[1],
                )
                for row in rows
            ]

        except dbapi.ProgrammingError as e:
            logger.error(f"SAP HANA query error for return warehouses: {e}")
            raise SAPDataError(
                "Failed to retrieve return warehouse data from SAP. Invalid query."
            ) from e
        except dbapi.Error as e:
            logger.error(f"SAP HANA data error for return warehouses: {e}")
            raise SAPDataError(
                "Failed to retrieve return warehouse data from SAP. Please try again later."
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
