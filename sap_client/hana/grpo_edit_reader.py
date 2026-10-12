"""One GRPO as it stands in SAP, for the screen that edits it after posting.

Read fresh from HANA every time -- the screen shows what SAP holds now, and the
posting queue reads it again just before it writes, so an edit is always made
against SAP's current values rather than the app's memory of them.

Only the fields that screen can change are read, plus what it needs to explain
why the rest cannot: the document's status and type, each line's status and the
A/P invoice that closed it, and the header's user-defined fields that exist in
this company (Beverages has no vehicle or bilty number on the GRPO header, for
one). Labels and sizes of those come from SAP's own field definitions (CUFD), so
the screen says "Bill Of Entry No." where SAP does.
"""

import logging
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from hdbcli import dbapi

from .connection import HanaConnection
from .service_grpo_options_reader import OCR_VALID_TODAY_SQL
from ..exceptions import SAPConnectionError, SAPDataError

logger = logging.getLogger(__name__)

#: OPDN user-defined fields the edit screen offers, in screen order, where the
#: company has them. People changed exactly these after posting (SAP's change
#: log, Oct 2024 - Oct 2026); the Service Layer takes all of them.
HEADER_UDFS = (
    "VehicleNoM",
    "BilltyNumber",
    "BiltyDate",
    "TransporterName",
    "LRNUmber",
    "Recv_Date",
    "UNE_CITY",
)

#: SAP's object type for an A/P invoice, as a line's TargetType holds it.
AP_INVOICE_OBJECT = 18


def _date(value) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    return value


def _decimal(value) -> Optional[Decimal]:
    if value is None:
        return None
    return Decimal(str(value))


def _text(value) -> str:
    return "" if value is None else str(value)


