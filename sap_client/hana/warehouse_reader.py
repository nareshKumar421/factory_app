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

    def get_warehouse_stock(
        self,
        warehouse_code: str,
        *,
        search: str = "",
        limit: int = 50,
        include_zero: bool = False,
    ) -> List[dict]:
        """Items held in one warehouse, for an item picker.

        Returns both ``on_hand`` and ``available`` (on hand minus committed),
        because the two genuinely differ and only the second one is safe to
        promise: an open transfer request already commits stock at its source, so
        offering raw on-hand would let two requests claim the same drums. In live
        data ``available`` can even go negative where a warehouse is already
        over-committed, so callers must handle that rather than assume a floor
        of zero.
        """
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
            safe_limit = max(1, min(int(limit or 50), 200))

            where = ['W."WhsCode" = ?', 'I."InvntItem" = ?']
            params: list = [str(warehouse_code), "Y"]

            if not include_zero:
                where.append('W."OnHand" > 0')

            if search:
                term = f"%{search.strip().upper()}%"
                where.append(
                    '(UPPER(W."ItemCode") LIKE ? OR UPPER(IFNULL(I."ItemName", \'\')) LIKE ?)'
                )
                params.extend([term, term])

            cursor.execute(
                f"""
                SELECT
                    W."ItemCode",
                    IFNULL(I."ItemName", ''),
                    W."OnHand",
                    IFNULL(W."IsCommited", 0),
                    IFNULL(W."OnOrder", 0),
                    IFNULL(I."InvntryUom", ''),
                    IFNULL(I."ManBtchNum", 'N'),
                    IFNULL(I."ItmsGrpCod", 0)
                FROM "{schema}"."OITW" W
                JOIN "{schema}"."OITM" I ON I."ItemCode" = W."ItemCode"
                WHERE {" AND ".join(where)}
                ORDER BY W."OnHand" DESC, W."ItemCode"
                LIMIT {safe_limit}
                """,
                tuple(params),
            )

            rows = []
            for row in cursor.fetchall():
                on_hand = float(row[2] or 0)
                committed = float(row[3] or 0)
                rows.append(
                    {
                        "item_code": row[0],
                        "item_name": row[1] or "",
                        "on_hand": on_hand,
                        "committed": committed,
                        "available": on_hand - committed,
                        "on_order": float(row[4] or 0),
                        "uom": row[5] or "",
                        "is_batch_managed": (row[6] or "N") == "Y",
                        "item_group": int(row[7] or 0),
                    }
                )
            return rows
        except dbapi.Error as e:
            logger.error(f"SAP HANA data error for warehouse stock: {e}")
            raise SAPDataError(
                "Failed to retrieve warehouse stock from SAP."
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

    def get_warehouse_branches(self) -> dict:
        """Map warehouse code -> ``OWHS.BPLid`` (the SAP branch).

        A transfer's branch pair decides whether SAP will accept it as one
        document or force it through an in-transit warehouse, so the caller
        needs the branch of every warehouse it touches — which the
        ``WarehouseDTO`` list does not carry.
        """
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
            cursor.execute(
                f'SELECT "WhsCode", "BPLid" FROM "{self.connection.schema}"."OWHS"'
            )
            return {
                row[0]: (int(row[1]) if row[1] is not None else None)
                for row in cursor.fetchall()
            }
        except dbapi.Error as e:
            logger.error(f"SAP HANA data error for warehouse branches: {e}")
            raise SAPDataError(
                "Failed to retrieve warehouse branches from SAP."
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

    def get_warehouse_print_info(self, warehouse_codes: List[str]) -> dict:
        """Letterhead data for the app's Branch Stock Transfer print.

        For each warehouse: its name and postal address from ``OWHS``, plus the
        GST registration and state of the SAP branch (``OBPL``) it belongs to —
        the same sources SAP's own Crystal print reads. Also carries the
        company's legal name (``OADM``) for the page header.
        """
        codes = [str(code).strip() for code in (warehouse_codes or []) if str(code).strip()]
        if not codes:
            return {"company_name": "", "company_email": "", "warehouses": {}}

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

            cursor.execute(f'SELECT "CompnyName", "E_Mail" FROM "{schema}"."OADM"')
            row = cursor.fetchone()
            company_name = (row[0] or "").strip() if row else ""
            company_email = (row[1] or "").strip() if row else ""

            placeholders = ", ".join("?" for _ in codes)
            cursor.execute(
                f"""
                SELECT
                    W."WhsCode",
                    W."WhsName",
                    W."Street",
                    W."Block",
                    TO_NVARCHAR(W."Building"),
                    W."City",
                    W."ZipCode",
                    W."State",
                    W."Country",
                    B."BPLName",
                    B."TaxIdNum",
                    S."Name"
                FROM "{schema}"."OWHS" W
                LEFT JOIN "{schema}"."OBPL" B ON B."BPLId" = W."BPLid"
                LEFT JOIN "{schema}"."OCST" S ON S."Code" = W."State"
                WHERE W."WhsCode" IN ({placeholders})
                """,
                codes,
            )

            warehouses = {}
            for row in cursor.fetchall():
                warehouses[row[0]] = {
                    "code": row[0],
                    "name": row[1] or "",
                    "street": row[2] or "",
                    "block": row[3] or "",
                    "building": row[4] or "",
                    "city": row[5] or "",
                    "zip_code": row[6] or "",
                    "state_code": row[7] or "",
                    "country": row[8] or "",
                    "branch_name": row[9] or "",
                    "gstin": row[10] or "",
                    "state_name": row[11] or (row[7] or ""),
                }

            return {
                "company_name": company_name,
                "company_email": company_email,
                "warehouses": warehouses,
            }

        except dbapi.Error as e:
            logger.error(f"SAP HANA data error for warehouse print info: {e}")
            raise SAPDataError(
                "Failed to retrieve warehouse print information from SAP."
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
