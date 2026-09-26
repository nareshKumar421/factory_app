"""HANA reads behind the BOM change screens ported from SAP Portal.

A bill of materials is ``OITT`` (one row per tree, keyed by the parent item's
code) with its lines in ``ITT1`` (``Father`` = the tree's code). The portal read
them through the Service Layer instead (``backend_v1/routes/sap.js`` lines
799-839): its search fetched the first 100 trees and filtered those in
JavaScript, so a tree past the hundredth could never be found. Here the search
runs in SQL with the text bound, over every tree.

Columns, as SAP B1 names them::

    OITT."Code"       TreeCode             ITT1."Father"    the tree
    OITT."TreeType"   P/S/A/T              ITT1."ChildNum"  line key
    OITT."Qauntity"   Quantity (SAP typo)  ITT1."VisOrder"  VisualOrder
    OITT."Name"       ProductDescription   ITT1."Type"      4 item, 290 resource, -18 text
    OITT."ToWH"       Warehouse            ITT1."Code"      ItemCode
    OITT."OcrCode"    DistributionRule     ITT1."Quantity"  Quantity
    OITT."Project"    Project              ITT1."Warehouse" Warehouse
    OITT."PriceList"  PriceList            ITT1."IssueMthd" M manual, B backflush
                                           ITT1."Price" / "Currency" / "Comment" / "Uom"

Every read raises when SAP cannot be read: the existence check gates a POST
and the snapshot gates a PUT, so "could not tell" must never read as "no tree".
"""

import logging
from decimal import Decimal

from hdbcli import dbapi

from ..exceptions import SAPConnectionError, SAPDataError
from .connection import HanaConnection

logger = logging.getLogger(__name__)

#: ``ITT1."Type"``.
LINE_TYPE_ITEM = 4
LINE_TYPE_RESOURCE = 290

TREE_TYPES = {
    "P": ("Production", "iProductionTree"),
    "S": ("Sales", "iSalesTree"),
    "A": ("Assembly", "iAssemblyTree"),
    "T": ("Template", "iTemplateTree"),
}
ISSUE_METHODS = {"M": "Manual", "B": "Backflush"}


def _clean(value) -> str:
    if value is None:
        return ""
    return value.strip() if isinstance(value, str) else str(value)


def _number(value) -> float:
    if value is None:
        return 0.0
    return float(value if not isinstance(value, Decimal) else value)


def _date(value):
    return value.strftime("%Y-%m-%d") if value is not None and hasattr(value, "strftime") else None


def _limit(value, default: int, ceiling: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(number, ceiling))


def _tree_type(code) -> dict:
    code = _clean(code).upper()
    label, sap = TREE_TYPES.get(code, (code or "", ""))
    return {"tree_type": code, "bom_type": label, "sap_tree_type": sap}


def _line_type(value) -> str:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return "item"
    if number == LINE_TYPE_RESOURCE:
        return "resource"
    if number == LINE_TYPE_ITEM:
        return "item"
    return "text"


