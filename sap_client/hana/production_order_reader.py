"""HANA reads behind the SAP production-order screens ported from SAP Portal.

SAP Portal (``backend_v1/routes/sap.js`` ~1056–1128) listed production orders
through the Service Layer and then summed what had been issued (``IGE1``) and
received (``IGN1``) against each one from HANA. This reads both from HANA,
with bound values, in two queries per page: the orders, then the movements
for exactly those orders.

``production_execution/services/sap_reader.py`` also reads ``OWOR`` for the
run screens; it is left as it is (released/open orders only) because the run
flow depends on it. This reader serves the order screens, which need every
status and the movements.

Facts the queries rely on:

* ``OWOR.Status``: ``P`` planned, ``R`` released, ``L`` closed, ``C`` cancelled.
* ``WOR1.ItemType``: ``4`` an item, ``290`` a resource (machine or labour).
* Issues and receipts point back at the order with ``BaseType = 202`` and
  ``BaseEntry = OWOR.DocEntry``; an issue line also carries ``BaseLine`` —
  the ``WOR1.LineNum`` it consumes.
"""

import logging

from hdbcli import dbapi

from .connection import HanaConnection
from ..exceptions import SAPConnectionError, SAPDataError, SAPValidationError

logger = logging.getLogger(__name__)

STATUS_LABELS = {"P": "Planned", "R": "Released", "L": "Closed", "C": "Cancelled"}
LINE_TYPE_ITEM = 4
LINE_TYPE_RESOURCE = 290
BASE_TYPE_PRODUCTION_ORDER = 202


def _clean(value) -> str:
    if value is None:
        return ""
    return value.strip() if isinstance(value, str) else str(value)


def _num(value) -> float:
    return float(value) if value is not None else 0.0


def _date(value) -> str | None:
    return value.strftime("%Y-%m-%d") if value is not None and hasattr(value, "strftime") else None


def _limit(value, default: int, ceiling: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(number, ceiling))


