import logging
from typing import Any, Dict, List

from hdbcli import dbapi

from .connection import HanaConnection
from ..exceptions import SAPConnectionError, SAPDataError

logger = logging.getLogger(__name__)


class HanaServiceGRPOOptionsReader:
    """Reads SAP master-data options used by service GRPO posting."""

    def __init__(self, context):
        self.connection = HanaConnection(context.hana)

    def get_options(self) -> Dict[str, List[Dict[str, Any]]]:
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

            return {
                "branches": self._get_branches(cursor, schema),
                "tax_codes": self._get_tax_codes(cursor, schema),
                "gl_accounts": self._get_gl_accounts(cursor, schema),
                "sac_codes": self._get_sac_codes(cursor, schema),
                "locations": self._get_locations(cursor, schema),
                "varieties": self._get_product_varieties(cursor, schema),
                "projects": self._get_budget_delivery_points(cursor, schema),
                "sub_accounts": self._get_sub_accounts(cursor, schema),
                "expense_codes": self._get_expense_codes(cursor, schema),
            }

        except dbapi.ProgrammingError as e:
            logger.error(f"SAP HANA query error for service GRPO options: {e}")
            raise SAPDataError(
                "Failed to retrieve service GRPO options from SAP. Invalid query or parameters."
            ) from e
        except dbapi.Error as e:
            logger.error(f"SAP HANA data error for service GRPO options: {e}")
            raise SAPDataError(
                "Failed to retrieve service GRPO options from SAP. Please try again later."
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

    @staticmethod
    def _get_branches(cursor, schema: str) -> List[Dict[str, Any]]:
        cursor.execute(
            f"""
                SELECT
                    "BPLId" AS branch_id,
                    "BPLName" AS branch_name,
                    IFNULL("State", '') AS state
                FROM "{schema}"."OBPL"
                WHERE IFNULL("Disabled", 'N') = 'N'
                ORDER BY "BPLId"
            """
        )
        return [
            {
                "branch_id": int(row[0]),
                "branch_name": row[1] or str(row[0]),
                "state": row[2] or "",
            }
            for row in cursor.fetchall()
        ]

    @staticmethod
    def _get_tax_codes(cursor, schema: str) -> List[Dict[str, Any]]:
        cursor.execute(
            f"""
                SELECT
                    "Code" AS tax_code,
                    "Name" AS tax_name,
                    "Rate" AS rate
                FROM "{schema}"."OSTC"
                ORDER BY "Code"
            """
        )
        return [
            {
                "tax_code": row[0],
                "tax_name": row[1] or row[0],
                "rate": float(row[2]) if row[2] is not None else None,
            }
            for row in cursor.fetchall()
        ]

    @staticmethod
    def _get_gl_accounts(cursor, schema: str) -> List[Dict[str, Any]]:
        cursor.execute(
            f"""
                SELECT
                    "AcctCode" AS account_code,
                    "AcctName" AS account_name
                FROM "{schema}"."OACT"
                WHERE "Postable" = 'Y'
                ORDER BY "AcctCode"
            """
        )
        return [
            {
                "account_code": row[0],
                "account_name": row[1] or row[0],
            }
            for row in cursor.fetchall()
        ]

    @staticmethod
    def _get_sac_codes(cursor, schema: str) -> List[Dict[str, Any]]:
        cursor.execute(
            f"""
                SELECT
                    "AbsEntry" AS sac_entry,
                    IFNULL("ServCode", '') AS sac_code,
                    IFNULL("ServName", '') AS sac_name
                FROM "{schema}"."OSAC"
                ORDER BY "ServCode"
            """
        )
        return [
            {
                "sac_entry": int(row[0]),
                "sac_code": row[1] or str(row[0]),
                "sac_name": row[2] or row[1] or str(row[0]),
            }
            for row in cursor.fetchall()
        ]

    @staticmethod
    def _get_locations(cursor, schema: str) -> List[Dict[str, Any]]:
        cursor.execute(
            f"""
                SELECT
                    "Code" AS location_code,
                    IFNULL("Location", '') AS location_name,
                    IFNULL("State", '') AS state
                FROM "{schema}"."OLCT"
                ORDER BY "Location"
            """
        )
        return [
            {
                "location_code": int(row[0]),
                "location_name": row[1] or str(row[0]),
                "state": row[2] or "",
            }
            for row in cursor.fetchall()
        ]

    @staticmethod
    def _get_product_varieties(cursor, schema: str) -> List[Dict[str, Any]]:
        cursor.execute(
            f"""
                SELECT
                    "OcrCode" AS variety_code,
                    IFNULL("OcrName", '') AS variety_name
                FROM "{schema}"."OOCR"
                WHERE "DimCode" = 1
                  AND IFNULL("Active", 'Y') = 'Y'
                ORDER BY "OcrName", "OcrCode"
            """
        )
        return [
            {
                "variety_code": row[0],
                "variety_name": row[1] or row[0],
            }
            for row in cursor.fetchall()
        ]

    @staticmethod
    def _get_budget_delivery_points(cursor, schema: str) -> List[Dict[str, Any]]:
        cursor.execute(
            f"""
                SELECT
                    "OcrCode" AS project_code,
                    IFNULL("OcrName", '') AS project_name
                FROM "{schema}"."OOCR"
                WHERE "DimCode" = 3
                  AND IFNULL("Active", 'Y') = 'Y'
                ORDER BY "OcrName", "OcrCode"
            """
        )
        return [
            {
                "project_code": row[0],
                "project_name": row[1] or row[0],
            }
            for row in cursor.fetchall()
        ]

    @staticmethod
    def _get_sub_accounts(cursor, schema: str) -> List[Dict[str, Any]]:
        cursor.execute(
            f"""
                SELECT
                    V."FldValue" AS sub_account_code,
                    IFNULL(V."Descr", V."FldValue") AS sub_account_name
                FROM "{schema}"."CUFD" C
                JOIN "{schema}"."UFD1" V
                  ON V."TableID" = C."TableID"
                 AND V."FieldID" = C."FieldID"
                WHERE C."TableID" = 'PDN1'
                  AND C."AliasID" = 'Sub_Account'
                  AND IFNULL(V."FldValue", '') <> ''
                ORDER BY V."IndexID"
            """
        )
        rows = [
            {
                "sub_account_code": row[0],
                "sub_account_name": row[1] or row[0],
            }
            for row in cursor.fetchall()
        ]
        return rows or [
            {"sub_account_code": "SALES", "sub_account_name": "SALES"},
            {"sub_account_code": "SALES RETURN", "sub_account_name": "SALES RETURN"},
            {"sub_account_code": "BST", "sub_account_name": "BST"},
        ]

    def get_expense_code_options(self) -> List[Dict[str, Any]]:
        """Just the additional-expense master (OEXD), without the other options.

        The material GRPO screen needs expense names for its freight rows but
        none of the service-GRPO dimensions, so this avoids running the other
        eight queries.
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
            return self._get_expense_codes(cursor, self.connection.schema)
        except dbapi.Error as e:
            logger.error(f"SAP HANA data error for expense codes: {e}")
            raise SAPDataError(
                "Failed to retrieve expense codes from SAP. Please try again later."
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

    @staticmethod
    def _get_expense_codes(cursor, schema: str) -> List[Dict[str, Any]]:
        cursor.execute(
            f"""
                SELECT
                    "ExpnsCode" AS expense_code,
                    IFNULL("ExpnsName", '') AS expense_name,
                    IFNULL("ExpnsAcct", '') AS expense_account,
                    IFNULL("RevAcct", '') AS revenue_account,
                    IFNULL("SacCode", '') AS sac_code
                FROM "{schema}"."OEXD"
                WHERE IFNULL("IsActive", 'Y') = 'Y'
                ORDER BY "ExpnsName", "ExpnsCode"
            """
        )
        return [
            {
                "expense_code": int(row[0]),
                "expense_name": row[1] or str(row[0]),
                "expense_account": row[2] or "",
                "revenue_account": row[3] or "",
                "sac_code": row[4] or "",
            }
            for row in cursor.fetchall()
        ]