class HanaBOMReader:
    """Trees and their lines for one company."""

    def __init__(self, context):
        self.connection = HanaConnection(context.hana)

    def search_trees(self, search: str = "", limit: int = 50) -> list[dict]:
        """Trees whose code, description or parent item name contains ``search``.

        An empty search lists the first trees by code (the portal's "List All").
        Each row carries its item and resource line counts, in the same query.
        """
        token = (search or "").strip().upper()
        like = f"%{token}%"
        rows = self._query(
            """
            SELECT TOP {limit}
                T."Code",
                COALESCE(NULLIF(T."Name", ''), M."ItemName", '') AS "Description",
                T."TreeType",
                T."Qauntity",
                IFNULL(T."ToWH", ''),
                IFNULL(C."Items", 0),
                IFNULL(C."Resources", 0),
                T."UpdateDate"
            FROM "{schema}"."OITT" T
            LEFT JOIN "{schema}"."OITM" M ON M."ItemCode" = T."Code"
            LEFT JOIN (
                SELECT "Father",
                       SUM(CASE WHEN "Type" = 4 THEN 1 ELSE 0 END) AS "Items",
                       SUM(CASE WHEN "Type" = 290 THEN 1 ELSE 0 END) AS "Resources"
                FROM "{schema}"."ITT1"
                GROUP BY "Father"
            ) C ON C."Father" = T."Code"
            WHERE ? = ''
               OR UPPER(T."Code") LIKE ?
               OR UPPER(IFNULL(T."Name", '')) LIKE ?
               OR UPPER(IFNULL(M."ItemName", '')) LIKE ?
            ORDER BY T."Code"
            """,
            (token, like, like, like),
            limit=_limit(limit, 50, 200),
        )
        return [
            {
                "tree_code": _clean(code),
                "description": _clean(description),
                **_tree_type(tree_type),
                "quantity": _number(quantity),
                "warehouse": _clean(warehouse),
                "item_count": int(items or 0),
                "resource_count": int(resources or 0),
                "updated_at": _date(updated),
            }
            for code, description, tree_type, quantity, warehouse, items, resources, updated in rows
        ]

    def get_tree(self, tree_code: str) -> dict | None:
        """One tree with every line (items, resources, text), in SAP's order.

        One query: the header columns repeat on each line, and a tree with no
        lines still comes back through the LEFT JOIN. ``None`` when SAP has no
        tree under that code.
        """
        rows = self._query(
            """
            SELECT
                T."Code",
                COALESCE(NULLIF(T."Name", ''), M."ItemName", ''),
                T."TreeType",
                T."Qauntity",
                IFNULL(T."ToWH", ''),
                IFNULL(T."OcrCode", ''),
                IFNULL(T."Project", ''),
                T."PriceList",
                T."UpdateDate",
                L."ChildNum",
                L."VisOrder",
                L."Type",
                L."Code",
                COALESCE(CM."ItemName", R."ResName", L."ItemName", ''),
                L."Quantity",
                IFNULL(L."Warehouse", ''),
                L."IssueMthd",
                L."Price",
                IFNULL(L."Currency", ''),
                IFNULL(L."Comment", ''),
                COALESCE(NULLIF(L."Uom", ''), CM."InvntryUom", '')
            FROM "{schema}"."OITT" T
            LEFT JOIN "{schema}"."OITM" M ON M."ItemCode" = T."Code"
            LEFT JOIN "{schema}"."ITT1" L ON L."Father" = T."Code"
            LEFT JOIN "{schema}"."OITM" CM ON CM."ItemCode" = L."Code"
            LEFT JOIN "{schema}"."ORSC" R ON R."ResCode" = L."Code"
            WHERE T."Code" = ?
            ORDER BY L."VisOrder", L."ChildNum"
            """,
            (tree_code,),
        )
        if not rows:
            return None
        head = rows[0]
        tree = {
            "tree_code": _clean(head[0]),
            "description": _clean(head[1]),
            **_tree_type(head[2]),
            "quantity": _number(head[3]),
            "warehouse": _clean(head[4]),
            "distribution_rule": _clean(head[5]),
            "project": _clean(head[6]),
            "price_list": int(head[7]) if head[7] is not None else None,
            "updated_at": _date(head[8]),
            "lines": [],
        }
        for row in rows:
            (child_num, visual_order, line_type, code, name, quantity, warehouse,
             issue, price, currency, comment, uom) = row[9:]
            if child_num is None and code is None:
                continue  # the LEFT JOIN's empty row: a tree with no lines
            tree["lines"].append(
                {
                    "child_num": int(child_num) if child_num is not None else None,
                    "visual_order": int(visual_order) if visual_order is not None else None,
                    "item_type": _line_type(line_type),
                    "item_code": _clean(code),
                    "item_name": _clean(name),
                    "quantity": _number(quantity),
                    "warehouse": _clean(warehouse),
                    "issue_method": ISSUE_METHODS.get(_clean(issue).upper(), "Manual"),
                    "unit_cost": _number(price),
                    "currency": _clean(currency),
                    "comment": _clean(comment),
                    "uom": _clean(uom),
                }
            )
        tree["item_count"] = sum(1 for line in tree["lines"] if line["item_type"] == "item")
        tree["resource_count"] = sum(1 for line in tree["lines"] if line["item_type"] == "resource")
        return tree

    def tree_exists(self, tree_code: str) -> bool:
        """Does SAP already hold a tree under this code? Raises if it cannot tell."""
        rows = self._query(
            'SELECT COUNT(*) FROM "{schema}"."OITT" WHERE "Code" = ?', (tree_code,)
        )
        return bool(rows and rows[0][0])

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _query(self, sql: str, params: tuple, limit: int | None = None) -> list:
        """Run one read; ``{schema}`` and ``{limit}`` are the only interpolations."""
        statement = sql.replace("{schema}", self.connection.schema)
        if limit is not None:
            statement = statement.replace("{limit}", str(int(limit)))
        conn = None
        cursor = None
        try:
            conn = self.connection.connect()
        except dbapi.Error as e:
            logger.error("SAP HANA connection failed while reading BOMs: %s", e)
            raise SAPConnectionError("Unable to connect to SAP HANA.") from e
        try:
            cursor = conn.cursor()
            cursor.execute(statement, params)
            return cursor.fetchall()
        except dbapi.Error as e:
            logger.error("SAP HANA BOM query failed: %s", e)
            raise SAPDataError("Failed to read BOMs from SAP.") from e
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