class HanaGRPOEditReader:
    """Reads one ``OPDN`` document and what its edit screen offers."""

    def __init__(self, context):
        self.connection = HanaConnection(context.hana)

    # ------------------------------------------------------------------
    # finding it
    # ------------------------------------------------------------------

    def find(self, doc_num: int) -> list:
        """Every GRPO with this number. Usually one; numbering series could repeat."""
        rows = self._query(
            """
            SELECT H."DocEntry", H."DocNum", H."DocType", H."DocDate",
                   IFNULL(H."CardName", ''), H."CANCELED"
            FROM "{schema}"."OPDN" H
            WHERE H."DocNum" = ?
            ORDER BY H."DocEntry" DESC
            """,
            (int(doc_num),),
        )
        return [
            {
                "doc_entry": row[0],
                "doc_num": row[1],
                "doc_type": row[2],
                "doc_date": _date(row[3]),
                "card_name": row[4],
                "cancelled": row[5] != "N",
            }
            for row in rows
        ]

    # ------------------------------------------------------------------
    # the document
    # ------------------------------------------------------------------

    def header_udfs(self) -> list:
        """The offered header UDFs this company has, with SAP's label, type and size."""
        rows = self._query(
            """
            SELECT C."AliasID", IFNULL(C."Descr", C."AliasID"), C."TypeID",
                   IFNULL(C."EditSize", 0)
            FROM "{schema}"."CUFD" C
            WHERE C."TableID" = 'OPDN' AND C."AliasID" IN ({aliases})
            """.replace("{aliases}", ", ".join("?" for _ in HEADER_UDFS)),
            HEADER_UDFS,
        )
        found = {
            row[0]: {
                "column": f"U_{row[0]}",
                "label": row[1],
                # SAP's TypeID: A alphanumeric, D date, N integer, B amount.
                "type": {"D": "date", "N": "integer", "B": "number"}.get(row[2], "text"),
                "max_length": int(row[3] or 0) if row[2] == "A" else None,
            }
            for row in rows
        }
        return [found[alias] for alias in HEADER_UDFS if alias in found]

    def document(self, doc_entry: int, udfs: Optional[list] = None) -> Optional[dict]:
        """The GRPO's editable state, or ``None`` when the company has no such document."""
        doc_entry = int(doc_entry)
        if udfs is None:
            udfs = self.header_udfs()
        udf_select = "".join(f', H."{udf["column"]}"' for udf in udfs)
        rows = self._query(
            f"""
            SELECT H."DocEntry", H."DocNum", H."DocType", H."DocStatus", H."CANCELED",
                   H."DocDate", H."DocDueDate", H."TaxDate",
                   IFNULL(H."CardCode", ''), IFNULL(H."CardName", ''),
                   IFNULL(H."NumAtCard", ''), IFNULL(H."Comments", ''),
                   H."CntctCode", H."AtcEntry", H."BPLId", IFNULL(H."BPLName", ''),
                   H."DocTotal", H."VatSum", IFNULL(U."USER_CODE", ''), IFNULL(U."U_NAME", ''),
                   H."CreateDate", H."UpdateDate"{udf_select}
            FROM "{{schema}}"."OPDN" H
            LEFT JOIN "{{schema}}"."OUSR" U ON U."USERID" = H."UserSign"
            WHERE H."DocEntry" = ?
            """,
            (doc_entry,),
        )
        if not rows:
            return None
        row = rows[0]
        udf_values = {}
        for index, udf in enumerate(udfs):
            value = row[22 + index]
            if udf["type"] == "date":
                value = _date(value)
            elif udf["type"] == "number":
                value = _decimal(value)
            udf_values[udf["column"]] = value
        header = {
            "doc_entry": row[0],
            "doc_num": row[1],
            "doc_type": row[2],
            "doc_status": row[3],
            "cancelled": row[4] != "N",
            "doc_date": _date(row[5]),
            "delivery_date": _date(row[6]),
            "document_date": _date(row[7]),
            "card_code": row[8],
            "card_name": row[9],
            "vendor_ref": row[10],
            "remarks": row[11],
            "contact_person": row[12],
            "attachment_entry": row[13] or None,
            "branch_id": row[14],
            "branch_name": row[15],
            "doc_total": _decimal(row[16]),
            "vat_sum": _decimal(row[17]),
            "created_by_code": row[18],
            "created_by_name": row[19],
            "created_on": _date(row[20]),
            "updated_on": _date(row[21]),
            "udfs": udf_values,
        }
        header["lines"] = self._lines(doc_entry)
        header["attachments"] = (
            self._attachments(header["attachment_entry"]) if header["attachment_entry"] else []
        )
        return header

    def _lines(self, doc_entry: int) -> list:
        rows = self._query(
            """
            SELECT L."LineNum", L."VisOrder", IFNULL(L."ItemCode", ''), IFNULL(L."Dscription", ''),
                   L."Quantity", L."PriceBefDi", L."DiscPrcnt", L."LineTotal", L."VatSum",
                   IFNULL(L."TaxCode", ''), IFNULL(L."AcctCode", ''),
                   IFNULL(L."OcrCode", ''), IFNULL(L."OcrCode2", ''), IFNULL(L."OcrCode3", ''),
                   L."LineStatus", L."TargetType", L."TrgetEntry", P."DocNum"
            FROM "{schema}"."PDN1" L
            LEFT JOIN "{schema}"."OPCH" P
                   ON L."TargetType" = 18 AND P."DocEntry" = L."TrgetEntry"
            WHERE L."DocEntry" = ?
            ORDER BY L."VisOrder", L."LineNum"
            """,
            (doc_entry,),
        )
        return [
            {
                "line_num": row[0],
                "item_code": row[2],
                "description": row[3],
                "quantity": _decimal(row[4]),
                "unit_price": _decimal(row[5]),
                "discount_percent": _decimal(row[6]),
                "amount": _decimal(row[7]),
                "tax_amount": _decimal(row[8]),
                "tax_code": row[9],
                "gl_account": row[10],
                "variety": row[11],
                "month": row[12],
                "budget": row[13],
                "line_status": row[14],
                "ap_invoice_doc_num": row[17] if row[15] == AP_INVOICE_OBJECT else None,
            }
            for row in rows
        ]

    def attachments(self, absolute_entry: int) -> list:
        """The files of one SAP attachment entry, in line order."""
        return self._attachments(absolute_entry)

    def attachment_shared_with(self, absolute_entry: int, doc_entry: int) -> list:
        """Other documents that point at the same attachment entry.

        A GRPO re-entered from a cancelled one ("Based On Goods Receipt PO ...")
        keeps the original's entry, so a file removed from it would vanish from
        the cancelled original too. No two live GRPOs share one (Oct 2026).
        """
        rows = self._query(
            """
            SELECT 'GRPO', H."DocNum", H."CANCELED" FROM "{schema}"."OPDN" H
             WHERE H."AtcEntry" = ? AND H."DocEntry" <> ?
            UNION ALL
            SELECT 'Draft', H."DocEntry", 'N' FROM "{schema}"."ODRF" H WHERE H."AtcEntry" = ?
            UNION ALL
            SELECT 'Purchase order', H."DocNum", H."CANCELED" FROM "{schema}"."OPOR" H
             WHERE H."AtcEntry" = ?
            UNION ALL
            SELECT 'A/P invoice', H."DocNum", H."CANCELED" FROM "{schema}"."OPCH" H
             WHERE H."AtcEntry" = ?
            UNION ALL
            SELECT 'Goods return', H."DocNum", H."CANCELED" FROM "{schema}"."ORPD" H
             WHERE H."AtcEntry" = ?
            """,
            (int(absolute_entry), int(doc_entry), int(absolute_entry), int(absolute_entry),
             int(absolute_entry), int(absolute_entry)),
        )
        return [
            {"document": row[0], "doc_num": row[1], "cancelled": row[2] != "N"} for row in rows
        ]

    def _attachments(self, absolute_entry: int) -> list:
        rows = self._query(
            """
            SELECT A."Line", IFNULL(A."FileName", ''), IFNULL(A."FileExt", ''), A."Date"
            FROM "{schema}"."ATC1" A
            WHERE A."AbsEntry" = ?
            ORDER BY A."Line"
            """,
            (int(absolute_entry),),
        )
        return [
            {
                "line": row[0],
                "filename": f"{row[1]}.{row[2]}" if row[2] else row[1],
                "date": _date(row[3]),
            }
            for row in rows
        ]

    # ------------------------------------------------------------------
    # choices
    # ------------------------------------------------------------------

    def contact_persons(self, card_code: str) -> list:
        rows = self._query(
            """
            SELECT C."CntctCode", IFNULL(C."Name", ''), IFNULL(C."Active", 'Y')
            FROM "{schema}"."OCPR" C
            WHERE C."CardCode" = ?
            ORDER BY C."Name"
            """,
            (card_code,),
        )
        return [{"code": row[0], "name": row[1], "active": row[2] == "Y"} for row in rows]

    def month_codes(self) -> list:
        """Dimension 2 -- the expense's effective month -- as SAP will take it today."""
        rows = self._query(
            f"""
            SELECT O."OcrCode", IFNULL(O."OcrName", O."OcrCode")
            FROM "{{schema}}"."OOCR" O
            WHERE O."DimCode" = 2
              AND IFNULL(O."Active", 'Y') = 'Y'
              AND {OCR_VALID_TODAY_SQL}
            """,
            (),
        )
        codes = [{"code": row[0], "name": row[1]} for row in rows if row[0]]

        def newest_first(code):
            # "09-2026" sorts as text into a jumble of years; sort by the month.
            month, _, year = code["code"].partition("-")
            if month.isdigit() and year.isdigit():
                return (0, -int(year), -int(month))
            return (1, 0, 0)

        return sorted(codes, key=lambda code: (newest_first(code), code["code"]))

    # ------------------------------------------------------------------
    # plumbing
    # ------------------------------------------------------------------

    def _query(self, sql: str, params: tuple) -> list:
        conn = None
        cursor = None
        try:
            conn = self.connection.connect()
        except dbapi.Error as e:
            logger.error("SAP HANA connection failed while reading a GRPO to edit: %s", e)
            raise SAPConnectionError("Unable to connect to SAP HANA.") from e

        try:
            cursor = conn.cursor()
            cursor.execute(sql.replace("{schema}", self.connection.schema), params)
            return cursor.fetchall()
        except dbapi.Error as e:
            logger.error("SAP HANA query for a GRPO to edit failed: %s", e)
            raise SAPDataError("Failed to read the GRPO from SAP.") from e
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
