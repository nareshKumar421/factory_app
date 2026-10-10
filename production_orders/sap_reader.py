"""HANA reads behind production order entries.

Everything a step checks before writing comes from here, and so does the
read-back that makes a step safe to send again: SAP's rules let an order have
one issue and one receipt, so "does this order already have an issue?" is a
complete answer after a timeout. The order itself is found by the entry's
reference, which the app writes into ``OWOR.Comments``.

Every read raises when SAP cannot be read (``SAPConnectionError`` when HANA is
unreachable, ``SAPDataError`` when a query fails): "could not tell" must never
read as "not there", or a retry would post twice.
"""

import logging
from decimal import Decimal

from hdbcli import dbapi

from sap_client.exceptions import SAPConnectionError, SAPDataError
from sap_client.hana.connection import HanaConnection

logger = logging.getLogger(__name__)


def _text(value) -> str:
    if value is None:
        return ""
    return value.strip() if isinstance(value, str) else str(value)


def _decimal(value) -> Decimal:
    if value is None:
        return Decimal("0")
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _day(value):
    """A HANA date column (a datetime at midnight) as a date."""
    return value.date() if hasattr(value, "date") else value


def _item(row) -> dict:
    (code, name, uom, per_box, litres, batch, sub_group, is_litre, group, frozen,
     variety, bom_warehouse, bom_quantity, tree_type) = row
    return {
        "item_code": _text(code),
        "item_name": _text(name),
        "uom": _text(uom),
        "pieces_per_box": _decimal(per_box),
        "litres_per_piece": _decimal(litres) if (_text(is_litre) == "Y" and litres is not None) else None,
        "batch_managed": _text(batch) == "Y",
        "sub_group": _text(sub_group),
        "item_group": int(group) if group is not None else None,
        "frozen": _text(frozen) == "Y",
        "variety": _text(variety),
        "bom_warehouse": _text(bom_warehouse),
        "bom_quantity": _decimal(bom_quantity) if bom_quantity is not None else None,
        "bom_type": _text(tree_type),
    }


_ITEM_COLUMNS = """
    I."ItemCode",
    I."ItemName",
    IFNULL(I."InvntryUom", ''),
    I."SalFactor2",
    I."SalPackUn",
    I."ManBtchNum",
    IFNULL(I."U_Sub_Group", ''),
    IFNULL(I."U_IsLitre", ''),
    I."ItmsGrpCod",
    I."frozenFor",
    (SELECT MIN(R."OcrCode") FROM "{schema}"."OOCR" R
      WHERE R."DimCode" = 1 AND R."Active" = 'Y'
        AND (R."OcrName" = I."U_Sub_Group" OR R."OcrCode" = I."U_Sub_Group")),
    IFNULL(T."ToWH", ''),
    T."Qauntity",
    IFNULL(T."TreeType", '')
"""


