import logging
import math
from datetime import date
from typing import Optional

from hdbcli import dbapi

from .connection import HanaConnection
from ..exceptions import SAPConnectionError, SAPDataError

logger = logging.getLogger(__name__)


class HanaStockTransferReader:
    """Read SAP Business One inventory transfers from OWTR/WTR1."""

    def __init__(self, context):
        self.connection = HanaConnection(context.hana)

    def list_transfers(
        self,
        search: Optional[str] = None,
        from_date: Optional[date] = None,
        to_date: Optional[date] = None,
        limit: int = 50,
    ) -> list[dict]:
        conn = None
        cursor = None

        try:
            conn = self.connection.connect()
        except dbapi.Error as e:
            logger.error("SAP HANA connection failed while reading stock transfers: %s", e)
            raise SAPConnectionError("Unable to connect to SAP HANA.") from e

        try:
            cursor = conn.cursor()
            schema = self.connection.schema
            safe_limit = max(1, min(int(limit or 50), 100))

            where = ['T0."CANCELED" = ?']
            params: list = ["N"]

            if from_date:
                where.append('T0."DocDate" >= ?')
                params.append(from_date)
            if to_date:
                where.append('T0."DocDate" <= ?')
                params.append(to_date)
            if search:
                # BST looks up a transfer purely by its invoice/document number:
                # the SAP DocNum or its reference (NumAtCard).
                term = f"%{search.lower()}%"
                where.append(
                    """(
                        LOWER(TO_NVARCHAR(T0."DocNum")) LIKE ?
                        OR LOWER(IFNULL(T0."NumAtCard", '')) LIKE ?
                    )"""
                )
                params.extend([term, term])

            query = f"""
                SELECT
                    T0."DocEntry",
                    T0."DocNum",
                    T0."DocDate",
                    T0."TaxDate",
                    IFNULL(T0."DocStatus", ''),
                    IFNULL(T0."Filler", ''),
                    IFNULL(T0."ToWhsCode", ''),
                    IFNULL(T0."Comments", ''),
                    IFNULL(T0."NumAtCard", ''),
                    T0."BPLId",
                    COUNT(T1."LineNum") AS line_count,
                    SUM(T1."Quantity") AS total_quantity
                FROM "{schema}"."OWTR" T0
                JOIN "{schema}"."WTR1" T1 ON T0."DocEntry" = T1."DocEntry"
                WHERE {" AND ".join(where)}
                GROUP BY
                    T0."DocEntry", T0."DocNum", T0."DocDate", T0."TaxDate",
                    T0."DocStatus", T0."Filler", T0."ToWhsCode",
                    T0."Comments", T0."NumAtCard", T0."BPLId"
                ORDER BY T0."DocDate" DESC, T0."DocNum" DESC
                LIMIT {safe_limit}
            """

            cursor.execute(query, tuple(params))
            return [self._header_from_row(row) for row in cursor.fetchall()]
        except dbapi.Error as e:
            logger.error("SAP HANA stock transfer list query failed: %s", e)
            raise SAPDataError("Failed to retrieve stock transfers from SAP.") from e
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

    def get_transfer(self, doc_entry: int) -> Optional[dict]:
        conn = None
        cursor = None

        try:
            conn = self.connection.connect()
        except dbapi.Error as e:
            logger.error("SAP HANA connection failed while reading stock transfer: %s", e)
            raise SAPConnectionError("Unable to connect to SAP HANA.") from e

        try:
            cursor = conn.cursor()
            schema = self.connection.schema

            cursor.execute(
                f"""
                SELECT
                    T0."DocEntry",
                    T0."DocNum",
                    T0."DocDate",
                    T0."TaxDate",
                    IFNULL(T0."DocStatus", ''),
                    IFNULL(T0."Filler", ''),
                    IFNULL(T0."ToWhsCode", ''),
                    IFNULL(T0."Comments", ''),
                    IFNULL(T0."NumAtCard", ''),
                    T0."BPLId",
                    COUNT(T1."LineNum") AS line_count,
                    SUM(T1."Quantity") AS total_quantity
                FROM "{schema}"."OWTR" T0
                JOIN "{schema}"."WTR1" T1 ON T0."DocEntry" = T1."DocEntry"
                WHERE T0."DocEntry" = ? AND T0."CANCELED" = 'N'
                GROUP BY
                    T0."DocEntry", T0."DocNum", T0."DocDate", T0."TaxDate",
                    T0."DocStatus", T0."Filler", T0."ToWhsCode",
                    T0."Comments", T0."NumAtCard", T0."BPLId"
                """,
                (doc_entry,),
            )
            header_row = cursor.fetchone()
            if not header_row:
                return None

            transfer = self._header_from_row(header_row)

            cursor.execute(
                f"""
                SELECT
                    T1."LineNum",
                    IFNULL(T1."ItemCode", ''),
                    IFNULL(T1."Dscription", ''),
                    T1."Quantity",
                    IFNULL(T1."unitMsr", ''),
                    IFNULL(T1."FromWhsCod", ''),
                    IFNULL(T1."WhsCode", ''),
                    CASE WHEN IFNULL(I."SalFactor2", 0) > 0
                         THEN IFNULL(I."SalFactor2", 0) ELSE 1 END
                FROM "{schema}"."WTR1" T1
                LEFT JOIN "{schema}"."OITM" I ON I."ItemCode" = T1."ItemCode"
                WHERE T1."DocEntry" = ?
                ORDER BY T1."LineNum"
                """,
                (doc_entry,),
            )
            transfer["lines"] = [self._line_from_row(row) for row in cursor.fetchall()]
            return transfer
        except dbapi.Error as e:
            logger.error("SAP HANA stock transfer detail query failed: %s", e)
            raise SAPDataError("Failed to retrieve stock transfer from SAP.") from e
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
    def _header_from_row(row) -> dict:
        doc_date = row[2].date() if hasattr(row[2], "date") else row[2]
        tax_date = row[3].date() if hasattr(row[3], "date") else row[3]
        return {
            "doc_entry": int(row[0]),
            "doc_num": str(row[1]),
            "doc_date": doc_date,
            "tax_date": tax_date,
            "doc_status": row[4] or "",
            "from_warehouse": row[5] or "",
            "to_warehouse": row[6] or "",
            "comments": row[7] or "",
            "reference": row[8] or "",
            "branch_id": int(row[9]) if row[9] is not None else None,
            "line_count": int(row[10] or 0),
            "total_quantity": float(row[11] or 0),
        }

    @staticmethod
    def _line_from_row(row) -> dict:
        quantity = float(row[3] or 0)
        # Pieces per carton = OITM.SalFactor2 — the same "pieces per sales case"
        # the sales-dispatch docking flow uses (see dispatch_plans hana_reader
        # _sales_pack_size_expr). A physical box the warehouse scans is one case,
        # so the bill's box count is the line quantity (in PCS) / SalFactor2,
        # rounded up like docking's ceil(quantity / packSize).
        pcs_per_carton = float(row[7] or 0) or 1.0
        box_count = math.ceil(round(quantity / pcs_per_carton, 4)) if pcs_per_carton else 0
        return {
            "line_num": int(row[0]),
            "item_code": row[1] or "",
            "item_name": row[2] or "",
            "quantity": quantity,
            "uom": row[4] or "",
            "from_warehouse": row[5] or "",
            "to_warehouse": row[6] or "",
            "pcs_per_carton": pcs_per_carton,
            "box_count": int(box_count),
        }