class HanaProductionOrderReader:
    """Production orders with what has been issued to and received from them."""

    def __init__(self, context):
        self.connection = HanaConnection(context.hana)

    def list_orders(self, status: str | None = None, search: str = "", limit: int = 50, offset: int = 0) -> dict:
        """Newest orders first; ``status`` one of P/R/L/C; ``search`` item or number."""
        where, params = [], []
        if status:
            if status not in STATUS_LABELS:
                raise SAPValidationError("Status must be P, R, L or C.")
            where.append('W."Status" = ?')
            params.append(status)
        needle = (search or "").strip().upper()
        if needle:
            where.append(
                '(UPPER(W."ItemCode") LIKE ? OR UPPER(COALESCE(W."ProdName", \'\')) LIKE ? '
                'OR CAST(W."DocNum" AS NVARCHAR(20)) LIKE ?)'
            )
            params.extend([f"%{needle}%", f"%{needle}%", f"%{needle}%"])
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        size = _limit(limit, 50, 200)
        try:
            skip = max(0, int(offset))
        except (TypeError, ValueError):
            skip = 0

        total = self._query(f'SELECT COUNT(*) FROM "{{schema}}"."OWOR" W {where_sql}', tuple(params))
        rows = self._query(
            f"""
            SELECT W."DocEntry", W."DocNum", W."ItemCode", W."ProdName", W."PlannedQty",
                   W."CmpltQty", W."RjctQty", W."Status", W."StartDate", W."DueDate",
                   W."Warehouse", I."InvntryUom"
            FROM "{{schema}}"."OWOR" W
            LEFT JOIN "{{schema}}"."OITM" I ON I."ItemCode" = W."ItemCode"
            {where_sql}
            ORDER BY W."DocEntry" DESC
            LIMIT {size} OFFSET {skip}
            """,
            tuple(params),
        )
        movements = self._movement_totals([int(row[0]) for row in rows])
        orders = []
        for entry, num, item, name, planned, completed, rejected, status_, start, due, whs, uom in rows:
            entry = int(entry)
            issued, received = movements.get(entry, (0.0, 0.0))
            orders.append(
                {
                    "doc_entry": entry,
                    "doc_num": int(num) if num is not None else None,
                    "item_code": _clean(item),
                    "item_name": _clean(name),
                    "planned_quantity": _num(planned),
                    "completed_quantity": _num(completed),
                    "rejected_quantity": _num(rejected),
                    "issued_quantity": issued,
                    "received_quantity": received,
                    "status": _clean(status_),
                    "status_label": STATUS_LABELS.get(_clean(status_), _clean(status_)),
                    "start_date": _date(start),
                    "due_date": _date(due),
                    "warehouse": _clean(whs),
                    "uom": _clean(uom),
                }
            )
        return {"count": int(total[0][0]) if total else 0, "results": orders}

    def order_detail(self, doc_entry: int) -> dict | None:
        """One order: header, component lines, and the issues and receipts made."""
        entry = int(doc_entry)
        header = self._query(
            """
            SELECT W."DocEntry", W."DocNum", W."ItemCode", W."ProdName", W."PlannedQty",
                   W."CmpltQty", W."RjctQty", W."Status", W."StartDate", W."DueDate",
                   W."Warehouse", I."InvntryUom", H."BPLid"
            FROM "{schema}"."OWOR" W
            LEFT JOIN "{schema}"."OITM" I ON I."ItemCode" = W."ItemCode"
            LEFT JOIN "{schema}"."OWHS" H ON H."WhsCode" = W."Warehouse"
            WHERE W."DocEntry" = ?
            """,
            (entry,),
        )
        if not header:
            return None
        (
            _, num, item, name, planned, completed, rejected, status_, start, due,
            whs, uom, branch,
        ) = header[0]

        lines = self._query(
            """
            SELECT C."LineNum", C."ItemCode", COALESCE(I."ItemName", R."ResName"), C."ItemType",
                   C."PlannedQty", C."IssuedQty", C."wareHouse", C."UomCode",
                   COALESCE(I."ManBtchNum", 'N')
            FROM "{schema}"."WOR1" C
            LEFT JOIN "{schema}"."OITM" I ON I."ItemCode" = C."ItemCode"
            LEFT JOIN "{schema}"."ORSC" R ON R."ResCode" = C."ItemCode"
            WHERE C."DocEntry" = ?
            ORDER BY C."LineNum"
            """,
            (entry,),
        )
        issues = self._movements("OIGE", "IGE1", entry)
        receipts = self._movements("OIGN", "IGN1", entry)
        issued, received = self._movement_totals([entry]).get(entry, (0.0, 0.0))
        return {
            "doc_entry": entry,
            "doc_num": int(num) if num is not None else None,
            "item_code": _clean(item),
            "item_name": _clean(name),
            "planned_quantity": _num(planned),
            "completed_quantity": _num(completed),
            "rejected_quantity": _num(rejected),
            "issued_quantity": issued,
            "received_quantity": received,
            "status": _clean(status_),
            "status_label": STATUS_LABELS.get(_clean(status_), _clean(status_)),
            "start_date": _date(start),
            "due_date": _date(due),
            "warehouse": _clean(whs),
            "branch_id": int(branch) if branch is not None else None,
            "uom": _clean(uom),
            "lines": [
                {
                    "line_num": int(line_num),
                    "item_code": _clean(code),
                    "item_name": _clean(line_name),
                    "is_resource": int(item_type or 0) == LINE_TYPE_RESOURCE,
                    "planned_quantity": _num(line_planned),
                    "issued_quantity": _num(line_issued),
                    "warehouse": _clean(line_whs),
                    "uom": _clean(line_uom),
                    "batch_managed": _clean(batch) == "Y",
                }
                for line_num, code, line_name, item_type, line_planned, line_issued, line_whs, line_uom, batch in lines
            ],
            "issues": issues,
            "receipts": receipts,
        }

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _movement_totals(self, entries: list[int]) -> dict[int, tuple[float, float]]:
        """``{DocEntry: (issued, received)}`` for these orders, in one query."""
        if not entries:
            return {}
        marks = ", ".join("?" for _ in entries)
        rows = self._query(
            f"""
            SELECT "BaseEntry", 'I', SUM("Quantity") FROM "{{schema}}"."IGE1"
            WHERE "BaseType" = {BASE_TYPE_PRODUCTION_ORDER} AND "BaseEntry" IN ({marks})
            GROUP BY "BaseEntry"
            UNION ALL
            SELECT "BaseEntry", 'R', SUM("Quantity") FROM "{{schema}}"."IGN1"
            WHERE "BaseType" = {BASE_TYPE_PRODUCTION_ORDER} AND "BaseEntry" IN ({marks})
            GROUP BY "BaseEntry"
            """,
            tuple(entries) + tuple(entries),
        )
        totals: dict[int, list[float]] = {}
        for base_entry, kind, quantity in rows:
            pair = totals.setdefault(int(base_entry), [0.0, 0.0])
            pair[0 if kind == "I" else 1] += _num(quantity)
        return {key: (value[0], value[1]) for key, value in totals.items()}

    def _movements(self, header_table: str, line_table: str, entry: int) -> list[dict]:
        rows = self._query(
            f"""
            SELECT H."DocEntry", H."DocNum", H."DocDate", SUM(L."Quantity"), MAX(H."Comments")
            FROM "{{schema}}"."{line_table}" L
            JOIN "{{schema}}"."{header_table}" H ON H."DocEntry" = L."DocEntry"
            WHERE L."BaseType" = {BASE_TYPE_PRODUCTION_ORDER} AND L."BaseEntry" = ?
            GROUP BY H."DocEntry", H."DocNum", H."DocDate"
            ORDER BY H."DocEntry" DESC
            """,
            (entry,),
        )
        return [
            {
                "doc_entry": int(doc_entry),
                "doc_num": int(doc_num) if doc_num is not None else None,
                "date": _date(doc_date),
                "quantity": _num(quantity),
                "comments": _clean(comments),
            }
            for doc_entry, doc_num, doc_date, quantity, comments in rows
        ]

    def _query(self, sql: str, params: tuple) -> list:
        conn = None
        cursor = None
        try:
            conn = self.connection.connect()
        except dbapi.Error as e:
            logger.error("SAP HANA connection failed while reading production orders: %s", e)
            raise SAPConnectionError("Unable to connect to SAP HANA.") from e
        try:
            cursor = conn.cursor()
            cursor.execute(sql.replace("{schema}", self.connection.schema), params)
            return cursor.fetchall()
        except dbapi.Error as e:
            logger.error("SAP HANA production-order query failed: %s", e)
            raise SAPDataError("Failed to read production orders from SAP.") from e
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
