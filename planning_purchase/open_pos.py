"""SAP's open purchase orders, line by line, for the company on the request.

EXIM's Open POs (``sap_sync`` ``get_open_pos``) read Oil only and only the POs
SAP user 15 raised - one buyer's list, with nothing on the page saying so.
Here every open line is read for the company the request is in, and who raised
it is a column (and a filter on the page) instead of a hidden rule.

An open line is one SAP still expects goods against: the PO is open, the line
is open, the PO is not cancelled. Its open value is in rupees (the line's own
rate in rupees times what is still to come), so a PO in dollars adds up with
the rest.
"""

import logging
from decimal import Decimal

from django.core.cache import caches
from django.utils import timezone
from hdbcli import dbapi

from sap_client.context import CompanyContext
from sap_client.exceptions import SAPConnectionError, SAPDataError
from sap_client.hana.connection import HanaConnection

logger = logging.getLogger(__name__)

CACHE_SECONDS = 120

_SQL = """
SELECT H."DocEntry", H."DocNum", H."DocDate", H."DocDueDate", H."CardCode", H."CardName", H."NumAtCard",
       H."DocCur", U."U_NAME", L."LineNum", L."ItemCode", L."Dscription", L."unitMsr", L."WhsCode",
       L."Quantity", L."OpenQty", L."Price", L."Currency", L."LineTotal", L."ShipDate", G."ItmsGrpNam"
  FROM "{schema}"."OPOR" H
  JOIN "{schema}"."POR1" L ON L."DocEntry" = H."DocEntry"
  LEFT JOIN "{schema}"."OUSR" U ON U."USERID" = H."UserSign"
  LEFT JOIN "{schema}"."OITM" I ON I."ItemCode" = L."ItemCode"
  LEFT JOIN "{schema}"."OITB" G ON G."ItmsGrpCod" = I."ItmsGrpCod"
 WHERE H."DocStatus" = 'O' AND H."CANCELED" = 'N' AND L."LineStatus" = 'O'
 ORDER BY H."DocDate", H."DocNum", L."LineNum"
"""


def _dec(value) -> Decimal:
    return Decimal(str(value)) if value is not None else Decimal("0")


def _day(value):
    return value.date() if hasattr(value, "date") else value


def _text(value) -> str:
    return " ".join(str(value).split()) if value is not None else ""


def read_open_po_lines(company_code: str) -> list:
    """Every open PO line. Raises SAPConnectionError / SAPDataError."""
    connection = HanaConnection(CompanyContext(company_code).hana)
    try:
        conn = connection.connect()
    except dbapi.Error as exc:
        logger.warning("open POs: SAP HANA connection failed: %s", exc)
        raise SAPConnectionError("Unable to connect to SAP HANA.") from exc
    try:
        cursor = conn.cursor()
        cursor.execute(_SQL.replace("{schema}", connection.schema))
        columns = [c[0] for c in cursor.description]
        rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
    except dbapi.Error as exc:
        logger.warning("open POs: read failed: %s", exc)
        raise SAPDataError("Failed to read open purchase orders from SAP.") from exc
    finally:
        try:
            conn.close()
        except Exception:
            pass

    out = []
    for r in rows:
        ordered, still_open = _dec(r["Quantity"]), _dec(r["OpenQty"])
        # The line's rate in rupees: its rupee total over its quantity.
        rate_inr = _dec(r["LineTotal"]) / ordered if ordered else _dec(r["Price"])
        out.append({
            "doc_entry": r["DocEntry"],
            "po_number": str(r["DocNum"]),
            "po_date": _day(r["DocDate"]),
            "due_date": _day(r["DocDueDate"]),
            "ship_date": _day(r["ShipDate"]),
            "vendor_code": _text(r["CardCode"]),
            "vendor_name": _text(r["CardName"]),
            "vendor_ref": _text(r["NumAtCard"]),
            "raised_by": _text(r["U_NAME"]),
            "line": r["LineNum"],
            "item_code": _text(r["ItemCode"]),
            "item_name": _text(r["Dscription"]),
            "item_group": _text(r["ItmsGrpNam"]),
            "unit": _text(r["unitMsr"]),
            "warehouse": _text(r["WhsCode"]),
            "ordered": ordered,
            "received": ordered - still_open,
            "open_qty": still_open,
            "price": _dec(r["Price"]),
            "currency": _text(r["Currency"]) or _text(r["DocCur"]),
            "open_value": (still_open * rate_inr).quantize(Decimal("0.01")),
        })
    return out


def open_pos(company, *, refresh=False) -> dict:
    """Every open PO line with its days open and overdue days, and the totals
    the page heads with. Kept two minutes in the shared cache."""
    cache = caches["shared"]
    key = f"planning_purchase:open_pos:{company.code}"
    data = None
    if not refresh:
        try:
            data = cache.get(key)
        except Exception as exc:
            logger.warning("open POs: cache read failed: %s", exc)
    if data is None:
        data = {"rows": read_open_po_lines(company.code), "read_at": timezone.now()}
        try:
            cache.set(key, data, CACHE_SECONDS)
        except Exception as exc:
            logger.warning("open POs: cache write failed: %s", exc)

    today = timezone.localdate()
    rows = []
    for row in data["rows"]:
        due = row["ship_date"] or row["due_date"]
        rows.append({
            **row,
            "days_open": (today - row["po_date"]).days if row["po_date"] else None,
            "overdue_days": (today - due).days if due and due < today else 0,
        })
    return {
        "read_at": data["read_at"],
        "rows": rows,
        "totals": {
            "lines": len(rows),
            "orders": len({r["doc_entry"] for r in rows}),
            "vendors": len({r["vendor_code"] for r in rows}),
            "open_value": sum((r["open_value"] for r in rows), Decimal("0")),
            "overdue_lines": sum(1 for r in rows if r["overdue_days"] > 0),
        },
        "raised_by": sorted({r["raised_by"] for r in rows if r["raised_by"]}),
        "item_groups": sorted({r["item_group"] for r in rows if r["item_group"]}),
        "warehouses": sorted({r["warehouse"] for r in rows if r["warehouse"]}),
    }