class ProductionOrderReader:
    """SAP reads for one company's production order entries."""

    def __init__(self, context):
        self.connection = HanaConnection(context.hana)

    # ------------------------------------------------------------------
    # Items, stock and batches
    # ------------------------------------------------------------------

    def search_products(self, item_group: int, search: str = "", limit: int = 30) -> list[dict]:
        """Items of ``item_group`` with a production BOM, by code or name."""
        token = (search or "").strip().upper()
        like = f"%{token}%"
        rows = self._query(
            f"""
            SELECT TOP {max(1, min(int(limit), 100))} {_ITEM_COLUMNS}
            FROM "{{schema}}"."OITM" I
            JOIN "{{schema}}"."OITT" T ON T."Code" = I."ItemCode" AND T."TreeType" = 'P'
            WHERE I."ItmsGrpCod" = ?
              AND I."frozenFor" = 'N'
              AND (? = '' OR UPPER(I."ItemCode") LIKE ? OR UPPER(I."ItemName") LIKE ?)
            ORDER BY I."ItemName"
            """,
            (int(item_group), token, like, like),
        )
        return [_item(row) for row in rows]

    def product(self, item_code: str) -> dict | None:
        """One item, with its BOM's size and warehouse and its variety code."""
        rows = self._query(
            f"""
            SELECT {_ITEM_COLUMNS}
            FROM "{{schema}}"."OITM" I
            LEFT JOIN "{{schema}}"."OITT" T ON T."Code" = I."ItemCode"
            WHERE I."ItemCode" = ?
            """,
            (str(item_code),),
        )
        return _item(rows[0]) if rows else None

    def varieties(self) -> list[dict]:
        """Active distribution rules of dimension 1 (the "variety")."""
        rows = self._query(
            """
            SELECT "OcrCode", "OcrName" FROM "{schema}"."OOCR"
            WHERE "DimCode" = 1 AND "Active" = 'Y' ORDER BY "OcrName"
            """,
            (),
        )
        return [{"code": _text(code), "name": _text(name)} for code, name in rows]

    def batch_managed_flags(self, item_codes) -> dict[str, bool]:
        codes = sorted({str(c) for c in item_codes if c})
        if not codes:
            return {}
        marks = ", ".join("?" for _ in codes)
        rows = self._query(
            f'SELECT "ItemCode", "ManBtchNum" FROM "{{schema}}"."OITM" WHERE "ItemCode" IN ({marks})',
            tuple(codes),
        )
        return {_text(code): _text(flag) == "Y" for code, flag in rows}

    def on_hand(self, pairs) -> dict[tuple[str, str], Decimal]:
        """``OITW.OnHand`` for each (item, warehouse); missing pairs read as 0."""
        pairs = sorted({(str(i), str(w)) for i, w in pairs if i and w})
        if not pairs:
            return {}
        items = sorted({i for i, _ in pairs})
        warehouses = sorted({w for _, w in pairs})
        rows = self._query(
            f"""
            SELECT "ItemCode", "WhsCode", "OnHand" FROM "{{schema}}"."OITW"
            WHERE "ItemCode" IN ({", ".join("?" for _ in items)})
              AND "WhsCode" IN ({", ".join("?" for _ in warehouses)})
            """,
            (*items, *warehouses),
        )
        found = {(_text(i), _text(w)): _decimal(q) for i, w, q in rows}
        return {pair: found.get(pair, Decimal("0")) for pair in pairs}

    def litres_by_warehouse(self, warehouses) -> list[dict]:
        """Litres held (``OnHand × SalPackUn`` of litre items), split by
        warehouse, item series and item group, for SAP's stock caps."""
        warehouses = sorted({str(w) for w in warehouses if w})
        if not warehouses:
            return []
        rows = self._query(
            f"""
            SELECT W."WhsCode", I."Series", I."ItmsGrpCod", SUM(W."OnHand" * I."SalPackUn")
            FROM "{{schema}}"."OITW" W
            JOIN "{{schema}}"."OITM" I ON I."ItemCode" = W."ItemCode"
            WHERE W."WhsCode" IN ({", ".join("?" for _ in warehouses)})
              AND W."OnHand" > 0
              AND I."U_IsLitre" = 'Y'
            GROUP BY W."WhsCode", I."Series", I."ItmsGrpCod"
            """,
            tuple(warehouses),
        )
        return [
            {
                "warehouse": _text(whs),
                "series": int(series) if series is not None else None,
                "group": int(group) if group is not None else None,
                "litres": _decimal(litres),
            }
            for whs, series, group, litres in rows
        ]

    def batch_numbers_starting(self, item_code: str, stem: str) -> list[str]:
        """Batch numbers SAP holds for ``item_code`` that begin with ``stem``.

        ``stem`` is built from a line code, digits and a date, so it holds no
        LIKE wildcards.
        """
        rows = self._query(
            'SELECT "DistNumber" FROM "{schema}"."OBTN" WHERE "ItemCode" = ? AND "DistNumber" LIKE ?',
            (str(item_code), f"{stem}%"),
        )
        return [_text(number) for (number,) in rows]

    def batch_exists(self, item_code: str, batch_number: str) -> bool:
        rows = self._query(
            'SELECT COUNT(*) FROM "{schema}"."OBTN" WHERE "ItemCode" = ? AND "DistNumber" = ?',
            (str(item_code), str(batch_number)),
        )
        return bool(rows and rows[0][0])

    def branch_of(self, warehouse: str) -> int | None:
        rows = self._query(
            'SELECT "BPLid" FROM "{schema}"."OWHS" WHERE "WhsCode" = ?', (str(warehouse),)
        )
        return int(rows[0][0]) if rows and rows[0][0] is not None else None

    # ------------------------------------------------------------------
    # Read-back of what a step posted
    # ------------------------------------------------------------------

    def order_by_reference(self, item_code: str, reference: str) -> dict | None:
        """The order the app posted for an entry, found by its Comments stamp."""
        rows = self._query(
            """
            SELECT "DocEntry", "DocNum", "Status"
            FROM "{schema}"."OWOR"
            WHERE "ItemCode" = ?
              AND ("Comments" = ? OR "Comments" LIKE ?)
              AND "Status" <> 'C'
            ORDER BY "DocEntry" DESC
            """,
            (str(item_code), reference, f"{reference} %"),
        )
        if not rows:
            return None
        doc_entry, doc_num, status = rows[0]
        return {"doc_entry": int(doc_entry), "doc_num": int(doc_num), "status": _text(status)}

    def order(self, doc_entry: int) -> dict | None:
        """An order's status and lines, in the order they were sent."""
        rows = self._query(
            """
            SELECT H."DocNum", H."Status", H."PlannedQty", H."CmpltQty",
                   L."LineNum", L."VisOrder", L."ItemCode", L."BaseQty", L."PlannedQty",
                   L."IssuedQty", IFNULL(L."wareHouse", ''), L."ItemType"
            FROM "{schema}"."OWOR" H
            LEFT JOIN "{schema}"."WOR1" L ON L."DocEntry" = H."DocEntry"
            WHERE H."DocEntry" = ?
            ORDER BY L."VisOrder", L."LineNum"
            """,
            (int(doc_entry),),
        )
        if not rows:
            return None
        head = rows[0]
        order = {
            "doc_entry": int(doc_entry),
            "doc_num": int(head[0]),
            "status": _text(head[1]),
            "planned_quantity": _decimal(head[2]),
            "completed_quantity": _decimal(head[3]),
            "lines": [],
        }
        for row in rows:
            line_num, visual_order, code, base, planned, issued, warehouse, item_type = row[4:]
            if line_num is None:
                continue
            order["lines"].append(
                {
                    "line_num": int(line_num),
                    "visual_order": int(visual_order) if visual_order is not None else None,
                    "item_code": _text(code),
                    "base_quantity": _decimal(base),
                    "planned_quantity": _decimal(planned),
                    "issued_quantity": _decimal(issued),
                    "warehouse": _text(warehouse),
                    "item_type": int(item_type) if item_type is not None else None,
                }
            )
        return order

    def issue_for(self, order_entry: int) -> dict | None:
        """The (not cancelled) issue for production posted against an order."""
        return self._document_for("OIGE", "IGE1", order_entry)

    def receipt_for(self, order_entry: int) -> dict | None:
        """The (not cancelled) receipt from production posted against an order."""
        return self._document_for("OIGN", "IGN1", order_entry)

    def _document_for(self, header: str, lines: str, order_entry: int) -> dict | None:
        rows = self._query(
            f"""
            SELECT H."DocEntry", H."DocNum"
            FROM "{{schema}}"."{header}" H
            WHERE H."CANCELED" = 'N'
              AND EXISTS (
                SELECT 1 FROM "{{schema}}"."{lines}" L
                WHERE L."DocEntry" = H."DocEntry" AND L."BaseType" = 202 AND L."BaseEntry" = ?
              )
            ORDER BY H."DocEntry"
            """,
            (int(order_entry),),
        )
        if not rows:
            return None
        return {"doc_entry": int(rows[0][0]), "doc_num": int(rows[0][1])}

    # ------------------------------------------------------------------
    # SAP's own list of orders
    # ------------------------------------------------------------------

    def sap_orders(self, *, status="", order_type="", date_from=None, date_to=None,
                   search="", limit=50, offset=0) -> dict:
        """Every production order in SAP, whoever made it and however, newest first.

        ``status_counts`` counts each status under the other filters (for the
        tabs); ``count`` is the rows ``status`` leaves. Each row says how much of
        its components' planned quantity has been issued.
        """
        where, params = [], []
        if order_type:
            where.append('H."Type" = ?')
            params.append(order_type)
        if date_from:
            where.append('H."PostDate" >= ?')
            params.append(date_from)
        if date_to:
            where.append('H."PostDate" <= ?')
            params.append(date_to)
        token = (search or "").strip().upper()
        if token:
            like = f"%{token}%"
            where.append(
                '(UPPER(H."ItemCode") LIKE ? OR UPPER(H."ProdName") LIKE ?'
                ' OR CAST(H."DocNum" AS NVARCHAR(20)) LIKE ?)'
            )
            params += [like, like, like]
        filters = " AND ".join(where) or "1 = 1"
        counts = self._query(
            f'SELECT H."Status", COUNT(*) FROM "{{schema}}"."OWOR" H WHERE {filters} GROUP BY H."Status"',
            tuple(params),
        )
        status_counts = {_text(code): int(n) for code, n in counts}
        if status:
            filters += ' AND H."Status" = ?'
            params.append(status)
        rows = self._query(
            f"""
            SELECT H."DocEntry", H."DocNum", H."Type", H."Status", H."ItemCode",
                   IFNULL(H."ProdName", ''), H."PlannedQty", H."CmpltQty", IFNULL(H."Uom", ''),
                   IFNULL(H."Warehouse", ''), H."PostDate", H."CloseDate",
                   IFNULL(U."U_NAME", ''), IFNULL(U."USER_CODE", '')
            FROM "{{schema}}"."OWOR" H
            LEFT JOIN "{{schema}}"."OUSR" U ON U."USERID" = H."UserSign"
            WHERE {filters}
            ORDER BY H."DocEntry" DESC
            LIMIT ? OFFSET ?
            """,
            (*params, max(1, int(limit)), max(0, int(offset))),
        )
        issued = {}
        if rows:
            entries = [int(row[0]) for row in rows]
            for doc_entry, issued_qty, planned_qty in self._query(
                f"""
                SELECT "DocEntry", SUM("IssuedQty"), SUM("PlannedQty")
                FROM "{{schema}}"."WOR1"
                WHERE "ItemType" = 4 AND "DocEntry" IN ({", ".join("?" for _ in entries)})
                GROUP BY "DocEntry"
                """,
                tuple(entries),
            ):
                issued[int(doc_entry)] = (_decimal(issued_qty), _decimal(planned_qty))
        results = []
        for row in rows:
            (doc_entry, doc_num, kind, state, code, name, planned, completed, uom, warehouse,
             posted, closed, user_name, user_code) = row
            issued_qty, issued_of = issued.get(int(doc_entry), (Decimal("0"), Decimal("0")))
            results.append(
                {
                    "doc_entry": int(doc_entry),
                    "doc_num": int(doc_num),
                    "type": _text(kind),
                    "status": _text(state),
                    "item_code": _text(code),
                    "item_name": _text(name),
                    "planned_quantity": _decimal(planned),
                    "completed_quantity": _decimal(completed),
                    "uom": _text(uom),
                    "warehouse": _text(warehouse),
                    "posting_date": _day(posted),
                    "close_date": _day(closed),
                    "created_by": _text(user_name),
                    "sap_user": _text(user_code),
                    "issued_quantity": issued_qty,
                    "issued_of": issued_of,
                }
            )
        count = status_counts.get(status, 0) if status else sum(status_counts.values())
        return {"count": count, "status_counts": status_counts, "results": results}

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _query(self, sql: str, params: tuple) -> list:
        conn = None
        cursor = None
        try:
            conn = self.connection.connect()
        except dbapi.Error as e:
            logger.error("SAP HANA connection failed (production orders): %s", e)
            raise SAPConnectionError("Unable to connect to SAP HANA.") from e
        try:
            cursor = conn.cursor()
            cursor.execute(sql.replace("{schema}", self.connection.schema), params)
            return cursor.fetchall()
        except dbapi.Error as e:
            logger.error("SAP HANA query failed (production orders): %s", e)
            raise SAPDataError("Failed to read production order data from SAP.") from e
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
